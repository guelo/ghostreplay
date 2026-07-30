# Drill evidence-boundary backfill and broad-event cleanup

This is the production procedure for `g-boundary-backfill`. It reconstructs the
earliest observed opening-root ply for legacy drills, then recomputes every frozen
legacy session through the same boundary helper as the live writer.

The repair changes only broad `blunder_opportunity_events`. It never writes
`opponent_decisions`, and `targeted_30d` / `targeted_reached_30d` must compare equal
before and after the cleanup.

## Required release ordering

Do not activate boundary-scoped runtime writes before the legacy stamping pass has had
its chance to inspect the old cohort:

1. Apply the nullable `drill_root_reached_ply` schema.
2. Build the release containing both backfill CLIs, but do not shift application
   traffic to its boundary-aware runtime writer yet.
3. Capture one UTC cutoff and run the legacy boundary reconstruction from that release
   artifact.
4. Verify and record the permanent NULL residue. NULL means the route target was never
   observed (or could not be reconstructed); it is an accepted fail-closed outcome.
5. Shift traffic to the boundary-aware release.
6. Recompute the frozen session cohort in committed pages.

On a platform where a release image can run a one-off command before promotion, steps
2–3 use that image and step 5 promotes the same bytes. Do not take a second cutoff on a
retry.

## Environment and frozen values

Run from `backend/` with the production database environment already loaded. Do not put
database credentials on the command line.

```bash
source .venv/bin/activate
umask 077
date -u +%Y-%m-%dT%H:%M:%SZ > /secure/ghostreplay-boundary-cutoff.txt
export LEGACY_CUTOFF="$(tr -d '\n' < /secure/ghostreplay-boundary-cutoff.txt)"
date -u +%Y-%m-%dT%H:%M:%SZ > /secure/ghostreplay-boundary-as-of.txt
export VERIFY_AS_OF="$(tr -d '\n' < /secure/ghostreplay-boundary-as-of.txt)"
```

`LEGACY_CUTOFF` is the exclusive `game_sessions.started_at` ceiling for both phases.
`VERIFY_AS_OF` freezes the 30-day targeted-counter window so rows do not age across the
before/after comparison.

## Phase 1: stamp legacy boundaries

The command examines both `fen_after` at ply P and `fen_before` at ply P-1, and writes
the minimum ply whose normalized FEN equals `drill_opening_key`. It never reads route-map
distance. Writes are `IS NULL` guarded and committed per session.

```bash
python scripts/backfill_drill_evidence_boundaries.py \
  --all-sessions \
  --started-before "$LEGACY_CUTOFF" \
  --progress-every 500
```

Keep the complete summary. `remaining_null` must equal `unreconstructable` after a
complete first pass. A retry from the beginning is safe; it must report `stamped=0`,
the prior stamped population as `already_stamped`, and the same residue.

For a deliberately selected legacy row:

```bash
python scripts/backfill_drill_evidence_boundaries.py \
  --session-id 01234567-89ab-cdef-0123-456789abcdef
```

## Phase 2: recompute broad events

Use pages of 250. A page is an operator checkpoint, not one transaction: every session
is committed independently. New sessions created at or after `LEGACY_CUTOFF` are
excluded because the live writer already gives them boundary-correct rows.

First page:

```bash
python scripts/recompute_srs_opportunities.py \
  --all-sessions \
  --started-before "$LEGACY_CUTOFF" \
  --limit 250 \
  --progress-every 25
```

Copy `last_session_id` exactly from the last committed progress or summary line and use
it for the next page:

```bash
python scripts/recompute_srs_opportunities.py \
  --all-sessions \
  --started-before "$LEGACY_CUTOFF" \
  --after-session-id 01234567-89ab-cdef-0123-456789abcdef \
  --limit 250 \
  --progress-every 25
```

Repeat until a page reports `processed_sessions=0 last_session_id=None`. If a process
stops mid-page, resume after its last printed committed UUID. If no UUID was printed
since the prior page, rerun that page from the prior checkpoint; upserts and stale-row
deletes are idempotent.

`--session-id` recomputes one session. `--blunder-id` and `--all-blunders` are also
boundary-aware, but the production cleanup is session-grained because only a session
pass naturally retires every invalid row for that session.

The two grains intentionally differ on creation time: blunder-grain repair deletes an
event when its session evidence predates `blunder.created_at`, while session-grain
cleanup preserves live-writer parity and may retain a broad row for a session that
started before a later-created blunder but uploaded after it. Targeted counters still
exclude that pre-creation evidence independently, so this asymmetry cannot move them.

## Verification

Run these queries with `legacy_cutoff` set to `LEGACY_CUTOFF` and `as_of` set to
`VERIFY_AS_OF` in the secured PostgreSQL operator console.

Boundary census:

```sql
SELECT
  count(*) AS legacy_drills,
  count(*) FILTER (WHERE drill_root_reached_ply IS NOT NULL) AS stamped,
  count(*) FILTER (WHERE drill_root_reached_ply IS NULL) AS null_residue
FROM game_sessions
WHERE session_mode = 'drill'
  AND started_at < :'legacy_cutoff'::timestamptz;
```

Targeted-counter fingerprint (capture before Phase 2 and compare after it):

```sql
WITH targeted_sessions AS (
  SELECT
    od.session_id,
    od.target_blunder_id AS blunder_id
  FROM opponent_decisions od
  JOIN blunders b ON b.id = od.target_blunder_id
  WHERE od.served_at >= :'as_of'::timestamptz - interval '30 days'
    AND od.served_at >= b.created_at
  GROUP BY od.session_id, od.target_blunder_id
),
targeted_counters AS (
  SELECT
    t.blunder_id,
    count(*) AS targeted_30d,
    count(*) FILTER (
      WHERE e.reached IS TRUE AND e.opportunity IS TRUE
    ) AS targeted_reached_30d
  FROM targeted_sessions t
  LEFT JOIN blunder_opportunity_events e
    ON e.session_id = t.session_id
   AND e.blunder_id = t.blunder_id
  GROUP BY t.blunder_id
)
SELECT
  count(*) AS blunders_with_targeting,
  coalesce(sum(targeted_30d), 0) AS targeted_30d,
  coalesce(sum(targeted_reached_30d), 0) AS targeted_reached_30d,
  md5(coalesce(string_agg(
    concat_ws(':', blunder_id, targeted_30d, targeted_reached_30d),
    '|' ORDER BY blunder_id
  ), '')) AS fingerprint
FROM targeted_counters;
```

Broad-event fingerprint:

```sql
SELECT
  count(*) AS events,
  count(DISTINCT session_id) AS sessions,
  count(*) FILTER (WHERE reached) AS reached,
  md5(coalesce(string_agg(
    md5(concat_ws(
      ':',
      session_id,
      blunder_id,
      extract(epoch FROM occurred_at),
      opportunity::int,
      reached::int
    )),
    '' ORDER BY session_id, blunder_id
  ), '')) AS fingerprint
FROM blunder_opportunity_events
WHERE session_id IN (
  SELECT id
  FROM game_sessions
  WHERE started_at < :'legacy_cutoff'::timestamptz
);
```

After the first complete sweep, capture the broad fingerprint, rerun Phase 2 from the
start, and compare it again. Equality proves the second pass is a data no-op over the
entire frozen cohort. The targeted fingerprint must equal its pre-Phase-2 value.

Do not require the total broad-event count to decrease. The repair simultaneously
deletes stale pre-root matches and adds or corrects genuine post-boundary matches, so
the net count can move in either direction.

## Production-shaped PostgreSQL 18 measurement

Measured 2026-07-30 against the 2026-07-24 production dump restored to PostgreSQL
18.4 and upgraded through `20260729_02`:

| Measurement | Observed |
|---|---:|
| Sessions / drills | 4,184 / 1,739 |
| Initial broad events / sessions represented | 398,400 / 3,620 |
| Boundary first pass | 4.87 s |
| Boundaries stamped / NULL residue | 1,507 / 232 |
| Boundary second pass | 11.31 s; 0 stamped; residue unchanged |
| Cleanup page, 25 sessions | 1.98 s |
| Cleanup page, 100 sessions | 4.89 s |
| Cleanup page, 250 sessions | 10.97 s |
| Remaining 3,809-session sweep | 141.01 s |
| Complete 4,184-session cleanup | 158.85 s (2 min 39 s) |
| 250-session replay | 9.95 s; aggregate unchanged |
| Final broad events / sessions represented / reached | 412,897 / 3,552 / 9,872 |

The selected production page size is **250 sessions**, giving an approximately
10–11-second checkpoint on this shape and 17 pages for the measured cohort. Budget
**three minutes** for the cleanup itself, plus operator checkpoint handling and
verification.

The historical dump predates `opponent_decisions`, so its targeted population is empty.
That makes the restore's targeted invariant structurally useful but numerically trivial;
the frozen targeted fingerprint above is mandatory on the live run.
