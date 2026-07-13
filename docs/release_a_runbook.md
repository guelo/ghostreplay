# Release A runbook — session-accuracy writers & concurrency

Release A (epic `g-rzti`, roll-up `g-release-integrate`) is the first of two
Railway deployments under `g-madh`. It ships the `game_sessions` accuracy schema,
the frozen v1 computation, the maintained write hooks, and the complete
concurrency regime, **while stats and history still compute live from
`session_moves`**. It performs **no backfill** and **no read switch** — those,
plus the `NOT VALID` CHECK validation and the benchmark, are Release B.

This runbook records the pre-production gate evidence and the rehearsed
forward-revert/recovery drills, and reserves the production-activation facts that
become **Release B's activation cutoff**. It is the Release-A-specific companion
to the general [`migration-deploy-runbook.md`](./migration-deploy-runbook.md);
its non-negotiable rule applies here verbatim.

Migrations introduced by Release A (retain both — never image-rollback across them):

- `20260709_01` — add `game_sessions.player_accuracy` + `player_accuracy_algo_version` and the `NOT VALID` `ck_game_sessions_player_accuracy` range CHECK.
- `20260709_02` — add `idx_rating_history_user_chain (user_id, games_played DESC, recorded_at DESC, id DESC)`, built `CONCURRENTLY`.

---

## 1. Gate evidence (pre-production)

### Required PostgreSQL gate

```
GHOSTREPLAY_REQUIRE_PG_TESTS=1 \
GHOSTREPLAY_TEST_PG_URL="postgresql://.../ghostreplay_test" \
GHOSTREPLAY_TEST_PG_MAINT_URL="postgresql://.../postgres" \
pytest -m pg_gate --strict-markers -rs
```

Recorded from one run against PostgreSQL 18.3 (throwaway cluster):

| Fact | Value |
|------|-------|
| Positive collected count (`-m pg_gate`) | **29 selected** (of 2024 collected, 1995 deselected) |
| Exit code | **0** |
| Passed | **29 passed** |
| SKIPPED entries in `-rs` | **0** (no test skipped) |

The gate fails closed: with `GHOSTREPLAY_REQUIRE_PG_TESTS=1` and no
`GHOSTREPLAY_TEST_PG_URL`, every `@pg_gate` test **fails** with
`GHOSTREPLAY_REQUIRE_PG_TESTS=1 but GHOSTREPLAY_TEST_PG_URL is not set` instead of
skipping. The gate mechanism itself (fixed manifests, empty-selection guard,
incomplete-manifest / incomplete-matrix detection, skip→fail promotion, and clean
developer-default skips) is proven by the pytester subprocess self-tests in
`backend/test_pg_gate_plugin.py`.

### Default (developer) SQLite suite

```
pytest -p no:cacheprovider -q      # from backend/, no PostgreSQL env
```

Recorded: **1981 passed, 43 skipped**, exit 0. The 43 skips are the 29 `pg_gate`
PostgreSQL tests plus the 14 pre-existing module-local analysis-cache /
position-analysis PostgreSQL skips (their own `skipif`, out of the Release-A
gate).

---

## 2. Forward-revert rehearsal

Rehearsed on a disposable PostgreSQL 18.3 database seeded with **100,000**
`rating_history` rows (the pre-build row count that sizes the `CONCURRENTLY`
build). These are **rehearsal-shape** timings on a single local cluster, not
production numbers — record the production durations in §4.

| Step | Operation | Rehearsed duration | Result |
|------|-----------|--------------------|--------|
| upgrade `…08_01 → 09_01` | add accuracy cols + `NOT VALID` CHECK | ~45 ms | cols present; CHECK `convalidated = false` |
| upgrade `09_01 → 09_02` | `CREATE INDEX CONCURRENTLY` (100k rows) | ~160 ms | `indisvalid = true` |
| **downgrade `09_02 → 09_01`** | `DROP INDEX CONCURRENTLY` | ~41 ms | index gone |
| **downgrade `09_01 → …08_01`** | drop CHECK then both columns | ~35 ms | cols + CHECK gone |
| re-upgrade `…08_01 → head` | reversibility check | ~150 ms | `indisvalid = true` again |

**Forward-revert procedure (never image rollback).** To revert Release A in
production, deploy a **forward-revert artifact** that reverts the application
behaviour while retaining the `20260709_01` / `20260709_02` migration files (see
the general runbook). An explicit `alembic downgrade` is a rehearsal/local tool,
not the normal production path; the rehearsal above proves the `downgrade()`
implementations are correct and reversible if a controlled schema revert is ever
required. Because Release A leaves reads live and takes no read switch, a
pure-code forward-revert needs no data migration: the two new columns and the
new index are simply left unused.

---

## 3. INVALID concurrent-index recovery drill

A `CREATE INDEX CONCURRENTLY` that fails partway (crash, statement cancel,
deadlock, or — as rehearsed — a UNIQUE build over duplicate values) leaves an
**INVALID** index behind. An invalid index is never usable and **cannot be
validated in place** — it must be dropped and rebuilt.

Rehearsed sequence (on the same disposable DB):

1. Forced failure: `CREATE UNIQUE INDEX CONCURRENTLY … ON rating_history(games_played)` over a deliberately duplicated value → **IntegrityError**, leaving the index with `indisvalid = false`.
2. Detect: `SELECT indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = '<index>';` → `false`.
3. Recover: `DROP INDEX CONCURRENTLY <index>;` → index removed cleanly.
4. Rebuild the real (non-unique) `idx_rating_history_user_chain` `CONCURRENTLY` → `indisvalid = true`.

If `20260709_02` leaves an invalid `idx_rating_history_user_chain` in production,
`DROP INDEX CONCURRENTLY idx_rating_history_user_chain;` then re-run the migration
(or `CREATE INDEX CONCURRENTLY` by hand with the exact definition in §5.5 of
SPEC.md). Do **not** `VALIDATE`.

---

## 4. Production activation evidence (fill after deployment)

> **Do not close `g-release-integrate` or `g-rzti` until this table is complete.**
> These four facts become Release B's activation cutoff.

| # | Fact | Value |
|---|------|-------|
| 1 | Release A activation identifier + timestamp (deploy id / commit + UTC) | Railway deployment `a436eca7-5899-440d-9601-f198ed9f31c3`, commit `8c3cedeb36dd86e381e3eb7de1c98f2e75f5d6c1`; first successful health response at `2026-07-12T17:23:16.325883206Z` |
| 2 | `20260709_01` duration (accuracy cols + `NOT VALID` CHECK) | Applied by deployment `0377a0e3-b425-4585-9916-e53a8d692228`, commit `8b49bed5653349a09ee980e81a9eac65ba25aa40`. Exact duration is not recoverable: Railway assigned the `20260709_01` and following `20260709_02` transition records the same buffered-log timestamp, `2026-07-11T10:33:43.599Z`, and PostgreSQL retained no statement-duration telemetry. Do not interpret this as a zero-duration migration. |
| 3 | `20260709_02` `CONCURRENTLY` index duration + `indisvalid` after build | Same deployment and duration-observability limitation as Fact 2. A timestamped production check at `2026-07-12T21:17:04.276118Z` returned `indisvalid = true` and `CREATE INDEX idx_rating_history_user_chain ON public.rating_history USING btree (user_id, games_played DESC, recorded_at DESC, id DESC)`. |
| 4 | Timestamp the last pre-A deployment is **removed** (not merely inactive/draining) | Deployment `081289e0-75a7-4f11-8500-43e59b2be747` logged `Stopping Container` at `2026-07-12T17:23:31.248016623Z`; Railway subsequently reported the deployment status as `REMOVED`. |

The production `rating_history` count was not captured contemporaneously when
deployment `0377a0e3-b425-4585-9916-e53a8d692228` built the index. A post-hoc
query at `2026-07-12T21:17:04.265865Z` found **1,529** current rows and **1,523**
extant rows with `recorded_at` at or before the migration transition timestamp.
Treat 1,523 only as a reconstructed logical build-time count (subject to any
later deletion), not as a contemporaneous measurement. The separately observed
pre-activation count was **1,528** rows and the total relation size was **784 kB**;
that observation preceded the schema-no-op activation deployment, not the
original concurrent index build.

The absent per-revision timings are an explicit Release-A observability gap, not
an input to Release B sizing. Release B independently times CHECK validation plus
the real accuracy backfill on a production snapshot and uses the last pre-A
removal timestamp above as its activation cutoff. Never downgrade or rebuild the
production index merely to manufacture the missing timings.
