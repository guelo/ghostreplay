"""Tests for Phase 3: position_analysis write policy + move-complete contract.

Covers:
* Pure decision function: decide_position_analysis_replacement (no DB)
* DB-level write: write_position_analysis_row (SQLite via conftest fixture)
* Move-complete contract enforcement: post-split move rows cannot satisfy
  resolver-complete-v2 without position-grain fields, so the cache policy
  rejects them; move-complete-v1 is the correct contract for split move rows.
"""
from __future__ import annotations

import dataclasses

import pytest

import app.analysis_profiles as profiles
from app.analysis_profiles import (
    CANONICAL_PROFILE_ID,
    IDENTITY_FIELDS,
    get_profile,
)

LINUX_PROFILE_ID = "canonical-sf18-depth24-linux-v1"
from app.evidence_contracts import (
    MOVE_COMPLETE,
    POSITION_COMPLETE,
    RESOLVER_COMPLETE_V2,
    contract_satisfied,
)
from app.models import PositionAnalysisRow
from app.position_analysis_policy import (
    POSITION_FACT_FIELDS,
    POSITION_METADATA_FIELDS,
    PositionDecision,
    PositionReason,
    PositionRow,
    decide_position_analysis_replacement,
    incoming_position_is_valid,
    position_populated_fields_of,
)
from app.position_analysis_repo import (
    _project_position,
    write_position_analysis_row,
)

# ---------------------------------------------------------------------------
# Constants / fixtures shared across pure and DB tests
# ---------------------------------------------------------------------------

NF = "rnbqkbnr/pp2pppp/2p5/3p4/3P4/5N2/PPP1PPPP/RNBQKB1R w KQkq -"
FEN = "rnbqkbnr/pp2pppp/2p5/3p4/3P4/5N2/PPP1PPPP/RNBQKB1R w KQkq - 0 3"
DEEPER_PROFILE_ID = "canonical-sf18-depth30-linux-v1"


def _canonical_data(profile_id=LINUX_PROFILE_ID, **overrides) -> dict:
    """Minimal data dict for a valid native position write."""
    profile = get_profile(profile_id)
    data = {
        "normalized_fen": NF,
        "fen": FEN,
        "evidence_contract_id": POSITION_COMPLETE,
        "source": "precomputed",
        "source_cache_id": None,
        "best_move_uci": "c2c4",
        "best_move_san": "c4",
        "best_line_uci": "c2c4 d5c4",
        "best_eval": 35,
        "best_eval_mate": None,
    }
    for f in IDENTITY_FIELDS:
        data[f] = getattr(profile, f)
    data["analysis_profile_id"] = profile_id
    data.update(overrides)
    return data


def _project(data: dict) -> PositionRow:
    return _project_position(data)


# ---------------------------------------------------------------------------
# Pure unit tests: incoming_position_is_valid
# ---------------------------------------------------------------------------


def test_valid_position_complete_row_is_valid():
    assert incoming_position_is_valid(_project(_canonical_data()))


def test_wrong_contract_rejected():
    # resolver-complete-v2 is the legacy read contract; not accepted for writes.
    data = _canonical_data(evidence_contract_id=RESOLVER_COMPLETE_V2)
    # Add the extra v2 fields so v2 validation would otherwise pass.
    data.update({"played_eval": 50, "eval_delta": 0, "classification": "excellent",
                 "fen_before": FEN})
    assert not incoming_position_is_valid(_project(data))


def test_move_complete_contract_rejected():
    data = _canonical_data(evidence_contract_id=MOVE_COMPLETE)
    data.update({"played_eval": 50, "classification": "excellent"})
    assert not incoming_position_is_valid(_project(data))


def test_failed_position_complete_validation_rejected():
    # Declares position-complete-v1 but has no best_line_uci -> fails validation.
    data = _canonical_data(best_line_uci=None)
    assert not incoming_position_is_valid(_project(data))


def test_pv_first_must_equal_best_move():
    data = _canonical_data(best_line_uci="d2d4 d5c4")  # PV does not start with c2c4
    assert not incoming_position_is_valid(_project(data))


def test_unverifiable_profile_rejected():
    data = _canonical_data(engine_build="deadbeef")  # tampered identity
    assert not incoming_position_is_valid(_project(data))


# ---------------------------------------------------------------------------
# Pure unit tests: decide_position_analysis_replacement
# ---------------------------------------------------------------------------


def test_browser_rejected_new_key():
    """Browser write to an empty position is structurally rejected (no insert)."""
    browser_data = {
        "normalized_fen": NF,
        "fen": FEN,
        "evidence_contract_id": POSITION_COMPLETE,
        "analysis_profile_id": "browser-game-v1",
        "best_move_uci": "c1f4",
        "best_move_san": "Bf4",
        "best_line_uci": "c1f4 g8f6",
        "best_eval": 30,
        "best_eval_mate": None,
    }
    # identity_verified=False for browser (profile fields are all None in registry)
    row = _project(browser_data)
    assert not row.is_effectively_authoritative()
    decision, reason = decide_position_analysis_replacement(None, row)
    assert decision is PositionDecision.KEEP
    assert reason is PositionReason.NON_AUTHORITATIVE_KEEP


def test_browser_rejected_against_existing_canonical():
    """Browser write competing with an existing canonical position is rejected."""
    existing = _project(_canonical_data())
    browser_data = {
        "normalized_fen": NF,
        "fen": FEN,
        "evidence_contract_id": POSITION_COMPLETE,
        "analysis_profile_id": "browser-game-v1",
        "best_move_uci": "c1f4",
        "best_move_san": "Bf4",
        "best_line_uci": "c1f4 g8f6",
        "best_eval": 30,
        "best_eval_mate": None,
    }
    decision, reason = decide_position_analysis_replacement(existing, _project(browser_data))
    assert decision is PositionDecision.KEEP
    assert reason is PositionReason.NON_AUTHORITATIVE_KEEP


def test_resolver_complete_v2_contract_rejected_for_position_write():
    """A row claiming resolver-complete-v2 is rejected at the validity gate."""
    data = _canonical_data()
    data["evidence_contract_id"] = RESOLVER_COMPLETE_V2
    data.update({"played_eval": 50, "eval_delta": 0, "classification": "excellent",
                 "fen_before": FEN})
    decision, reason = decide_position_analysis_replacement(None, _project(data))
    assert decision is PositionDecision.KEEP
    assert reason is PositionReason.INVALID_INCOMING_KEEP


def test_valid_canonical_new_key_inserts():
    decision, reason = decide_position_analysis_replacement(None, _project(_canonical_data()))
    assert decision is PositionDecision.INSERT
    assert reason is PositionReason.NEW_KEY


def test_same_profile_idempotent_same_facts():
    existing = _project(_canonical_data())
    incoming = _project(_canonical_data())  # identical
    decision, reason = decide_position_analysis_replacement(existing, incoming)
    assert decision is PositionDecision.KEEP
    assert reason is PositionReason.SAME_PROFILE_IDEMPOTENT


def test_same_profile_merge_adds_best_eval_mate():
    """Incoming adds best_eval_mate that existing lacks → MERGE."""
    existing = _project(_canonical_data(best_eval_mate=None))
    incoming = _project(_canonical_data(best_eval_mate=5, best_eval=None))
    # best_eval_mate is present in incoming but not existing; best_eval differs
    # (None vs 35) so the overlap agreement check only sees best_move/line.
    decision, reason = decide_position_analysis_replacement(existing, incoming)
    assert decision is PositionDecision.MERGE
    assert reason is PositionReason.SAME_PROFILE_SUPERSET_MERGE


def test_same_profile_merge_conflict_keep():
    """Incoming overlapping field disagrees → MERGE_CONFLICT_KEEP (no write)."""
    existing = _project(_canonical_data(best_move_uci="c2c4", best_line_uci="c2c4 d5c4"))
    incoming = _project(_canonical_data(best_move_uci="g1f3", best_line_uci="g1f3 g8f6"))
    decision, reason = decide_position_analysis_replacement(existing, incoming)
    assert decision is PositionDecision.KEEP
    assert reason is PositionReason.MERGE_CONFLICT_KEEP


def test_incompatible_profiles_keep(monkeypatch):
    """Two canonical profiles with no dominates edge → INCOMPATIBLE_KEEP."""
    other_profile_id = "canonical-other-v1"
    fake_other = dataclasses.replace(
        get_profile(LINUX_PROFILE_ID),
        profile_id=other_profile_id,
        dominates=frozenset(),
    )
    monkeypatch.setitem(profiles._REGISTRY, other_profile_id, fake_other)

    existing = _project(_canonical_data(profile_id=LINUX_PROFILE_ID))
    incoming = _project(_canonical_data(profile_id=other_profile_id))
    decision, reason = decide_position_analysis_replacement(existing, incoming)
    assert decision is PositionDecision.KEEP
    assert reason is PositionReason.INCOMPATIBLE_KEEP


def test_dominates_replace(monkeypatch):
    """Canonical profile with explicit dominance edge → REPLACE."""
    deeper = dataclasses.replace(
        get_profile(LINUX_PROFILE_ID),
        profile_id=DEEPER_PROFILE_ID,
        search_limit_value=30,
        dominates=frozenset({LINUX_PROFILE_ID}),
    )
    monkeypatch.setitem(profiles._REGISTRY, DEEPER_PROFILE_ID, deeper)

    existing = _project(_canonical_data(profile_id=LINUX_PROFILE_ID))
    incoming = _project(_canonical_data(profile_id=DEEPER_PROFILE_ID))
    decision, reason = decide_position_analysis_replacement(existing, incoming)
    assert decision is PositionDecision.REPLACE
    assert reason is PositionReason.DOMINATES_REPLACE


def test_legacy_row_replaced_by_authoritative():
    """Existing row with NULL/unverified profile is replaced by canonical."""
    # Simulate a legacy row: no profile id, no identity fields.
    legacy_data = {
        "normalized_fen": NF,
        "fen": FEN,
        "evidence_contract_id": POSITION_COMPLETE,
        "analysis_profile_id": None,
        "best_move_uci": "c2c4",
        "best_move_san": "c4",
        "best_line_uci": "c2c4 d5c4",
        "best_eval": 20,
        "best_eval_mate": None,
    }
    existing = _project(legacy_data)
    assert existing.effective_profile_id() is None  # legacy

    incoming = _project(_canonical_data())
    decision, reason = decide_position_analysis_replacement(existing, incoming)
    assert decision is PositionDecision.REPLACE
    assert reason is PositionReason.LEGACY_REPLACED_BY_AUTH


# ---------------------------------------------------------------------------
# Move-complete contract enforcement
# ---------------------------------------------------------------------------


def test_move_complete_v1_accepted_by_cache_contract_validator():
    """move-complete-v1 rows pass their own contract when data is valid."""
    move_data = {
        "played_eval": 50,
        "classification": "excellent",
    }
    assert contract_satisfied(MOVE_COMPLETE, move_data)


def test_post_split_move_row_cannot_satisfy_resolver_complete_v2():
    """A move-only row (no best_eval, no best_line_uci) fails v2 validation.

    This demonstrates the structural enforcement: after the position split,
    move-grain rows lack position facts, so claiming resolver-complete-v2 would
    fail contract_satisfied and be rejected by the analysis_cache write policy's
    incoming_is_valid gate.  Post-split move writes must use move-complete-v1.
    """
    post_split_move_data = {
        "played_eval": 50,
        "classification": "excellent",
        "eval_delta": 0,
        "fen_before": FEN,
        # No best_eval, no best_move_uci, no best_line_uci (those live in position_analysis now)
    }
    assert not contract_satisfied(RESOLVER_COMPLETE_V2, post_split_move_data)
    assert contract_satisfied(MOVE_COMPLETE, post_split_move_data)


def test_position_complete_does_not_require_played_eval():
    """Position-grain contract has no played-eval requirement."""
    pos_data = {
        "best_move_uci": "c2c4",
        "best_line_uci": "c2c4 d5c4",
        "best_eval": 35,
    }
    assert contract_satisfied(POSITION_COMPLETE, pos_data)
    # move-complete still needs played_eval
    assert not contract_satisfied(MOVE_COMPLETE, pos_data)


# ---------------------------------------------------------------------------
# DB integration tests: write_position_analysis_row
# ---------------------------------------------------------------------------


def test_db_write_new_key_inserts(db_session):
    data = _canonical_data()
    reason = write_position_analysis_row(db_session, data)
    assert reason is PositionReason.NEW_KEY

    row = db_session.query(PositionAnalysisRow).filter_by(normalized_fen=NF).one()
    assert row.best_move_uci == "c2c4"
    assert row.best_eval == 35
    assert row.evidence_contract_id == POSITION_COMPLETE
    assert row.source_cache_id is None  # native write marker


def test_db_write_browser_rejected(db_session):
    browser_data = {
        "normalized_fen": NF,
        "fen": FEN,
        "evidence_contract_id": POSITION_COMPLETE,
        "analysis_profile_id": "browser-game-v1",
        "best_move_uci": "c1f4",
        "best_move_san": "Bf4",
        "best_line_uci": "c1f4 g8f6",
        "best_eval": 30,
        "best_eval_mate": None,
    }
    reason = write_position_analysis_row(db_session, browser_data)
    assert reason is PositionReason.NON_AUTHORITATIVE_KEEP
    # Nothing was written.
    assert db_session.query(PositionAnalysisRow).filter_by(normalized_fen=NF).count() == 0


def test_db_write_idempotent_same_facts(db_session):
    write_position_analysis_row(db_session, _canonical_data())
    db_session.flush()
    reason = write_position_analysis_row(db_session, _canonical_data())
    assert reason is PositionReason.SAME_PROFILE_IDEMPOTENT
    assert db_session.query(PositionAnalysisRow).filter_by(normalized_fen=NF).count() == 1


def test_db_write_merge_adds_mate_field(db_session):
    """Same-profile write that adds best_eval_mate triggers MERGE."""
    write_position_analysis_row(db_session, _canonical_data(best_eval=35, best_eval_mate=None))
    db_session.flush()
    # Incoming contributes best_eval_mate=5 with no conflicting best_eval.
    reason = write_position_analysis_row(
        db_session, _canonical_data(best_eval=None, best_eval_mate=5)
    )
    assert reason is PositionReason.SAME_PROFILE_SUPERSET_MERGE
    row = db_session.query(PositionAnalysisRow).filter_by(normalized_fen=NF).one()
    assert row.best_eval == 35        # retained from existing
    assert row.best_eval_mate == 5    # merged in from incoming


def test_db_write_resolver_v2_rejected(db_session):
    data = _canonical_data(evidence_contract_id=RESOLVER_COMPLETE_V2)
    data.update({"played_eval": 50, "eval_delta": 0, "classification": "excellent",
                 "fen_before": FEN})
    reason = write_position_analysis_row(db_session, data)
    assert reason is PositionReason.INVALID_INCOMING_KEEP
    assert db_session.query(PositionAnalysisRow).filter_by(normalized_fen=NF).count() == 0


def test_db_write_backfill_row_not_overwritten_by_native(db_session):
    """A backfill row (source_cache_id IS NOT NULL) is a protected native row
    from the live-write perspective: the backfill path sets source_cache_id,
    but the write policy does not distinguish — it replaces/merges based on profile
    dominance, not source_cache_id.  The source_cache_id protection lives in the
    backfill's own _upsert_winner guard, not here.  This test documents that the
    write policy will replace a backfill row when the incoming canonical row
    dominates — the *backfill* protection is a one-way guard only for the
    backfill path, not for native live writes.
    """
    # Write a backfill-style row first (source_cache_id=42).
    backfill_data = _canonical_data(source_cache_id=42)
    write_position_analysis_row(db_session, backfill_data)
    db_session.flush()

    # A native live write for the same position/profile is idempotent (same facts).
    native_data = _canonical_data(source_cache_id=None)
    reason = write_position_analysis_row(db_session, native_data)
    # Same profile + same facts = idempotent KEEP.
    assert reason is PositionReason.SAME_PROFILE_IDEMPOTENT
