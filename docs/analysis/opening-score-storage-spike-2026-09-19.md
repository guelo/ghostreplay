# Opening-score storage selection — 2026-09-19 UTC

Status: **B50 selection, revised fixture budgets and spike code approved on 2026-09-19**. The original budgets below remain rejected; the [approved budget revision](opening-score-storage-budgets-2026-09-19.md) governs fixture regression checks and the production qualification handoff. The sealed JSON is unchanged; its status and budget fields describe the original submission, not the subsequent decision.

This is the concrete synthetic selection report for `g-score-store-spike`. User review is complete, satisfying that gate and unblocking the writer implementation bead. No application schema, reader, publication lock, or rollout behavior is part of this change.

[Reproduction and measurement contracts](../../backend/scripts/BENCH_OPENING_SCORE_STORAGE.md) · [Complete measurements](opening-score-storage-spike-2026-09-19.json).

The [budget revision](opening-score-storage-budgets-2026-09-19.md) replaces the rejected ceilings with a fresh A reference, B50-based limits, direct checkpoint-heavy measurements, individual-read uncertainty and repeated memory controls. Its measurements are in a separate artifact; this report retains the original results and corrected interpretations.

Artifact SHA-256: `839391064eaa6112e43784bda125486031872006efc7e4aae568ad23b3620878`. Run ID: `daacaeeda2`. The JSON contains the measured source hashes; it was produced from uncommitted benchmark sources awaiting review.

## Selection

Approved selection: **b50**. Layout B, fillfactor 50 on position/root tables, exact full-read Python diff, bounded executemany (500 rows), and explicit C-collated machine identities. The selection and revised fixture-budget approval are recorded in `g-cut-score-churn`.

The five required baseline configurations and one evidence-justified cache trial ran. Selection uses all four comparison budgets plus exact semantic parity; it favors an already-passing B for simplicity. Only the provisional winner was qualified for 100 fixed-working-set cycles. Exploratory runs exposed and corrected timing-boundary defects; their latency verdicts were discarded. All numbers below come from the final corrected run.

The selected current-set schema has stable numeric row IDs and owner/color/natural-key uniqueness. Wide position/root rows retain all semantic fields, including exact confidence; children carry no batch ID, generation, or computed-at churn. Edges and scope are diffed by their complete natural keys. One small marker carries publication/freshness metadata and is replaced atomically with payload changes. Production generation reservation, supersession, mixed-format conversion, and reader retries remain owned by the following implementation beads.

## Representative comparison

Combined WAL includes publication/prune and scheduled ordinary vacuum/ANALYZE. Space includes heap, indexes, TOAST, FSM, and VM. Each cell has 20 advancing-time publications after the same two-publication warmup. Read setup is outside the timer for every adapter.

| Cell | Combined WAL MiB | Vacuumed MiB | Publication p95 ms | Original nested read p95 ms | Original mechanical verdict |
| --- | ---: | ---: | ---: | ---: | --- |
| a | 785.65 | 334.70 | 2,874.74 | 10.44 | reference |
| b100 | 175.82 | 56.73 | 1,145.67 | 5.56 | pass |
| b50 | 44.44 | 31.04 | 975.93 | 4.84 | pass |
| b50_cache | 44.47 | 31.04 | 976.56 | 11.02 | pass |
| d100 | 84.04 | 35.35 | 903.27 | 4.20 | pass |
| d50 | 43.18 | 29.28 | 1,015.72 | 17.44 | noisy read statistic; inconclusive |

The original read statistic takes the maximum of five warmed reads per publication, then p95 across only 20 such maxima. Its outlier sensitivity does not establish that D50 regresses: the median of those maxima is 4.13 ms for D50 versus 4.09 ms for A, and all uncached layouts are about 3.5–4.1 ms. A's 10.44 ms reference is itself tail-sensitive. Retain the original values for audit, but do not interpret the mechanical gate as evidence against D50. B wins because it already passes untuned and the design favors simpler B. The revised measurement retains individual reads and reports paired blocks.

Every measured publication passed full persisted-payload equality, including nullable metrics, exact confidence, branches, counters, timestamps, edges, scope, and final ID maps when cached. Bounded reads include 32 positions, 16 roots, edges for 16 parents, and the candidate marker fence.

Attributable WAL record bytes by logical group (indexes included; mixed/metadata, TOAST attribution gaps, and page/alignment bytes remain separate in JSON):

| Group | A MiB | Selected MiB | Reduction |
| --- | ---: | ---: | ---: |
| positions | 265.28 | 31.04 | 88.3% |
| roots | 33.92 | 2.04 | 94.0% |
| edges | 329.48 | 5.87 | 98.2% |
| scope | 143.99 | 3.76 | 97.4% |

Thus combined savings can be assessed separately from the confidence-bearing position/root writes.

## Workload provenance and changed fields

The installed real graph/overlay/scorer produced 1,149 positions / 1,143 edges initially, growing to 1,363 / 1,357. The persistence-only fixture uses 17 replicas, initially 19,533 positions, 19,431 edges, 1,853 roots, and 18,309 scope rows. Replica identities are explicitly synthetic, not legal FENs or new evidence.

The deterministic observation runs from 2026-01-01T12:10:00+00:00 through 2026-01-09T17:30:00+00:00: four active events 20 minutes apart, then a two-day idle gap, repeated. Normal/drill terminals, evidence correction/deletion, warm reads, and unrelated epoch changes are included. No production telemetry was used; these are synthetic frequencies.

| Reason | Steady rebuilds | Fraction | Exactly equal stable output |
| --- | ---: | ---: | ---: |
| cache_miss | 0 | 0.0% | 0 |
| decay_staleness | 4 | 20.0% | 4 |
| evidence_change | 16 | 80.0% | 0 |
| registry_drift | 0 | 0.0% | 0 |
| stale_branch_keys | 0 | 0.0% | 0 |

Denominator: 20 actual steady rebuilds from 60 requests. There were 40 cached results, including 20 metadata-only epoch re-arms, and zero failures. Outside that denominator, startup produced one cache-miss rebuild and one no-evidence result; forced controls produced one registry-drift rebuild, one stale-branch-key rebuild, and one frozen-time cached result.

Time-only rebuilds changed 68.8%–70.5% of common position confidences; all non-confidence payload stayed equal. Evidence rebuilds changed stable output every time. The JSON supplies per-field counts and root/position/edge/scope membership changes for every rebuild.

## Cache, hash, statement text, and optional work

The paired cache trial used b50. Publication p95 ratio to its full-read baseline was 1.001; observed single-pair hit fraction was 85.0%, including labeled cold/restart/external-marker misses. Mean retention/accounting cost was 290.15 ms. Its optimistic break-even hit fraction was 77.4%.

This does not show that caching cannot help. A hit avoids roughly 378 ms of payload reads, while recursive deep-size accounting adds about 290 ms: that cost belongs to this trial's accounting method, not inherently to caching. The selected design defers the cache because uncached B50 already improves publication p95 by about 3× versus A and the large entries constrain residency. Cheaper accounting or measured production locality could justify revisiting it later; this spike does not select that extra mechanism.

One representative entry retained 30.34 MiB, including ID maps. At most 2 such pairs fit the initial 64 MiB/eight-entry bounds; the bytes limit binds first. Equal identity strings are shared within a candidate; the deep-size estimate counts shared objects once per entry and conservatively charges each entry independently.

The separately labeled eight-pair round-robin cache control recorded 0 hits, 32 misses, and 30 evictions. That is a synthetic capacity-pressure trace, not measured production residency. Cold/eviction/mismatch paths and accounting costs remain in the raw report.

Hash screen: eligible equal-output share 20.0%; optimistic avoided-read time 79.37 ms/publication; measured mean encoder+SHA-256 cost 398.28 ms/publication; resulting optimistic net saving -318.92 ms versus the predeclared 48.80 ms threshold. The optimistic break-even eligible share is 116.5%. Hashing is screened out; no durable format, hash metadata, or hash reader is selected.

Actual SQL from the independent before-execution probe (the timed cells' after-event `statements` arrays contain templates before ORM expansion):

| Write path | A max SQL bytes | Selected max SQL bytes | A RETURNING |
| --- | ---: | ---: | --- |
| opening_position_scores | 568 | 387 | False |
| opening_position_edges | 463 | 269 | False |
| user_opening_scores | 784,353 | 847 | True |
| opening_score_batch_shared_scope | 80,832 | 101 | True |

Position/edge inserts remain fixed Core executemany statements without RETURNING. A's root/scope ORM writes still produce long, bounded 1,000-row pages and final remainders. This is not evidence that historical query-text storage growth has disappeared. The selected publisher already uses short fixed executemany statements for all payload groups, so separate SQL-text remediation is unnecessary. Retain the current large-write regression and the new expanded-page trace regression.

Skipped cells and reasons:

- C: excluded by design
- losing-layout tuning: stop rule
- production telemetry: not required or available in this synthetic run
- E: D meets WAL target; chunked confidence not eligible
- COPY/extra fillfactor/vacuum/100k sweeps: no measured concern justifying them
- hash: optimistic savings after measured encoder cost below material threshold
- fallback-frequency sweep: bounded reads stay below actual 1500ms scheduler quiet window

## Original sustained qualification and rejected budgets

The qualification uses a fixed union of captured keys and repeats the captured values, not 100 new scorer requests. Its A reference uses the identical inputs and checkpoint/vacuum schedule.

| Measure | A, 100 cycles | Selected, 100 cycles | Rejected original limit |
| --- | ---: | ---: | ---: |
| Combined WAL MiB | 4,341.59 | 171.11 | 2,170.79 |
| Vacuumed total MiB | 360.21 | 29.61 | 360.21 |
| Publication p95 ms | 6,398.38 | 1,069.90 | 7,038.22 |
| Bounded-read p95 ms | 39.78 | 7.46 | 43.76 |

The original mechanical comparison passed, but its A latency reference is unsuitable for release budgets. A slowed to 4–5 s block medians during cycles 51–90, with individual publications up to 33.5 s, then recovered while WAL and footprint stayed flat. That pattern is consistent with host interference rather than growing storage cost. The revised reference replaces this latency window; no samples are silently removed. Last five selected equal-vacuum windows were all 29.61 MiB, with 0.0% growth and fixed live counts.

The original WAL schedule checkpointed once per ten publications. The 96% sustained reduction is specific to that schedule. A publication after a checkpoint incurs more full-page WAL, so production observation must separate warm and post-checkpoint publication costs and add measured ordinary maintenance WAL. The deployed checkpoint frequency is unknown; a five-minute setting must not be assumed. The budget revision directly measures the every-publication-checkpoint case rather than treating the low-checkpoint result as a production forecast.

The held-reader case retained 50,884 dead tuples; after release and ordinary vacuum, 0 remained. Every relation/index/TOAST footprint is retained in JSON.

Fresh persistence-worker RSS high-water was 227.81 MiB; publication allocation peak was 48.34 MiB. The original 1.1× limits (250.59 and 53.17 MiB) are rejected as too tight for one sample. The revision uses repeated fresh workers and larger headroom. These measure persistence with two candidates, not whole-worker scorer/evidence memory. The scorer-process high-water before replication was 184.73 MiB. Integrated qualification must still measure the actual implementation under its production workload.

Selected cache budget: 0 entries / 0.00 MiB. None of the original numerical release limits was approved.

Measured update/HOT counters after the sustained run:

| Relation | Updates | HOT updates | HOT fraction |
| --- | ---: | ---: | ---: |
| edges | 7,106 | 3,810 | 53.6% |
| positions | 1,504,262 | 1,504,221 | 99.9973% |
| roots | 60,996 | 60,987 | 99.9852% |

## Nonshipping hourly-time control

Hourly bucketing produced a maximum absolute synthetic confidence deviation of 0.021631281. The winning-adapter bucketed replay used 28.93 MiB combined WAL versus 44.44 MiB in the continuous comparison. This reused the qualified adapter; it is an estimate, not another layout selection or shipping proposal. Scorer time is separate from real publication/freshness timestamps. Quantization requires separate model/fingerprint and UX approval.

## Review boundary and limitations

Validation passed 30 distinct checks: the 29-check targeted run (spike release-seal cases plus the existing large-write regression), followed by the added actual expanded-SQL regression. Ruff, compilation, saved-source-hash verification, and saved qualification/reclamation checks passed.

The test suite checks destination guards, real reason accounting, epoch re-arms, exact replay across all layouts, C collation, cache bounds/ID maps/eviction/mismatch, rollback, post-commit cache failure, protocol-byte accounting, hash opportunity/cost accounting, and identical read-timer boundaries. It is release-only and excluded from pre-push.

This is synthetic evidence with explicitly replicated persistence rows on a local PostgreSQL 18.4 C-locale cluster. The DB round trip, synthetic reason mix, payload distribution, controlled maintenance cadence, and cache residency do not establish production behavior. HOT counts are measured in the JSON, not assumed from fillfactor. No losing-layout tuning or production rollout was performed.

The full-read diff transfers an average 11.37 MB of result DataRows per representative publication, versus 0.589 MB for A (decimal MB; excludes protocol control messages/TLS). Loopback hides the network transfer cost of this approximately 19× increase. Integrated qualification must remeasure publication and bounded-read latency over the actual application-to-database path, record RTT/throughput and checkpoint settings, and obtain final latency-ceiling review before activation. Local provisional ceilings cannot establish that result.

The B50 selection and revised fixture budgets are approved and recorded in `g-cut-score-churn`, completing `g-score-store-spike` and making `g-score-store-writer` actionable. Reader implementation, integrated production-shape qualification including the real network path, cutover, and seven-day observation remain separate gates. This spike changes no production behavior; qualification must derive and obtain review of production-shape ceilings before activation and observation.
