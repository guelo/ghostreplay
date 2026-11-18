# Ghost Replay Backend

This directory contains the FastAPI backend skeleton for Ghost Replay.

## Getting started

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt -c requirements.production.constraints.txt
export DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/ghostreplay"
uvicorn app.main:app --reload --port 8000 --no-access-log
```

### Maia3 engine (opponent moves)

Bot moves come from a local Maia3 engine that the API runs as a separate UCI
process (`app/maia3_client.py`; license: `THIRD_PARTY_NOTICES.md`). It is off
unless `MAIA3_ENABLED=1`, and it needs torch, upstream maia3 and the pinned
weights. The supported dev platforms are macOS (arm64) and Linux x86_64:

```bash
pip install -r requirements.txt -r requirements-dev.txt -r requirements-maia3.txt -c requirements.production.constraints.txt
MAIA3_ENABLED=1 python scripts/prepare_maia3_engine.py   # once: fetch, verify, smoke
MAIA3_ENABLED=1 uvicorn app.main:app --reload --port 8000 --no-access-log
```

`prepare_maia3_engine.py` downloads the checkpoint (maia3-23m, about 92 MB) into
the git-ignored `maia3_weights/` at its pinned revision and checks its size and
sha256. It then starts the engine, plays one move and ends with
`maia3 smoke ok ... child_hwm_kb=...`. Without the weights, or with
`MAIA3_ENABLED=0` (the default), opponent moves answer 503 and the browser plays
its local Stockfish instead. `MAIA3_MODEL=maia3-5m` selects the smaller
checkpoint. The other `MAIA3_*` settings are in `app/maia3_client.py`
(`Maia3Settings`).

`scripts/probe_maia3.py` measures how strongly the engine plays at each Elo,
top-move (T=0) and sampled (T=1), against Lichess blitz players of the same
rating, on the positions in `scripts/fixtures/maia3_probe_positions.json`. It
also measures cold start, per-move latency and child memory (`--latency`). It
needs Stockfish (`STOCKFISH_PATH`, or `brew install stockfish`); its docstring
gives the commands, and its cache lives in the git-ignored `.maia3_probe/`.

On Railway, the service's `NIXPACKS_INSTALL_CMD` adds `-r requirements-maia3.txt`,
and the build command in `.railway/railway.ts` runs `prepare_maia3_engine.py`, so
the weights are baked into the image. With `MAIA3_ENABLED=1` on the service, any
failure there, including the smoke test, fails the build. A deployment that still
boots without valid weights answers 503 on `/health`, so Railway's deploy
healthcheck keeps the previous deployment. The pins are in
`requirements-maia3.constraints.txt`, which the requirements file applies itself.

## Database migrations (Alembic)

```bash
cd backend
alembic -c alembic.ini upgrade head
```

## Dependencies

`requirements.txt` lists the runtime dependencies without versions;
`requirements.production.constraints.txt` pins their full closure to what Railway
installs (the service's `NIXPACKS_INSTALL_CMD`, set in the Railway dashboard,
passes the same `-c`). Always install with those constraints so tests run the
libraries production runs. When the constraints file changes, rerun the install
above in every existing venv: `test_dependency_pins.py` fails the suite (and so
the pre-push gate and CI) when the venv's runtime versions differ from the pins,
or when a runtime dependency has no pin. To bump a dependency, change its pin,
reinstall, and run the full suite under `-W error` before deploying. A venv
with `requirements-maia3.txt` installed is also held to
`requirements-maia3.constraints.txt` and to the torch pin. After changing either,
run the Maia3 engine release test (`test_maia3_engine_release.py`; its docstring
gives the command).

## Testing

```bash
cd backend
source .venv/bin/activate
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
