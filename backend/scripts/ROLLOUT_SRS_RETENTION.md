# SRS retention rollout at M=60 days, G=1 hour

Operational authority: `g-srs-retain-rollout`. This runbook records preparation,
not authorization to enable freezing or delete production rows. The synthetic
qualification and restoration rehearsal are in
[COMPACT_SRS_EVENTS.md](COMPACT_SRS_EVENTS.md); recovery and scheduling details
remain in [RECOVER_SRS_FOLD.md](RECOVER_SRS_FOLD.md) and
[SCHEDULE_SRS_CLEANUP.md](SCHEDULE_SRS_CLEANUP.md).

The baseline and steps below were written against Railway. `g-3ap0e` moves
production to an OVH VPS, holds step 2 (a new Railway API volume) until after
that cutover, and reserves a `srs-recovery` directory for this rollout there.
On the VPS, read "Railway deployment" as the VPS deployment, and see
[Export storage guard](#export-storage-guard) before trusting the guard.

## Verified production baseline — 2026-09-25

Read-only Railway metadata and container checks found:

| Check | Result |
| --- | --- |
| Successful active API deployment | `a9757150dc4469b5b2fd958a9c3e55d2c9f3f311` |
| Qualification revision included | `022a19a` is an ancestor |
| Alembic revision | `20260923_01` |
| Policy | version 1, M=60, G=3600; readiness, freeze and cleanup all false |
| Summary coverage | 1,092 summaries; zero missing, stale or nonzero folded summaries |
| Existing folds | zero user prefixes; first-fold anchor NULL |
| Reader parity | 11,894 five-counter comparisons, zero mismatches |
| Comparison scope | all 1,092 blunders across 16 owners; 71 session exclusions |
| API recovery volume | absent |
| Export directory / hourly job environment settings | both absent |

The reader comparison ran inside one PostgreSQL `REPEATABLE READ`, `READ ONLY`
transaction, using one database timestamp and a 30-second per-statement timeout.
It compared the deployed `app.srs_opportunity.load_opportunity_counters` against
the unchanged `srs_raw_oracle.load_opportunity_counters`, in batches of 100 IDs,
for each owner with no exclusion and each of their ten most recent sessions
(or all sessions if fewer). Only aggregate counts were emitted. No production
facts, owner identifiers or per-user scores were exported.

This proves parity for those existing facts and exclusions. It does not claim
that production traffic exercised every review/replay/revert/repair race;
those operation sequences and restoration are covered by the synthetic
PostgreSQL qualification. The switches were not changed. This snapshot does
not establish old-writer drain or alert delivery.

## Preparation before activation review

1. Deploy the reviewed export guard, then verify the actual successful Railway
   revision and migration again. Git pushes autodeploy this service, but a push
   is not evidence that the deployment succeeded. Confirm all old API instances,
   workers, repair processes and their in-flight database transactions drained.
   Requests may outlive G under the qualified interlock contract; no universal
   request-duration limit is required.
2. Mount a dedicated durable volume on the API service. Proposed mount:
   `/srs-recovery`; proposed export directory: `/srs-recovery/exports`.
   Configure `GHOSTREPLAY_SRS_FOLD_EXPORT_DIR` to that directory as an absolute
   path, create it as the application owner with mode 0700, and verify files
   are 0600. `fold_srs_opportunities.py status` prints the directory it will
   use; until it is configured it prints `export directory: UNAVAILABLE (...)`
   with the reason. Verify a
   synthetic file survives a redeploy before any real export is trusted. Keep
   the directory exclusive to this database. Never place it in the deploy image
   or attach it to a separate service that the API cannot access.
3. Verify one API process owns maintenance. Select and test external delivery
   for the error events in the scheduling runbook, using a synthetic alert.
   No destination has been selected or delivery tested as of the baseline.
4. With cleanup still false, enable the hourly job on that process. Verify its
   disabled sweep and expiry run at startup and again on the hourly cadence.
   Verify expiry on synthetic artifacts only; do not invent production manifests.
5. Re-run `reconcile_srs_review_basis.py --check` after drain. If it finds gaps,
   reconcile under the documented procedure and recheck before readiness.
   Confirm summaries/prefixes and first-fold anchor still match the no-fold
   baseline; repeat reader parity if facts or reader code changed materially.

## Export storage guard

When `app.database_url.is_deploy_platform` is true, `export_dir()` refuses an
unset `GHOSTREPLAY_SRS_FOLD_EXPORT_DIR` and a relative one: both resolve inside
the deploy image. Before their first attempt, both sweeps also create and remove
a probe file, so a configured directory this process cannot write (wrong mount
owner, read-only) is refused the same way, on any host. While storage is
refused:

| Path | Behaviour |
| --- | --- |
| `fold_srs_opportunities.py status` | Prints `export directory: UNAVAILABLE (<reason>)` and the rest of the status. Status does not write, so it cannot detect an unwritable directory |
| `fold_srs_opportunities.py sweep` | With cleanup disabled, reports `disabled` as before. With cleanup enabled and candidates, attempts no fold, prints one `ERROR export storage: ...`, exits 1 |
| Hourly `scheduled_sweep` (cleanup enabled) | Logs one `srs_cleanup_export_unavailable` ERROR, attempts no fold, still measures backlog and raises lag alerts |
| Expiry (`expire` verb and hourly job) | Still deletes expired manifests and the files they name; skips the orphan sweep, logs `srs_fold_orphan_sweep_skipped`, and the verb exits 1. Expiry does not probe |
| A single `fold_user_batch` | Reports `export_failed` and deletes nothing |

A full disk is not refused up front: an empty probe file fits on a full disk,
so each export still fails and retries as a transient error. The guard also
cannot prove that a configured absolute path is durable. Mount verification
(step 2) is still an operator gate.

The unset and relative checks only act where `is_deploy_platform` recognises
the platform: today that means Railway and Render environment variables. On the
OVH VPS they do nothing until `g-3ap0e` step 1.2 adds
`GHOSTREPLAY_DEPLOY_PLATFORM` to that check. Do not enable cleanup on a host
before confirming the guard is active there. Run this in the API container,
from its working directory (`/app` on Railway and in the VPS image):

```bash
env -u GHOSTREPLAY_SRS_FOLD_EXPORT_DIR /opt/venv/bin/python scripts/fold_srs_opportunities.py status
# must print: export directory: UNAVAILABLE (...)
```

Use `/opt/venv/bin/python`, not a bare `python`: the interpreter on `PATH`
does not have the app's dependencies, so the check would fail with an import
error instead of running (see
[RETAIN_OPPONENT_DECISIONS.md](RETAIN_OPPONENT_DECISIONS.md)). On the VPS, run
it through `docker compose exec api`.

## Activation, catch-up and finite recovery

Present the evidence above for the activation decision. Follow the database
ladder: readiness, freeze, then cleanup. Preserve M=60/G=3600 and update the
policy version with each policy change. Reviews remain available at every age.
The seven-day reader canary and separate small-fold approval were retired;
they are not additional gates.

Before enabling cleanup, announce: **full raw-history rollback expires seven
days after the first committed fold; after that only compact-aware releases
are rollback targets.** Record the actual first-fold timestamp and deadline
immediately after the first committed batch. Do not copy recovery rows into
the tracker. Exports/manifests remain finite even after rollback eligibility
ends: each expires seven days after its own batch.

Measure the eligible backlog and bounded sweep progress, then declare a
concrete UTC catch-up deadline from that measurement before sustained catch-up.
There is no measured deadline yet. Respect the existing batch/transaction
budgets; do not increase them to force a forecast. Verify healthy backlog lag
at or below 24 hours after catch-up, with hourly forced cleanup and expiry.

Stop cleanup on a counter mismatch, unreadable/missing recovery artifact,
invariant failure, unknown backlog, or missed catch-up deadline. Investigate
24-hour lag alerts. Keep artifact expiry running when only cleanup is paused.
If rollback is required while eligible, pause writers and folding, leave
freeze on, restore every batch using the recovery runbook, and validate
counters, prefixes and the recovery anchor before changing reader/schema.
Restoration must preserve subsequent reviews/writes and respect user purges.

Before the first expiry, verify and announce the compact-aware-only boundary
again. Verify actual expired-file and manifest removal. Measure relation,
index, TOAST and export bytes separately, including allocated versus live
storage. Ordinary DELETE/VACUUM reuse does not promise physical-volume shrinkage;
no table rewrite is authorized by this rollout.

Keep the bead open through catch-up, hourly maintenance verification and actual
finite artifact expiry. A code commit or activation alone does not complete it.
