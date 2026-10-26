import base64
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from booking import connection, create_app
from manage import load_catalog

SECRET = 'test-only-session-key-with-at-least-32-chars'


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ('GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET','FACEBOOK_CLIENT_ID','FACEBOOK_CLIENT_SECRET'):
        monkeypatch.delenv(key, raising=False)
    path = str(tmp_path / 'test.db')
    app = create_app(path, SECRET)
    start = (int(time.time()) // 900 + 100) * 900
    with connection(path, True) as db:
        for user in ('alice','bob'):
            db.execute('INSERT INTO users VALUES (?)',(user,))
            db.execute('INSERT INTO tokens VALUES (?,?,?)',(hashlib.sha256(user.encode()).hexdigest(),user,int(time.time())+3600))
        db.execute("INSERT INTO masters VALUES ('m','Master')")
        db.execute("INSERT INTO masters VALUES ('other','Other')")
        db.execute("INSERT INTO services VALUES ('s','m','Service',1800,900)")
        db.execute('INSERT INTO availability VALUES (?,?,?)',('m',start,start+86400))
    return TestClient(app), path, start, app


def body(start, **updates):
    return dict(master_id='m',service_id='s',start=datetime.fromtimestamp(start,timezone.utc).isoformat(),**updates)


def headers(user='alice', key='request-0001'):
    return {'Authorization': 'Bearer '+user, 'Idempotency-Key': key}


def test_retry_overlap_buffer_and_adjacent(env):
    client, _, start, _ = env
    first = client.post('/api/bookings',json=body(start),headers=headers())
    assert first.status_code == 201
    assert client.post('/api/bookings',json=body(start),headers=headers()).json() == first.json()
    assert client.post('/api/bookings',json=body(start+1),headers=headers()).status_code == 409
    assert client.post('/api/bookings',json=body(start+1800),headers=headers(key='buffer-0001')).status_code == 409
    assert client.post('/api/bookings',json=body(start+2700),headers=headers(key='adjacent-01')).status_code == 201


@pytest.mark.parametrize('offset,status', [(-1000000,422),(367*86400,422),(-1,409),(86400,409),(86400-1800,409)])
def test_time_boundaries(env,offset,status):
    client, _, start, _ = env
    assert client.post('/api/bookings',json=body(start+offset),headers=headers()).status_code == status


def test_identity_validation_and_ownership(env):
    client, path, start, _ = env
    assert client.post('/api/bookings',json=body(start)).status_code == 401
    assert client.post('/api/bookings',json=body(start),headers=headers('invalid')).status_code == 401
    with connection(path,True) as db:
        db.execute('UPDATE tokens SET expires=0 WHERE user_id=?',('bob',))
    assert client.get('/api/bookings',headers=headers('bob')).status_code == 401
    bad = body(start); bad['start']='2030-01-01T10:00:00'
    assert client.post('/api/bookings',json=bad,headers=headers()).status_code == 422
    bad = body(start); bad['master_id']='other'
    assert client.post('/api/bookings',json=bad,headers=headers()).status_code == 404
    bad = body(start); bad['user_id']='bob'
    assert client.post('/api/bookings',json=bad,headers=headers()).status_code == 422
    assert client.post('/api/bookings',json=body(start),headers={'Authorization':'Bearer alice'}).status_code == 422


def test_cancel_scope_and_release(env):
    client, _, start, _ = env
    first = client.post('/api/bookings',json=body(start),headers=headers()).json()
    url = '/api/bookings/'+first['id']+'/cancel'
    assert client.get('/api/bookings',headers=headers('bob')).json() == []
    assert client.post(url,headers=headers('bob')).status_code == 404
    assert client.post(url,headers=headers()).json()['status'] == 'cancelled'
    assert client.post(url,headers=headers()).status_code == 200
    assert client.post('/api/bookings',json=body(start),headers=headers('bob')).status_code == 201


def test_concurrent_requests(env):
    _, path, start, app = env
    def reserve(index):
        with TestClient(app) as client:
            return client.post('/api/bookings',json=body(start),headers=headers(key=f'race-{index:08}')).status_code
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(reserve,range(8)))
    assert statuses.count(201) == 1
    assert statuses.count(409) == 7
    with connection(path) as db:
        assert db.execute('SELECT count(*) FROM bookings').fetchone()[0] == 1


def test_concurrent_same_idempotency(env):
    _, _, start, app = env
    def reserve(_):
        with TestClient(app) as client:
            return client.post('/api/bookings',json=body(start),headers=headers()).json()['id']
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(set(pool.map(reserve,range(8)))) == 1


def test_slots(env):
    client, _, start, _ = env
    day = datetime.fromtimestamp(start,timezone.utc).date().isoformat()
    url = f'/api/masters/m/slots?service_id=s&day={day}'
    assert start in client.get(url).json()['starts']
    client.post('/api/bookings',json=body(start),headers=headers())
    assert start not in client.get(url).json()['starts']
    assert client.get('/api/masters/m/slots?service_id=s&day=bad').status_code == 422
    assert client.get(f'/api/masters/other/slots?service_id=s&day={day}').status_code == 404


def test_oauth_disabled_and_csrf_and_revocation(env):
    client, _, _, _ = env
    assert client.get('/auth/google').status_code == 503
    assert client.get('/auth/facebook/callback?code=fake&state=fake').status_code == 503
    assert client.post('/auth/agent-token',headers={'origin':'http://localhost:8000'}).status_code == 401
    signed = TimestampSigner(SECRET).sign(base64.b64encode(json.dumps({'user':'alice'}).encode())).decode()
    client.cookies.set('session',signed)
    assert client.post('/auth/agent-token',headers={'origin':'https://evil.example'}).status_code == 403
    response = client.post('/auth/agent-token',headers={'origin':'http://localhost:8000'})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    token = response.json()['access_token']
    assert client.get('/api/bookings',headers=headers(token)).status_code == 200
    client.post('/auth/logout',headers={'origin':'http://localhost:8000'})
    assert client.get('/api/bookings',headers=headers(token)).status_code == 401


def test_secret_required():
    with pytest.raises(RuntimeError):
        create_app(secret='short')


def test_oauth_rejects_forged_state(env, monkeypatch):
    _, path, _, _ = env
    monkeypatch.setenv('GOOGLE_CLIENT_ID', 'test-client')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET', 'test-secret')
    client = TestClient(create_app(path, SECRET))
    response = client.get('/auth/google/callback?code=forged&state=forged', follow_redirects=False)
    assert response.status_code == 400
    assert client.post('/auth/agent-token', headers={'origin':'http://localhost:8000'}).status_code == 401


def test_oauth_validated_identity_and_missing_subject(env, monkeypatch):
    _, path, _, _ = env
    import booking
    class FakeClient:
        async def authorize_access_token(self, request):
            return {'userinfo': self.profile}
        profile = {'sub':'provider-user-123'}
    fake = FakeClient()
    class FakeOAuth:
        def register(self, *args, **kwargs):
            pass
        def create_client(self, provider):
            return fake
    monkeypatch.setattr(booking, 'OAuth', FakeOAuth)
    monkeypatch.setenv('GOOGLE_CLIENT_ID', 'test-client')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET', 'test-secret')
    client = TestClient(create_app(path, SECRET))
    assert client.get('/auth/google/callback', follow_redirects=False).status_code == 307
    assert client.post('/auth/agent-token',headers={'origin':'http://localhost:8000'}).status_code == 200
    fake.profile = {'email':'not-an-identity@example.com'}
    assert client.get('/auth/google/callback').status_code == 400


def test_started_booking_cannot_cancel(env):
    client, path, start, _ = env
    first = client.post('/api/bookings',json=body(start),headers=headers()).json()
    with connection(path,True) as db:
        db.execute('UPDATE bookings SET start=0 WHERE id=?',(first['id'],))
    assert client.post('/api/bookings/'+first['id']+'/cancel',headers=headers()).status_code == 409


def test_bad_catalog_does_not_write(env):
    _, path, _, _ = env
    with pytest.raises(ValueError):
        load_catalog(path,{'masters':[{'id':'new','name':'New'}], 'services':[{'id':'bad','master_id':'missing','name':'bad','duration':-1,'buffer':0}], 'availability':[]})
    with connection(path) as db:
        assert db.execute("SELECT 1 FROM masters WHERE id='new'").fetchone() is None


def test_catalog_import_and_transaction_rollback(env):
    _, path, start, _ = env
    catalog = {'masters':[{'id':'m','name':'Updated'}],
        'services':[{'id':'s','master_id':'m','name':'Updated','duration':900,'buffer':0}],
        'availability':[{'master_id':'m','start':datetime.fromtimestamp(start,timezone.utc).isoformat(),
                         'end':datetime.fromtimestamp(start+3600,timezone.utc).isoformat()}]}
    load_catalog(path,catalog)
    load_catalog(path,catalog)
    with connection(path) as db:
        assert db.execute("SELECT duration FROM services WHERE id='s'").fetchone()[0] == 900
    catalog['masters'] = [{'id':'new','name':'New'}]
    catalog['services'][0]['master_id']='new'
    catalog['availability']=[]
    with pytest.raises(ValueError):
        load_catalog(path,catalog)
    with connection(path) as db:
        assert db.execute("SELECT 1 FROM masters WHERE id='new'").fetchone() is None


def test_openapi_auth_contract(env):
    client, _, _, _ = env
    schema = client.get('/openapi.json').json()
    assert schema['paths']['/api/bookings']['post']['security'] == [{'HTTPBearer': []}]
    assert client.get('/').status_code == 200
