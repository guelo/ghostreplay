# Ghost Replay Backend

This directory contains the FastAPI backend skeleton for Ghost Replay.

## Getting started

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/ghostreplay"
uvicorn app.main:app --reload --port 8000
```

## Database migrations (Alembic)

```bash
cd backend
alembic -c alembic.ini upgrade head
```

## Endpoints

- `GET /` -> basic service info
- `GET /health` -> health check
