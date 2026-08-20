"""Tests for provable async opening-baseline recovery (g-f3m4).

Two layers:

- ``run_baseline_snapshot_job`` (``app.opening_score_delta``): the DB-backed worker
  job that composes current-batch freshness with the durable start watermark.
- ``OpeningBaselineScheduler`` mechanics (coalescing, run_due, shutdown, best-effort
  facade): driven with fake sessions + a recording job, mirroring
  ``test_session_evidence_scheduler``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import app.opening_baseline_scheduler as baseline_mod
import app.opening_evidence as opening_evidence
import app.opening_score_delta as score_delta_mod
from conftest import TestingSessionLocal
from app.fen import normalize_fen
from app.models import (
    AnalysisCache,
    Blunder,
    BlunderReview,
    GameSession,
    OpeningScoreBatch,
    OpeningScoreBatchSharedScope,
    Position,
    SessionMove,
    SharedEvidenceScopeVersion,
    User,
    UserOpeningScore,
)
from app.opening_baseline_scheduler import (
    OpeningBaselineScheduler,
    enqueue_baseline_snapshot,
)
from app.opening_cache import (
    SCORE_MODEL_VERSION,
    bump_evidence_seq,
    capture_freshness_snapshot,
    opening_score_inputs_fingerprint,
)
from app.opening_graph import get_opening_graph
from app.opening_roots import OpeningRoot, OpeningRoots, get_opening_roots
from app.opening_rootcalc import SYNTHETIC_INITIAL_FEN, root_calc_config_fingerprint
from app.opening_transposition_artifact import EMPTY_DENSIFIED_EDGES
from app.opening_score_delta import (
    BASELINE_RETRYABLE_SOURCES,
    BASELINE_TERMINAL_SOURCES,
    BaselineWatermarkMismatch,
    BaselineSnapshotSource,
    _baseline_terminal_classification,
    capture_baseline_watermark,
    fill_opening_baselines_for_batch,
    run_baseline_snapshot_job,
    snapshot_opening_baseline,
)

# Fixed timestamps prove the new contract is independent of wall-clock ordering.
T_BEFORE = datetime(2026, 6, 1, tzinfo=timezone.utc)
T_START = datetime(2026, 6, 15, tzinfo=timezone.utc)
T_AFTER = datetime(2026, 6, 20, tzinfo=timezone.utc)


def _baseline_envelope(scores):
    return {
        "schema_version": 2,
        "model_version": SCORE_MODEL_VERSION,
        "root_calc_config_fingerprint": root_calc_config_fingerprint(),
        "routing_edge_fingerprint": EMPTY_DENSIFIED_EDGES.fingerprint,
        "scores": scores,
    }


# ---------------------------------------------------------------------------
# DB seeders
# ---------------------------------------------------------------------------
def _make_session(
    db, *, user_id=123, player_color="white", status="active",
    started_at=T_START, baseline=None, with_watermark=True,
) -> uuid.UUID:
    sid = uuid.uuid4()
    watermark = (
        capture_baseline_watermark(db, user_id, player_color)
        if with_watermark
        else None
    )
    watermark_values = watermark or (None, None, None)
    db.add(GameSession(
        id=sid, user_id=user_id, started_at=started_at, status=status,
        result="checkmate_win" if status == "ended" else None, engine_elo=1500,
        player_color=player_color, session_mode="normal",
        opening_score_baseline=baseline,
        baseline_watermark_seq=watermark_values[0],
        baseline_watermark_epoch=watermark_values[1],
        baseline_watermark_fingerprint=watermark_values[2],
    ))
    db.commit()
    return sid


def _seed_batch(
    db, *, user_id=123, player_color="white", computed_at, fresh=True, scores,
) -> int:
    """Seed a batch + score rows. ``fresh=True`` stamps the registry fingerprint
    AND the full g-jact freshness bundle ``_is_batch_fresh`` checks (a no-evidence
    user has a deterministic empty bundle); ``fresh=False`` leaves them mismatched
    so the batch is provably stale."""
    if fresh:
        registry_fp = opening_score_inputs_fingerprint(
            get_opening_graph(), get_opening_roots()
        )
        snap = capture_freshness_snapshot(db, user_id, player_color)
        batch = OpeningScoreBatch(
            user_id=user_id, player_color=player_color, generation=1,
            registry_fingerprint=registry_fp,
            inputs_fingerprint=snap.inputs_fingerprint,
            evidence_seq=snap.evidence_seq,
            cache_epoch=snap.cache_epoch,
            scoped_shared_digest=snap.scoped_shared_digest,
            computed_at=computed_at,
        )
    else:
        batch = OpeningScoreBatch(
            user_id=user_id, player_color=player_color, generation=1,
            registry_fingerprint="stale-registry-fp", inputs_fingerprint=None,
            computed_at=computed_at,
        )
    db.add(batch)
    db.flush()
    if fresh:
        db.add_all(
            [
                OpeningScoreBatchSharedScope(
                    batch_id=batch.id, kind="raw", fen=fen
                )
                for fen in snap.shared_raw_fens
            ]
            + [
                OpeningScoreBatchSharedScope(
                    batch_id=batch.id, kind="norm", fen=fen
                )
                for fen in snap.shared_norm_fens
            ]
        )
    for key, score in scores.items():
        db.add(UserOpeningScore(
            batch_id=batch.id, user_id=user_id, player_color=player_color,
            opening_key=key, opening_name="x", opening_family="x",
            opening_score=score, confidence=0.5, coverage=0.5, weighted_depth=1.0,
            sample_size=5, computed_at=computed_at,
        ))
    db.commit()
    return batch.id


def _seed_position(db, *, user_id=123) -> int:
    pos = Position(
        user_id=user_id, fen_hash=f"h{uuid.uuid4().hex[:12]}", fen_raw="fen",
        active_color="white",
    )
    db.add(pos)
    db.flush()
    return pos.id


def _seed_blunder(db, *, user_id=123, source_session_id=None) -> int:
    pos_id = _seed_position(db, user_id=user_id)
    blunder = Blunder(
        user_id=user_id, position_id=pos_id, bad_move_san="a", best_move_san="b",
        eval_loss_cp=100, source_session_id=source_session_id,
    )
    db.add(blunder)
    db.flush()
    return blunder.id


def _seed_review(db, *, session_id, user_id=123) -> None:
    blunder_id = _seed_blunder(db, user_id=user_id)
    db.add(BlunderReview(
        blunder_id=blunder_id, session_id=session_id, passed=True,
        move_played_san="a", eval_delta_cp=0,
    ))
    db.commit()


def _seed_move(db, *, session_id, with_fen_before=True) -> None:
    db.add(SessionMove(
        session_id=session_id, move_number=1, color="white", move_san="e4",
        fen_before=(
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
            if with_fen_before else None
        ),
        fen_after="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        segment="normal",
    ))
    db.commit()


def _baseline(db, sid) -> str | None:
    db.expire_all()
    return db.get(GameSession, sid).opening_score_baseline


def _seed_candidate_history(db) -> None:
    prior = _make_session(db, status="ended", started_at=T_BEFORE)
    _seed_move(db, session_id=prior)
    bump_evidence_seq(db, 123, "white")
    db.commit()


# ---------------------------------------------------------------------------
# run_baseline_snapshot_job — capture logic
# ---------------------------------------------------------------------------
def test_later_fresh_batch_is_persisted_when_inputs_unchanged(db_session):
    # A batch built after start is valid when both proofs bind it to start state.
    sid = _make_session(db_session)
    _seed_batch(db_session, computed_at=T_AFTER, fresh=True, scores={"k": 42.0})

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "cached_fresh"
    assert json.loads(_baseline(db_session, sid)) == _baseline_envelope({"k": 42.0})


def test_digest_ineligible_active_move_does_not_prevent_fill(db_session):
    # Active session moves are not yet digest-visible and therefore do not bump
    # evidence_seq. Their mere presence must not recreate the removed NOT EXISTS.
    sid = _make_session(db_session)
    _seed_move(db_session, session_id=sid)
    _seed_batch(db_session, computed_at=T_AFTER, fresh=True, scores={"k": 42.0})

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "cached_fresh"
    assert json.loads(_baseline(db_session, sid)) == _baseline_envelope({"k": 42.0})


@pytest.mark.parametrize("evidence", ["review", "ghost_target"])
def test_digest_visible_per_user_change_rejects_later_batch(db_session, evidence):
    sid = _make_session(db_session)
    if evidence == "review":
        _seed_review(db_session, session_id=sid)
    else:
        _seed_blunder(db_session, source_session_id=sid)
        db_session.commit()
    # The real writers perform this bump in the source transaction; focused API
    # tests cover those choke points. This unit isolates the historical proof.
    bump_evidence_seq(db_session, 123, "white")
    db_session.commit()
    _seed_batch(db_session, computed_at=T_AFTER, fresh=True, scores={"k": 42.0})

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "watermark_mismatch"
    assert _baseline(db_session, sid) is None


def test_brand_new_user_persists_empty_baseline(db_session):
    # No batch, no evidence -> a valid empty envelope (empty_no_evidence),
    # so the session's first openings later read as new.
    sid = _make_session(db_session)

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "empty_no_evidence"
    assert json.loads(_baseline(db_session, sid)) == _baseline_envelope({})


def test_already_set_baseline_is_a_noop(db_session):
    # An already-set baseline is idempotent -> already_set, value untouched.
    sid = _make_session(db_session, baseline=json.dumps({"x": 1.0}))
    _seed_batch(db_session, computed_at=T_BEFORE, fresh=True, scores={"k": 42.0})

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "already_set"
    assert json.loads(_baseline(db_session, sid)) == {"x": 1.0}


def test_conditional_update_race_leaves_baseline_null(db_session):
    sid = _make_session(db_session)
    _seed_batch(db_session, computed_at=T_AFTER, fresh=True, scores={"k": 42.0})

    with patch(
        "app.opening_score_delta._conditional_store_baseline",
        return_value=False,
    ):
        source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "raced_evidence_or_already_set"
    assert _baseline(db_session, sid) is None


def test_cold_cache_with_evidence_skipped(db_session):
    # No batch but the user has evidence (cold, e.g. post-restart) -> NULL,
    # skipped_cold. Seed evidence via a prior ended session's move.
    prior = _make_session(db_session, status="ended", started_at=T_BEFORE)
    _seed_move(db_session, session_id=prior)
    sid = _make_session(db_session)

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "skipped_cold"
    assert _baseline(db_session, sid) is None


def test_stale_pre_session_batch_skipped(db_session):
    # A batch whose fingerprints don't match is retryable stale.
    sid = _make_session(db_session)
    _seed_batch(db_session, computed_at=T_BEFORE, fresh=False, scores={"k": 42.0})

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "skipped_stale"
    assert _baseline(db_session, sid) is None


def test_computed_at_equality_no_longer_controls_acceptance(db_session):
    sid = _make_session(db_session)
    started = db_session.get(GameSession, sid).started_at
    _seed_batch(db_session, computed_at=started, fresh=True, scores={"k": 7.0})

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "cached_fresh"
    assert json.loads(_baseline(db_session, sid)) == _baseline_envelope({"k": 7.0})


def test_legacy_session_without_watermark_is_terminal(db_session):
    sid = _make_session(db_session, with_watermark=False)
    assert run_baseline_snapshot_job(db_session, sid, 123, "white") == "watermark_missing"
    assert _baseline(db_session, sid) is None


def test_missing_session_returns_missing(db_session):
    source = run_baseline_snapshot_job(db_session, uuid.uuid4(), 123, "white")
    assert source == "missing_session"


def test_not_active_session_skipped(db_session, caplog):
    sid = _make_session(db_session, status="ended")
    with caplog.at_level(logging.INFO, logger="app.opening_score_delta"):
        source = run_baseline_snapshot_job(db_session, sid, 123, "white")
    assert source == "not_active"
    completion = next(
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("opening_baseline_job ")
    )
    assert "terminal_session_classification=supported_game_end" in completion


def test_failed_job_does_not_read_expired_session_for_completion_log(
    monkeypatch, caplog
):
    class RollbackAwareDb:
        rolled_back = False

        def get(self, model, session_id):
            return session

        def rollback(self):
            self.rolled_back = True

    class RollbackAwareSession:
        opening_score_baseline = None
        baseline_watermark_seq = 1
        baseline_watermark_epoch = 2
        baseline_watermark_fingerprint = "fp"
        user_id = 123
        player_color = "white"
        session_mode = "normal"
        drill_terminal_reason = None

        def _loaded(self, value):
            if db.rolled_back:
                raise RuntimeError("expired ORM state read after rollback")
            return value

        @property
        def result(self):
            return self._loaded(None)

        @property
        def drill_state(self):
            return self._loaded(None)

        @property
        def status(self):
            return self._loaded("active")

    db = RollbackAwareDb()
    session = RollbackAwareSession()

    def fail_cached_scores(*args, **kwargs):
        raise RuntimeError("database failed")

    monkeypatch.setattr(
        score_delta_mod,
        "list_cached_opening_scores",
        fail_cached_scores,
    )

    with caplog.at_level(logging.INFO, logger="app.opening_score_delta"):
        source = run_baseline_snapshot_job(db, uuid.uuid4(), 123, "white")

    assert source == "failed"
    assert db.rolled_back is True
    completion = next(
        record.getMessage()
        for record in caplog.records
        if "terminal_session_classification=" in record.getMessage()
    )
    assert "terminal_session_classification=active" in completion


def test_failed_classification_is_closed_and_cannot_escape_job(monkeypatch, caplog):
    class FakeDb:
        def get(self, model, session_id):
            return SimpleNamespace(
                opening_score_baseline=None,
                status="ended",
                user_id=123,
                player_color="white",
            )

        def rollback(self):
            pass

    monkeypatch.setattr(
        score_delta_mod,
        "_baseline_terminal_classification",
        lambda *args: (_ for _ in ()).throw(RuntimeError("classification failed")),
    )

    with caplog.at_level(logging.INFO, logger="app.opening_score_delta"):
        source = run_baseline_snapshot_job(FakeDb(), uuid.uuid4(), 123, "white")

    assert source == "not_active"
    completion = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("opening_baseline_job ")
    )
    assert "terminal_session_classification=classification_failed" in completion


def test_untrusted_queued_identity_captures_from_the_row(db_session):
    # (11) The queued (user_id, player_color) is an untrusted routing hint. Seed the
    # session owner (123/white) with their own fresh pre-session batch, and a
    # DIFFERENT user/color (999/black) with theirs. A job carrying the WRONG pair
    # must NOT capture the other user's scores; a correct-pair job captures the
    # session owner's batch — proving capture keys off the ROW, not the payload.
    sid = _make_session(db_session, user_id=123, player_color="white")
    _seed_batch(
        db_session, user_id=123, player_color="white", computed_at=T_BEFORE,
        fresh=True, scores={"owner": 42.0},
    )
    _seed_batch(
        db_session, user_id=999, player_color="black", computed_at=T_BEFORE,
        fresh=True, scores={"other": 99.0},
    )

    mismatch = run_baseline_snapshot_job(db_session, sid, 999, "black")
    assert mismatch == "session_mismatch"
    assert _baseline(db_session, sid) is None

    ok = run_baseline_snapshot_job(db_session, sid, 123, "white")
    assert ok == "cached_fresh"
    assert json.loads(_baseline(db_session, sid)) == _baseline_envelope(
        {"owner": 42.0}
    )


def test_source_classification_is_closed_and_disjoint():
    assert BASELINE_RETRYABLE_SOURCES.isdisjoint(BASELINE_TERMINAL_SOURCES)
    assert BASELINE_RETRYABLE_SOURCES | BASELINE_TERMINAL_SOURCES == frozenset(
        BaselineSnapshotSource
    )


def test_durable_batch_push_fill_recovers_a_cold_start(db_session):
    sid = _make_session(db_session)
    batch_id = _seed_batch(
        db_session,
        computed_at=T_AFTER,
        fresh=True,
        scores={"k": 42.0},
    )

    filled = fill_opening_baselines_for_batch(
        batch_id,
        session_factory=TestingSessionLocal,
    )

    assert filled == 1
    assert json.loads(_baseline(db_session, sid)) == _baseline_envelope({"k": 42.0})


@pytest.mark.parametrize(
    "scores",
    [{}, {SYNTHETIC_INITIAL_FEN: 42.0}],
    ids=["zero-rows", "synthetic-only"],
)
def test_evidence_backed_empty_batch_suppresses_all_baseline_capture_paths(
    db_session, scores
):
    # This persisted shape covers both an all-quarantined generation and the
    # intentionally indistinguishable clean rootless-evidence case.
    _seed_candidate_history(db_session)
    sid = _make_session(db_session)
    batch_id = _seed_batch(
        db_session,
        computed_at=T_AFTER,
        fresh=True,
        scores=scores,
    )

    assert snapshot_opening_baseline(db_session, 123, "white") is None
    source = run_baseline_snapshot_job(db_session, sid, 123, "white")
    filled = fill_opening_baselines_for_batch(
        batch_id,
        session_factory=TestingSessionLocal,
    )

    assert source == "skipped_quarantined_empty"
    assert filled == 0
    assert _baseline(db_session, sid) is None


def test_partial_surviving_root_still_captures_baseline(db_session):
    _seed_candidate_history(db_session)
    sid = _make_session(db_session)
    _seed_batch(
        db_session,
        computed_at=T_AFTER,
        fresh=True,
        scores={"survivor": 42.0},
    )

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "cached_fresh"
    assert json.loads(_baseline(db_session, sid)) == _baseline_envelope(
        {"survivor": 42.0}
    )


def test_unrelated_shared_write_after_start_still_accepts(db_session):
    _seed_candidate_history(db_session)
    sid = _make_session(db_session)
    db_session.execute(
        text(
            "INSERT INTO analysis_cache "
            "(fen_before, move_uci, move_san, played_eval) "
            "VALUES ('8/8/8/8/8/8/8/8 w - - 0 1', 'a2a3', 'a3', 1)"
        )
    )
    db_session.commit()
    _seed_batch(db_session, computed_at=T_AFTER, fresh=True, scores={"k": 42.0})

    assert run_baseline_snapshot_job(db_session, sid, 123, "white") == "cached_fresh"


@pytest.mark.parametrize("revert", [False, True], ids=["changed", "changed_then_reverted"])
def test_in_scope_shared_history_after_start_rejects(db_session, revert):
    _seed_candidate_history(db_session)
    sid = _make_session(db_session)
    start_full = (
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/"
        "RNBQKBNR w KQkq - 0 1"
    )
    db_session.execute(
        text(
            "INSERT INTO analysis_cache "
            "(fen_before, move_uci, move_san, played_eval) "
            "VALUES (:fen, 'e2e4', 'e4', 1)"
        ),
        {"fen": start_full},
    )
    db_session.commit()
    if revert:
        db_session.execute(
            text("DELETE FROM analysis_cache WHERE fen_before = :fen"),
            {"fen": start_full},
        )
        db_session.commit()
    _seed_batch(db_session, computed_at=T_AFTER, fresh=True, scores={"k": 42.0})

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "watermark_mismatch"
    assert _baseline(db_session, sid) is None


def test_viewer_association_change_marks_batch_scope_and_rejects(db_session):
    """An eligibility-only write is shared evidence, not metadata.

    The real claim writer stores the parent before the start watermark, then an
    idempotent resubmission adds only its viewer association afterward. Both digest
    projections and both exact scope kinds move while every evidence column remains
    byte-identical; a fresh later batch must still fail Proof 2.
    """
    from app.analysis_cache_policy import Reason
    from app.analysis_cache_repo import write_analysis_cache_rows
    from app.analysis_profiles import (
        BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
        stamp_profile_full,
    )

    _seed_candidate_history(db_session)
    start_full = (
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/"
        "RNBQKBNR w KQkq - 0 1"
    )
    user = db_session.get(User, 123)
    if user is None:
        db_session.add(User(id=123, username="baseline-association-user"))
    db_session.commit()

    browser_row = {
        "fen_before": start_full,
        "move_uci": "e2e4",
        "move_san": "e4",
        "best_move_uci": "d2d4",
        "best_move_san": "d4",
        "best_line_uci": "d2d4 d7d5",
        "played_eval": -30,
        "best_eval": 20,
        "eval_delta": 50,
        "classification": "good",
        "source": "analysis",
        "analysis_profile_id": BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
        "evidence_contract_id": "resolver-complete-v2",
        **stamp_profile_full(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID),
    }
    write_analysis_cache_rows(db_session, [browser_row])
    db_session.expire_all()
    cache = db_session.query(AnalysisCache).filter_by(
        fen_before=start_full,
        move_uci="e2e4",
    ).one()
    evidence_columns = {
        column: getattr(cache, column)
        for column in (
            "played_eval",
            "best_eval",
            "eval_delta",
            "classification",
            "best_move_uci",
            "best_line_uci",
        )
    }
    before = opening_evidence.raw_evidence_inputs_snapshot(
        db_session, 123, "white"
    )

    sid = _make_session(db_session)
    start_epoch = db_session.get(GameSession, sid).baseline_watermark_epoch
    cache_id = cache.id
    db_session.rollback()
    results = write_analysis_cache_rows(
        db_session,
        [browser_row],
        submitter_user_id=123,
    )
    assert [reason for _, reason in results] == [Reason.SAME_PROFILE_IDEMPOTENT]
    db_session.expire_all()
    refreshed = db_session.get(AnalysisCache, cache_id)
    assert {
        column: getattr(refreshed, column) for column in evidence_columns
    } == evidence_columns

    after = opening_evidence.raw_evidence_inputs_snapshot(db_session, 123, "white")
    assert after.digest != before.digest
    assert after.scoped_shared_digest != before.scoped_shared_digest
    versions = db_session.query(SharedEvidenceScopeVersion).filter(
        SharedEvidenceScopeVersion.fen.in_(
            [start_full, normalize_fen(start_full)]
        )
    ).all()
    assert {(row.kind, row.fen) for row in versions} == {
        ("raw", start_full),
        ("norm", normalize_fen(start_full)),
    }
    assert all(row.last_changed_epoch > start_epoch for row in versions)

    _seed_batch(db_session, computed_at=T_AFTER, fresh=True, scores={"k": 42.0})

    source = run_baseline_snapshot_job(db_session, sid, 123, "white")

    assert source == "watermark_mismatch"
    assert _baseline(db_session, sid) is None


def test_s0_s1_lower_bound_batch_is_rejected_by_proof_one(db_session):
    sid = _make_session(db_session)
    bump_evidence_seq(db_session, 123, "white")
    db_session.commit()
    batch_id = _seed_batch(
        db_session, computed_at=T_AFTER, fresh=True, scores={"k": 42.0}
    )
    # Model a batch stamped at S0 that read evidence committed at S1.
    batch = db_session.get(OpeningScoreBatch, batch_id)
    batch.evidence_seq = 0
    db_session.commit()

    assert run_baseline_snapshot_job(db_session, sid, 123, "white") == "skipped_stale"


def test_empty_start_rejects_evidence_that_appeared_then_disappeared(db_session):
    sid = _make_session(db_session)
    prior = _make_session(db_session, status="ended", started_at=T_BEFORE)
    _seed_move(db_session, session_id=prior)
    db_session.query(SessionMove).filter(SessionMove.session_id == prior).delete()
    bump_evidence_seq(db_session, 123, "white")
    db_session.commit()

    assert run_baseline_snapshot_job(db_session, sid, 123, "white") == "watermark_mismatch"
    assert _baseline(db_session, sid) is None


@pytest.mark.parametrize(
    "seq,epoch,fingerprint",
    [
        (1, None, None),
        (None, 1, None),
        (None, None, "fp"),
        (1, 1, None),
        (1, None, "fp"),
        (None, 1, "fp"),
    ],
)
def test_partial_watermark_combinations_violate_check(
    db_session, seq, epoch, fingerprint
):
    sid = _make_session(db_session, with_watermark=False)
    session = db_session.get(GameSession, sid)
    session.baseline_watermark_seq = seq
    session.baseline_watermark_epoch = epoch
    session.baseline_watermark_fingerprint = fingerprint

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------------
# OpeningBaselineScheduler mechanics (fake sessions + recording job)
# ---------------------------------------------------------------------------
class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _RecordingJob:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.sessions: list[object] = []

    def __call__(self, db, **kwargs):
        self.sessions.append(db)
        self.calls.append(kwargs)
        return "already_set"


def _make_scheduler(run_job=None, **kwargs):
    sessions: list[_FakeSession] = []

    def factory():
        s = _FakeSession()
        sessions.append(s)
        return s

    params = {"auto_start": False}
    params.update(kwargs)
    sched = OpeningBaselineScheduler(
        session_factory=factory, run_job=run_job or _RecordingJob(), **params
    )
    return sched, sessions


def test_run_due_runs_job_once_with_fresh_session():
    job = _RecordingJob()
    sched, sessions = _make_scheduler(run_job=job)
    sid = uuid.uuid4()

    sched.enqueue(sid, 7, "white")
    sched.run_due()

    assert job.calls == [{"session_id": sid, "user_id": 7, "player_color": "white"}]
    assert len(sessions) == 1
    assert sessions[0].closed is True


def test_duplicate_enqueues_for_same_session_coalesce():
    job = _RecordingJob()
    sched, _ = _make_scheduler(run_job=job)
    sid = uuid.uuid4()

    sched.enqueue(sid, 7, "white")
    sched.enqueue(sid, 7, "white")
    sched.enqueue(sid, 7, "white")
    sched.run_due()

    assert len(job.calls) == 1


@pytest.mark.parametrize(
    ("attempts", "expected_bucket"),
    [(0, "0"), (1, "1"), (2, "2_3"), (3, "2_3"), (4, "4_7"), (7, "4_7"), (8, "8_plus")],
)
def test_probe_snapshots_pending_attempts_without_mutating_queue(
    attempts, expected_bucket
):
    clock = _FakeClock()
    sched, _ = _make_scheduler(clock=clock)
    sid = uuid.uuid4()
    sched.enqueue(sid, 7, "white")
    sched._pending[sid].attempts = attempts
    before = vars(sched._pending[sid]).copy()

    probe = sched.probe(sid)

    assert probe.state is baseline_mod.BaselineSchedulerState.PENDING
    assert probe.attempts_bucket == expected_bucket
    assert vars(sched._pending[sid]) == before
    assert sched._thread is None


def test_probe_reports_inflight_without_waking_or_requeueing():
    started = threading.Event()
    release = threading.Event()

    def blocking_job(db, **kwargs):
        started.set()
        assert release.wait(timeout=5.0)
        return "already_set"

    sched, _ = _make_scheduler(run_job=blocking_job)
    sid = uuid.uuid4()
    sched.enqueue(sid, 7, "white")
    runner = threading.Thread(target=sched.run_due)
    runner.start()
    assert started.wait(timeout=5.0)
    try:
        probe = sched.probe(sid)
        assert probe.state is baseline_mod.BaselineSchedulerState.INFLIGHT
        assert probe.attempts_bucket == "1"
        assert sid not in sched._pending
    finally:
        release.set()
        runner.join(timeout=5.0)


def test_terminal_observation_is_closed_and_registers_only_eligible_miss(monkeypatch):
    from app.opening_score_scheduler import ScoreSchedulerState, TerminalRecomputeProbe

    session = SimpleNamespace(
        id=uuid.uuid4(), user_id=7, player_color="white",
        started_at=datetime.now(timezone.utc) - timedelta(seconds=90),
        opening_score_baseline=None, baseline_watermark_seq=1,
        baseline_watermark_epoch=2, baseline_watermark_fingerprint="fp",
    )
    monkeypatch.setattr(
        baseline_mod,
        "probe_baseline_snapshot",
        lambda session_id: baseline_mod.BaselineSchedulerProbe(
            baseline_mod.BaselineSchedulerState.PENDING, "2_3"
        ),
    )
    registered = []

    def score_probe(user_id, player_color, *, register_convergence):
        registered.append((user_id, player_color, register_convergence))
        return TerminalRecomputeProbe(
            ScoreSchedulerState.PENDING,
            "opaque-probe" if register_convergence else None,
        )

    monkeypatch.setattr("app.opening_score_scheduler.probe_terminal_recompute", score_probe)
    properties = baseline_mod.terminal_baseline_observation(
        session, baseline_mod.TerminalKind.GAME_END
    )

    assert properties == {
        "opening_baseline_state": "missing_with_watermark",
        "opening_baseline_scheduler_state": "pending",
        "opening_baseline_attempts_bucket": "2_3",
        "opening_recompute_state": "pending",
        "terminal_kind": "game_end",
        "session_age_bucket": "1m_5m",
        "barrier_cohort": "disabled",
        "barrier_outcome": "disabled",
        "barrier_wait_budget_ms": 0,
        "barrier_wait_ms": 0,
        "convergence_probe_id": "opaque-probe",
    }
    assert registered == [(7, "white", True)]

    session.opening_score_baseline = "{}"
    properties = baseline_mod.terminal_baseline_observation(
        session, baseline_mod.TerminalKind.GAME_END
    )
    assert properties["opening_baseline_state"] == "present"
    assert "convergence_probe_id" not in properties
    assert registered[-1] == (7, "white", False)


def test_terminal_observation_marks_unmeasured_baseline_state(monkeypatch):
    class BrokenSession:
        @property
        def opening_score_baseline(self):
            raise RuntimeError("expired session")

    properties = baseline_mod.terminal_baseline_observation(
        BrokenSession(), baseline_mod.TerminalKind.GAME_END
    )

    assert properties["opening_baseline_state"] == "observation_failed"
    assert properties["opening_baseline_scheduler_state"] == "probe_failed"
    assert properties["opening_recompute_state"] == "probe_failed"
    assert "convergence_probe_id" not in properties


def test_terminal_observation_retains_registered_probe_on_later_failure(monkeypatch):
    from app.opening_score_scheduler import ScoreSchedulerState, TerminalRecomputeProbe

    class AgeFailureSession:
        id = uuid.uuid4()
        user_id = 7
        player_color = "white"
        opening_score_baseline = None
        baseline_watermark_seq = 1
        baseline_watermark_epoch = 2
        baseline_watermark_fingerprint = "fp"

        @property
        def started_at(self):
            raise RuntimeError("age unavailable")

    monkeypatch.setattr(
        baseline_mod,
        "probe_baseline_snapshot",
        lambda session_id: baseline_mod.BaselineSchedulerProbe(
            baseline_mod.BaselineSchedulerState.PENDING, "1"
        ),
    )
    monkeypatch.setattr(
        "app.opening_score_scheduler.probe_terminal_recompute",
        lambda *args, **kwargs: TerminalRecomputeProbe(
            ScoreSchedulerState.PENDING, "opaque-probe"
        ),
    )

    properties = baseline_mod.terminal_baseline_observation(
        AgeFailureSession(), baseline_mod.TerminalKind.GAME_END
    )

    assert properties["opening_baseline_state"] == "missing_with_watermark"
    assert properties["opening_baseline_scheduler_state"] == "probe_failed"
    assert properties["opening_recompute_state"] == "probe_failed"
    assert properties["convergence_probe_id"] == "opaque-probe"


@pytest.mark.parametrize(
    ("session", "mismatch", "expected"),
    [
        (None, None, "missing_session"),
        (SimpleNamespace(result="abandon", drill_state=None, session_mode="normal",
                         drill_terminal_reason=None, status="ended"), None, "abandon"),
        (SimpleNamespace(result=None, drill_state="failed", session_mode="drill",
                         drill_terminal_reason="accuracy", status="active"),
         BaselineWatermarkMismatch.SEQ, "supported_drill_accuracy_fail"),
        (SimpleNamespace(result=None, drill_state=None, session_mode="normal",
                         drill_terminal_reason=None, status="active"),
         BaselineWatermarkMismatch.SEQ, "unrelated_evidence_drift"),
    ],
)
def test_baseline_completion_terminal_classification_is_closed(
    session, mismatch, expected
):
    assert _baseline_terminal_classification(session, mismatch) == expected


def test_distinct_sessions_each_run_with_own_session():
    job = _RecordingJob()
    sched, sessions = _make_scheduler(run_job=job)
    sid1, sid2 = uuid.uuid4(), uuid.uuid4()

    sched.enqueue(sid1, 1, "white")
    sched.enqueue(sid2, 2, "black")
    sched.run_due()

    assert sorted(c["session_id"] for c in job.calls) == sorted([sid1, sid2])
    assert job.sessions[0] is not job.sessions[1]
    assert all(s.closed for s in sessions)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_retryable_outcome_uses_injected_backoff_then_converges():
    clock = _FakeClock()
    outcomes = iter(["raced_evidence_or_already_set", "cached_fresh"])

    def job(db, **kwargs):
        return next(outcomes)

    sched, _ = _make_scheduler(run_job=job, clock=clock)
    sid = uuid.uuid4()
    sched.enqueue(sid, 7, "white")

    sched.run_due()
    assert sched._pending[sid].attempts == 1
    assert sched._pending[sid].not_before == 101.0
    sched.run_due()
    assert sid in sched._pending

    clock.advance(1.0)
    sched.run_due()
    assert sid not in sched._pending


def test_cold_retry_requests_one_ordinary_recompute_when_none_scheduled():
    clock = _FakeClock()
    sched, _ = _make_scheduler(
        run_job=lambda db, **kwargs: "skipped_cold",
        clock=clock,
    )
    sid = uuid.uuid4()
    sched.enqueue(sid, 7, "black")

    with (
        patch(
            "app.opening_score_scheduler.is_recompute_scheduled",
            return_value=False,
        ),
        patch("app.opening_score_scheduler.request_recompute") as request,
    ):
        sched.run_due()

    from app.opening_score_scheduler import OpeningScoreTrigger

    request.assert_called_once_with(
        7,
        "black",
        source=OpeningScoreTrigger.BASELINE_RECOVERY,
    )


def test_quarantined_empty_source_is_terminal_without_recompute_or_retry():
    clock = _FakeClock()
    sched, _ = _make_scheduler(
        run_job=lambda db, **kwargs: "skipped_quarantined_empty",
        clock=clock,
    )
    sid = uuid.uuid4()
    sched.enqueue(sid, 7, "black")

    with (
        patch("app.opening_score_scheduler.is_recompute_scheduled") as scheduled,
        patch("app.opening_score_scheduler.request_recompute") as request,
    ):
        sched.run_due()

    assert sid not in sched._pending
    scheduled.assert_not_called()
    request.assert_not_called()


def test_retry_budget_stops_at_eight_attempts():
    clock = _FakeClock()
    calls = 0

    def job(db, **kwargs):
        nonlocal calls
        calls += 1
        return "raced_evidence_or_already_set"

    sched, _ = _make_scheduler(run_job=job, clock=clock)
    sid = uuid.uuid4()
    sched.enqueue(sid, 7, "white")
    for delay in (0, 1, 2, 4, 8, 16, 30, 30):
        clock.advance(delay)
        sched.run_due()

    assert calls == 8
    assert sid not in sched._pending


def test_attempt_log_reports_age_requeue_and_budget_exhaustion(caplog):
    clock = _FakeClock()
    sched, _ = _make_scheduler(
        run_job=lambda db, **kwargs: "raced_evidence_or_already_set",
        clock=clock,
    )
    sid = uuid.uuid4()
    sched.enqueue(sid, 7, "white")

    with caplog.at_level(logging.INFO, logger="app.opening_baseline_scheduler"):
        for delay in (0, 1, 2, 4, 8, 16, 30, 30):
            clock.advance(delay)
            sched.run_due()

    records = [
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("opening_baseline_scheduler_attempt ")
    ]
    assert len(records) == 8
    assert "attempt=1" in records[0]
    assert "enqueue_age_ms=0.0" in records[0]
    assert "requeued=True" in records[0]
    assert "retry_budget_exhausted=False" in records[0]
    assert "attempt=8" in records[-1]
    assert "requeued=False" in records[-1]
    assert "retry_budget_exhausted=True" in records[-1]


def test_elapsed_budget_can_stop_before_second_attempt():
    clock = _FakeClock()

    def job(db, **kwargs):
        clock.advance(120.0)
        return "raced_evidence_or_already_set"

    sched, _ = _make_scheduler(run_job=job, clock=clock)
    sid = uuid.uuid4()
    sched.enqueue(sid, 7, "white")
    sched.run_due(now=100.0)

    assert sid not in sched._pending


def test_duplicate_during_backoff_preserves_budget_and_deadline():
    clock = _FakeClock()
    sched, _ = _make_scheduler(
        run_job=lambda db, **kwargs: "raced_evidence_or_already_set",
        clock=clock,
    )
    sid = uuid.uuid4()
    sched.enqueue(sid, 7, "white")
    sched.run_due()
    first = sched._pending[sid].first_enqueued_at
    deadline = sched._pending[sid].not_before

    clock.advance(0.5)
    sched.enqueue(sid, 7, "white")

    assert sched._pending[sid].first_enqueued_at == first
    assert sched._pending[sid].not_before == deadline
    assert sched._pending[sid].attempts == 1


def test_unknown_source_is_terminal_contract_error(caplog):
    sched, _ = _make_scheduler(run_job=lambda db, **kwargs: "future_source")
    sid = uuid.uuid4()
    sched.enqueue(sid, 7, "white")

    with caplog.at_level("ERROR", logger="app.opening_baseline_scheduler"):
        sched.run_due()

    assert sid not in sched._pending
    assert any("unknown source" in record.getMessage() for record in caplog.records)


def test_shutdown_drain_suppresses_retry_requeue():
    sched, _ = _make_scheduler(
        run_job=lambda db, **kwargs: "raced_evidence_or_already_set"
    )
    sid = uuid.uuid4()
    sched.enqueue(sid, 7, "white")
    sched.run_due()
    assert sid in sched._pending

    with sched._cond:
        sched._shutdown = True
    sched.run_due(now=float("inf"))

    assert sid not in sched._pending


def test_session_closed_when_job_raises_and_later_enqueue_runs():
    def boom(db, **kwargs):
        raise RuntimeError("job failed")

    sched, sessions = _make_scheduler(run_job=boom)
    sched.enqueue(uuid.uuid4(), 7, "white")
    # The failure is swallowed and the session still closed.
    sched.run_due()
    assert len(sessions) == 1
    assert sessions[0].closed is True

    # Not wedged: a later session still runs.
    sched.enqueue(uuid.uuid4(), 8, "black")
    sched.run_due()
    assert len(sessions) == 2
    assert sessions[1].closed is True


def test_enqueue_swallows_auto_start_failure(monkeypatch):
    sched, _ = _make_scheduler(auto_start=True)

    def boom(self):
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(OpeningBaselineScheduler, "start", boom)
    # enqueue swallows the start failure internally (no propagation).
    sched.enqueue(uuid.uuid4(), 1, "white")


def test_daemon_containment_leaves_the_singleton_reusable():
    """``conftest.stop_scheduler_daemon`` must stop the worker AND unlatch it.

    Stopping is what keeps a leaked daemon from writing the shared database for the
    rest of the run; unlatching is what keeps that containment from silently breaking
    every later enqueue. ``shutdown()`` alone fails the second half: ``enqueue``
    returns on ``_shutdown`` before it ever reaches ``start()`` — proven on a throwaway
    instance here rather than by restarting the real singleton, which would run real
    jobs against the configured database (g-rating-serialize-flake).
    """
    from conftest import stop_scheduler_daemon

    def _reusable(sched, ran: threading.Event) -> None:
        """Enqueue and require the job to actually RUN.

        Deliberately NOT an assertion on ``_pending``: unlatching restores
        auto-start, so the worker legitimately races the test to drain the entry
        and a non-empty ``_pending`` is not something this test may rely on. The
        job having run is the outcome that is both stable and the one we mean.
        """
        sched.enqueue(uuid.uuid4(), 1, "white")
        try:
            assert ran.wait(timeout=5.0), "a contained scheduler dropped a later enqueue"
        finally:
            sched.shutdown(drain=False, timeout=5.0)

    # The half that is easy to get wrong: a latched scheduler accepts nothing.
    # auto_start=False so no worker can exist at all — the empty ``_pending`` is
    # then unambiguously the early return on ``_shutdown``, never a drain.
    latched, _ = _make_scheduler(auto_start=False)
    latched.shutdown(drain=False, timeout=5.0)
    latched.enqueue(uuid.uuid4(), 1, "white")
    with latched._cond:
        assert latched._pending == {}, "a latched scheduler silently dropped the enqueue"

    # Case 1: a LIVE daemon is stopped, unlatched, and still usable afterwards.
    live_ran = threading.Event()
    live, _ = _make_scheduler(run_job=lambda db, **kw: live_ran.set(), auto_start=True)
    live.start()
    assert live._thread is not None and live._thread.is_alive()

    assert stop_scheduler_daemon(live) is True
    assert live._thread is None          # the worker is gone...
    assert live._shutdown is False       # ...and the singleton is not latched
    _reusable(live, live_ran)

    # Case 2: ALREADY LATCHED with no live thread — the state a test leaves behind
    # when it runs app.main.lifespan for real (that start()s and shutdown()s the
    # baseline and evidence singletons). There is no daemon to join, so the helper
    # reports False, but it must STILL unlatch: otherwise every later facade call
    # silently no-ops for the rest of the run.
    dormant_ran = threading.Event()
    dormant, _ = _make_scheduler(
        run_job=lambda db, **kw: dormant_ran.set(), auto_start=True
    )
    dormant.shutdown(drain=False, timeout=5.0)
    assert dormant._thread is None and dormant._shutdown is True

    assert stop_scheduler_daemon(dormant) is False  # nothing was running...
    assert dormant._shutdown is False               # ...but it is no longer latched
    _reusable(dormant, dormant_ran)


def test_facade_swallows_enqueue_failure(monkeypatch):
    called: list[int] = []

    def boom(self, *args, **kwargs):
        called.append(1)
        raise RuntimeError("enqueue blew up")

    # Patch the CLASS, never the singleton INSTANCE. `monkeypatch.setattr(instance,
    # "enqueue", ...)` records the inherited BOUND METHOD as the old value (getattr
    # finds it on the class) and undo restores it as an INSTANCE attribute — which
    # then permanently shadows every later class-level patch, including the two
    # endpoint tests below. That is how the real baseline daemon escaped into the
    # rest of the run and started writing the shared database
    # (g-rating-serialize-flake).
    monkeypatch.setattr(OpeningBaselineScheduler, "enqueue", boom)
    # The module facade is best-effort: never propagates into /start.
    enqueue_baseline_snapshot(uuid.uuid4(), 1, "white")
    assert called, "the facade never reached the faulting enqueue"
    assert "enqueue" not in baseline_mod._scheduler.__dict__, (
        "the singleton kept an instance-level `enqueue`, which shadows class patches"
    )


def test_shutdown_drain_true_runs_pending():
    job = _RecordingJob()
    sched, sessions = _make_scheduler(run_job=job, auto_start=True)
    sched.start()
    sid = uuid.uuid4()
    sched.enqueue(sid, 1, "white")
    sched.shutdown(drain=True, timeout=5.0)

    assert len(job.calls) == 1
    assert job.calls[0]["session_id"] == sid
    assert all(s.closed for s in sessions)


def test_shutdown_drain_does_not_lose_notify_between_worker_iterations(monkeypatch):
    job = _RecordingJob()
    sched, _ = _make_scheduler(
        run_job=job,
        clock=time.monotonic,
        auto_start=True,
    )

    real_run_due = OpeningBaselineScheduler.run_due
    worker_between_iterations = threading.Event()
    release_worker = threading.Event()
    gate_first_run = True

    def gated_run_due(self, now=None):
        nonlocal gate_first_run
        if self is sched and gate_first_run:
            gate_first_run = False
            worker_between_iterations.set()
            assert release_worker.wait(timeout=5.0)
        return real_run_due(self, now)

    monkeypatch.setattr(OpeningBaselineScheduler, "run_due", gated_run_due)

    first_sid = uuid.uuid4()
    sched.enqueue(first_sid, 1, "white")
    assert worker_between_iterations.wait(timeout=5.0)

    second_sid = uuid.uuid4()
    sched.enqueue(second_sid, 2, "black")
    with sched._cond:
        sched._pending[second_sid].not_before = sched.clock() + 30.0

    shutdown_errors: list[BaseException] = []

    def shut_down():
        try:
            sched.shutdown(drain=True, timeout=0.5)
        except BaseException as exc:
            shutdown_errors.append(exc)

    shutdown_thread = threading.Thread(target=shut_down)
    shutdown_thread.start()

    with sched._cond:
        deadline = time.monotonic() + 5.0
        while not sched._shutdown:
            remaining = deadline - time.monotonic()
            assert remaining > 0
            sched._cond.wait(timeout=remaining)

    release_worker.set()
    shutdown_thread.join(timeout=2.0)

    if sched._thread is not None and sched._thread.is_alive():
        with sched._cond:
            for entry in sched._pending.values():
                entry.not_before = sched.clock()
            sched._cond.notify_all()
        sched._thread.join(timeout=2.0)
        sched.shutdown(drain=True, timeout=2.0)

    assert not shutdown_thread.is_alive()
    assert shutdown_errors == []
    assert sched._pending == {}
    assert sched._inflight == set()
    assert sched._thread is None
    assert [call["session_id"] for call in job.calls] == [first_sid, second_sid]


def test_shutdown_drain_defensively_parks_if_pending_key_is_marked_inflight(
    monkeypatch,
):
    job = _RecordingJob()
    sched, _ = _make_scheduler(run_job=job)
    sid = uuid.uuid4()
    sched.enqueue(sid, 1, "white")
    with sched._cond:
        sched._shutdown = True
        sched._inflight.add(sid)

    parked = threading.Event()
    real_wait = threading.Condition.wait

    def observed_wait(condition, timeout=None):
        if condition is sched._cond:
            assert timeout == 0.1
            parked.set()
        return real_wait(condition, timeout)

    monkeypatch.setattr(threading.Condition, "wait", observed_wait)
    worker = threading.Thread(target=sched._worker_loop)
    worker.start()
    assert parked.wait(timeout=5.0)
    assert job.calls == []

    with sched._cond:
        sched._inflight.discard(sid)
        sched._cond.notify_all()
    worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert [call["session_id"] for call in job.calls] == [sid]


def test_shutdown_drain_false_clears_pending():
    job = _RecordingJob()
    sched, _ = _make_scheduler(run_job=job, auto_start=False)
    sid = uuid.uuid4()
    sched.enqueue(sid, 1, "white")
    with sched._cond:
        assert sid in sched._pending

    sched.shutdown(drain=False, timeout=5.0)

    with sched._cond:
        assert not sched._pending
    assert job.calls == []


def test_thread_integration_real_worker_runs_job():
    done = threading.Event()
    seen: list[dict] = []

    def run_job(db, **kwargs):
        seen.append(kwargs)
        done.set()

    sched, sessions = _make_scheduler(run_job=run_job, auto_start=True)
    sched.start()
    sid = uuid.uuid4()
    try:
        sched.enqueue(sid, 42, "black")
        assert done.wait(timeout=5.0)
        assert len(seen) == 1
        assert seen[0]["session_id"] == sid
        assert seen[0]["user_id"] == 42
    finally:
        sched.shutdown(drain=True, timeout=5.0)
    assert all(s.closed for s in sessions)


# ---------------------------------------------------------------------------
# Endpoint best-effort: /start returns 201 even when the enqueue machinery faults
# ---------------------------------------------------------------------------
def test_game_start_returns_201_when_enqueue_faults(client, auth_headers, monkeypatch):
    # Bind the REAL facade into the endpoint (overriding the autouse no-op) and make
    # the underlying scheduler enqueue fault: the facade must swallow it so /start
    # still returns 201 (best-effort contract).
    called: list[int] = []

    def boom(self, *args, **kwargs):
        called.append(1)
        raise RuntimeError("scheduler blew up")

    monkeypatch.setattr(OpeningBaselineScheduler, "enqueue", boom)
    with patch(
        "app.api.game.enqueue_baseline_snapshot",
        baseline_mod.enqueue_baseline_snapshot,
    ):
        resp = client.post(
            "/api/game/start",
            json={"engine_elo": 1500, "player_color": "white"},
            headers=auth_headers(user_id=123),
        )
    assert resp.status_code == 201
    # Without this the test passes on a 201 that never faulted at all — which is
    # exactly what happened while an instance-level `enqueue` shadowed this patch,
    # letting /start spin up the REAL daemon instead (g-rating-serialize-flake).
    assert called, "the endpoint never reached the faulting enqueue"


def test_game_start_capture_failure_is_savepoint_isolated_and_still_enqueues(
    client, auth_headers, db_session
):
    enqueued = []

    def record_enqueue(*args):
        enqueued.append(args)

    with (
        patch(
            "app.opening_score_delta.opening_score_inputs_fingerprint",
            side_effect=RuntimeError("fingerprint failed"),
        ),
        patch("app.api.game.enqueue_baseline_snapshot", side_effect=record_enqueue),
    ):
        resp = client.post(
            "/api/game/start",
            json={"engine_elo": 1500, "player_color": "white"},
            headers=auth_headers(user_id=123),
        )

    assert resp.status_code == 201
    sid = uuid.UUID(resp.json()["session_id"])
    db_session.expire_all()
    session = db_session.get(GameSession, sid)
    assert (
        session.baseline_watermark_seq,
        session.baseline_watermark_epoch,
        session.baseline_watermark_fingerprint,
    ) == (None, None, None)
    assert enqueued


DRILL_ROOT_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"


def _drill_roots() -> OpeningRoots:
    root = OpeningRoot(
        opening_key=DRILL_ROOT_FEN, opening_name="King's Pawn Game",
        opening_family="King's Pawn Game", eco="B00", depth=1,
        parent_keys=frozenset(), child_keys=frozenset(),
    )
    return OpeningRoots({DRILL_ROOT_FEN: root}, {DRILL_ROOT_FEN: frozenset([DRILL_ROOT_FEN])})


def test_drill_start_returns_201_when_enqueue_faults(client, auth_headers, monkeypatch):
    called: list[int] = []

    def boom(self, *args, **kwargs):
        called.append(1)
        raise RuntimeError("scheduler blew up")

    monkeypatch.setattr(OpeningBaselineScheduler, "enqueue", boom)
    with (
        patch(
            "app.api.drills.enqueue_baseline_snapshot",
            baseline_mod.enqueue_baseline_snapshot,
        ),
        patch("app.api.drills.get_opening_roots", return_value=_drill_roots()),
    ):
        resp = client.post(
            "/api/drills/start",
            json={
                "opening_key": DRILL_ROOT_FEN, "player_color": "white",
                "engine_elo": 1500, "strictness": "standard",
            },
            headers=auth_headers(user_id=123),
        )
    assert resp.status_code == 201
    assert called, "the endpoint never reached the faulting enqueue"


def test_drill_start_capture_failure_is_savepoint_isolated_and_still_enqueues(
    client, auth_headers, db_session
):
    enqueued = []

    def record_enqueue(*args):
        enqueued.append(args)

    with (
        patch(
            "app.opening_score_delta.opening_score_inputs_fingerprint",
            side_effect=RuntimeError("fingerprint failed"),
        ),
        patch("app.api.drills.enqueue_baseline_snapshot", side_effect=record_enqueue),
        patch("app.api.drills.get_opening_roots", return_value=_drill_roots()),
    ):
        resp = client.post(
            "/api/drills/start",
            json={
                "opening_key": DRILL_ROOT_FEN,
                "player_color": "white",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(user_id=123),
        )

    assert resp.status_code == 201
    sid = uuid.UUID(resp.json()["session_id"])
    db_session.expire_all()
    session = db_session.get(GameSession, sid)
    assert (
        session.baseline_watermark_seq,
        session.baseline_watermark_epoch,
        session.baseline_watermark_fingerprint,
    ) == (None, None, None)
    assert enqueued
