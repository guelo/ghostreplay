# Ghost Replay Backend

This directory contains the FastAPI backend skeleton for Ghost Replay.

## Getting started

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Endpoints

- `GET /` -> basic service info
- `GET /health` -> health check
