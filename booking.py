"""Booking API. All times stored as UTC seconds; writes serialized by SQLite."""
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Literal

from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.sessions import SessionMiddleware


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS masters(id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS services(id TEXT PRIMARY KEY, master_id TEXT NOT NULL, name TEXT NOT NULL,
 duration INTEGER NOT NULL CHECK(duration > 0), buffer INTEGER NOT NULL CHECK(buffer >= 0));
CREATE TABLE IF NOT EXISTS availability(master_id TEXT NOT NULL, start INTEGER NOT NULL,
 end INTEGER NOT NULL CHECK(end > start), PRIMARY KEY(master_id,start));
CREATE TABLE IF NOT EXISTS bookings(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, master_id TEXT NOT NULL,
 service_id TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL, busy_until INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('confirmed','cancelled')));
CREATE INDEX IF NOT EXISTS booking_overlap ON bookings(master_id,status,start,busy_until);
CREATE TABLE IF NOT EXISTS idempotency(user_id TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
 booking_id TEXT NOT NULL, PRIMARY KEY(user_id,key));
"""


@contextmanager
def connection(path, write=False):
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        if write:
            db.execute('BEGIN IMMEDIATE')
        yield db
        if write:
            db.commit()
    except BaseException:
        if write:
            db.rollback()
        raise
    finally:
        db.close()


class BookingInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    master_id: str = Field(min_length=1, max_length=100)
    service_id: str = Field(min_length=1, max_length=100)
    start: datetime

    @field_validator('start')
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None or value.microsecond:
            raise ValueError('Use a timezone-aware timestamp with whole seconds')
        return value


def create_app(db_path=None, secret=None, base_url=None):
    db_path = db_path or os.getenv('BOOKING_DB', 'booking.db')
    secret = secret or os.getenv('SESSION_SECRET', '')
    base_url = (base_url or os.getenv('BASE_URL', 'http://localhost:8000')).rstrip('/')
    if len(secret) < 32:
        raise RuntimeError('Set SESSION_SECRET to at least 32 random characters')
    app = FastAPI(title='Master Booking Agent API', version='1.0.0')
    app.add_middleware(SessionMiddleware, secret_key=secret, https_only=base_url.startswith('https:'),
                       same_site='lax', max_age=3600)
    with connection(db_path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)
    oauth = OAuth()
    enabled = []
    for provider in ('google', 'facebook'):
        client_id = os.getenv(provider.upper() + '_CLIENT_ID')
        client_secret = os.getenv(provider.upper() + '_CLIENT_SECRET')
        if not client_id or not client_secret:
            continue
        enabled.append(provider)
        if provider == 'google':
            oauth.register(provider, client_id=client_id, client_secret=client_secret,
                server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
                client_kwargs={'scope': 'openid profile email', 'code_challenge_method': 'S256'})
        else:
            version = os.getenv('FACEBOOK_GRAPH_VERSION')
            if not version or not version.startswith('v') or not version[1:].replace('.', '').isdigit():
                raise RuntimeError('Set FACEBOOK_GRAPH_VERSION to the version enabled for your Meta app')
            oauth.register(provider, client_id=client_id, client_secret=client_secret,
                authorize_url=f'https://www.facebook.com/{version}/dialog/oauth',
                access_token_url=f'https://graph.facebook.com/{version}/oauth/access_token',
                api_base_url=f'https://graph.facebook.com/{version}/', client_kwargs={'scope': 'public_profile'})

    bearer = HTTPBearer(auto_error=False)

    def authenticated(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not credentials or credentials.scheme.lower() != 'bearer':
            raise HTTPException(401, 'Bearer token required')
        digest = hashlib.sha256(credentials.credentials.encode()).hexdigest()
        with connection(db_path) as db:
            row = db.execute('SELECT user_id FROM tokens WHERE hash=? AND expires>?',
                             (digest, int(time.time()))).fetchone()
        if not row:
            raise HTTPException(401, 'Invalid or expired token')
        return row['user_id']

    def browser_user(request):
        user = request.session.get('user')
        if not user:
            raise HTTPException(401, 'Sign in first')
        if request.headers.get('origin') != base_url:
            raise HTTPException(403, 'Origin rejected')
        return user

    @app.get('/', response_class=HTMLResponse)
    def home():
        return '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
        <title>Запись к мастеру</title><body><h1>Запись к мастеру через агента</h1>
        <p>Войдите, создайте личный API-токен и подключите его к своему агенту.</p>
        <p><a href="/auth/google">Войти через Google</a> · <a href="/auth/facebook">Войти через Facebook</a></p>
        <button id="create">Создать токен на 24 часа</button><button id="logout">Выйти</button>
        <pre id="result" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre><p><a href="/docs">Описание API</a></p>
        <script>async function post(url){const r=await fetch(url,{method:'POST'});document.getElementById('result').textContent=JSON.stringify(await r.json(),null,2)}
        document.getElementById('create').onclick=()=>post('/auth/agent-token');
        document.getElementById('logout').onclick=()=>post('/auth/logout');</script></body></html>'''

    @app.get('/auth/{provider}')
    async def login(provider: Literal['google', 'facebook'], request: Request):
        if provider not in enabled:
            raise HTTPException(503, 'Identity provider is not configured')
        return await oauth.create_client(provider).authorize_redirect(request, f'{base_url}/auth/{provider}/callback')

    @app.get('/auth/{provider}/callback')
    async def callback(provider: Literal['google', 'facebook'], request: Request):
        if provider not in enabled:
            raise HTTPException(503, 'Identity provider is not configured')
        try:
            client = oauth.create_client(provider)
            token = await client.authorize_access_token(request)
            if provider == 'google':
                profile = token.get('userinfo') or {}
                subject = profile.get('sub')
            else:
                # App secret proof binds Graph requests to this registered application.
                import hmac
                proof = hmac.new(os.environ['FACEBOOK_CLIENT_SECRET'].encode(),
                                 token['access_token'].encode(), hashlib.sha256).hexdigest()
                response = await client.get('me', token=token, params={'fields': 'id', 'appsecret_proof': proof})
                response.raise_for_status()
                subject = response.json().get('id')
            if not isinstance(subject, str) or not subject:
                raise ValueError('Missing identity')
        except (OAuthError, ValueError, KeyError):
            raise HTTPException(400, 'Authentication failed') from None
        except Exception:
            raise HTTPException(502, 'Identity provider unavailable') from None
        user = f'{provider}:{subject}'
        with connection(db_path, True) as db:
            db.execute('INSERT OR IGNORE INTO users VALUES (?)', (user,))
        request.session.clear()
        request.session['user'] = user
        return RedirectResponse('/')

    @app.post('/auth/agent-token')
    def issue_token(request: Request):
        user = browser_user(request)
        token = secrets.token_urlsafe(32)
        expires = int(time.time()) + 86400
        with connection(db_path, True) as db:
            db.execute('DELETE FROM tokens WHERE expires<=?', (int(time.time()),))
            db.execute('INSERT INTO tokens VALUES (?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), user, expires))
        from fastapi.responses import JSONResponse
        return JSONResponse({'access_token': token, 'token_type': 'Bearer', 'expires_at': expires},
                            headers={'Cache-Control': 'no-store'})

    @app.post('/auth/logout')
    def logout(request: Request):
        user = browser_user(request)
        with connection(db_path, True) as db:
            db.execute('DELETE FROM tokens WHERE user_id=?', (user,))
        request.session.clear()
        return {'signed_out': True}

    @app.get('/api/masters')
    def masters():
        with connection(db_path) as db:
            return [dict(row) for row in db.execute('SELECT * FROM masters ORDER BY id')]

    @app.get('/api/masters/{master_id}/services')
    def services(master_id: str):
        with connection(db_path) as db:
            return [dict(row) for row in db.execute('SELECT * FROM services WHERE master_id=?', (master_id,))]

    @app.get('/api/masters/{master_id}/slots')
    def slots(master_id: str, service_id: str, day: str):
        from datetime import date, timezone
        try:
            start = int(datetime.combine(date.fromisoformat(day), datetime.min.time(), timezone.utc).timestamp())
        except ValueError:
            raise HTTPException(422, 'day must be an ISO date') from None
        with connection(db_path) as db:
            service = db.execute('SELECT * FROM services WHERE id=? AND master_id=?', (service_id,master_id)).fetchone()
            if not service:
                raise HTTPException(404, 'Service not found for master')
            windows = db.execute('SELECT * FROM availability WHERE master_id=? AND start<? AND end>?',
                                 (master_id,start+86400,start)).fetchall()
            busy = db.execute("SELECT start,busy_until FROM bookings WHERE master_id=? AND status='confirmed' AND start<? AND busy_until>?",
                              (master_id,start+86400+service['duration']+service['buffer'],start)).fetchall()
        result = set()
        for window in windows:
            first = window['start'] + max(0, (start-window['start']+899)//900)*900
            for t in range(first, min(window['end'], start+86400), 900):
                end = t+service['duration']+service['buffer']
                if t >= max(start, time.time()) and end <= window['end'] and not any(t < b['busy_until'] and end > b['start'] for b in busy):
                    result.add(t)
        return {'starts': sorted(result), 'timezone': 'UTC', 'timestamp_unit': 'seconds'}

    @app.post('/api/bookings', status_code=201)
    def book(body: BookingInput, user=Depends(authenticated),
             idempotency_key: str = Header(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9._:-]+$')):
        start = int(body.start.timestamp())
        payload = json.dumps([body.master_id, body.service_id, start])
        with connection(db_path, True) as db:
            old = db.execute('SELECT * FROM idempotency WHERE user_id=? AND key=?', (user,idempotency_key)).fetchone()
            if old:
                if old['payload'] != payload:
                    raise HTTPException(409, 'Idempotency key reused with different request')
                return dict(db.execute('SELECT * FROM bookings WHERE id=?', (old['booking_id'],)).fetchone())
            if start <= time.time() or start > time.time()+366*86400:
                raise HTTPException(422, 'Booking must be in the next 366 days')
            service = db.execute('SELECT * FROM services WHERE id=? AND master_id=?', (body.service_id,body.master_id)).fetchone()
            if not service:
                raise HTTPException(404, 'Service not found for master')
            end = start+service['duration']
            busy_until = end+service['buffer']
            window = db.execute('SELECT 1 FROM availability WHERE master_id=? AND start<=? AND end>=?',
                                (body.master_id,start,busy_until)).fetchone()
            if not window:
                raise HTTPException(409, 'Outside master availability')
            overlap = db.execute("SELECT 1 FROM bookings WHERE master_id=? AND status='confirmed' AND start<? AND busy_until>?",
                                 (body.master_id,busy_until,start)).fetchone()
            if overlap:
                raise HTTPException(409, 'Time is already booked')
            booking_id = secrets.token_urlsafe(18)
            db.execute("INSERT INTO bookings VALUES (?,?,?,?,?,?,?, 'confirmed')",
                       (booking_id,user,body.master_id,body.service_id,start,end,busy_until))
            db.execute('INSERT INTO idempotency VALUES (?,?,?,?)', (user,idempotency_key,payload,booking_id))
            return dict(db.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone())

    @app.get('/api/bookings')
    def bookings(user=Depends(authenticated)):
        with connection(db_path) as db:
            return [dict(row) for row in db.execute('SELECT * FROM bookings WHERE user_id=? ORDER BY start', (user,))]

    @app.post('/api/bookings/{booking_id}/cancel')
    def cancel(booking_id: str, user=Depends(authenticated)):
        with connection(db_path, True) as db:
            row = db.execute('SELECT * FROM bookings WHERE id=? AND user_id=?', (booking_id,user)).fetchone()
            if not row:
                raise HTTPException(404, 'Booking not found')
            if row['status'] == 'confirmed' and row['start'] <= time.time():
                raise HTTPException(409, 'Started booking cannot be cancelled')
            db.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
            return {'id': booking_id, 'status': 'cancelled'}

    return app
