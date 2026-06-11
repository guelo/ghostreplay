"""Tests for the evidence-contract registry and profile resolution."""

from app.analysis_profiles import (
    CANONICAL_PROFILE_ID,
    IDENTITY_FIELDS,
    get_profile,
    resolve_profile,
)
from app.evidence_contracts import (
    MINIMAL_BEST_EVAL,
    MINIMAL_PLAYED_EVAL,
    RESOLVER_COMPLETE,
    contract_satisfied,
    select_browser_contract,
)


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
    observed = {f: getattr(p, f) for f in IDENTITY_FIELDS}
    assert resolve_profile(observed) == CANONICAL_PROFILE_ID


def test_resolve_profile_offspec_is_none():
    p = get_profile(CANONICAL_PROFILE_ID)
    observed = {f: getattr(p, f) for f in IDENTITY_FIELDS}
    observed["search_limit_value"] = 17  # off-spec depth
    assert resolve_profile(observed) is None
