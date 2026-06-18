"""Unit tests for the pure position-grain policy (g-position-analysis Phase 2)."""

import dataclasses

import app.analysis_profiles as profiles
from app.analysis_profiles import (
    CANONICAL_PROFILE_ID,
    IDENTITY_FIELDS,
    get_profile,
)
from app.position_analysis_policy import (
    POSITION_METADATA_FIELDS,
    PositionCandidate,
    conflict_signature,
    is_eligible_position_candidate,
    position_conflict_axes,
    position_signature,
    select_position_winner,
)

LINUX_PROFILE_ID = "canonical-sf18-depth24-linux-v1"
DEEPER_PROFILE_ID = "canonical-sf18-depth30-linux-v1"
NF = "rnbqkbnr/pp2pppp/2p5/3p4/3P4/5N2/PPP1PPPP/RNBQKB1R w KQkq -"
FEN = "rnbqkbnr/pp2pppp/2p5/3p4/3P4/5N2/PPP1PPPP/RNBQKB1R w KQkq - 0 3"


# --- Candidate / row builders --------------------------------------------------


def _candidate(cache_id, *, profile_id=LINUX_PROFILE_ID, best_move="c2c4",
               best_line="c2c4 d5c4", best_eval=35, best_eval_mate=None):
    profile = get_profile(profile_id)
    metadata = {f: getattr(profile, f, None) for f in POSITION_METADATA_FIELDS}
    return PositionCandidate(
        cache_id=cache_id,
        normalized_fen=NF,
        fen=FEN,
        profile_id=profile_id,
        contract_id="resolver-complete-v2",
        source="precomputed",
        best_move_uci=best_move,
        best_move_san=best_move,
        best_line_uci=best_line,
        best_eval=best_eval,
        best_eval_mate=best_eval_mate,
        metadata=metadata,
    )


def _eligible_row(profile_id=LINUX_PROFILE_ID, **overrides):
    """A full-identity, resolver-complete-v2, position-complete cache-row dict."""
    profile = get_profile(profile_id)
    row = {
        "analysis_profile_id": profile_id,
        "evidence_contract_id": "resolver-complete-v2",
        "best_move_uci": "c2c4",
        "best_line_uci": "c2c4 d5c4",
        "best_eval": 35,
        "best_eval_mate": None,
    }
    for field in IDENTITY_FIELDS:
        row[field] = getattr(profile, field)
    row.update(overrides)
    return row


# --- Eligibility ---------------------------------------------------------------


def test_eligible_full_canonical_row():
    assert is_eligible_position_candidate(_eligible_row())


def test_browser_row_is_not_eligible():
    # Non-authoritative profile (no identity backing) fails the authority gate even
    # if it carries position facts — this is the g-ul4p browser sibling.
    browser = {
        "analysis_profile_id": "browser-game-v1",
        "evidence_contract_id": "resolver-complete-v1",
        "best_move_uci": "c1f4",
        "best_line_uci": "c1f4 g8f6",
        "best_eval": 30,
    }
    assert not is_eligible_position_candidate(browser)


def test_v1_contract_row_is_not_eligible():
    # Full canonical identity but declares resolver-complete-v1 -> fails the
    # legacy-v2 position projection (fails closed for non-v2).
    assert not is_eligible_position_candidate(
        _eligible_row(evidence_contract_id="resolver-complete-v1")
    )


def test_mismatched_identity_row_is_not_eligible():
    # Claims the canonical profile but a tampered engine_build -> identity unverified.
    assert not is_eligible_position_candidate(_eligible_row(engine_build="deadbeef"))


def test_eligible_row_requires_pv_first_equals_best():
    assert not is_eligible_position_candidate(
        _eligible_row(best_line_uci="d2d4 d7d5")
    )


# --- Signature -----------------------------------------------------------------


def test_signature_ignores_cp_and_pv_differences():
    a = _candidate(1, best_eval=35, best_line="c2c4 d5c4")
    b = _candidate(2, best_eval=80, best_line="c2c4 g8f6")
    assert position_signature(a) == position_signature(b)


def test_signature_distinguishes_best_move():
    a = _candidate(1, best_move="c2c4", best_line="c2c4 d5c4")
    b = _candidate(2, best_move="g1f3", best_line="g1f3 g8f6")
    assert position_signature(a) != position_signature(b)


def test_signature_distinguishes_mate_from_cp_and_sign():
    cp = _candidate(1, best_eval=900, best_eval_mate=None)
    mate_white = _candidate(2, best_eval=None, best_eval_mate=3)
    mate_black = _candidate(3, best_eval=None, best_eval_mate=-3)
    mate_white_far = _candidate(4, best_eval=None, best_eval_mate=7)
    assert position_signature(cp) != position_signature(mate_white)
    assert position_signature(mate_white) != position_signature(mate_black)
    # Same side mating at a different distance is NOT a disagreement.
    assert position_signature(mate_white) == position_signature(mate_white_far)


# --- Winner selection ----------------------------------------------------------


def test_single_candidate_no_conflict():
    selection = select_position_winner([_candidate(1)])
    assert selection.winner.cache_id == 1
    assert selection.is_conflict is False


def test_siblings_agree_one_winner_no_conflict():
    # Two sibling rows (e.g. different played moves) that agree on the position's
    # best move -> one winner, no conflict. Higher cache_id wins within a profile.
    selection = select_position_winner([_candidate(1), _candidate(2)])
    assert selection.is_conflict is False
    assert selection.winner.cache_id == 2


def test_cp_only_difference_is_not_a_conflict():
    selection = select_position_winner(
        [_candidate(1, best_eval=35), _candidate(2, best_eval=80)]
    )
    assert selection.is_conflict is False


def test_stronger_search_wins_selected_dominant(monkeypatch):
    deeper = dataclasses.replace(
        get_profile(LINUX_PROFILE_ID), profile_id=DEEPER_PROFILE_ID,
        search_limit_value=30,
    )
    monkeypatch.setitem(profiles._REGISTRY, DEEPER_PROFILE_ID, deeper)
    # The depth-30 candidate sorts LAST by preference (not in the priority tuple)
    # yet is promoted because it is strictly stronger.
    weaker = _candidate(2, profile_id=LINUX_PROFILE_ID, best_move="c2c4",
                        best_line="c2c4 d5c4")
    stronger = _candidate(1, profile_id=DEEPER_PROFILE_ID, best_move="g1f3",
                          best_line="g1f3 g8f6")
    selection = select_position_winner([weaker, stronger])
    assert selection.winner.cache_id == 1
    assert selection.is_conflict is True
    assert selection.policy_reason == "selected_dominant"


def test_equal_strength_best_move_disagreement_prefers_linux():
    # linux vs standard canonical, equal strength, different best moves: the linux
    # winner is kept by deterministic tiebreak and the conflict is recorded.
    linux = _candidate(1, profile_id=LINUX_PROFILE_ID, best_move="c2c4",
                       best_line="c2c4 d5c4")
    standard = _candidate(2, profile_id=CANONICAL_PROFILE_ID, best_move="g1f3",
                          best_line="g1f3 g8f6")
    selection = select_position_winner([linux, standard])
    assert selection.winner.profile_id == LINUX_PROFILE_ID
    assert selection.is_conflict is True
    assert selection.policy_reason == "conflict_best_known_kept"


# --- Conflict axes / signature -------------------------------------------------


def test_conflict_axes_populate_only_differing_axes():
    a = _candidate(1, best_move="c2c4", best_line="c2c4 d5c4", best_eval=35)
    b = _candidate(2, best_move="g1f3", best_line="g1f3 g8f6", best_eval=40)
    axes = position_conflict_axes([b, a])  # unordered input
    assert axes["candidate_cache_ids"] == [1, 2]  # sorted by cache_id
    assert axes["best_move_disagreement"] == ["c2c4", "g1f3"]
    assert axes["pv_disagreement"] == ["c2c4 d5c4", "g1f3 g8f6"]
    assert axes["best_eval_disagreement"] == [35, 40]
    assert axes["best_eval_mate_disagreement"] is None  # both None -> agreed


def test_conflict_axes_cp_only_marks_best_move_agreed():
    a = _candidate(1, best_move="c2c4", best_eval=35)
    b = _candidate(2, best_move="c2c4", best_eval=80)
    axes = position_conflict_axes([a, b])
    assert axes["best_move_disagreement"] is None
    assert axes["best_eval_disagreement"] == [35, 80]


def test_conflict_signature_is_deterministic_and_summary_independent():
    a = _candidate(1, best_move="c2c4", best_line="c2c4 d5c4")
    b = _candidate(2, best_move="g1f3", best_line="g1f3 g8f6")
    axes1 = position_conflict_axes([a, b])
    axes2 = position_conflict_axes([b, a])  # different input order
    sig1 = conflict_signature(axes1, "conflict_best_known_kept")
    sig2 = conflict_signature(axes2, "conflict_best_known_kept")
    assert sig1 == sig2
    # policy_reason participates in the signature.
    assert conflict_signature(axes1, "selected_dominant") != sig1
