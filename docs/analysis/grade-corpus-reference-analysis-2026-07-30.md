# Grade-corpus reference analysis — 2026-07-30

This report analyzes the complete Node corpus captures
`grade-corpus-current-2026-07-30.jsonl` and
`grade-corpus-references-2026-07-30.json`. Both were captured from the clean,
detached revision `be1ca71e1af46277b7d2bdd5cb4046ba26d4b01d` with Stockfish
18 Lite single-thread, Hash 128, and the checked-in 224-position corpus.

The reference artifact is valid negative evidence: it completed every row and
passes its schema, provenance, corpus-digest, depth, row-accounting, and summary
recomputation checks. Its predeclared unadjudicable-rate gate failed.

| Artifact | SHA-256 |
|---|---|
| current depth-17 JSONL | `7feef688082c4d0a5617122bfd8b648954f3a3688494c1640c4c88446d1290e5` |
| depth-26/27 references JSON | `9c09273be0286ad54d6fa6f5c2701eeba6dff45a1ac49f6c3e36053e4c433360` |

## Outcome

| Population | Rows | Share of corpus |
|---|---:|---:|
| Adjudicated | 181 | 80.80% |
| Best-move disagreement | 21 | 9.38% |
| Classification disagreement | 18 | 8.04% |
| Engine-timeout decline | 4 | 1.79% |
| **Total unadjudicable** | **43** | **19.20%** |

The gate requires at most 10%; the observed 19.20% fails it. Removing all four
timeout-affected rows still leaves 39 semantic disagreements, or 17.41% of the
corpus. Retrying the timeouts cannot change this run into a pass.

The disagreement is not confined to one corpus phase:

| Phase | Total | Best-move | Classification | Declined | All unadjudicable |
|---|---:|---:|---:|---:|---:|
| Opening | 135 | 13 | 7 | 0 | 20 (14.81%) |
| Middlegame | 51 | 2 | 4 | 4 | 10 (19.61%) |
| Endgame | 38 | 6 | 7 | 0 | 13 (34.21%) |

Endgames are the least stable cohort, but opening-only evidence also exceeds the
10% ceiling. This is therefore neither a timeout-only result nor solely an
endgame-fixture artifact.

## Classification disagreements

Eleven of the 18 classification disagreements use CP scores in both references.
All eleven cross one adjacent win-chance classification boundary. In ten, at
least one reference is within one percentage point of the crossed boundary; in
four, both are. These are genuine fixed-depth score changes amplified by a hard
classification boundary, not inconsistent classification code.

| Position | Primary | Bias | Primary loss | Bias loss |
|---|---|---|---:|---:|
| `threshold-recording-50cp` | excellent | good | 1.29% | 3.31% |
| `eco-055-c18` | excellent | good | 1.64% | 2.37% |
| `eco-059-c30` | good | inaccuracy | 9.37% | 12.31% |
| `eco-077-c85` | good | inaccuracy | 7.91% | 10.12% |
| `eco-081-d03` | good | inaccuracy | 8.61% | 10.06% |
| `eco-110-e61` | good | excellent | 2.73% | 1.64% |
| `eco-114-e90` | good | inaccuracy | 7.79% | 10.89% |
| `kasparov-topalov-1999-ply-006` | excellent | good | 1.45% | 2.00% |
| `kasparov-topalov-1999-ply-033` | good | excellent | 2.20% | 1.47% |
| `kasparov-topalov-1999-ply-039` | excellent | good | 0.37% | 2.74% |
| `kasparov-topalov-1999-ply-048` | inaccuracy | mistake | 16.17% | 21.25% |

The other seven are constructed endgames where one observation sees a mate
surface and the other sees CP, or where their mate horizons differ:
`constructed-endgame-004`, `-009`, `-011`, `-015`, `-019`, `-022`, and `-023`.
They account for every endgame classification disagreement and show that the
endgame concentration is a horizon/representation effect, not threshold jitter.

The explicit `threshold-recording-50cp` row is unadjudicable, independently
failing the requirement that every named 50cp/drill/blunder-boundary crossing be
adjudicated.

## Best-move disagreements

Of the 21 best-move disagreements:

- One reference names the played move in 11 rows; the other names a different
  move.
- Both references name moves other than the played move in 10 rows.
- Eight rows retain the same classification despite disagreeing on best-move
  identity.
- Five involve the bias reference's contradiction-resolution search; 16 do not.

The 11 played-versus-other rows are `eco-007-a12`, `eco-015-a41`,
`eco-044-b52`, `eco-049-b91`, `eco-086-d24`,
`kasparov-topalov-1999-ply-008`, `-022`, `-023`, and
`constructed-endgame-001`, `-007`, `-017`.

The 10 other-versus-other rows are `eco-034-b12`, `eco-039-b26`,
`eco-046-b67`, `eco-083-d11`, `eco-100-d99`,
`kasparov-topalov-1999-ply-002`, `-011`, and
`constructed-endgame-010`, `-012`, `-014`.

This split matters: more than half are direct uncertainty over whether the
played move is best, while the rest preserve the played move's non-best status
but cannot establish one canonical best-move identity.

## Timeout declines

All timeout declines are consecutive sharp middlegame positions from
Kasparov–Topalov 1999:

- plies 050 and 051 completed primary but timed out in bias;
- plies 052 and 053 timed out in both observations.

That is four affected rows and six one-hour search timeouts. The per-observation
error handling worked as intended: completed observations and phases remain in
the artifact, and later rows completed normally.

## Named regressions and current baseline

- `regression-1e4` is best/0cp in both references.
- `regression-g-kgiq-nb6` takes the `played-promoted` branch: both references
  name `c4b6` as best with delta 0. The depth-17 current protocol instead names
  `a5a6`; its post-move score puts played `c4b6` 77cp higher, which the harness
  records as an ordering inversion rather than hiding with a clamp.

The current pass completed 224/224 rows and exposed 11 ordering inversions
(4.91%). Of the 181 adjudicated reference rows, current has eight such errors.
On the remaining 173 rows, current matches the reference best move in 141
(81.50%), the classification in 137 (79.19%), and both in 120 (69.36%). These
figures are a descriptive baseline; candidate comparisons must keep the same
denominator and error policy.

## Interpretation

The capture should not be rerun unchanged. The failed result is reproducible and
its dominant cause is semantic disagreement between the two predeclared
references. Any future tie-break or deeper reference protocol would be a new,
versioned experiment; it must not overwrite or retroactively reinterpret this
one.
