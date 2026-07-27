"""Backend port of the frontend win-chance move classifier.

This is an EXACT port of ``classifyMoveAdvanced`` + ``checkMateEvents`` +
``calculateWinChance`` from ``src/workers/analysisUtils.ts``. The two
implementations are pinned together by a shared golden-vector fixture
(``backend/tests/fixtures/classification_vectors.json``) consumed by both a
Python test and a TS test, so they cannot silently drift.

Both post-move scores fed to the classifier come from the OPPONENT-to-move
position (the position reached after the move), sharing a single ``score_pov``.
``mover`` is the color that played the move being classified.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Win-chance logistic model constants (Lichess), mirrored from analysisUtils.ts.
WIN_CHANCE_MULTIPLIER = -0.00368208
CP_CEILING = 1000

MoveClassification = str  # one of best/excellent/good/inaccuracy/mistake/blunder

VALID_CLASSIFICATIONS = frozenset(
    {"best", "excellent", "good", "inaccuracy", "mistake", "blunder"}
)


@dataclass(frozen=True)
class EngineScore:
    """Engine-reported score from the resulting position's point of view."""

    type: str  # 'cp' | 'mate'
    value: int

    @staticmethod
    def from_dict(data: dict) -> "EngineScore":
        return EngineScore(type=data["type"], value=int(data["value"]))


def calculate_win_chance(score: EngineScore, pov: str) -> float:
    """Convert an engine score to a win chance in [-1.0, 1.0], white-relative.

    Port of ``calculateWinChance``. ``pov`` is the perspective the score was
    reported from ('white' | 'black').
    """
    white_value = score.value if pov == "white" else -score.value

    if score.type == "mate":
        if score.value == 0:
            cp = -CP_CEILING if pov == "white" else CP_CEILING
        else:
            cp = CP_CEILING if white_value > 0 else -CP_CEILING
    else:
        cp = max(-CP_CEILING, min(CP_CEILING, white_value))

    return 2 / (1 + math.exp(WIN_CHANCE_MULTIPLIER * cp)) - 1


def check_mate_events(
    prev_score: EngineScore,
    next_score: EngineScore,
    score_pov: str,
    mover: str,
) -> MoveClassification | None:
    """Port of ``checkMateEvents``. Detects MateCreated / MateLost transitions."""
    flip_prev = 1 if mover == score_pov else -1
    m_pv = prev_score.value * flip_prev
    m_nv = next_score.value * flip_prev

    # MateCreated: cp -> losing mate (blundered into being mated).
    if prev_score.type == "cp" and next_score.type == "mate" and m_nv < 0:
        if m_pv < -999:
            return "inaccuracy"
        if m_pv < -700:
            return "mistake"
        return "blunder"

    # MateLost: winning mate -> cp or losing mate.
    if (
        prev_score.type == "mate"
        and m_pv > 0
        and (next_score.type == "cp" or (next_score.type == "mate" and m_nv < 0))
    ):
        res_cp = m_nv if next_score.type == "cp" else -1000
        if res_cp > 999:
            return "inaccuracy"
        if res_cp > 700:
            return "mistake"
        return "blunder"

    return None


def _classify_win_chance_drop(drop: float) -> MoveClassification:
    """Map a mover-relative win-chance loss to a classification label.

    Factored out of ``classify_move_advanced`` so the post-move classifier and the
    root-alternative classifier (:func:`classify_root_alternative`) share ONE
    threshold ladder and cannot drift. Mirrors the same ladder in
    ``analysisUtils.ts``.
    """
    if drop >= 0.30:
        return "blunder"
    if drop >= 0.20:
        return "mistake"
    if drop >= 0.10:
        return "inaccuracy"
    if drop >= 0.02:
        return "good"
    return "excellent"


def classify_move_advanced(
    prev_score: EngineScore,
    next_score: EngineScore,
    score_pov: str,
    mover: str,
    is_best: bool,
) -> MoveClassification:
    """Port of ``classifyMoveAdvanced``.

    ``prev_score`` is the post-best-move score, ``next_score`` the post-played
    score, both from the opponent-to-move position sharing ``score_pov``.
    """
    if is_best:
        return "best"

    mate_result = check_mate_events(prev_score, next_score, score_pov, mover)
    if mate_result:
        return mate_result

    prev_wc = calculate_win_chance(prev_score, score_pov)
    next_wc = calculate_win_chance(next_score, score_pov)

    drop = -(next_wc - prev_wc) if mover == "white" else (next_wc - prev_wc)

    return _classify_win_chance_drop(drop)


def white_relative_to_mover_score(
    cp: int | None, mate: int | None, mover: str
) -> EngineScore | None:
    """Convert a WHITE-relative stored eval to a ROOT mover-relative EngineScore.

    Evidence rows store white-relative evals; the root-alternative classifier needs
    mover-relative (root side-to-move) scores. A present mate count uses the raw
    ``mate`` (a mate->CP conversion is only for the delta arithmetic), else the
    finite CP. ``None`` when neither is usable.
    """
    if mate is not None:
        return EngineScore("mate", mate if mover == "white" else -mate)
    if cp is None:
        return None
    return EngineScore("cp", cp if mover == "white" else -cp)


def validate_root_alternative_classification(
    *,
    mover: str,
    played_uci: str | None,
    best_uci: str | None,
    played_eval: int | None,
    played_eval_mate: int | None,
    best_eval: int | None,
    best_eval_mate: int | None,
    eval_delta: int | None,
    classification: str | None,
) -> bool:
    """The FULL non-authoritative classification rule — ingress AND read-time reuse.

    :func:`classify_root_alternative` alone is NOT validation: it short-circuits to
    ``best`` whenever ``is_best``, so equal UCIs would bless unequal CP facts,
    unequal mate facts, or a nonzero loss. This helper closes that short circuit and
    is the SINGLE implementation both callers use — the analysis-evidence ingress
    (``api.session._build_evidence_cache_row``) and the read-time coherent-tuple
    resolver — so a row that could not be submitted can never be read back either.

    Inputs are the raw WHITE-relative stored facts plus the stored delta and label.
    The rule:

    * for MATCHING UCIs: exact equality of the played/best CP values, exact equality
      of the played/best mate values, ``eval_delta == 0`` (a real zero, NOT a
      null-coalesced one), and ``classification == "best"``;
    * for NON-matching UCIs: a stored ``best`` is rejected outright;
    * in ALL cases the label must equal what :func:`classify_root_alternative`
      rederives from the mover-relative scores.

    ``mover`` is the root side to move ('white' | 'black').
    """
    if classification not in VALID_CLASSIFICATIONS:
        return False
    if played_uci is None or best_uci is None:
        return False

    is_best = played_uci == best_uci
    if is_best:
        if played_eval != best_eval:
            return False
        if played_eval_mate != best_eval_mate:
            return False
        # A real zero, not `(eval_delta or 0) == 0`: a null delta on an exact-best
        # row is missing evidence, not a proven zero loss.
        if eval_delta != 0:
            return False
        if classification != "best":
            return False
    elif classification == "best":
        return False

    best_score = white_relative_to_mover_score(best_eval, best_eval_mate, mover)
    played_score = white_relative_to_mover_score(played_eval, played_eval_mate, mover)
    if best_score is None or played_score is None:
        return False
    return (
        classify_root_alternative(best_score, played_score, mover, is_best)
        == classification
    )


def classify_root_alternative(
    best_score: EngineScore,
    played_score: EngineScore,
    mover: str,
    is_best: bool,
) -> MoveClassification:
    """Classify a played root move vs. the best root move from ONE completed search.

    Root-alternative contract (g-reuse-d21-search §5.1): ``best_score`` and
    ``played_score`` are both ROOT side-to-move (``mover``)-relative EngineScores
    reported by the SAME completed request — NOT the post-move opponent-to-move
    scores :func:`classify_move_advanced` takes. Routing root scores through that
    classifier's argument order would misclassify, so this uses a truthful root
    contract while SHARING :func:`calculate_win_chance`, :func:`check_mate_events`,
    and :func:`_classify_win_chance_drop`.

    ``is_best`` (played UCI == best UCI) short-circuits to ``best``. The frontend
    ``classifyRootAlternative`` mirrors this exactly, pinned by the shared golden
    fixture ``backend/tests/fixtures/root_classification_vectors.json``.
    """
    if is_best:
        return "best"

    # Both scores are already mover-relative (root position, ``mover`` to move),
    # so mate transitions are evaluated with score_pov == mover (no opponent-frame
    # flip): best plays the "prev/better" role, played the "next/worse" role.
    mate_result = check_mate_events(best_score, played_score, mover, mover)
    if mate_result:
        return mate_result

    best_wc = calculate_win_chance(best_score, mover)
    played_wc = calculate_win_chance(played_score, mover)

    # Mover-relative loss from best to played (best is >= played for the mover).
    drop = (best_wc - played_wc) if mover == "white" else (played_wc - best_wc)

    return _classify_win_chance_drop(drop)
