# Vacuum WAL budget applicability — approved review

The qualification handoff calls `vacuum_wal_bytes_per_ten_publications` a
production-shape ceiling without naming its checkpoint schedule. The evaluator's
`build_ceilings` derives it exclusively from **C1 warm windows**. It must not be
applied to C2's post-checkpoint windows as though those were the same workload.

This matters on the single-active-user deployment, where gaps between rebuilds
usually exceed the checkpoint timeout. It is a budget applicability gap, not a
newly demonstrated storage regression: the first Railway diagnostic run's C2:S1
B50 vacuum maximum was **4,799,283 bytes**, exactly the value already present in
the approved local qualification report. That Railway run is diagnostic only
because its initial SQLAlchemy version differed from production.

At 23,806 logical rows, the existing warm-only formula yields 524,288 bytes.
Comparing the post-checkpoint result to that warm ceiling would therefore fail
even the qualification's own C2 result. C2 explicitly checkpoints before every
publication; the warm fit does not bound that schedule. The user approved the separate post-checkpoint fit after reproducing it from
the sealed report. The historical C2-versus-warm failure remains visible in
the evaluator output; the approved schedule-specific metric resolves its scope.

## Approved correction

Keep the approved warm ceiling unchanged and label its schedule explicitly.
Add a separate post-checkpoint maintenance ceiling derived from the **existing**
qualified C2:S1/S2/S3 evidence, using the same reviewed fitting procedure:

- B50's attributed vacuum WAL only; exclude legacy and shared/unattributed WAL.
- Maximum per ten-publication vacuum window; six retained windows at each size.
- Least-squares fit over three measured sizes, plus the maximum positive
  residual so fit error does not consume headroom.
- Apply 1.5× headroom and round up to 256 KiB.
- Apply only from 23,806 through 95,224 logical rows. No extrapolation.

The [generated machine-readable fit](opening-score-vacuum-budget-scope-2026-09-24.json)
records all points, fit coefficients, residuals, limits and the source SHA-256.
At S1 the ceiling is 7,340,032 bytes; S2 is 16,252,928 bytes; S3 is 34,078,720 bytes.
The cutover evaluator regenerates this artifact using qualification
`build_ceilings` and its new
`post_checkpoint_vacuum_wal_bytes_per_ten_publications` metric, from the
unchanged sealed qualification report. The source hash is emitted with the fit.
The negative fitted intercept is a regression coefficient, not a claim of
negative physical overhead; the range restriction is mandatory.

The approved addition changes no warm ceiling or production setting. The
post-checkpoint vacuum ceiling is the production-applicable one because rebuild
gaps exceed the checkpoint timeout, mirroring publication WAL treatment. The
handoff to observation carries both schedule-specific limits and size ranges;
the warm ceiling is a lower bound. Budget approval does not authorize activation.
The one-active-user evidence remains representative of size and row mix only.
