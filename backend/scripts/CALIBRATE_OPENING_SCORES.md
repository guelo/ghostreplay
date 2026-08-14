# Opening-score calibration tooling

This directory keeps the reproducible evidence tools used to develop and inspect opening
score candidates. It does not provide a release-authorization workflow. The shipped sm-v2-5
score semantics were reviewed as a product change; a future score, default, or cutoff change needs
ordinary reviewed evidence and its own implementation plan.

## Safety and scope

The tools read production-derived evidence only when explicitly asked to capture a cohort.
They never select or apply public cutoffs by themselves.

- Keep cohort artifacts and full selector results in a private directory outside every Git
  worktree. Do not put them in the repository, `/tmp`, Desktop, or a synced directory.
- Supply the release-guard user only with `GHOSTREPLAY_RELEASE_GUARD_USER` from the private
  environment file; never place a user ID on the command line.
- Do not overwrite an existing artifact, result, or readiness report. Create a new private
  filename for a new run.
- `select-candidates` is analysis evidence. Its output is not authority to change the model,
  defaults, bands, or cutoffs.

The capture artifact and its committed `cohort_provenance.json` record bind the frozen
inputs, source manifest digest, capture runtime, clock, graph, roots, and evidence
derivation. The selector verifies that pair before scoring it.

## Capture a frozen cohort

Use the repository venv and wrapper. The wrapper starts an isolated source-fence process
(`-I -S`), which parses the scorer's literal `SCORER_SOURCE_FILES` manifest without importing
the scorer, hashes it before the child interpreter exists, removes inherited `PYTHON*`
settings, provides only the selected environment's dependency paths, and disables bytecode
writes. The child verifies the inherited digest, bytecode state, import origins, installed
chess origin, and a clean committed scorer tree before it reads the database.

```bash
cd backend
source .venv/bin/activate
set -a; . ~/.ghostreplay-release.env; set +a
./scripts/capture_cohort.sh \
  --output /absolute/private/store/cohort.json
```

Capture reads PostgreSQL in one repeatable-read snapshot, retries if the cohort moves during
the fence, pseudonymizes the artifact, writes it atomically with mode `0600`, then publishes
the reviewable provenance record at `backend/scripts/fixtures/cohort_provenance.json`.

Artifact schema v3 records each pair's distinct score-contributing sessions as exact
`normal`/`drill` counts. Capture derives them from the sessions represented by the frozen
overlay and fences the complete session-to-mode mapping across the snapshot window. Schema-v2
artifacts cannot reconstruct that information and are deliberately unsupported for readiness
analysis; capture a fresh v3 artifact rather than editing or migrating old private bytes. A
converted drill remains classified as `drill` because its persisted `session_mode` is not
rewritten. If a contributing session is deleted or becomes unclassifiable after the snapshot,
the post-snapshot fence treats that as movement and retries; an invalid mapping already present
inside the snapshot remains a hard refusal.

Review that record before committing it. The artifact remains private and untracked. Do not
recapture merely to rerun analysis unless the source/artifact/provenance pair has changed.

## Analyze candidates

After the artifact and provenance record are available, run the analysis-only selector:

```bash
cd backend
source .venv/bin/activate
python scripts/calibrate_opening_scores_v2.py select-candidates \
  --artifact /absolute/private/store/cohort.json \
  --result-output /absolute/private/store/result.json
```

The selector loads the fixed provenance record, validates the frozen artifact, scores the
candidate grid deterministically, and writes a full private result using result format version
2. Its redacted stdout contains only names, booleans, digests, candidate identity, and any
approved winner cutoffs. Existing format-1 private `result.json` files are historical and
unsupported; do not migrate them in place.

Exit status:

| Code | Meaning |
| --- | --- |
| `0` | A candidate mechanism was selected and the private result was written. |
| `1` | No candidate was selected; the private result was still written. |
| `2` | Invalid private input/output path or CLI usage. |
| `3` | Source manifest/digest, bytecode, or import-origin stability refusal. |
| `4` | Artifact, provenance, or selection binding rejection. |
| `5` | Output publication or result-summary validation failure. |
| `6` | Unexpected internal failure, reported without private details. |

The old `select-release` spelling is deliberately rejected with a retirement message. It is
not an alias.

## Assess cutoff-cohort readiness

The readiness command evaluates whether a current-model population is mature enough to begin
the statistical work in `g-cutoff-recalib`:

```bash
cd backend
source .venv/bin/activate
python scripts/calibrate_opening_scores_v2.py cutoff-readiness \
  --artifact /absolute/private/store/cohort-v3.json \
  --report-output /absolute/private/store/readiness-2026-08.json \
  --baseline-report /absolute/private/store/readiness-baseline.json
```

Omit `--baseline-report` for the first snapshot. The report will be valid but fail closed on
the temporal checks; a later run needs a compatible snapshot at least 14 days newer. Inputs
and outputs must be absolute paths outside every registered worktree, and output is
no-clobber.

Archived baselines are authenticated by their report digest and compatibility envelope before
use. A baseline written under an older report schema, readiness policy, score model, or default
cell is valid historical evidence but is not comparable: the current command still writes an
exit-0 report with `temporal_baseline_compatible=false`. A baseline claiming the current
contract must pass the complete current report validator before any of its boundaries are used.
Malformed bytes, a bad self-digest, or a self-inconsistent current-contract report remain input
refusals.

The full private report contains the measured census, color representation, normal/drill
mix, subject concentration, provisional candidate boundaries, leave-one-subject-out results,
deterministic subject-bootstrap intervals, and the two-snapshot comparison. Stdout contains
only model/config identity, digests, check names, booleans, and reason codes. Never copy the
private measured operands into Beads or Git.

Readiness report schema v2 and policy v2 are fixed in code before looking at candidate
boundaries: 20 subjects and
6 per color; 12 normal-playing subjects and 4 per color; at least 60% normal sessions; no
subject above 20% of sessions or 15% of named scores; leave-one-out grade reassignment at
most 5%; bootstrap collision rate at most 1% and p95 grade reassignment at most 10%; and a
compatible 14-day baseline whose boundaries reassign at most 5% of current grades. The current
scoring model and default-cell fingerprint are report identity invariants, not readiness checks
that compare values generated by the CLI with themselves; the artifact-sourced captured model
remains an informative readiness check.

Cutoff derivation and leave-one-out details distinguish `insufficient_scores` from
`cutoff_collision`. Bootstrap evidence records requested, attempted, successful, collided, and
insufficient-score replicate counts. If base cutoffs cannot be derived or fewer than two
subjects exist, no resampling is attempted and the collision rate and reassignment p95 are
`null`; the associated checks fail closed as `not_attempted`. No report equates an analysis that
did not run with 1,000 measured collisions. If resampling runs but produces no successful
replicate, the unavailable reassignment p95 fails closed as `no_successful_replicates` instead.

`ready_for_recalibration=true` means only that representative-data analysis may begin. Every
report pins `authorizes_cutoff_emission=false`; this mode does not alter cohort fitness, raise
the cutoff-sufficiency version, write `format.ts`, or approve any public boundary. The
command exits `0` whenever it successfully writes a valid report, whether readiness is true
or false. Operational refusals use codes `2` through `6`, matching the restricted-path,
source-stability, input-validation, publication/self-validation, and unexpected-failure classes
above. Artifact and baseline input rejections are exit `4`; a result that fails the producer's
own serialization or redaction validation is exit `5`.

## Candidate interpretation

Candidate scoring remains deterministic and preserves the existing mechanism and cutoff
truth tables. `winner_cutoffs` is intentionally absent unless the cohort-fitness criteria
permit cutoffs; currently that policy remains fail-closed. A selected mechanism therefore
does not itself approve a cutoff or product change.

Review the private result with the artifact/provenance pair and record the product decision in
the normal change review. Never copy real scores, cohort operands, or grades derived from a
real player into issue tracking, shell history, or this repository.

## Tests and release-only benchmark

The ordinary backend gate deselects `@pytest.mark.release_seal`. That marker now belongs only
to the documented production-shape PostgreSQL delta-lane benchmark in
`BENCH_OPENING_SCORE_DELTA_LANE.md`; it is not part of calibration and must not be run unless
its disposable PostgreSQL 18 fixture is deliberately provisioned.

For calibration changes, run the focused source-fence, capture, scorer, and selection tests,
then the ordinary backend suite with `-m "not release_seal"`.
