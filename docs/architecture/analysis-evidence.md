# Analysis evidence contract

This document records the cross-cutting rules that let Ghost Replay reuse chess
analysis without presenting a weak, malformed, or wrongly scoped result as
canonical. It is an architecture contract, not a schema or HTTP reference:
the current model, routes, policies, and tests named below are authoritative
for fields, algorithms, and payloads.

## Centipawn-loss representations

Raw analysis evidence and user-facing centipawn loss (CPL) are different
quantities. Raw deltas stay uncapped where they are needed for audit and
contract validation. The display/decision CPL is derived at read, projection,
or decision time by flooring at zero and capping at the product ceiling.
That derived value drives player-facing loss displays and severity decisions;
raw evidence is never overwritten merely to make it displayable.

New session-move writes and review-event writes have their own normalization
boundaries, while legacy session rows may predate them. Consumers must still
normalize at their read boundary rather than assuming the stored value is
already safe. The backend's Decimal half-up helper and the browser's
`Math.round` are separate implementations; they agree for the nonnegative CPL
domain only.

The executable authorities are
[`backend/app/centipawn_loss.py`](../../backend/app/centipawn_loss.py) for
backend normalization and rounding, and
[`src/utils/gameStats.ts`](../../src/utils/gameStats.ts) for browser aggregate
rounding. [`src/workers/analysisUtils.ts`](../../src/workers/analysisUtils.ts)
owns the browser loss normalizer and ceiling; the cross-runtime cap is pinned
by [`backend/tests/fixtures/cpl_cap_vectors.json`](../../backend/tests/fixtures/cpl_cap_vectors.json).
The models, route projections, and tests define the particular columns and
uses.

## Evaluation perspective and mate interpretation

Analysis normalizes an engine score before it compares the best and played
continuations, so capture and review decisions have one player-relative
meaning. Readers may project the same fact to a white-relative representation
where their storage or display contract requires it; they must not silently
mix those perspectives.

Mate is retained as an explicit mate count as well as receiving a centipawn
conversion when a common comparison surface needs one. In particular, a mate
count of zero means the side to move is checkmated. The conversion supports
threshold and loss decisions; it does not erase the mate meaning needed for
faithful display or evidence projection.

The browser authority is
[`src/workers/analysisUtils.ts`](../../src/workers/analysisUtils.ts), with
cross-boundary projections in
[`src/services/analysisEvidence.ts`](../../src/services/analysisEvidence.ts).
The corresponding worker and evidence tests define the edge cases.

## Two evidence grains

One analysis run can establish two different facts:

- **Position grain:** the best move, principal variation, and evaluation of a
  normalized chess position. `position_analysis` keeps at most one trusted
  winner for that normalized FEN.
- **Move grain:** the evaluation and classification of one played move from a
  full position. `analysis_cache` is keyed by the full-position/move pair;
  its normalized position key supports a transposition-aware fallback in the
  opening-tree reader.

The grains must not be substituted for each other. A consumer may publish
position facts only from trusted position evidence, and move facts only from
the corresponding trusted move evidence. During the migration, a complete
legacy cache record can project into both grains through explicit, fail-closed
adapters; that compatibility path does not change the ownership model.

The storage models are in
[`backend/app/models.py`](../../backend/app/models.py), and the registered
grain contracts and migration projections are in
[`backend/app/evidence_contracts.py`](../../backend/app/evidence_contracts.py).
The opening-tree fallback is implemented in
[`backend/app/tree_eval.py`](../../backend/app/tree_eval.py).

## Trust, capability, and user scope

Trust is deliberately more than a source label. A row must identify a known
profile, match that profile's identity requirements, and satisfy the semantic
shape contract it claims. The policy then grants individual capabilities—for
example, reuse or display overlay—rather than treating any cached row as
interchangeable. Some non-authoritative evidence also requires an association
with the viewing user; canonical evidence is shared only where its capability
permits it.

The policy fails closed for unknown profiles, identity mismatches, malformed
contracts, missing scope, and absent capabilities. A client-provided provenance
claim is validation input, not a way to gain authority.

[`backend/app/evidence_policy.py`](../../backend/app/evidence_policy.py) owns
identity, capability, and scope rules;
[`backend/app/analysis_trust.py`](../../backend/app/analysis_trust.py) applies
the grain-specific read gates.

## Replacement and publication

Move-cache writers use the shared replacement policy. Replacement compares
verified profile authority, explicit policy relationships, compatible contracts,
and where applicable declared comparable strength; it does not infer quality
from a bare source label or a raw depth value. A narrower post-split write can
move the position grain to its dedicated store, but it must never discard move
evidence.

Position storage accepts only authoritative, `position-complete-v1` winners;
this fail-closed gate runs before any strength or dominance comparison, even
for a new position key.

Today the production path that populates `position_analysis` and appends
`PositionAnalysisConflict` records is the Phase-2/backfill writer in
[`backend/app/position_analysis_backfill.py`](../../backend/app/position_analysis_backfill.py).
The native position writer in
[`backend/app/position_analysis_repo.py`](../../backend/app/position_analysis_repo.py)
encodes the intended post-split policy but currently has test callers only.
Likewise, the canonical precompute producer still stamps the legacy combined
`resolver-complete-v2` contract; it has not cut over to the native move/position
writers. The distinct grains therefore describe the target ownership model and
the explicit migration projections, not a claim that the native write path is
live.

Readers resolve a coherent tuple at the capability they need. They must not
combine a trusted position best move with an unrelated or untrusted played-move
claim and call the result canonical. There is one deliberate, tracked exception:
when both grains are effectively authoritative, the resolver currently skips
factual-coherence and classification revalidation, so disagreeing canonical
siblings can still pair. `g-open-canon-coherence` owns that remaining gap.
Publication otherwise degrades to the available trusted grain, the player’s own
permitted evidence, or no reusable analysis—not an invented cross-grain result.

The move and position policies live in
[`backend/app/analysis_cache_policy.py`](../../backend/app/analysis_cache_policy.py)
and
[`backend/app/position_analysis_policy.py`](../../backend/app/position_analysis_policy.py).
The move-cache DB writer is
[`backend/app/analysis_cache_repo.py`](../../backend/app/analysis_cache_repo.py);
coherent read pairing is in
[`backend/app/evidence_coherence.py`](../../backend/app/evidence_coherence.py).

## Freshness and safe degradation

Opening-score snapshots are published artifacts, scoped by user and side. A
snapshot is usable only while its stored evidence/cursor and shared-evidence
freshness proofs still match current inputs. Missing, legacy, partial, or stale
proof material is treated as stale rather than as a fresh score. A warm result
may remain displayable while a background recomputation verifies it, but a
consumer must not claim a fresh canonical result without that proof.

Evidence ingestion is similarly resilient at row granularity: malformed or
stale browser evidence is refused for cache promotion while the ordinary game
record can still persist. When trusted reuse is unavailable, callers use their
normal analysis/fallback path or show an unavailable result; they do not
launder weak evidence into a trusted response.

The current freshness and opening-evidence implementation is in
[`backend/app/opening_cache.py`](../../backend/app/opening_cache.py),
[`backend/app/opening_evidence.py`](../../backend/app/opening_evidence.py), and
the opening/session routes. Their tests are the authority for edge cases.

## Exact authorities

- Registered profiles and contracts:
  [`backend/app/analysis_profiles.py`](../../backend/app/analysis_profiles.py),
  [`backend/app/evidence_contracts.py`](../../backend/app/evidence_contracts.py).
- Persisted shapes and migrations:
  [`backend/app/models.py`](../../backend/app/models.py) and
  [`backend/alembic/versions/`](../../backend/alembic/versions/).
- API publication and ingestion:
  [`backend/app/api/analysis.py`](../../backend/app/api/analysis.py) and
  [`backend/app/api/session.py`](../../backend/app/api/session.py), with the
  generated FastAPI OpenAPI document for the exact wire contract.
- Current position-grain population and conflict audit:
  [`backend/app/position_analysis_backfill.py`](../../backend/app/position_analysis_backfill.py).
- Behavioral proof: the evidence, cache, position, session, and opening tests
  under [`backend/`](../../backend/).
