# Opening storage cutover: real-path measurement and review

Status: **measurements and S1 budgets reviewed; review fixes prepared; production writer remains legacy**.
The cutover bead remains in progress. This report does not authorize activation
or start the separate seven-day observation period.

## Evidence and scope

The user authorized temporary non-production Railway services. An isolated
Postgres 18.6 service was restored from the existing private qualification dump,
and an idle Python 3.12.7 container exercised its private Railway network path.
Both ran in SFO. No production database writes, configuration changes or
deployments were performed. Production remained on compatibility revision
`cdb8f890ca5435d7fe2e05f518d65c8eb0ecb689` with one application instance.

The final runs used the production package inventory, including SQLAlchemy
2.0.54, and source/server/runtime seals. The unchanged qualified paired
workloads and evaluator supplied the relative gates. The new runner independently
checks environment, database identity and sentinel before allowing writes.
The [aggregate JSON](opening-score-storage-cutover-2026-09-24.json) records
input hashes, distributions, confidence intervals and proposals. Raw captures,
reports and logs remain private; the downloaded evidence archive SHA-256 is
`5356ec0b268403d6dc373bb2d7772c433c622cba8d71545de142febf1792c309`.

C1 retained seven paired blocks, 70 publications and 910 individual reads of
each composite per layout at 24,848 logical rows. C2 retained six paired blocks
and 60 publications per layout at 23,806 logical rows. No cross-host pooling or
extrapolation was used. These replay workloads are not representative live
traffic; the production census had one active user.

## Results

Every relative gate passed. Publication and read gates require the upper end of
the paired-block bootstrap 95% interval to be at most 1.1.

| Metric | B50/A ratio | 95% interval |
| --- | ---: | --- |
| C1 publication p95 | 0.644 | 0.435–0.775 |
| C1 composite D p95 | 0.925 | 0.869–1.031 |
| C1 composite T format-stage p95 | 0.797 | 0.713–1.036 |
| C2 publication p95 (expectation) | 0.555 | 0.493–0.699 |

Combined publication-plus-vacuum WAL ratios were 0.059 for C1 and 0.191 for
C2, below the 0.5 limit. Vacuumed footprint ratios were 0.074 for both cells,
below the 1.0 limit. Existing size-adjusted C1 warm publication WAL, C1 vacuum
WAL, C2 post-checkpoint publication WAL, and both footprint ceilings passed;
exact comparisons are in the aggregate JSON, including the failed historical
C2-versus-warm comparison and the separate post-checkpoint metric.

The existing vacuum-WAL fit used C1 warm windows exclusively. Applying it to
C2 would reject the already-qualified local C2 result as well as this Railway
run: both measured 4,799,283 bytes at S1. The
[separate C2 budget proposal](opening-score-vacuum-budget-scope-2026-09-24.md)
preserves the warm ceiling and proposes a post-checkpoint ceiling of 7,340,032
bytes at 23,806 rows, fitted only over the already-qualified C2 size range.
The user approved this interpretation and budget. Qualification’s ceiling
builder now emits the post-checkpoint vacuum metric and regenerates its fit
from the sealed report; the warm ceiling remains unchanged. Post-checkpoint
vacuum is production-applicable because rebuild gaps exceed checkpoint timeout.

## Proposed real-path absolute limits

The user accepted these **limits at the measured S1 size only**, with the
previously reviewed methodology’s headroom factors. S1 is one times the captured
production pair, representing today’s measured size. These are not a
production-wide fit. The evaluator always emits unreviewed proposals; this
review record is the authority for their acceptance.

| Metric | Measured | Proposed ceiling | Logical rows |
| --- | ---: | ---: | ---: |
| C1 publication p95 | 2,333.03 ms | 3,500 ms | 24,848 |
| C1 warm composite D p95 | 61.40 ms | 123 ms | 24,848 |
| C1 warm composite T format-stage p95 | 36.40 ms | 73 ms | 24,848 |
| C2 publication p95 (expectation) | 1,791.39 ms | 2,688 ms | 23,806 |
| Worker RSS maximum | 177,184,768 B | 265,777,152 B | 24,848 |
| Publication allocation maximum | 39,287,193 B | 58,930,790 B | 24,848 |

Publication and memory use 1.5× headroom; reads use 2×. Latency ceilings round
up to whole milliseconds; memory rounds up to whole bytes. RSS and allocation
use five fresh workers per layout. Observation must retain at least 500 reads
per comparable window and explicitly resolve applicability for other sizes.
Use **3,500 ms as the publication p95 limit** and retain the C2 value of
2,688 ms as an expectation. Production is post-checkpoint with reads, matching
neither C1’s warm/interleaved-read nor C2’s post-checkpoint/no-read schedule.
Composite T is slower than the local expectation (36.4 ms here versus 28 ms
at 99,392 rows); its upper relative bound, 1.036, is closest to the 1.1 gate.

Both read proposals come exclusively from the deviating **C1 warm cell at
`max_wal_size=8192MB`**. They do not establish post-checkpoint read ceilings at
128MB. The observation handoff must preserve this warm-only provenance even
though the original summarizer labels the proposals only as unreviewed.

## Corrections retained in the evidence

The first warm run at `max_wal_size=128MB` discarded all ten blocks because
checkpoints contaminated the warm windows. C1 was repeated using qualification
§9.5's warm-only 8GB fallback. C2 and memory retained 128MB; the temporary
database was restored to 128MB afterward. This exception is recorded explicitly
and does not propose changing production settings.
The archived C1 settings confirm `8192MB` from `configuration file`, with
`wal_keep_size=2048MB` from the same source. The final run used a fresh per-cell
database and the r3 runtime/source manifest; no r1 samples were pooled. Three
timed-checkpoint discards left seven retained pairs, above the four-pair floor.
The user's asynchronous reply explicitly approved this fallback after the run
and teardown had completed. That approval covers the measurement procedure,
not the proposed budgets or activation, and does not resolve
`g-vacuum-budget-scope`.

A fresh unpinned build initially installed SQLAlchemy 2.1.0. Those preliminary
runs are diagnostic only. Final runs matched the complete production inventory.
The proposed production constraints file prevents that drift. A separate cold
build verified the constraints and explicit virtualenv creation: deployment
`c4170d21-c9d0-4e05-837c-9e0fec565060` succeeded, its complete package inventory
matched production, Python was 3.12.7 and the writer default was `legacy`.

The initial Linux memory subprocesses inherited the capture-heavy parent's
`ru_maxrss`, yielding an identical 1.73GB high-water mark. Linux preserves these
[resource measurements across exec](https://www.man7.org/linux/man-pages/man2/getrusage.2.html).
The final collector launched the same sealed worker from a parent that never
loads the full capture, alternating five workers per layout. It retains the
original report and all child reports. The evaluator rejects the contaminated
Linux launch mode. Interrupted and preliminary reports remain in the private
archive and are excluded from final evaluation.

## Implementation, validation and handoff

The implementation adds a validated deployment selector, defaulting to legacy,
that takes effect on subsequent real rebuilds. Explicit maintenance overrides
remain available. No conversion sweep or scheduler redesign is introduced.
The runbook documents coordinated restart/drain, lazy conversion and rollback:
disable current writes, retain compatibility readers/guard, and reverse-convert
every current pair before using a pre-compatibility binary or schema.

Validation: 340 existing storage/cache/scheduler/analytics regression tests
passed; all 49 selector, endpoint/runtime guard, evaluator and memory
collector tests passed, including the review regressions. The temporary cold-build verification also passed.

The temporary Railway environment was deleted after the evidence archive was
downloaded and hash-verified. Project status now lists only the original
production environment and two production services, both successful. Existing
local qualification artifacts and clusters remain retained for their separate
teardown decision.

The user approved commit after two fixes: public evidence must be an evaluator
projection, and startup must validate and log the selector (also documented in
the environment example). Both fixes are implemented. The regenerated JSON
retains `activation_authorized: false`, the deviation flag, warm labels, all
shape comparisons, per-cell vacuum maxima/window counts and five B50 memory
samples. Historical measurement/evaluation artifacts are retained unchanged;
new evaluation r4 supplies the public projection. The historical warm-fit C2
check remains `pending_review` in computed evidence, with its resolution recorded
in this approved review and the separate vacuum report. No hand-added status,
archive digest or approval metadata is attributed to the evaluator.

Pending work is
tracked in `g-score-store-cutover`, `g-score-runtime-pin`,
`g-nixpacks-cold-venv` and `g-vacuum-budget-scope`. After approval, commit and
deploy the compatibility release, verify its actual revision/runtime, coordinate
the publisher, enable B50 and verify atomic production conversion plus mixed
reads. Only then hand the reviewed profile and actual activation timestamp to
`g-score-store-observe`. No observation start time exists yet.
