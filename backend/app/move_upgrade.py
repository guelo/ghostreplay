"""Analysis-time move re-annotation payload (g-xox0).

A :class:`MoveUpgrade` is a stronger re-annotation of a single played move, built
ONCE server-side from a stored ``analysis_cache`` row in the SAME mover-relative
representation the frontend's ``AnalysisMove`` uses. Both delivery paths consume it:

* Part B — the ``POST /{session_id}/analysis-evidence`` endpoint returns the upgrade
  for the row it just stored, so the open MoveList patches immediately.
* Part C — ``GET /{session_id}/analysis`` overlays it per move by joining
  ``session_moves`` against ``analysis_cache`` on the exact ``(fen_before, move_uci)``.

Because both go through the single :func:`build_move_upgrade` builder, the immediate
patch and the durable overlay cannot diverge, and there is exactly ONE white->mover
perspective conversion + SAN derivation.
"""
from __future__ import annotations

import chess
from pydantic import BaseModel

from app.analysis_cache_policy import display_upgrade_eligible, project_cache_row
from app.analysis_profiles import get_profile
from app.centipawn_loss import centipawn_loss
from app.fen import active_color
from app.models import AnalysisCache
from app.move_classification import MoveClassification


class MoveUpgrade(BaseModel):
    """One stronger re-annotation of a played move.

    ``classification`` is the ONLY required field (re-annotating the played move IS
    the point); every eval/SAN field is nullable to match ``AnalysisMove`` and to
    tolerate a move-grain (``move-complete-v1``) row that carries no
    ``eval_delta``/``best_eval`` (mate-only move evidence).
    """

    classification: MoveClassification  # REQUIRED — drives the MoveList badge
    eval_cp: int | None = None  # MOVER-relative (matches AnalysisMove.eval_cp)
    eval_mate: int | None = None  # MOVER-relative
    best_move_san: str | None = None  # drives the best-move arrow
    best_move_eval_cp: int | None = None  # side-to-move-relative
    # Side-to-move-relative LOSS, clamped >= 0. Maps 1:1 onto AnalysisMove.eval_delta;
    # NEVER sign-flipped (it is not white-relative).
    eval_delta: int | None = None
    # Backend-stamped from the producing profile. Drives the source-aware display
    # precedence rule (Part B.3): a non-authoritative (browser-analysis) overlay must
    # not override a TRUSTED position; a canonical/authoritative overlay always
    # applies. The FE must NOT re-derive authority by mapping profile ids.
    authoritative: bool
    analysis_profile_id: str | None = None  # provenance
    depth: int | None = None  # provenance (search_limit_value)


def _best_move_san(fen_before: str, best_move_uci: str | None) -> str | None:
    """Derive the best move's SAN from its UCI + the position, or None."""
    if not best_move_uci:
        return None
    try:
        board = chess.Board(fen_before)
        move = chess.Move.from_uci(best_move_uci)
    except (ValueError, chess.InvalidMoveError):
        return None
    if move not in board.legal_moves:
        return None
    return board.san(move)


def _white_cp_to_mover(white_cp: int | None, *, is_white: bool) -> int | None:
    """Convert a white-relative CP score to mover/side-to-move-relative."""
    if white_cp is None:
        return None
    return white_cp if is_white else -white_cp


def _white_mate_to_mover(white_mate: int | None, *, is_white: bool) -> int | None:
    """Convert a white-relative mate count to mover-relative (0 stays 0)."""
    if white_mate is None:
        return None
    if is_white or white_mate == 0:
        return white_mate
    return -white_mate


def build_move_upgrade(row: AnalysisCache, fen_before: str) -> MoveUpgrade:
    """Build the wire :class:`MoveUpgrade` from a stored ``analysis_cache`` row.

    Performs the ONE white->mover perspective conversion (the cache stores
    ``played_eval``/``best_eval`` white-relative; the wire is mover-relative) and
    derives ``best_move_san`` from ``best_move_uci`` + ``fen_before`` via
    python-chess. ``eval_delta`` is a side-to-move-relative loss clamped >= 0 and
    passes through UNCHANGED — it is NOT white-relative and must never be sign-flipped.
    """
    is_white = active_color(fen_before) == "white"
    profile = get_profile(row.analysis_profile_id)
    return MoveUpgrade(
        classification=row.classification,
        eval_cp=_white_cp_to_mover(row.played_eval, is_white=is_white),
        eval_mate=_white_mate_to_mover(row.played_eval_mate, is_white=is_white),
        best_move_san=_best_move_san(fen_before, row.best_move_uci),
        best_move_eval_cp=_white_cp_to_mover(row.best_eval, is_white=is_white),
        eval_delta=centipawn_loss(row.eval_delta),
        authoritative=bool(profile and profile.authoritative),
        analysis_profile_id=row.analysis_profile_id,
        depth=row.search_limit_value,
    )


def _row_dict(row: AnalysisCache) -> dict:
    """Full column dict of a stored row (for the policy projector / contract check)."""
    return {col.name: getattr(row, col.name) for col in AnalysisCache.__table__.columns}


def move_upgrade_for_row(row: AnalysisCache) -> MoveUpgrade | None:
    """Project + gate + build in one seam shared by Parts B and C.

    Returns a :class:`MoveUpgrade` only when the stored row is
    :func:`~app.analysis_cache_policy.display_upgrade_eligible` (identity-verified,
    contract-satisfied, classification-carrying, dominates ``browser-game-v1``); else
    ``None``. Gating both the immediate read-back and the durable overlay through the
    same predicate keeps them from diverging.
    """
    projected = project_cache_row(_row_dict(row))
    if not display_upgrade_eligible(projected):
        return None
    return build_move_upgrade(row, row.fen_before)
