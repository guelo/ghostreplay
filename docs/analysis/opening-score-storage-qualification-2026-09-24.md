# Opening score storage — integrated qualification decision

- Run id: `r2`
- Tested revision (the CELLS): `0c690ac42e7e5f790b9d89ffab48a93d572cf0da`
- Evaluator revision (this reading of them): `43db2506e4aedb93fa25b6d3e7f5568c893dd3c0`
  The two are different artefacts with different lifetimes: a change to how the record is READ does not re-measure anything, and §5's homogeneity check would refuse a cell re-run at the evaluator's commit into this run's inputs.
- Settings digest: `a39de1bec346d79e`
- Cluster: `ghostreplay-score-storage-qual`
- Host: macOS-26.2-arm64-arm-64bit
- Revision, host and cluster checked over: `C1:S0`, `C1:S1`, `C1:S2`, `C1:S3`, `C2:S1`, `C2:S2`, `C2:S3`, `C3:S0`, `C3:S1`, `C3:S2`, `C3:S3`, `C5:S1`, `C6:S0`, `C6:S1`, `C6:S2`, `C6:S3`, `C1:SF`, `C3:SF`, `C5:SF`, `C6:SF`, `C4:S1:A_old`, `C4:S1:A_new`, `C4:SF:A_old`, `C4:SF:A_new`, `C7:legacy`, `C7:current-b50-v1`
- Exempt from the revision check (predecessor commit by construction): `C4:S1:A_old`, `C4:SF:A_old`
- Aggregate verdict: **pass**

## What this verdict authorises

Passing authorises READINESS for the cutover workflow. It is not a claim that production is deployed or observed.

## Deferred absolute ceilings

These are `local_host_only` and owed to `g-score-store-cutover` before any B50 write is activated for a production pair. They are that gate's EXPECTATION and a local regression baseline, never production limits:

- `composite_d_p95_ms` = 138 at 99392 logical rows
- `composite_t_format_stage_p95_ms` = 28 at 99392 logical rows
- `integrated_worker_rss_bytes` = 565731541 at 99392 logical rows
- `publication_allocation_peak_bytes` = 239735198 at 99392 logical rows
- `publication_p95_ms` = 2507 at 99392 logical rows

## Per-gate verdicts

- `C1:S0:combined_wal`: pass
- `C1:S0:composite_d_p95`: pass
- `C1:S0:composite_t_format_stage_p95`: pass
- `C1:S0:publication_p95`: pass
- `C1:S0:vacuumed_footprint`: pass
- `C1:S1:combined_wal`: pass
- `C1:S1:composite_d_p95`: pass
- `C1:S1:composite_t_format_stage_p95`: pass
- `C1:S1:publication_p95`: pass
- `C1:S1:vacuumed_footprint`: pass
- `C1:S2:combined_wal`: pass
- `C1:S2:composite_d_p95`: pass
- `C1:S2:composite_t_format_stage_p95`: pass
- `C1:S2:publication_p95`: pass
- `C1:S2:vacuumed_footprint`: pass
- `C1:S3:combined_wal`: pass
- `C1:S3:composite_d_p95`: pass
- `C1:S3:composite_t_format_stage_p95`: pass
- `C1:S3:publication_p95`: pass
- `C1:S3:vacuumed_footprint`: pass
- `C2:S1:combined_wal`: pass
- `C2:S1:publication_p95`: pass
- `C2:S1:vacuumed_footprint`: pass
- `C2:S2:combined_wal`: pass
- `C2:S2:publication_p95`: pass
- `C2:S2:vacuumed_footprint`: pass
- `C2:S3:combined_wal`: pass
- `C2:S3:publication_p95`: pass
- `C2:S3:vacuumed_footprint`: pass
- `C4:S1:publication_p95`: pass
- `C4:SF:publication_p95`: pass
- `C5:S1:fixed_live_counts:A`: pass
- `C5:S1:fixed_live_counts:B50`: pass
- `C5:S1:orphans`: pass
- `C5:S1:plateau_growth:B50`: pass
- `C5:S1:reclamation:A`: pass
- `C5:S1:reclamation:B50`: pass
- `C7:current-b50-v1:drill`: pass
- `C7:current-b50-v1:normal`: pass
- `C7:legacy:drill`: pass
- `C7:legacy:normal`: pass
- `ceiling:composite_d_p95_ms`: deferred
- `ceiling:composite_t_format_stage_p95_ms`: deferred
- `ceiling:integrated_worker_rss_bytes`: deferred
- `ceiling:post_checkpoint_publication_wal_bytes`: pass
- `ceiling:publication_allocation_peak_bytes`: deferred
- `ceiling:publication_p95_ms`: deferred
- `ceiling:vacuum_wal_bytes_per_ten_publications`: pass
- `ceiling:vacuumed_footprint_bytes`: pass
- `ceiling:warm_publication_wal_bytes`: pass

## Recorded settings deviation

§9.5 permits ONE recorded settings deviation — max_wal_size raised for C1 only, if and only if observed discards leave fewer than two complete paired blocks. C1's warm ceiling is already a LOWER BOUND, so the deviation cannot loosen the production-applicable ceiling, which is C2's and keeps the census value. Pool identity includes the settings digest, so nothing is pooled across the deviation either way.

- `C1:S0`: max_wal_size 128 -> 8192
- `C1:S1`: max_wal_size 128 -> 8192
- `C1:S2`: max_wal_size 128 -> 8192
- `C1:S3`: max_wal_size 128 -> 8192

## Fixture tie-back cells (SF)

regression tie-back only (§4.1/§4.2): never a fit point, never a fitted coverage input, never an acceptance gate, and measured on the other cluster

- `C1:SF` (paired): per-gate, see the JSON
- `C3:SF` (memory): rss 153337856 B, allocation peak 4603497 B
- `C5:SF` (plateau): per-gate, see the JSON
- `C6:SF` (network): network terms recorded, provenance checked

## Plateau, reclamation and orphans (C5)

The last-five growth GATE is the selected design's (§4.8 as scoped rev 16): the approved budget places it under `selected_design_ceilings`, and layout A enters that budget only through the A-relative ratios. A's growth is carried below as a characterisation and gates nothing. Fixed live counts, reclamation and orphans are gated for BOTH layouts — those are the leak proofs.

### C5 at S1

- `fixed_live_counts:A`: pass (True)
- `fixed_live_counts:B50`: pass (True)
- `orphans`: pass
- `plateau_growth:B50`: pass (0.0)
- `reclamation:A`: pass
- `reclamation:B50`: pass
- `plateau_growth:A`: 0.07139 — NOT GATED (reference limit 0.05, statistic: (last - first) / first over the last five windows (SUPERSEDED by rev 16's max-minus-min; equal on a monotone series, smaller on one that oscillates)); window totals 88702976, 139075584, 147759104, 148717568, 148996096, 149004288

## Legacy-retirement control (C4, A_new vs A_old)

Gate: A_new publication p95 / A_old publication p95 <= 1.1, on cluster `ghostreplay-score-storage-qual`.

- `S1`: pass (0.573x)

Retirement duration and publication-lock hold are ONE-SIDED: A_old has no atomic retirement stage to compare against, so those figures characterise A_new and gate nothing.

## Legacy-retirement control at SF (C4, §4.7's other half)

Gate: A_new publication p95 / A_old publication p95 <= 1.1, on cluster `ghostreplay-score-storage-spike`.

- `SF`: pass (0.855x)

Retirement duration and publication-lock hold are ONE-SIDED: A_old has no atomic retirement stage to compare against, so those figures characterise A_new and gate nothing.

## Delta lane (C7)

warm whole-graph-contention p95 < 3000.0 ms.

- `legacy:drill`: pass (1851.49 ms)
- `legacy:normal`: pass (1851.687 ms)
- `current-b50-v1:drill`: pass (1792.858 ms)
- `current-b50-v1:normal`: pass (1873.815 ms)

Process-cold visibility is recorded in the JSON and never mixed into the warm p95 (§4.9).

## Upstream seal drift

Six of the twelve source digests recorded in the approved budget report have drifted, so `summarize_opening_score_budgets` and `remeasure_opening_score_budgets` would now raise `measured source changed` against it. Comparability for the SF tie-back therefore rests on the regenerated timeline's deterministic fields, and the tie-back is a regression signal only — it never was an acceptance gate.

