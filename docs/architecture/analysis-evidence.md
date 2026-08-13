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

## Browser-submitted evidence and scoped reuse

Browser evidence is authenticated but not attested: a user can submit a coherent-looking
claim without proving that the declared search ran. The policy therefore distinguishes a
claim that may affect its submitter's own experience from canonical evidence that may
affect every player. A non-authoritative row can never become canonical simply because it
looks complete or strong.

An analysis-cache submission association means only that one user independently submitted
a tuple consistent with that row. It is neither ownership nor a write right, and it is not
returned or exposed as product telemetry. For non-authoritative rows, every capability
except the purely presentational display overlay is owner-scoped by default. The policy
denies tree evaluation to non-canonical evidence because that shared consumer cannot safely
express a per-viewer result; it denies drill grading because fabricated evidence must never
grade a drill. A missing viewer admits only effectively authoritative evidence for an
owner-scoped capability; display overlay remains the deliberate exception.

Claims are made inside the cache writer's transaction, after the replacement decision.
A replacement clears stale associations before any eligible incoming claim is recorded;
a keep or merge can associate a submitter only when the stored facts agree with, and are
covered by, the facts they submitted. This prevents an old or partial agreement from granting a user
fields it did not provide, while allowing independent users to establish their own
eligibility for the same shared row.

## Capability-specific reads and coherent publication

Readers name both their consumer capability and viewer. Generic lookup, interactive/game
publication, opening evidence, and drill grading are distinct grants; a read grant is not
permission to publish a durable game-analysis result. Canonical evidence remains globally
available only through its authoritative capabilities.

Position and move evidence may be combined for reusable publication only by the
coherent-evidence resolver. For a pair containing non-authoritative evidence, it requires both
grains to satisfy the same requested capability for the viewer, compatible settings and facts, a
finite loss value, and validation of the move classification. Both effectively authoritative
grains retain their legacy pairing behavior; its known factual-coherence exception is documented
below.
A consumer that has only one usable grain degrades to that grain or to no reusable result;
it must not assemble a seemingly canonical answer from unrelated rows.

Opening-score freshness includes this eligibility. The shared evidence digest represents
the full deterministic association set and every move attribute that can affect coherence,
rather than one requesting user's membership. Therefore an association or trust-relevant
change invalidates a snapshot whose opening evidence selection could have changed.

## Replacement and publication

Move-cache writers use the shared replacement policy. Replacement compares
verified profile authority, explicit policy relationships, compatible contracts,
and where applicable declared comparable strength; it does not infer quality
from a bare source label or a raw depth value. A narrower post-split write can
move the position grain to its dedicated store, but it must never discard move
evidence.

The visible analysis board's fixed depth-21 MultiPV producer is an explicit tier
successor to the fixed legacy in-game browser producer. The current in-game
producer is declared-dynamic and has no categorical profile edge: it can represent
server-accepted searches that are depth 21+, use another network, or otherwise do
not match the visible worker. For that producer, the session-scoped evidence
endpoint supplies the saved move's validated provenance as a compare-and-replace
witness. Under the cache-row lock, promotion is allowed only when the stored row
matches that witness exactly, shares the visible worker's engine and network, and
uses the shipped in-game search settings (including Hash 128) at a depth strictly
below 21. This lets a completed visible search re-annotate the played move—including
promotion to Best—without globally ordering every dynamic in-game row below d21.
The visible producer remains non-authoritative: canonical evidence still wins and
cannot be overwritten by the browser tier.

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
- Operational cache repair:
  [`backend/scripts/REPAIR_ANALYSIS_CACHE.md`](../../backend/scripts/REPAIR_ANALYSIS_CACHE.md).
- Behavioral proof: the evidence, cache, position, session, and opening tests
  under [`backend/`](../../backend/).
