"""Grain-specific trust helpers (app/analysis_trust.py).

The load-bearing property is CROSS-GRAIN ISOLATION: each grain is gated on its OWN
expected contract id, so an authoritative row that satisfies one grain's contract
must NOT read as trusted at the other grain. A legacy resolver-complete-v2 row is
the one exception — it projects into both grains during the migration.
"""

from app.analysis_profiles import CANONICAL_PROFILE_ID, IDENTITY_FIELDS, get_profile
from app.analysis_trust import (
    cache_row_as_move_dict,
    cache_row_as_position_dict,
    move_trust_flags,
    position_trust_flags,
    source_rank,
)

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def _identity(profile_id: str = CANONICAL_PROFILE_ID) -> dict:
    """Full canonical identity columns so _effectively_authoritative passes."""
    profile = get_profile(profile_id)
    data = {"analysis_profile_id": profile_id}
    for field in IDENTITY_FIELDS:
        data[field] = getattr(profile, field)
    return data


def _position_facts() -> dict:
    return {
        "best_move_uci": "e2e4",
        "best_line_uci": "e2e4 e7e5",
        "best_eval": 20,
        "best_eval_mate": None,
    }


def _move_facts() -> dict:
    return {
        "played_eval": 20,
        "played_eval_mate": None,
        "classification": "best",
        "eval_delta": 0,
    }


# --- cross-grain negatives -----------------------------------------------------


def test_authoritative_move_complete_row_is_not_position_trusted():
    data = {**_identity(), "evidence_contract_id": "move-complete-v1", **_move_facts()}
    assert move_trust_flags(data) == (True, True, True)
    # Same authoritative row must NOT read as position-trusted: it declares the move
    # grain, and it is not a legacy v2 projection.
    authoritative, satisfied, trusted = position_trust_flags(data)
    assert authoritative is True
    assert satisfied is False
    assert trusted is False


def test_authoritative_position_complete_row_is_not_move_trusted():
    data = {**_identity(), "evidence_contract_id": "position-complete-v1", **_position_facts()}
    assert position_trust_flags(data) == (True, True, True)
    authoritative, satisfied, trusted = move_trust_flags(data)
    assert authoritative is True
    assert satisfied is False
    assert trusted is False


def test_legacy_v2_row_is_trusted_at_both_grains():
    data = {
        **_identity(),
        "evidence_contract_id": "resolver-complete-v2",
        "fen_before": START,
        **_position_facts(),
        **_move_facts(),
    }
    assert position_trust_flags(data)[2] is True
    assert move_trust_flags(data)[2] is True


def test_browser_v1_row_is_trusted_at_neither_grain():
    data = {
        "analysis_profile_id": "browser-game-v1",
        "evidence_contract_id": "resolver-complete-v1",
        **_position_facts(),
        **_move_facts(),
    }
    p_auth, _, p_trusted = position_trust_flags(data)
    m_auth, _, m_trusted = move_trust_flags(data)
    assert p_auth is False and p_trusted is False
    assert m_auth is False and m_trusted is False


def test_authoritative_but_missing_facts_fails_its_own_grain():
    # Declares position-complete + canonical identity but has no PV -> not satisfied.
    data = {**_identity(), "evidence_contract_id": "position-complete-v1",
            "best_move_uci": "e2e4", "best_line_uci": None, "best_eval": 20}
    authoritative, satisfied, trusted = position_trust_flags(data)
    assert authoritative is True
    assert satisfied is False
    assert trusted is False


# --- source_rank ---------------------------------------------------------------


def test_source_rank_ordering():
    assert source_rank("precomputed") < source_rank("game") < source_rank("other")
    assert source_rank(None) == source_rank("anything-unknown")


# --- row projectors (getattr-based; no ORM import) -----------------------------


class _Row:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_cache_row_projectors_round_trip_trust():
    profile = get_profile(CANONICAL_PROFILE_ID)
    common = {f: getattr(profile, f) for f in IDENTITY_FIELDS}
    row = _Row(
        analysis_profile_id=CANONICAL_PROFILE_ID,
        evidence_contract_id="resolver-complete-v2",
        best_move_uci="e2e4", best_line_uci="e2e4 e7e5", best_eval=20,
        best_eval_mate=None, played_eval=20, played_eval_mate=None,
        classification="best", eval_delta=0, **common,
    )
    assert position_trust_flags(cache_row_as_position_dict(row))[2] is True
    assert move_trust_flags(cache_row_as_move_dict(row))[2] is True
