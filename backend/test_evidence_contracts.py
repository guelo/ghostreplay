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
    MOVE_COMPLETE,
    POSITION_COMPLETE,
    RESOLVER_COMPLETE,
    RESOLVER_COMPLETE_V2,
    contract_satisfied,
    is_strict_successor,
    is_superset_or_successor,
    legacy_v2_satisfies_move,
    legacy_v2_satisfies_position,
    project_v2_to_move,
    project_v2_to_position,
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
        # Declared contract — read by the legacy-v2 projection gate, ignored by the
        # resolver-complete-v2 validator itself.
        "evidence_contract_id": RESOLVER_COMPLETE_V2,
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


# --- Grain-specific contracts: position-complete-v1 ----------------------------


def _position_row(**overrides):
    row = {
        "best_move_uci": "e2e4",
        "best_line_uci": ["e2e4", "e7e5"],
        "best_eval": 35,
    }
    row.update(overrides)
    return row


def test_position_complete_accepts_finite_best_eval():
    assert contract_satisfied(POSITION_COMPLETE, _position_row())


def test_position_complete_accepts_mate_only():
    # best_eval absent but an explicit mate count is present.
    assert contract_satisfied(
        POSITION_COMPLETE, _position_row(best_eval=None, best_eval_mate=4)
    )


def test_position_complete_rejects_single_move_pv():
    assert not contract_satisfied(POSITION_COMPLETE, _position_row(best_line_uci=["e2e4"]))


def test_position_complete_rejects_pv_not_starting_at_best_move():
    assert not contract_satisfied(
        POSITION_COMPLETE, _position_row(best_line_uci=["d2d4", "d7d5"])
    )


def test_position_complete_rejects_no_eval():
    assert not contract_satisfied(
        POSITION_COMPLETE, _position_row(best_eval=None, best_eval_mate=None)
    )


def test_position_complete_does_not_require_played_or_delta():
    # Position grain carries no played-move delta; absence must not fail it.
    row = _position_row()
    assert "eval_delta" not in row and "played_eval" not in row
    assert contract_satisfied(POSITION_COMPLETE, row)


# --- Grain-specific contracts: move-complete-v1 --------------------------------


def _move_row(**overrides):
    row = {"played_eval": 12, "classification": "good"}
    row.update(overrides)
    return row


def test_move_complete_accepts_finite_played_eval():
    assert contract_satisfied(MOVE_COMPLETE, _move_row())


def test_move_complete_accepts_played_mate():
    assert contract_satisfied(
        MOVE_COMPLETE, _move_row(played_eval=None, played_eval_mate=-3)
    )


def test_move_complete_rejects_missing_or_invalid_classification():
    assert not contract_satisfied(MOVE_COMPLETE, _move_row(classification=None))
    assert not contract_satisfied(MOVE_COMPLETE, _move_row(classification="huge"))


def test_move_complete_rejects_no_played_eval_or_mate():
    assert not contract_satisfied(
        MOVE_COMPLETE, _move_row(played_eval=None, played_eval_mate=None)
    )


def test_move_complete_ignores_eval_delta():
    # A move-only row has no best_eval and need not carry a consistent eval_delta;
    # an arbitrary/absent delta must not change the verdict (contrast v2).
    assert contract_satisfied(MOVE_COMPLETE, _move_row(eval_delta=99999))
    assert contract_satisfied(MOVE_COMPLETE, _move_row(best_eval=None, eval_delta=None))


def test_move_complete_required_fields_is_informational_only():
    # required_fields lists 'classification' but NOT played_eval; a row populating
    # the played_eval_mate alternative (no played_eval) still validates, documenting
    # that contract_satisfied is driven by validate(), never required_fields.
    row = _move_row(played_eval=None, played_eval_mate=2)
    assert "played_eval_mate" in row and row["played_eval"] is None
    assert contract_satisfied(MOVE_COMPLETE, row)


# --- Legacy-v2 grain projection ------------------------------------------------


def test_legacy_v2_satisfies_both_grains():
    row = _v2_row()
    assert legacy_v2_satisfies_position(row)
    assert legacy_v2_satisfies_move(row)


def test_legacy_v2_position_projection_drops_played_delta():
    # The position projection must not carry move-grain fields.
    projected = project_v2_to_position(_v2_row())
    assert set(projected) == {
        "best_move_uci", "best_line_uci", "best_eval", "best_eval_mate"
    }


def test_legacy_v2_fails_position_when_pv_missing():
    assert not legacy_v2_satisfies_position(_v2_row(best_line_uci=["e2e4"]))


def test_legacy_v2_fails_move_when_classification_bad():
    assert not legacy_v2_satisfies_move(_v2_row(classification="huge"))


def test_legacy_v2_move_projection_includes_played_mate_field():
    # project_v2_to_move must surface played_eval_mate even when absent in the
    # source dict, so a played-mate v2 row can satisfy the move grain.
    projected = project_v2_to_move(_v2_row(played_eval=None, played_eval_mate=-5))
    assert projected["played_eval_mate"] == -5
    assert legacy_v2_satisfies_move(
        _v2_row(played_eval=None, played_eval_mate=-5)
    )


def test_legacy_v2_helpers_fail_closed_on_non_v2_row():
    # A row carrying every projected grain field but DECLARING a non-v2 contract
    # must NOT be treated as a legacy v2 projection — the helpers gate on the
    # declared v2 id so a browser/v1/minimal row can never spuriously pass.
    v1_row = _v2_row(evidence_contract_id=RESOLVER_COMPLETE)
    assert not legacy_v2_satisfies_position(v1_row)
    assert not legacy_v2_satisfies_move(v1_row)
    # An absent contract id also fails closed.
    no_id = _v2_row()
    no_id.pop("evidence_contract_id")
    assert not legacy_v2_satisfies_position(no_id)
    assert not legacy_v2_satisfies_move(no_id)
