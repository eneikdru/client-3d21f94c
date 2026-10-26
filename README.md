# Запись к мастеру через агента

FastAPI + SQLite: Google/Facebook login, personal agent tokens, master catalogs,
availability, transactional booking and cancellation. No external deployment is included.

## Запуск

Python 3.12+. In PowerShell:

```powershell
python -m venv .venv
./.venv/Scripts/python -m pip install -r requirements.lock.txt
$env:SESSION_SECRET = (python -c "import secrets; print(secrets.token_urlsafe(48))")
$env:BASE_URL = 'http://localhost:8000'
./.venv/Scripts/python manage.py catalog.example.json
./.venv/Scripts/python -m uvicorn booking:create_app --factory
```

Open http://localhost:8000. Example dates are illustrative; set actual working
hours in a copy of `catalog.example.json` and import it. Durations and buffers
are seconds; timestamps require explicit timezones. The catalog is maintained
by a trusted operator through CLI, not a public admin API. Import upserts entries;
it does not remove old availability. Use a controlled DB migration to remove hours.

## Identity setup

Set environment variables from `.env.example` (the file is not loaded automatically).
Register Google and Meta applications and supply their client IDs and secrets.
Register exact redirect URLs:

- `BASE_URL/auth/google/callback`
- `BASE_URL/auth/facebook/callback`

For Google, enable OpenID Connect (`openid profile email`). Authlib validates
state, nonce and the signed ID token using Google's discovery document.
For Facebook, select `FACEBOOK_GRAPH_VERSION` supported by your application;
login uses code exchange and the authenticated Graph `/me` identity, never a
client-supplied email. Identities from different providers remain distinct.
Providers without credentials return 503 rather than simulating login.

Production requires HTTPS, a stable random session secret, persistent local
storage and a reverse proxy with request/rate limits. Use a single SQLite database
on a local disk shared by workers; this is not a multi-host database architecture.
Do not use the Dropbox working copy database as a production datastore.

## Agent protocol

After login, click “Создать токен” and configure it as a secret in your agent.
It expires in 24 hours; logout revokes all tokens for that identity. OAuth tokens
are never returned to the agent. The booking API accepts only personal Bearer
tokens, not browser cookies. Interactive docs: `/docs`; OpenAPI: `/openapi.json`.

The repository's `openapi.json` is generated from the application. Regenerate it
with `python export_openapi.py` after API changes. A regression test compares it
with the runtime schema, including the required `Idempotency-Key` header.
For a remote agent, configure the deployed HTTPS base URL in its connector;
this contract does not provision a Meta AI integration or a hosted server.

1. `GET /api/masters` and `/api/masters/{id}/services` select a master/service.
2. `GET /api/masters/{id}/slots?service_id=haircut&day=2026-11-01` lists UTC
   epoch seconds on the requested **UTC day**, at 15-minute intervals anchored
   to working-window start. Convert to the customer's timezone for display.
3. Confirm master, service and local date/time with the customer before writing.
4. `POST /api/bookings` with `Authorization: Bearer TOKEN`, unique
   `Idempotency-Key: request-12345678`, and JSON:

```json
{"master_id":"anna","service_id":"haircut","start":"2026-11-01T10:00:00+04:00"}
```

5. Retry network failures using the same key and payload. A changed payload
   with the same key returns 409. A cancelled booking replay remains cancelled.
6. `GET /api/bookings` lists only the token owner's bookings.
7. `POST /api/bookings/{id}/cancel` cancels an owned, future booking. It is
   repeatable; started appointments cannot be cancelled.

The server checks the full service plus buffer against working hours and all
confirmed bookings inside `BEGIN IMMEDIATE`. Adjacent half-open intervals are
allowed. Availability reads are advisory; only a successful booking reserves time.
The API permits arbitrary whole-second starts inside availability; the slot list
is a convenient subset, not a separate restriction. Booking horizon: 366 days.
Errors: 401 invalid token, 404 unknown/inaccessible resource, 409 conflict, 422
invalid input. No booking is inferred from free text without structured input.

## Meta AI integration boundary

This repository provides an agent-callable HTTP API and its OpenAPI schema.
Facebook Login does **not** automatically install an action inside Meta AI.
Direct Meta AI execution requires an officially supported integration surface
and approval/configuration for your account. This integration has not been
verified or provisioned. A connector that supports custom HTTP tools can call
the API now after configuring the personal token. Do not paste tokens into
ordinary chat messages. WhatsApp/Messenger bots would require a separate
channel adapter and are not implemented here.

## Проверки

```powershell
./.venv/Scripts/python -m pytest -q
```

Tests cover competing writers, duplicate retries, overlap/buffer boundaries,
tenant ownership, invalid/expired credentials, CSRF, revocation, invalid dates,
unavailable providers and invalid catalog rollback. Live OAuth requires registered
provider credentials and is not claimed as verified by mocked tests.

Google identity reference: https://developers.google.com/identity/openid-connect/openid-connect
Authlib reference: https://docs.authlib.org/en/latest/client/starlette.html

Gateway interaction cost and usefulness: `GATEWAY_AUDIT.md`.
