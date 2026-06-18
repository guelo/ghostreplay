"""Integration tests for the Phase-2 position-analysis backfill.

Uses the conftest in-memory SQLite (hand-written schema includes ``position_analysis``
and ``position_analysis_conflicts``). Trusted rows seed FULL canonical identity so
the eligibility gate accepts them; untrusted rows use browser-game-v1.
"""

import json

import pytest

from app.analysis_profiles import CANONICAL_PROFILE_ID, IDENTITY_FIELDS, get_profile
from app.fen import normalize_fen
from app.models import (
    AnalysisCache,
    PositionAnalysisConflict,
    PositionAnalysisRow,
)
from app.position_analysis_backfill import backfill_position_analysis

LINUX_PROFILE_ID = "canonical-sf18-depth24-linux-v1"

# Same position via different move orders -> identical normalized FEN.
GUL4P_FEN = "rnbqkbnr/pp2pppp/2p5/3p4/3P4/5N2/PPP1PPPP/RNBQKB1R w KQkq - 0 3"
GUL4P_NF = normalize_fen(GUL4P_FEN)
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def _seed(db, *, fen, move_uci, profile_id=None, browser=False, **fields):
    values = dict(fields)
    if browser:
        values.setdefault("analysis_profile_id", "browser-game-v1")
        values.setdefault("evidence_contract_id", "resolver-complete-v1")
        values.setdefault("source", "game")
    elif profile_id is not None:
        profile = get_profile(profile_id)
        values["analysis_profile_id"] = profile_id
        values.setdefault("evidence_contract_id", "resolver-complete-v2")
        values.setdefault("source", "precomputed")
        for f in IDENTITY_FIELDS:
            values.setdefault(f, getattr(profile, f))
    row = AnalysisCache(
        fen_before=fen,
        normalized_fen_before=values.pop("normalized_fen_before", normalize_fen(fen)),
        move_uci=move_uci,
        move_san=values.pop("move_san", move_uci),
        **values,
    )
    db.add(row)
    db.commit()
    return row


def _position_row(db, normalized_fen):
    return (
        db.query(PositionAnalysisRow)
        .filter(PositionAnalysisRow.normalized_fen == normalized_fen)
        .one_or_none()
    )


def _conflicts(db, normalized_fen):
    return (
        db.query(PositionAnalysisConflict)
        .filter(PositionAnalysisConflict.normalized_fen == normalized_fen)
        .all()
    )


# --- Group-then-pick + winner stamp --------------------------------------------


def test_siblings_group_to_one_winner_no_conflict(db_session):
    # Two played-move siblings at the same position agree on the best move.
    _seed(db_session, fen=GUL4P_FEN, move_uci="c1f4", profile_id=LINUX_PROFILE_ID,
          best_move_uci="c2c4", best_move_san="c4", best_line_uci="c2c4 d5c4",
          best_eval=35)
    second = _seed(db_session, fen=GUL4P_FEN, move_uci="g1e5",
                   profile_id=LINUX_PROFILE_ID, best_move_uci="c2c4",
                   best_move_san="c4", best_line_uci="c2c4 d5c4", best_eval=35)

    stats = backfill_position_analysis(db_session, progress_every=0)

    assert stats.groups_scanned == 1
    assert stats.candidates_eligible == 2
    assert stats.winners_inserted == 1
    assert stats.conflicts_recorded == 0
    row = _position_row(db_session, GUL4P_NF)
    assert row is not None
    assert row.best_move_uci == "c2c4"
    assert row.source == "precomputed"
    assert row.evidence_contract_id == "position-complete-v1"
    assert row.analysis_profile_id == LINUX_PROFILE_ID
    assert row.fen == GUL4P_FEN
    # Higher cache_id wins the within-profile deterministic tiebreak.
    assert row.source_cache_id == second.id


def test_gul4p_browser_sibling_filtered_clean_winner(db_session):
    # Canonical c1f4 row whose POSITION best move is c2c4.
    canonical = _seed(db_session, fen=GUL4P_FEN, move_uci="c1f4",
                      profile_id=LINUX_PROFILE_ID, best_move_uci="c2c4",
                      best_move_san="c4", best_line_uci="c2c4 d5c4", best_eval=35)
    # Untrusted browser c2c4 row claiming a DIFFERENT best move (c1f4).
    _seed(db_session, fen=GUL4P_FEN, move_uci="c2c4", browser=True,
          best_move_uci="c1f4", best_move_san="Bf4", best_line_uci="c1f4 g8f6",
          best_eval=30)

    stats = backfill_position_analysis(db_session, progress_every=0)

    row = _position_row(db_session, GUL4P_NF)
    assert row.best_move_uci == "c2c4"  # canonical drives it; browser filtered out
    assert row.source_cache_id == canonical.id
    assert stats.conflicts_recorded == 0  # browser never grouped -> no disagreement
    assert _conflicts(db_session, GUL4P_NF) == []


def test_equal_strength_best_move_conflict_prefers_linux(db_session):
    linux = _seed(db_session, fen=GUL4P_FEN, move_uci="a2a3",
                  profile_id=LINUX_PROFILE_ID, best_move_uci="c2c4",
                  best_move_san="c4", best_line_uci="c2c4 d5c4", best_eval=35)
    _seed(db_session, fen=GUL4P_FEN, move_uci="h2h3", profile_id=CANONICAL_PROFILE_ID,
          best_move_uci="g1f3", best_move_san="Nf3", best_line_uci="g1f3 g8f6",
          best_eval=28)

    stats = backfill_position_analysis(db_session, progress_every=0)

    row = _position_row(db_session, GUL4P_NF)
    assert row.best_move_uci == "c2c4"  # linux preferred
    assert row.source_cache_id == linux.id
    assert stats.conflicts_recorded == 1
    conflicts = _conflicts(db_session, GUL4P_NF)
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.policy_reason == "conflict_best_known_kept"
    assert conflict.position_analysis_id == row.id
    assert sorted(json.loads(conflict.best_move_disagreement)) == ["c2c4", "g1f3"]
    # CP-only audit axis is captured too, but it did not trigger the conflict.
    assert sorted(json.loads(conflict.best_eval_disagreement)) == [28, 35]


def test_cp_only_difference_records_no_conflict(db_session):
    # Same best move, different CP eval -> one winner, NO conflict.
    _seed(db_session, fen=GUL4P_FEN, move_uci="a2a3", profile_id=LINUX_PROFILE_ID,
          best_move_uci="c2c4", best_move_san="c4", best_line_uci="c2c4 d5c4",
          best_eval=35)
    _seed(db_session, fen=GUL4P_FEN, move_uci="h2h3", profile_id=CANONICAL_PROFILE_ID,
          best_move_uci="c2c4", best_move_san="c4", best_line_uci="c2c4 d5c4",
          best_eval=80)

    stats = backfill_position_analysis(db_session, progress_every=0)

    assert stats.winners_inserted == 1
    assert stats.conflicts_recorded == 0
    assert _conflicts(db_session, GUL4P_NF) == []


# --- Idempotency / safety ------------------------------------------------------


def test_rerun_is_idempotent(db_session):
    # One clean group + one conflicting group.
    _seed(db_session, fen=GUL4P_FEN, move_uci="c1f4", profile_id=LINUX_PROFILE_ID,
          best_move_uci="c2c4", best_move_san="c4", best_line_uci="c2c4 d5c4",
          best_eval=35)
    _seed(db_session, fen=START_FEN, move_uci="e2e4", profile_id=LINUX_PROFILE_ID,
          best_move_uci="e2e4", best_move_san="e4", best_line_uci="e2e4 e7e5",
          best_eval=20)
    _seed(db_session, fen=START_FEN, move_uci="d2d4", profile_id=CANONICAL_PROFILE_ID,
          best_move_uci="d2d4", best_move_san="d4", best_line_uci="d2d4 d7d5",
          best_eval=18)

    first = backfill_position_analysis(db_session, progress_every=0)
    assert first.winners_inserted == 2
    assert first.conflicts_recorded == 1
    before = {
        r.normalized_fen: r.updated_at
        for r in db_session.query(PositionAnalysisRow).all()
    }

    second = backfill_position_analysis(db_session, progress_every=0)

    assert second.groups_scanned == 2
    assert second.winners_unchanged == 2
    assert second.winners_updated == 0
    assert second.winners_inserted == 0
    assert second.conflicts_recorded == 0
    assert second.conflicts_skipped_duplicate == 1
    # No new conflict rows and updated_at did not churn.
    assert db_session.query(PositionAnalysisConflict).count() == 1
    after = {
        r.normalized_fen: r.updated_at
        for r in db_session.query(PositionAnalysisRow).all()
    }
    assert after == before


def test_native_row_is_protected(db_session):
    # A future Phase-3 native write (source_cache_id IS NULL) is never clobbered.
    native = PositionAnalysisRow(
        normalized_fen=GUL4P_NF,
        fen=GUL4P_FEN,
        best_move_uci="e2e4",
        source="precomputed",
        source_cache_id=None,
    )
    db_session.add(native)
    db_session.commit()
    _seed(db_session, fen=GUL4P_FEN, move_uci="c1f4", profile_id=LINUX_PROFILE_ID,
          best_move_uci="c2c4", best_move_san="c4", best_line_uci="c2c4 d5c4",
          best_eval=35)

    stats = backfill_position_analysis(db_session, progress_every=0)

    assert stats.skipped_existing_protected == 1
    assert stats.winners_inserted == 0
    assert stats.winners_updated == 0
    row = _position_row(db_session, GUL4P_NF)
    assert row.best_move_uci == "e2e4"  # untouched native pick
    assert row.source_cache_id is None


def test_backfill_owned_row_updates_forward(db_session):
    # Run 1 picks the only candidate as winner.
    first_row = _seed(db_session, fen=GUL4P_FEN, move_uci="c1f4",
                      profile_id=LINUX_PROFILE_ID, best_move_uci="c2c4",
                      best_move_san="c4", best_line_uci="c2c4 d5c4", best_eval=35)
    backfill_position_analysis(db_session, progress_every=0)
    assert _position_row(db_session, GUL4P_NF).source_cache_id == first_row.id

    # A newer same-profile sibling (higher cache_id) with a DIFFERENT best move
    # appears. Equal strength -> the higher-cache_id row wins the deterministic
    # tiebreak, so the backfill-owned row updates forward to the new winner.
    newer = _seed(db_session, fen=GUL4P_FEN, move_uci="g1e5",
                  profile_id=LINUX_PROFILE_ID, best_move_uci="g1f3",
                  best_move_san="Nf3", best_line_uci="g1f3 g8f6", best_eval=30)

    second = backfill_position_analysis(db_session, progress_every=0)

    assert second.winners_updated == 1
    assert second.winners_unchanged == 0
    row = _position_row(db_session, GUL4P_NF)
    assert row.best_move_uci == "g1f3"
    assert row.source_cache_id == newer.id
    # The new candidate set is a genuinely different disagreement -> appended.
    assert second.conflicts_recorded == 1


def test_dry_run_writes_nothing(db_session):
    _seed(db_session, fen=GUL4P_FEN, move_uci="c1f4", profile_id=LINUX_PROFILE_ID,
          best_move_uci="c2c4", best_move_san="c4", best_line_uci="c2c4 d5c4",
          best_eval=35)

    stats = backfill_position_analysis(db_session, progress_every=0, dry_run=True)

    assert stats.winners_inserted == 1  # work computed
    assert db_session.query(PositionAnalysisRow).count() == 0  # but rolled back
    assert db_session.query(PositionAnalysisConflict).count() == 0


# --- Eligibility filtering at the orchestration layer ---------------------------


def test_identity_mismatch_group_has_no_eligible_winner(db_session):
    # Claims the canonical profile + v2 contract (passes the SQL pre-filter) but a
    # tampered engine_build fails identity verification -> no eligible candidate.
    _seed(db_session, fen=GUL4P_FEN, move_uci="c1f4", profile_id=CANONICAL_PROFILE_ID,
          engine_build="tampered", best_move_uci="c2c4", best_move_san="c4",
          best_line_uci="c2c4 d5c4", best_eval=35)

    stats = backfill_position_analysis(db_session, progress_every=0)

    assert stats.groups_scanned == 1
    assert stats.skipped_no_eligible == 1
    assert stats.winners_inserted == 0
    assert _position_row(db_session, GUL4P_NF) is None


def test_targeted_normalized_fen_only_backfills_that_position(db_session):
    _seed(db_session, fen=GUL4P_FEN, move_uci="c1f4", profile_id=LINUX_PROFILE_ID,
          best_move_uci="c2c4", best_move_san="c4", best_line_uci="c2c4 d5c4",
          best_eval=35)
    _seed(db_session, fen=START_FEN, move_uci="e2e4", profile_id=LINUX_PROFILE_ID,
          best_move_uci="e2e4", best_move_san="e4", best_line_uci="e2e4 e7e5",
          best_eval=20)

    stats = backfill_position_analysis(
        db_session, normalized_fen=GUL4P_NF, progress_every=0
    )

    assert stats.groups_scanned == 1
    assert _position_row(db_session, GUL4P_NF) is not None
    assert _position_row(db_session, normalize_fen(START_FEN)) is None


def test_targeted_run_picks_up_legacy_null_normalized_row(db_session):
    # A legacy row with NULL normalized_fen_before whose fen_before normalizes to
    # the target. A targeted run must still find it (it is loaded via the OR-NULL
    # filter and grouped by the Python-computed normalized FEN).
    legacy = _seed(db_session, fen=GUL4P_FEN, move_uci="c1f4",
                   profile_id=LINUX_PROFILE_ID, normalized_fen_before=None,
                   best_move_uci="c2c4", best_move_san="c4",
                   best_line_uci="c2c4 d5c4", best_eval=35)
    # An unrelated legacy null row at a different position must NOT be persisted.
    _seed(db_session, fen=START_FEN, move_uci="e2e4", profile_id=LINUX_PROFILE_ID,
          normalized_fen_before=None, best_move_uci="e2e4", best_move_san="e4",
          best_line_uci="e2e4 e7e5", best_eval=20)

    stats = backfill_position_analysis(
        db_session, normalized_fen=GUL4P_NF, progress_every=0
    )

    assert stats.groups_scanned == 1
    row = _position_row(db_session, GUL4P_NF)
    assert row is not None and row.source_cache_id == legacy.id
    assert _position_row(db_session, normalize_fen(START_FEN)) is None


def test_limit_without_dry_run_is_rejected(db_session):
    # A row cap can load a partial candidate set (a NULL/non-NULL sibling may lie
    # beyond the cap), so a limited run must never persist.
    with pytest.raises(ValueError, match="limit requires dry_run"):
        backfill_position_analysis(db_session, limit=1, progress_every=0)


def test_limit_dry_run_persists_nothing(db_session):
    # Two siblings of one group; even with a row cap that could split it, the
    # dry-run rolls everything back so no partial winner is persisted.
    _seed(db_session, fen=GUL4P_FEN, move_uci="c1f4", profile_id=LINUX_PROFILE_ID,
          best_move_uci="c2c4", best_move_san="c4", best_line_uci="c2c4 d5c4",
          best_eval=35)
    _seed(db_session, fen=GUL4P_FEN, move_uci="g1e5", profile_id=LINUX_PROFILE_ID,
          best_move_uci="c2c4", best_move_san="c4", best_line_uci="c2c4 d5c4",
          best_eval=35)

    backfill_position_analysis(db_session, limit=1, dry_run=True, progress_every=0)

    assert db_session.query(PositionAnalysisRow).count() == 0
    assert db_session.query(PositionAnalysisConflict).count() == 0


def test_protected_native_row_conflict_records_null_winner_id(db_session):
    # A protected native row owns the FEN AND the cache candidates disagree: the
    # conflict is still audited, but its position_analysis_id is NULL (the backfill
    # selected no winner here) rather than pointing at the unrelated native row.
    native = PositionAnalysisRow(
        normalized_fen=GUL4P_NF,
        fen=GUL4P_FEN,
        best_move_uci="e2e4",
        source="precomputed",
        source_cache_id=None,
    )
    db_session.add(native)
    db_session.commit()
    _seed(db_session, fen=GUL4P_FEN, move_uci="a2a3", profile_id=LINUX_PROFILE_ID,
          best_move_uci="c2c4", best_move_san="c4", best_line_uci="c2c4 d5c4",
          best_eval=35)
    _seed(db_session, fen=GUL4P_FEN, move_uci="h2h3", profile_id=CANONICAL_PROFILE_ID,
          best_move_uci="g1f3", best_move_san="Nf3", best_line_uci="g1f3 g8f6",
          best_eval=28)

    stats = backfill_position_analysis(db_session, progress_every=0)

    assert stats.skipped_existing_protected == 1
    assert stats.conflicts_recorded == 1
    conflicts = _conflicts(db_session, GUL4P_NF)
    assert len(conflicts) == 1
    assert conflicts[0].position_analysis_id is None
    # Native row untouched.
    assert _position_row(db_session, GUL4P_NF).best_move_uci == "e2e4"
