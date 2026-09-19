# Opening-score storage budget revision — 2026-09-19 UTC

**B50 and these replacement fixture budgets were approved on 2026-09-19**, including the minimum 500-read sample and mandatory production-shape qualification below. The original A-derived budgets remain rejected. This revision records a fresh reference, selected-design ceilings, checkpoint-aware WAL and repeated memory measurements. The approval completes the spike review gate and unblocks the writer implementation bead; production qualification and activation remain separate gates.

[Selection decision](opening-score-storage-spike-2026-09-19.md) · [Complete revised measurements](opening-score-storage-budgets-2026-09-19.json) · [Reproduction](../../backend/scripts/BENCH_OPENING_SCORE_STORAGE.md#budget-revision-after-selection-review).

Revised artifact SHA-256: `32846a015aa35ac0a06a3bdaafdea6567fde8ffcf7f00a05b7f10d2386c073fe`. Run ID: `8e7f70dd35`. The original artifact remains byte-for-byte unchanged (`839391064eaa6112e43784bda125486031872006efc7e4aae568ad23b3620878`); its rejected budgets are preserved only for audit. All seven original source hashes still match. The revised JSON also pins the new measurement/summary sources and additional loaded model/graph authorities. All eight deterministic timeline fields match the original; elapsed scorer time and process RSS are intentionally excluded.

Source provenance at commit: the revised run's `backend/app/models.py` hash includes concurrent, uncommitted opponent-retention schema additions (the session expiry column and `OpponentTargetFact`). Those unrelated edits are excluded from the spike commit; its opening-score models match the measured source. The JSON records the actual measured file hash, so reproducing from the spike commit alone will report that model-source difference. Neither artifact was regenerated for closeout.

## Fresh A reference and read uncertainty

Both layouts ran 100 fixed-membership publications on the same regenerated synthetic payloads. Ten-publication blocks alternate AB, BA, AB, BA, with matched checkpoint/vacuum schedules. All 500 individual warmed read samples per layout are retained. No outliers or slow blocks were removed.

**Read-ceiling evaluation requires at least 500 pooled individual reads per comparable workload/configuration window.** Apply the 8 ms ceiling to that pooled p95, including all samples, not to individual reads or 50-read block p95s. For a matched A-relative read comparison, each layout needs at least 500 reads. Blocks remain diagnostics and the units for paired uncertainty: B50 has 50-read block p95s of 8.28 and 6.23 ms while its qualifying 500-read pooled p95 is 3.79 ms. Fewer than 500 reads is insufficient evidence, not a pass or failure; collect a larger representative sample. Do not pool unlike workload sizes or revisions/settings to reach the minimum. The checkpoint-heavy control has only 100 reads and does not independently qualify a read ceiling.

| Measure | Fresh A | B50 |
| --- | ---: | ---: |
| Publication median ms | 2,798.40 | 899.82 |
| Publication p95 ms | 2,965.78 | 971.44 |
| Maximum publication ms | 3,026.37 | 1,120.92 |
| Individual bounded-read median ms | 3.66 | 3.12 |
| Individual bounded-read p95 ms | 4.33 | 3.79 |
| Combined WAL MiB, checkpoint every ten | 4,341.57 | 171.25 |
| Final vacuumed footprint MiB | 360.21 | 29.61 |

A's ten block medians are 2,721.92, 2,819.63, 2,826.46, 2,782.50, 2,795.96, 2,791.96, 2,803.13, 2,874.08, 2,723.60, 2,867.87 ms. Max/min block-median ratio is 1.06; maximum publication/median is 1.08. The earlier 4–5 s median window and 33.5 s publication are absent. Host load and client CPU measurements are retained; this stable rerun cannot prove complete host isolation.

Paired ten-publication block bootstrap (4,000 deterministic resamples) gives B50/A p95 ratios: publication 0.33, descriptive 95% interval 0.31–0.35; bounded reads 0.88, interval 0.81–1.05. Both relative 1.1 gates are pass/pass. A gate is inconclusive when its interval straddles 1.1, requiring more paired sampling. These intervals describe this local sample, not production uncertainty. The original D50 nested-max read verdict remains inconclusive and was not used to change the approved B50 selection.

## Direct checkpoint-heavy WAL measurement

Fresh schemas replayed all 20 growing representative publications with a checkpoint before every publication. Normal vacuum still runs every ten. This is a measured endpoint, not the earlier projection. Full-page writes and the remaining cluster settings are captured in the JSON. Production checkpoint settings/frequency remain unmeasured.

| 20 representative publications | A | B50 |
| --- | ---: | ---: |
| Combined WAL MiB, checkpoint before each | 1,144.92 | 111.85 |
| Post-checkpoint publication WAL p95 MiB | 54.94 | 5.85 |
| Vacuum WAL total MiB | 190.30 | 4.80 |

B50 reduces combined WAL by 90.23% under this checkpoint-heavy schedule. The low-checkpoint sustained run saves 96.06%; neither percentage is a deployment forecast. Compare warm/post-checkpoint publication counts and actual maintenance WAL separately during observation.

Maintenance also changed with the checkpoint schedule: the original representative A/B50 vacuum totals were 47.59/0.83 MiB, versus 190.30/4.80 MiB here. Keeping the original maintenance WAL constant would miss that measured difference. The approved fixture limits therefore keep maintenance separate and require the same cadence in comparisons.

## Approved replacement fixture ceilings

Both requirements apply: retain the A-relative minimum improvements **and** enforce the B50-based absolute ceilings below. A getting slower cannot loosen the latter. These are regression ceilings for this synthetic fixture only: the fixed set has 23,545 positions and 71,111 logical payload rows; the growing fixture ends at 23,171 positions and 69,989 logical rows. Logical rows count positions, roots, edges and scope once, excluding the marker, indexes and MVCC versions. The unreplicated synthetic user produced only 1,149–1,363 positions; persistence-only replication multiplied its payload by 17.

**Production observation must not use these fixture-sized absolute ceilings.** Before closing `g-score-store-qualify`, derive and obtain review of production-shape WAL, footprint, memory and latency ceilings for representative per-owner/color sizes and row mixes, changed-row fractions, fixed/growing membership and actual checkpoint/vacuum settings. Record the logical-row denominators, fixed overhead, applicable size ranges and sampling windows with the tested revision. Retain the A-relative improvement requirements. The qualified result may use reviewed per-row formulas or size-specific limits backed by matched measurements; copying the 9.5 MiB, 47 MiB or other synthetic constants, or assuming linear scaling from the 17× replicas, does not satisfy this gate. `g-score-store-cutover` must hand off that reviewed profile, and `g-score-store-observe` must use it for the seven-day decision. An absent profile, out-of-range workload or insufficient sample is unresolved evidence and requires qualification/review before production acceptance.

A-relative requirements: combined WAL ≤0.5× matched A under the same checkpoint/vacuum schedule; vacuumed footprint ≤1× A; publication and individual bounded-read p95 ≤1.1× matched A, with the paired uncertainty rule above. Latency also has the separate provisional local ceilings below.

| Metric | B50 anchor | Approved fixture ceiling | Derivation |
| --- | ---: | ---: | --- |
| Warm publication WAL, fixed-set p95 MiB | 1.49 | 2.25 | 1.5×, round up 0.25 MiB |
| Warm publication WAL, growing-set p95 MiB | 2.58 | 4.00 | 1.5×, round up 0.25 MiB |
| Post-checkpoint publication WAL, p95 MiB | 6.26 | 9.50 | 1.5×, round up 0.25 MiB |
| Vacuum WAL per ten fixed-set publications, MiB | 0.28 | 0.50 | 1.5× maximum, round up 0.25 MiB |
| Vacuum WAL per ten growing-set publications, MiB | 2.79 | 4.25 | 1.5× maximum, round up 0.25 MiB |
| Vacuumed total footprint, MiB | 31.04 | 47.00 | 1.5×, round up 1 MiB |
| Local publication p95, ms | 971.44 fixed / 1,022.33 checkpoint-heavy | 1,500.00 | 1.5× fixed anchor, rounded; 1.47× checkpoint-heavy |
| Local pooled bounded-read p95, ms (≥500 reads) | 3.79 | 8.00 | 2×, round up 1 ms |
| Isolated persistence RSS, MiB | 228.66 | 350.00 | 1.5× repeated maximum, round up 10 MiB |
| Publication allocation peak, MiB | 47.97 | 75.00 | 1.5× repeated maximum, round up 5 MiB |

Warm WAL anchors cover both fixed and growing representative workloads; post-checkpoint anchors cover fixed and every-publication-checkpoint replays. Footprint/maintenance anchors cover all selected representative and fixed runs. Exact byte operands are in JSON. Continue requiring exact parity, fixed live counts and ≤5% footprint growth across the last five equal-vacuum windows. Selected cache limits remain zero entries/zero bytes.

The 6.26 MiB post-checkpoint anchor has 10 samples and the 2.58 MiB growing-set warm anchor has 18 samples. With the recorded nearest-rank p95 definition, both are sample maxima. Their 1.5× headroom is retained, but these small samples do not establish a stable population tail. The 1,500 ms publication ceiling is also retained with its explicitly thinner 1.47× headroom under the checkpoint-heavy schedule; it is not described as at least 1.5× every measured schedule.

For a fixture qualification interval, the absolute combined-WAL ceiling is `Nwarm × warm_ceiling + Npostcheckpoint × postcheckpoint_ceiling + Nvacuum10 × vacuum_ceiling`, with per-state p95 checks in addition. `Nvacuum10` counts comparable ten-publication maintenance windows; a different vacuum cadence requires measured maintenance accounting and a matched A baseline, not rounding up free allowances. Also enforce the ≤0.5× matched-A combined ratio. This replaces the single per-100 WAL constant. Production observation uses the same accounting structure with its separately qualified production-shape operands, never these fixture-sized allowances by default.

Use the fixed-working-set WAL profile for fixed membership, and the growing profile only when membership grows. This prevents the 2.79 MiB growing-data vacuum anchor from loosening the fixed-set maintenance check (measured maximum 0.28 MiB). The resulting envelopes are 302.50 MiB for the recorded fixed 100 publications and 198.50 MiB for the recorded 20 checkpoint-heavy growing publications. Those are illustrative count-weighted totals, not universal per-100 limits.

Five fresh persistence workers measured untraced RSS of 225.95, 225.66, 228.66, 228.62, 226.52 MiB and allocation peaks of 47.97, 47.97, 47.96, 47.97, 47.97 MiB. These budgets exclude scorer/evidence allocations and are not a whole-application memory limit. Integrated qualification must measure the real worker too.

All local comparison/absolute/parity gates pass: **True**. The last-five-window footprint growth is 0.00%, with fixed live counts: True. The original slow-reader reclamation result remains applicable to the unchanged adapter; this budget revision did not repeat it.

The raw artifact and source hashes remain unchanged after this review clarification. Its local read verdict uses the 500-read fixed-set pool; block diagnostics and the 100-read checkpoint control are not separate read acceptance verdicts. The minimum sample and production-shape handoff requirements here govern future qualification and observation; the historical summary's `local_gates_pass` does not establish production acceptance.

## Network gate and review status

The original representative full-read diff returns 11.37 MB of DataRows per publication versus 0.589 MB for A (decimal MB; protocol control/TLS excluded). These measurements use loopback. The final publication/read latency ceilings must be reviewed after a matched run over the actual application-to-database path, with RTT/throughput, deployed checkpoint/full-page-write/WAL-compression settings and complete worker memory recorded. This requirement is now explicit in `g-score-store-qualify`; activation remains blocked until it passes. No remote database or production data was accessed by this revision.

B50 remains the approved simple baseline. Cache deferral reflects already-good baseline latency and roughly 30 MiB entries/two-pair capacity, not a finding that caching cannot help; the 290 ms deep-size overhead is method-dependent. Hashing remains unselected. No losing-layout tuning or additional storage variants ran.

The user approved the complete spike, code and revised budget contract on 2026-09-19. This completes `g-score-store-spike` and unblocks `g-score-store-writer`. Readers, integrated production-shape/network qualification, cutover and seven-day observation remain separate work; final production ceilings still require their own measured review. The sealed JSON retains its historical proposal status and field names; this decision record is the approval authority.

Validation: all 240 new measured publications passed exact persisted parity; eight new release-seal checks passed with warnings as errors. The checks cover individual-read aggregation, reference-independent ceilings, checkpoint separation, repeated memory maxima, paired read uncertainty, the count-weighted WAL bound, and fixed-versus-growing allowances. Ruff, compilation and artifact/source/timeline verification passed. The original sealed experiment and its previous 30 validation checks remain unchanged.
