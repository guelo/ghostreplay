# Migration-Bearing Deployment Rollback Runbook

This runbook defines the production rollback policy for Ghost Replay releases
that advance the Alembic migration head.

## Non-negotiable rule

Never use Railway's image rollback action to cross a migration-bearing deploy.
Use a new forward-revert deployment that retains the complete migration
history.

Railway's image rollback restores application artifacts and service settings;
it does not downgrade PostgreSQL. The Ghost Replay start command runs
`alembic upgrade head` before Uvicorn. If the database is stamped at a
revision missing from the selected image, Alembic exits before the health
endpoint binds.

An image rollback is eligible only when the target artifact contains the
database's current migration revision and is schema-compatible. In practice,
treat it as a pure-code rollback between artifacts with the same Alembic head.

## Forward-revert artifact

A valid forward-revert artifact is a new commit and deployment with these
properties:

- It reverts the unsafe application behavior.
- It retains every migration file already applied in production.
- Its models and queries are compatible with the current production schema.
- `alembic upgrade head` is a no-op against a database already at that head.
- It has an explicit data/read-semantics safety argument for unused new data.

Never delete or revert an applied migration file as part of an application
rollback. An Alembic `downgrade()` implementation is a development and local
rehearsal tool, not the normal production rollback path.

## Prepare and rehearse

1. Identify the current production Alembic revision and revision introduced
   by the deployment being reverted.
2. Prepare a revert commit that changes application code but keeps the full
   `alembic/versions` history.
3. Restore a disposable database at the production migration head, or use the
   approved migration rehearsal database.
4. Run `alembic upgrade head` with the would-be revert artifact. It must exit
   successfully without applying a second copy of the migration.
5. Boot the application from that artifact and call `/health`.
6. Exercise the read/write path whose semantics motivated the revert.
7. Record the revision, artifact commit, rehearsal result, and operator.

Backend Python commands must run from the project virtual environment:

```bash
cd backend
source .venv/bin/activate
alembic current
alembic upgrade head
```

Use only a disposable or explicitly approved database during rehearsal.

## Deploy and verify

1. Deploy the rehearsed forward-revert artifact as a new Railway deployment.
2. Confirm the migration command exits successfully.
3. Confirm the new deployment becomes healthy and active.
4. Confirm the previous deployment drains and is removed according to the
   release-specific runbook.
5. Verify the reverted application behavior and its critical writes.
6. Record the deployment URL or identifier, timestamps, database revision, and
   smoke-test result in the release bead.

## Schema downgrade exception

If production must physically remove or transform schema, handle that as a
separate maintenance operation with its own compatibility plan, backup/restore
procedure, rehearsed Alembic command, and traffic controls. Do not smuggle a
schema downgrade into Railway image rollback.

The ordering must ensure that every serving artifact remains compatible with
the database state throughout the operation.

## Release-specific data invariants

This runbook proves that the revert artifact can boot against the migrated
schema. It does not prove that reverting writers or readers preserves feature
data. Each migration-bearing release must also document:

- which writers remain required after the migration;
- whether the revert reads old, new, or live-computed data;
- whether a hookless window creates data the applied migration will not heal;
- whether a new repair migration is required before re-enabling a read path.

The session-accuracy rules are in
[`session-accuracy-versioning.md`](session-accuracy-versioning.md).
