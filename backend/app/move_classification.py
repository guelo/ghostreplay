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

    if drop >= 0.30:
        return "blunder"
    if drop >= 0.20:
        return "mistake"
    if drop >= 0.10:
        return "inaccuracy"
    if drop >= 0.02:
        return "good"
    return "excellent"
