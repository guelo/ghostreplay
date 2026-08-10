# Ghost Replay Backend

This directory contains the FastAPI backend skeleton for Ghost Replay.

## Getting started

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/ghostreplay"
uvicorn app.main:app --reload --port 8000 --no-access-log
```

## Database migrations (Alembic)

```bash
cd backend
alembic -c alembic.ini upgrade head
```

## Testing

```bash
cd backend
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

### PostgreSQL gate

Tests marked `pg_gate` exercise behavior SQLite cannot prove. Point the gate at a
dedicated test database and provide separate maintenance authority for the
UUID-named disposable databases used by migration tests:

```bash
GHOSTREPLAY_REQUIRE_PG_TESTS=1 \
GHOSTREPLAY_TEST_PG_URL="postgresql://.../ghostreplay_test" \
GHOSTREPLAY_TEST_PG_MAINT_URL="postgresql://.../postgres" \
pytest -m pg_gate --strict-markers -rs
```

`GHOSTREPLAY_TEST_PG_URL` (or the legacy `TEST_DATABASE_URL_PG`) is never dropped.
When a run selects at least one `@pg_gate` test or a known shared-schema PostgreSQL
fixture and the URL is configured, an autouse session fixture opens a dedicated
connection, labels it `ghostreplay_pytest_schema_lease`, and holds a session-scoped
PostgreSQL advisory lock from the first test setup through final fixture teardown.
The fixture check includes the legacy analysis-cache and position-analysis PG tests,
which deliberately use local `skipif` markers instead of `@pg_gate`. An ambient URL
is still ignored by genuinely SQLite-only selections. Concurrent PG invocations
targeting the same database wait for the lease, so Alembic, per-test `TRUNCATE`
resets, and legacy-fixture writes cannot overlap. The waiter immediately names the
holder PID/application/state/query and repeats that diagnostic periodically.
Different databases do not share the lease, and PostgreSQL releases it automatically
if a test process exits or dies.

`GHOSTREPLAY_TEST_PG_MAINT_URL` is used only to create and remove
`ghostreplay_mig_test_<uuid>` databases. Required mode fails if that URL is absent;
the fixture also refuses any name outside that disposable namespace or equal to the
shared test database.

## Endpoints

- `GET /` -> basic service info
- `GET /health` -> health check
