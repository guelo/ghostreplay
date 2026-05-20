# Render Migration Runbook

This runbook moves the Ghost Replay backend and Postgres database from Railway to Render.

## Render Resources

The root `render.yaml` defines:

- `ghostreplay-api`: Python web service for the FastAPI backend.
- `ghostreplay-db`: managed Render Postgres database.
- `DATABASE_URL`: injected from the Render Postgres internal connection string.
- `JWT_SECRET`: dashboard-provided secret. Reuse the current Railway value if existing user sessions should survive the cutover.

The backend already accepts Render's `postgresql://...` database URL and normalizes it to `postgresql+psycopg://...` for SQLAlchemy.

## Configure Render

1. In Render, create a new Blueprint from this repository and approve `render.yaml`.
2. Keep the web service and database in the same region. The checked-in default is `oregon`.
3. In the `ghostreplay-api` environment settings, set `JWT_SECRET` to the Railway production value or a new long random secret.
4. Let the first deploy run. Render runs `cd backend && alembic upgrade head` as the pre-deploy command, then starts Uvicorn on `$PORT`.
5. Verify `https://<render-service>.onrender.com/health` returns `200`.

## Database Cutover

Schedule a brief downtime window so no writes are missed.

1. Stop or disable traffic to the Railway backend.
2. Export Railway Postgres using its public connection URL:

   ```bash
   pg_dump "$RAILWAY_DATABASE_PUBLIC_URL" -F c -f railway_backup.dump
   ```

3. Import into Render Postgres using the external database URL from the Render dashboard:

   ```bash
   pg_restore --verbose --no-acl --no-owner -d "$RENDER_DATABASE_EXTERNAL_URL" railway_backup.dump
   ```

4. In Render, trigger a fresh deploy of `ghostreplay-api` so Alembic verifies the restored schema is at head.
5. Smoke-test auth, game start/end, history, openings, drills, and `/health` against the Render URL.
6. Update the frontend deployment's `VITE_API_URL` to the Render backend URL, then redeploy the frontend.
7. If you use a custom backend domain, move DNS from Railway to the Render web service after the smoke tests pass.

For large databases, use directory format for the backup if you want parallel dump/restore:

```bash
pg_dump "$RAILWAY_DATABASE_PUBLIC_URL" -F d -j 4 -f railway_backup_dir
pg_restore --verbose --no-acl --no-owner -j 4 -d "$RENDER_DATABASE_EXTERNAL_URL" railway_backup_dir
```

## Rollback

Keep Railway resources intact until Render has served production traffic successfully. If cutover fails before DNS/frontend changes propagate, restore traffic to the Railway backend and keep the Railway database as the source of truth.
