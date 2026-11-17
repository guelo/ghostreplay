# Opening storage: production observation baseline

Status: baseline verified; production acceptance remains unresolved under
`g-score-store-observe`. No representative seven-day window has started.

## Verified deployment and census

Read-only Railway checks on 2026-09-25 independently confirmed:

- Successful application deployment `4ca1d9ad-d513-429c-a947-0ed903c8912e`,
  commit `a9757150dc4469b5b2fd958a9c3e55d2c9f3f311`, one configured replica.
- Deployment creation: `2026-09-25T07:49:52.942Z`. The startup message
  `INFO OPENING_SCORE_STORAGE_FORMAT=current-b50-v1` was recorded at
  `2026-09-25T07:50:54.859003477Z`. This establishes selector-enabled startup,
  not the instant traffic became healthy or the first committed publication.
  Railway labels the envelope `error`, but the application message is INFO;
  the envelope alone is not evidence of a startup failure.
- Python 3.12.7; all 39 installed package versions match the deployed
  `requirements.production.constraints.txt`.
- Census snapshot `2026-09-25T08:27:05.883327Z`: **one active user** in the
  trailing 30 days; one latest B50 pair and 23 latest legacy pairs.
- PostgreSQL 18.6; `checkpoint_timeout=300s`, `max_wal_size=128MB`,
  `shared_buffers=16384` 8-KiB blocks (128MiB), `full_page_writes=on`,
  `wal_compression=off`, `wal_level=replica`.

The user previously reported first B50 publication at
`2026-09-25T08:05:44.676448Z` and successful dashboard, detail/tree,
legacy-color and new-session baseline/delta smoke checks. Those remain
operator-reported checks, distinct from this session's direct verification.
Historical publisher drain/direct-script exclusion remains unattested; current
single-replica configuration cannot prove that historical coordination.

## Storage invariants checked

A separate repeatable-read, read-only snapshot at
`2026-09-25T08:27:52.563952Z`, with a 15-second statement timeout, passed:

- Every row in each of the four `opening_current_*` tables belongs to a pair
  whose latest marker is B50.
- Every root, position, edge and shared-scope legacy payload has an existing
  legacy parent marker and belongs to a pair whose latest marker is legacy.
- Every current pair has exactly one marker.
- Current markers have non-null evidence sequence, cache epoch and scoped
  shared digest.

Latest means greatest generation per `(user_id, player_color)`. Queries emitted
only aggregate counts, configuration and pass/fail booleans. They did not emit
IDs, FENs, scores or grades, and performed no writes or maintenance.
These checks establish ownership/retirement and stamp presence at one snapshot;
they do not establish semantic completeness, current evidence freshness,
race-free operation over time, or bounded growth.

## Acceptance applicability

The original gate requires seven representative days of normal/drill activity
and daily decay. One active user cannot establish representative traffic by
waiting seven days. No telemetry provisioning or acceptance clock is justified
by this census alone. Changing that gate requires a recorded user decision.

The cutover's reviewed budgets remain the authority, superseding earlier bead
notes that described all latency proposals as unreviewed or required deriving
new thresholds from the observation itself:

| Measure | Applicable authority and limitation |
| --- | --- |
| Publication p95 | Reviewed S1 limit 3500ms; C2 2688ms is an expectation only. |
| Composite D / T format-stage p95 | 123ms / 73ms at 24848 logical rows, derived from warm C1 with 8192MB max WAL. Not post-checkpoint 128MB read limits. |
| Memory | RSS 265777152B and allocation 58930790B at measured S1; fresh-worker provenance matters. |
| WAL and vacuumed footprint | Use sealed qualification size fits; separate reviewed C2 post-checkpoint vacuum fit. Do not extrapolate or use fixture constants. |
| Read samples | At least 500 individual reads per comparable size/revision/settings window; each layout separately for A/B comparisons. |

See cutover commit `a975715`, its
`docs/analysis/opening-score-storage-cutover-2026-09-24.md`, adjacent JSON and
`opening-score-vacuum-budget-scope-2026-09-24.md`, all on master since the
cutover merge. No source updates or historical reports were overwritten when
this baseline was recorded.

## Proposed decision for review

Accept a narrower **single-user deployment verification** milestone using this
census, ownership checks, existing qualified matched-path benchmarks and the
operator's functional smoke checks. Explicitly defer representative production
performance acceptance. This proposal does not turn benchmark replay into live
traffic or treat absent measurements as passing. The original bead remains open
unless its acceptance contract is explicitly revised.

Retain all legacy tables/readers, the selector and publication guards: 23
dormant legacy pairs still require them. Create no cleanup migration. Revisit
when broader real usage exists and the measurement gaps below are addressed.

## Measurement gaps before the original gate can pass

The deployed scheduler's `opening_score_recompute_run` records rebuild reason,
trigger mix and whole-worker duration. `opening_score_terminal_convergence`
provides terminal convergence timing. Whole-worker duration is not publication
transaction latency, and terminal convergence is not composite T format-stage
latency. Existing logs alone do not provide a complete budget-matched sample.

B50 uses exact full-read diffs without a selected payload cache or payload hash
optimization. Their hit-rate/residency requirement is **not applicable**;
unrelated replay-cache metrics must not be substituted.

Any future collection must identify deployed revision, runtime, database
settings, workload size/mix and checkpoint regime for every comparable window;
retain individual measurements privately under the existing private-artifact
rules; and distinguish normal, drill and daily-decay coverage. Publication,
bounded-read and allocation instrumentation still need matching measurement
boundaries. Process-wide RSS is not isolated publication allocation.

Table live/dead estimates and relation sizes can describe storage, but a single
snapshot cannot prove reclamation or a plateau. Compare ordinary-vacuum
boundaries, record live logical evidence growth separately, and account for
legacy conversion. Global WAL LSN deltas under concurrent traffic are not
table-specific opening WAL; they cannot be silently compared with the isolated
qualification ceilings. No forced vacuum, checkpoint, rebuild, conversion,
benchmark workload or production instrumentation was run for this baseline.
