# Booking engine architecture

Python 3.12+, FastAPI, Authlib, Pydantic v2 and SQLite. The executable schema
is SCHEMA in booking.py. Generate the API contract with python export_openapi.py;
tests compare the static contract with the runtime schema.

## Identity and agent access

Google OpenID Connect and Facebook OAuth establish provider-scoped identities.
Browser sessions use signed cookies; provider access tokens are not stored in
these sessions. A same-origin browser POST issues a 24-hour personal Bearer
token. Only its SHA-256 hash is stored. Logout revokes all tokens belonging to
the identity. The booking API uses Bearer tokens independently of browser cookies.

## Actual database columns

All timestamps are UTC epoch seconds. Duration and buffer are seconds.

| Table | Columns |
|---|---|
| users | id (primary key) |
| tokens | hash (primary key), user_id, expires |
| masters | id (primary key), name |
| services | id (primary key), master_id, name, duration, buffer |
| availability | master_id, start, end; primary key (master_id, start) |
| bookings | id (primary key), user_id, master_id, service_id, start, end, busy_until, status |
| idempotency | user_id, key, payload, booking_id; primary key (user_id, key) |

SQLite foreign-key constraints are not declared in the current schema.
Application checks enforce master/service association; the trusted operator CLI
validates catalog relationships. Do not assume database foreign-key enforcement.

## Reservations and concurrency

The application enables WAL at startup. Each write uses BEGIN IMMEDIATE to
serialize competing writers against the same local SQLite database.

1. Authenticate the token owner and validate structured input.
2. Inside the write transaction, check (user_id, Idempotency-Key). A replay of
   the same normalized request returns the existing booking; different input
   with that key returns 409.
3. Check the service belongs to the selected master and the requested start is
   in the future, within the 366-day booking horizon.
4. Compute end = start + duration and busy_until = end + buffer.
5. Require the full [start, busy_until) interval to fit a working window.
6. Reject any confirmed booking of that master where
   existing.start < busy_until AND existing.busy_until > start.
7. Insert the booking and idempotency record in the same transaction.

Adjacent intervals are allowed. Slot discovery is advisory; the write repeats
all checks. Listings use 15-minute steps anchored to working-window start.
The day filter is a UTC date, and slot results are epoch seconds.

## State and ownership

Stored statuses are lowercase confirmed and cancelled. A future owned booking
may transition from confirmed to cancelled. Repeat cancellation is idempotent.
Started confirmed bookings cannot be cancelled. Other identities receive 404
when attempting to cancel an inaccessible booking. Cancellation preserves the
original booking and its idempotency record.

## Deployment and integration boundary

Production needs HTTPS, provider credentials, a persistent local database and
request/rate limits at a reverse proxy. SQLite workers must use the same local
disk; multi-host database operation is not supported by this design.
GitHub publication does not deploy the application. The exported OpenAPI file
describes HTTP tools but does not provision a Meta AI integration.
