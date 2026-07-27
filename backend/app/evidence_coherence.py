"""The single place a POSITION grain may be combined with a MOVE grain (g-v21l).

Before this module every consumer that needed "the best move AND what the played
move scored" hand-rolled the pairing, and each did it slightly differently:
``/api/analysis/lookup`` paired on ``compare_search_strength(...) is EQUAL``,
``opening_evidence._apply_cache_fallbacks`` did the same with no factual-coherence
check at all. Equal search strength is not enough — two same-profile sibling rows
can compare EQUAL while ASSERTING DIFFERENT FACTS, and that combination must be
refused rather than silently mixed.

:func:`resolve_coherent_evidence_tuple` is therefore the ONLY sanctioned pairing.
Both consumers call it; no consumer may hand-roll the rules again.

Scope guard: the coherence requirement applies whenever EITHER grain is not
effectively authoritative. An authoritative/authoritative pair keeps today's
equal-strength behavior byte-for-byte, so canonical results do not shift beyond the
grant change (tightening that pair is filed separately as g-open-canon-coherence).

Dependency tier: imports ``analysis_trust`` / ``evidence_policy`` /
``move_classification`` only — no ORM, no session, no query. Consumers build the
two grain carriers from rows they already loaded.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.analysis_trust import ResolvedEvidence
from app.evidence_policy import (
    Capability,
    Supersession,
    compare_evidence_rows,
)
from app.move_classification import validate_root_alternative_classification


@dataclass(frozen=True)
class PositionGrain:
    """Position-grain facts plus the immutable provenance of the row they came from."""

    evidence: ResolvedEvidence
    best_move_uci: str
    best_move_san: str | None
    best_line_uci: list[str] | None
    best_eval: int | None  # white-relative
    best_eval_mate: int | None  # white-relative


@dataclass(frozen=True)
class MoveGrain:
    """Move-grain facts plus provenance, and any best-* facts the SAME row embeds.

    A legacy ``resolver-complete-v2`` ``analysis_cache`` row is COMBINED: it carries
    both grains. A future native ``move-complete-v1`` row is move-only and leaves
    the ``best_*`` fields ``None``; the two cases take different coherence branches.
    """

    evidence: ResolvedEvidence
    move_uci: str
    played_eval: int | None  # white-relative
    played_eval_mate: int | None  # white-relative
    eval_delta: int | None  # side-to-move-relative, clamped >= 0
    classification: str | None
    best_move_uci: str | None = None
    best_line_uci: list[str] | None = None
    best_eval: int | None = None
    best_eval_mate: int | None = None


@dataclass(frozen=True)
class CoherentEvidence:
    """One coherent analysis tuple: a position slice and a move slice that agree."""

    best_move_uci: str
    best_move_san: str | None
    best_line_uci: list[str] | None
    best_eval: int | None
    best_eval_mate: int | None
    played_eval: int | None
    played_eval_mate: int | None
    classification: str | None
    eval_delta: int


def _mover(fen: str) -> str | None:
    parts = fen.split()
    if len(parts) < 2 or parts[1] not in ("w", "b"):
        return None
    return "white" if parts[1] == "w" else "black"


def _settings_compatible(a: ResolvedEvidence, b: ResolvedEvidence) -> bool:
    """Check 2: the two grains describe searches that may be combined.

    Either the effective profile ids AND the complete identity snapshots are
    identical — the strongest statement available, and the only one meaningful for a
    declared-dynamic profile whose registry values are all ``None`` — or the shared
    comparator reports :attr:`Supersession.EQUAL`, i.e. two measurably
    equal-strength searches under compatible semantics.
    """
    if (
        a.effective_profile_id() is not None
        and a.effective_profile_id() == b.effective_profile_id()
        and a.identity == b.identity
    ):
        return True
    return compare_evidence_rows(a, b).outcome is Supersession.EQUAL


def _recomputed_delta(
    best_eval: int | None, played_eval: int | None, mover: str
) -> int | None:
    """Side-to-move-relative, clamped loss from white-relative CP evals.

    Mirrors ``_validate_resolver_complete_v2`` exactly: white to move is
    ``best - played``, black to move ``played - best``, clamped at >= 0.
    """
    if best_eval is None or played_eval is None:
        return None
    raw = best_eval - played_eval if mover == "white" else played_eval - best_eval
    return max(raw, 0)


def _combined_facts_match(position: PositionGrain, move: MoveGrain) -> bool:
    """Every best-* fact the COMBINED move row embeds equals the position winner's.

    Applied whether or not the combined row is ALSO the resolved position source:
    when it is, the comparison is trivially true and the source identifier proves
    it; when it is not, this is what refuses two equal-strength siblings that
    disagree about which move is best or what it scores.
    """
    return (
        move.best_move_uci == position.best_move_uci
        and (move.best_line_uci or None) == (position.best_line_uci or None)
        and move.best_eval == position.best_eval
        and move.best_eval_mate == position.best_eval_mate
    )


def resolve_coherent_evidence_tuple(
    fen: str,
    position: PositionGrain,
    move: MoveGrain,
    capability: Capability,
    viewer_user_id: int | None,
) -> CoherentEvidence | None:
    """Pair a position grain with a move grain, or refuse (``None``).

    Every applicable check must pass:

    1. both grains independently satisfy their grain contract and hold
       ``capability`` for ``viewer_user_id`` (:meth:`ResolvedEvidence.holds`);
    2. their settings are compatible (:func:`_settings_compatible`);
    3. their FACTS are coherent —

       * a satisfied COMBINED row is preferred: its embedded best move, PV, and
         CP/mate facts must exactly match the capability-filtered position result
         before both slices are built from it;
       * a move-only row assembles the two slices only if every overlapping fact
         agrees and a recomputed side-to-move-relative, clamped delta equals the
         stored ``eval_delta``;
       * source/provenance ambiguity, disagreement, an incomplete combination, or a
         failed validation returns ``None``;

    4. ``eval_delta`` is finite CP data;
    5. the shared full classification rule holds for every NON-authoritative move
       row. Effectively authoritative rows retain their stored classification
       behavior.

    Check 3's overlap-agreement requirement is what makes this strictly stronger
    than an equal-search-strength check alone.
    """
    if not position.evidence.holds(capability, viewer_user_id):
        return None
    if not move.evidence.holds(capability, viewer_user_id):
        return None

    both_authoritative = (
        position.evidence.is_effectively_authoritative()
        and move.evidence.is_effectively_authoritative()
    )

    if not _settings_compatible(position.evidence, move.evidence):
        return None

    mover = _mover(fen)
    if mover is None:
        return None

    delta = move.eval_delta
    if not isinstance(delta, int) or isinstance(delta, bool):
        return None

    is_combined = move.best_move_uci is not None
    if not both_authoritative:
        # Coherence applies whenever EITHER grain is non-canonical. An
        # authoritative/authoritative pair keeps its pre-change behavior.
        if is_combined:
            if not _combined_facts_match(position, move):
                return None
        else:
            # Move-only row: nothing overlaps except the delta, which must be
            # reproducible from the paired position best and this row's played eval.
            recomputed = _recomputed_delta(position.best_eval, move.played_eval, mover)
            if recomputed is None or recomputed != delta:
                return None

    if not both_authoritative and not validate_root_alternative_classification(
        mover=mover,
        played_uci=move.move_uci,
        best_uci=move.best_move_uci if is_combined else position.best_move_uci,
        played_eval=move.played_eval,
        played_eval_mate=move.played_eval_mate,
        best_eval=move.best_eval if is_combined else position.best_eval,
        best_eval_mate=move.best_eval_mate if is_combined else position.best_eval_mate,
        eval_delta=delta,
        classification=move.classification,
    ):
        return None

    # Slices. For a combined row every best-* fact has already been proven equal to
    # the position winner's (or the pair is canonical/canonical, whose pre-change
    # behavior reads the position winner), so the position slice is taken from the
    # resolved position in both branches — one source of truth for "which move is
    # best", never the move row's own copy.
    return CoherentEvidence(
        best_move_uci=position.best_move_uci,
        best_move_san=position.best_move_san,
        best_line_uci=position.best_line_uci,
        best_eval=position.best_eval,
        best_eval_mate=position.best_eval_mate,
        played_eval=move.played_eval,
        played_eval_mate=move.played_eval_mate,
        classification=move.classification,
        eval_delta=delta,
    )
