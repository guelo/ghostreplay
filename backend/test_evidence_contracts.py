"""Tests for the evidence-contract registry and profile resolution."""

from app.analysis_profiles import (
    CANONICAL_PROFILE_ID,
    RESOLUTION_FIELDS,
    get_profile,
    resolve_profile,
)
from app.evidence_contracts import (
    MINIMAL_BEST_EVAL,
    MINIMAL_PLAYED_EVAL,
    RESOLVER_COMPLETE,
    RESOLVER_COMPLETE_V2,
    contract_satisfied,
    is_strict_successor,
    is_superset_or_successor,
    select_browser_contract,
)

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
# Same position with black to move.
BLACK_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1"


def _v2_row(**overrides):
    row = {
        "fen_before": START_FEN,
        "best_move_uci": "e2e4",
        "best_line_uci": ["e2e4", "e7e5"],
        "classification": "good",
        "played_eval": 10,
        "best_eval": 40,
        "eval_delta": 30,  # white to move: best - played
    }
    row.update(overrides)
    return row


def test_v2_accepts_consistent_row():
    assert contract_satisfied(RESOLVER_COMPLETE_V2, _v2_row())


def test_v2_rejects_missing_or_invalid_classification():
    assert not contract_satisfied(RESOLVER_COMPLETE_V2, _v2_row(classification=None))
    assert not contract_satisfied(RESOLVER_COMPLETE_V2, _v2_row(classification="huge"))


def test_v2_rejects_single_move_pv():
    assert not contract_satisfied(RESOLVER_COMPLETE_V2, _v2_row(best_line_uci=["e2e4"]))


def test_v2_rejects_incomplete_eval_triple():
    assert not contract_satisfied(RESOLVER_COMPLETE_V2, _v2_row(played_eval=None))
    assert not contract_satisfied(RESOLVER_COMPLETE_V2, _v2_row(best_eval=None))
    assert not contract_satisfied(RESOLVER_COMPLETE_V2, _v2_row(eval_delta=None))
    assert not contract_satisfied(RESOLVER_COMPLETE_V2, _v2_row(eval_delta=-1))


def test_v2_rejects_inconsistent_delta():
    # White to move: delta must equal best - played = 30, not an arbitrary value.
    assert not contract_satisfied(RESOLVER_COMPLETE_V2, _v2_row(eval_delta=99))


def test_v2_rejects_wrong_active_color_delta():
    # Black to move: correct delta is max(played - best, 0) = 0; a delta computed
    # as if white were to move (30) must be rejected.
    assert not contract_satisfied(
        RESOLVER_COMPLETE_V2, _v2_row(fen_before=BLACK_FEN, eval_delta=30)
    )
    assert contract_satisfied(
        RESOLVER_COMPLETE_V2, _v2_row(fen_before=BLACK_FEN, eval_delta=0)
    )


def test_v2_fails_closed_on_bad_fen():
    assert not contract_satisfied(RESOLVER_COMPLETE_V2, _v2_row(fen_before=None))
    assert not contract_satisfied(RESOLVER_COMPLETE_V2, _v2_row(fen_before="not a fen"))


def test_v2_supersession():
    assert is_superset_or_successor(RESOLVER_COMPLETE_V2, RESOLVER_COMPLETE)
    assert is_superset_or_successor(RESOLVER_COMPLETE_V2, MINIMAL_PLAYED_EVAL)
    assert is_superset_or_successor(RESOLVER_COMPLETE_V2, MINIMAL_BEST_EVAL)
    # v1 must NOT supersede v2.
    assert not is_superset_or_successor(RESOLVER_COMPLETE, RESOLVER_COMPLETE_V2)
    assert is_strict_successor(RESOLVER_COMPLETE_V2, RESOLVER_COMPLETE)
    assert not is_strict_successor(RESOLVER_COMPLETE_V2, RESOLVER_COMPLETE_V2)


def test_resolver_complete_requires_usable_pv():
    assert contract_satisfied(RESOLVER_COMPLETE, {
        "classification": "best",
        "best_move_uci": "e2e4",
        "best_line_uci": ["e2e4", "e7e5"],
    })
    # PV that does not start at best move is unusable.
    assert not contract_satisfied(RESOLVER_COMPLETE, {
        "classification": "best",
        "best_move_uci": "e2e4",
        "best_line_uci": ["d2d4", "d7d5"],
    })
    # Single-move PV is insufficient.
    assert not contract_satisfied(RESOLVER_COMPLETE, {
        "eval_delta": 0,
        "best_move_uci": "e2e4",
        "best_line_uci": ["e2e4"],
    })


def test_minimal_contracts():
    assert contract_satisfied(MINIMAL_PLAYED_EVAL, {"played_eval": 5})
    assert not contract_satisfied(MINIMAL_PLAYED_EVAL, {"played_eval": None})
    assert contract_satisfied(MINIMAL_BEST_EVAL, {"best_eval": -3})


def test_select_browser_contract_prefers_specific():
    full = {
        "classification": "best",
        "best_move_uci": "e2e4",
        "best_line_uci": ["e2e4", "e7e5"],
        "played_eval": 20,
    }
    assert select_browser_contract(full) == RESOLVER_COMPLETE
    assert select_browser_contract({"played_eval": 20}) == MINIMAL_PLAYED_EVAL
    assert select_browser_contract({"best_eval": 20}) == MINIMAL_BEST_EVAL
    assert select_browser_contract({"move_san": "e4"}) is None


def test_resolve_profile_exact_match():
    p = get_profile(CANONICAL_PROFILE_ID)
    observed = {f: getattr(p, f) for f in RESOLUTION_FIELDS}
    assert resolve_profile(observed) == CANONICAL_PROFILE_ID


def test_resolve_profile_offspec_is_none():
    p = get_profile(CANONICAL_PROFILE_ID)
    observed = {f: getattr(p, f) for f in RESOLUTION_FIELDS}
    observed["search_limit_value"] = 17  # off-spec depth
    assert resolve_profile(observed) is None
