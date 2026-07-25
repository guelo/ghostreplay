#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"

DEFAULT_DB_URL="sqlite:///$BACKEND_DIR/.tmp/e2e.sqlite3"
export DATABASE_URL="${DATABASE_URL:-$DEFAULT_DB_URL}"
export BACKEND_PORT="${BACKEND_PORT:-8010}"
export JWT_SECRET="${JWT_SECRET:-e2e-test-secret-32-bytes-minimum}"

if [[ "$DATABASE_URL" == sqlite:///* ]]; then
  DB_PATH="${DATABASE_URL#sqlite:///}"
  mkdir -p "$(dirname "$DB_PATH")"
fi

cd "$BACKEND_DIR"
source .venv/bin/activate
python scripts/seed_e2e_data.py --reset --database-url "$DATABASE_URL"

# Keep idle connections alive longer than any gap a run can produce.
#
# Playwright's request context pools sockets, so with uvicorn's 5s default the
# server closes an idle keep-alive connection while the client still holds it.
# A request issued in the window between that close and the client noticing it
# dies with ECONNRESET / "socket hang up" before reaching the app — a transport
# race, reproducible at a ~5.000s idle gap, that failed the pre-push gate on a
# fixture login. 75s clears the 30s per-test timeout with room to spare, so the
# server never closes first during a run. See g-e2e-login-econnreset.
exec uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" --no-access-log \
  --timeout-keep-alive 75
