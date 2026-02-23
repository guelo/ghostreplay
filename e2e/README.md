# Playwright E2E

This directory contains end-to-end tests powered by Playwright.

## Commands

```bash
npm run test:e2e
npm run test:e2e:headed
npm run test:e2e:ui
```

## Seeded Accounts

The backend seed script (`backend/scripts/seed_e2e_data.py`) creates deterministic users:

- `e2e_due_user` / `e2e-pass-123` (has due blunder fixtures)
- `e2e_stable_user` / `e2e-pass-123` (has non-due blunder fixtures)
- `e2e_empty_user` / `e2e-pass-123` (no blunders)

Credentials can be overridden via environment variables:

- `E2E_DUE_USERNAME`, `E2E_DUE_PASSWORD`
- `E2E_STABLE_USERNAME`, `E2E_STABLE_PASSWORD`
- `E2E_EMPTY_USERNAME`, `E2E_EMPTY_PASSWORD`

## Backend Bootstrapping

`playwright.config.ts` starts:

1. `scripts/e2e/start_backend.sh`:
   - activates `backend/.venv`
   - resets and seeds the E2E database
   - runs FastAPI on `127.0.0.1:${E2E_BACKEND_PORT:-8010}`
2. Vite dev server with `VITE_API_URL` pointed at that backend.
