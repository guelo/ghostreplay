# Opening-score calibration tooling

This directory keeps the reproducible evidence tools used to develop and inspect opening
score candidates. It does not provide a release-authorization workflow. The shipped sm-v2-4
decision was reviewed as a product change; a future score, default, or cutoff change needs
ordinary reviewed evidence and its own implementation plan.

## Safety and scope

The tools read production-derived evidence only when explicitly asked to capture a cohort.
They never select or apply public cutoffs by themselves.

- Keep cohort artifacts and full selector results in a private directory outside every Git
  worktree. Do not put them in the repository, `/tmp`, Desktop, or a synced directory.
- Supply the release-guard user only with `GHOSTREPLAY_RELEASE_GUARD_USER` from the private
  environment file; never place a user ID on the command line.
- Do not overwrite an existing artifact or result. Create a new private filename for a new
  run.
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
