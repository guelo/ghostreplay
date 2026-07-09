"""Unit tests for the analysis-time move re-annotation building blocks (g-xox0).

Covers the pure backend seam shared by Parts B and C:
  * :func:`app.analysis_cache_policy.display_upgrade_eligible` — which stored rows
    may re-annotate a played move's MoveList label.
  * :func:`app.move_upgrade.build_move_upgrade` — the ONE white->mover perspective
    conversion + best-move SAN derivation.
  * :func:`app.move_upgrade.move_upgrade_for_row` — project + gate + build.
"""

from app.analysis_cache_policy import display_upgrade_eligible, project_cache_row
from app.analysis_profiles import (
    BROWSER_ANALYSIS_PROFILE_ID,
    BROWSER_PROFILE_ID,
    CANONICAL_PROFILE_ID,
    JEFFML_PROFILE_ID,
    stamp_profile_full,
)
from app.evidence_contracts import MOVE_COMPLETE, POSITION_COMPLETE, RESOLVER_COMPLETE_V2
from app.models import AnalysisCache
from app.move_upgrade import build_move_upgrade, move_upgrade_for_row

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"  # white to move
FEN_BLACK = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"  # black to move


# --------------------------------------------------------------------------- #
# fixtures / builders
# --------------------------------------------------------------------------- #
def _v2_data(profile_id, *, fen=START, **overrides):
    """A resolver-complete-v2 cache-row dict stamped with ``profile_id`` identity."""
    data = {
        "fen_before": fen,
        "move_uci": "e2e4",
        "move_san": "e4",
        "best_move_uci": "e2e4",
        "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5",
        "played_eval": 30,
        "played_eval_mate": None,
        "best_eval": 30,
        "best_eval_mate": None,
        "eval_delta": 0,
        "classification": "best",
        "source": "analysis",
        "analysis_profile_id": profile_id,
        "evidence_contract_id": RESOLVER_COMPLETE_V2,
        **stamp_profile_full(profile_id),
    }
    data.update(overrides)
    return data


def _row(profile_id, **overrides):
    return AnalysisCache(**_v2_data(profile_id, **overrides))


# --------------------------------------------------------------------------- #
# display_upgrade_eligible
# --------------------------------------------------------------------------- #
def test_eligible_browser_analysis():
    row = project_cache_row(_v2_data(BROWSER_ANALYSIS_PROFILE_ID))
    assert display_upgrade_eligible(row) is True


def test_eligible_canonical():
    row = project_cache_row(_v2_data(CANONICAL_PROFILE_ID))
    assert display_upgrade_eligible(row) is True


def test_rejects_browser_game_self():
    # browser-game-v1 does not dominate itself -> never overlay-eligible.
    row = project_cache_row(
        _v2_data(
            BROWSER_PROFILE_ID,
            evidence_contract_id="resolver-complete-v1",
            best_line_uci="e2e4 e7e5",
        )
    )
    assert display_upgrade_eligible(row) is False


def test_rejects_jeffml():
    row = project_cache_row(_v2_data(JEFFML_PROFILE_ID))
    assert display_upgrade_eligible(row) is False


def test_rejects_legacy_unidentified():
    # No profile id at all -> not identity-verified.
    data = _v2_data(BROWSER_ANALYSIS_PROFILE_ID)
    data["analysis_profile_id"] = None
    assert display_upgrade_eligible(project_cache_row(data)) is False


def test_rejects_identity_mismatch():
    # Claims browser-analysis but engine_build does not match the registry.
    data = _v2_data(BROWSER_ANALYSIS_PROFILE_ID)
    data["engine_build"] = "deadbeef" * 8
    assert display_upgrade_eligible(project_cache_row(data)) is False


def test_rejects_contract_unsatisfied():
    # Eligible profile but the v2 contract is not satisfied (delta inconsistent).
    data = _v2_data(BROWSER_ANALYSIS_PROFILE_ID, played_eval=30, best_eval=100, eval_delta=0)
    assert display_upgrade_eligible(project_cache_row(data)) is False


def test_rejects_position_grain_no_classification():
    # A bare position-grain row (no classification) cannot re-annotate the move.
    data = {
        "fen_before": START,
        "move_uci": "e2e4",
        "move_san": "e4",
        "best_move_uci": "e2e4",
        "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5",
        "best_eval": 30,
        "classification": None,
        "analysis_profile_id": CANONICAL_PROFILE_ID,
        "evidence_contract_id": POSITION_COMPLETE,
        **stamp_profile_full(CANONICAL_PROFILE_ID),
    }
    row = project_cache_row(data)
    assert "classification" not in row.populated_fields
    assert display_upgrade_eligible(row) is False


# --------------------------------------------------------------------------- #
# build_move_upgrade — perspective conversion
# --------------------------------------------------------------------------- #
def test_build_white_to_move_no_flip():
    row = _row(BROWSER_ANALYSIS_PROFILE_ID, played_eval=30, best_eval=55, classification="excellent")
    up = build_move_upgrade(row, START)
    assert up.eval_cp == 30  # mover == white, no flip
    assert up.best_move_eval_cp == 55
    assert up.best_move_san == "e4"  # derived from best_move_uci + fen
    assert up.classification == "excellent"


def test_build_black_to_move_flips_cp():
    # Black to move: white-relative -> mover-relative flips the sign.
    row = _row(
        BROWSER_ANALYSIS_PROFILE_ID,
        fen=FEN_BLACK,
        move_uci="e7e5",
        move_san="e5",
        best_move_uci="e7e5",
        best_move_san="e5",
        best_line_uci="e7e5 g1f3",
        played_eval=40,
        best_eval=40,
    )
    up = build_move_upgrade(row, FEN_BLACK)
    assert up.eval_cp == -40
    assert up.best_move_eval_cp == -40
    assert up.best_move_san == "e5"


def test_build_eval_delta_passthrough_never_flipped():
    # eval_delta is a side-to-move-relative loss and passes through unchanged on
    # BOTH colors (never sign-flipped).
    white = build_move_upgrade(
        _row(BROWSER_ANALYSIS_PROFILE_ID, played_eval=10, best_eval=55, eval_delta=45), START
    )
    black = build_move_upgrade(
        _row(
            BROWSER_ANALYSIS_PROFILE_ID,
            fen=FEN_BLACK,
            move_uci="e7e5",
            move_san="e5",
            best_move_uci="e7e5",
            best_move_san="e5",
            best_line_uci="e7e5 g1f3",
            played_eval=-10,
            best_eval=35,
            eval_delta=45,
        ),
        FEN_BLACK,
    )
    assert white.eval_delta == 45
    assert black.eval_delta == 45


def test_build_mate_only_row_white():
    row = _row(
        BROWSER_ANALYSIS_PROFILE_ID,
        played_eval=None,
        played_eval_mate=3,
        best_eval=None,
        best_eval_mate=3,
    )
    up = build_move_upgrade(row, START)
    assert up.eval_cp is None
    assert up.eval_mate == 3  # white to move, no flip


def test_build_mate_only_row_black_flips():
    row = _row(
        BROWSER_ANALYSIS_PROFILE_ID,
        fen=FEN_BLACK,
        move_uci="e7e5",
        move_san="e5",
        best_move_uci="e7e5",
        best_move_san="e5",
        best_line_uci="e7e5 g1f3",
        played_eval=None,
        played_eval_mate=2,
        best_eval=None,
        best_eval_mate=2,
    )
    up = build_move_upgrade(row, FEN_BLACK)
    assert up.eval_mate == -2  # black to move flips
    assert up.eval_cp is None


def test_build_mate_zero_stays_zero_on_black():
    row = _row(
        BROWSER_ANALYSIS_PROFILE_ID,
        fen=FEN_BLACK,
        move_uci="e7e5",
        move_san="e5",
        best_move_uci="e7e5",
        best_move_san="e5",
        best_line_uci="e7e5 g1f3",
        played_eval=None,
        played_eval_mate=0,
    )
    up = build_move_upgrade(row, FEN_BLACK)
    assert up.eval_mate == 0


def test_build_null_eval_delta_stays_valid():
    # A move-grain (move-complete-v1) row carries a classification but no eval_delta.
    row = AnalysisCache(
        fen_before=START,
        move_uci="e2e4",
        move_san="e4",
        played_eval=30,
        classification="good",
        eval_delta=None,
        analysis_profile_id=CANONICAL_PROFILE_ID,
        evidence_contract_id=MOVE_COMPLETE,
        **stamp_profile_full(CANONICAL_PROFILE_ID),
    )
    up = build_move_upgrade(row, START)
    assert up.eval_delta is None
    assert up.classification == "good"
    assert up.best_move_san is None  # no best_move_uci on this row


def test_build_authoritative_flag():
    assert build_move_upgrade(_row(CANONICAL_PROFILE_ID), START).authoritative is True
    assert build_move_upgrade(_row(BROWSER_ANALYSIS_PROFILE_ID), START).authoritative is False


def test_build_records_provenance():
    up = build_move_upgrade(_row(BROWSER_ANALYSIS_PROFILE_ID), START)
    assert up.analysis_profile_id == BROWSER_ANALYSIS_PROFILE_ID
    # search_limit_value of browser-analysis-v1 is depth 21.
    assert up.depth == 21


# --------------------------------------------------------------------------- #
# move_upgrade_for_row — project + gate + build
# --------------------------------------------------------------------------- #
def test_for_row_returns_upgrade_when_eligible():
    up = move_upgrade_for_row(_row(BROWSER_ANALYSIS_PROFILE_ID))
    assert up is not None
    assert up.classification == "best"


def test_for_row_none_when_ineligible():
    # A browser-game row is not overlay-eligible (does not dominate itself).
    row = _row(BROWSER_PROFILE_ID, evidence_contract_id="resolver-complete-v1")
    assert move_upgrade_for_row(row) is None
