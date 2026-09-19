from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import uuid

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app import srs_write_telemetry as telemetry
from app.api import session as session_api
from app.api.session import _compute_blunder_opportunity_events, SessionMovesRequest
from app.models import BlunderOpportunityEvent, SessionMove
from app.session_evidence_scheduler import SessionEvidenceScheduler
from scripts.recompute_srs_opportunities import (
    recompute_all_blunders, recompute_one_blunder, recompute_srs_opportunities,
)
from scripts.report_srs_writes import report
from test_srs_opportunity import _blunder, _position, _session


@pytest.fixture
def private_store(tmp_path, monkeypatch):
    # Synthetic fixtures may live under backend/.tmp (AGENTS.md fallback).
    # Production root validation remains covered independently below.
    monkeypatch.setattr(telemetry, "forbidden_worktree_roots", lambda: [])
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    monkeypatch.setenv(telemetry.ENV, str(directory))
    monkeypatch.setattr(telemetry, "_store", None)
    monkeypatch.setattr(telemetry, "_store_path", None)
    monkeypatch.setattr(telemetry, "_failures", 0)
    return telemetry.get_store()


def seed(db):
    session = _session(db, user_id=123,
                       started_at=datetime.now(timezone.utc) - timedelta(days=40))
    position = _position(db, user_id=123, active_color="white",
                         fen="8/8/8/8/8/8/K7/4k3 w - - 0 1")
    blunder = _blunder(db, user_id=123, position=position)
    db.add(BlunderOpportunityEvent(session_id=session.id, blunder_id=blunder.id,
                                  occurred_at=session.started_at, opportunity=True, reached=True))
    db.commit()
    return session, blunder, position


def compute(db, session):
    _compute_blunder_opportunity_events(db, session_id=session.id,
                                       user_id=123, player_color="white")


def test_root_commit_is_observed_after_durability_and_keeps_transaction_clean(
    private_store, db_session, monkeypatch,
):
    session, _, _ = seed(db_session)
    def clock(db):
        assert not db.in_transaction()
        with db.get_bind().connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM blunder_opportunity_events")) == 0
        return datetime(2026, 12, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(telemetry, "database_clock", clock)
    compute(db_session, session)
    assert [r["outcome"] for r in private_store.rows()] == ["pending"]
    db_session.commit()
    row, = private_store.rows()
    assert row["outcome"] == "committed" and row["wrote"] == 1
    assert row["database_at"] == "2026-12-01T00:00:00+00:00"
    assert not db_session.in_transaction()


def test_rollback_and_close_never_become_success(private_store, db_session):
    session, _, _ = seed(db_session)
    compute(db_session, session)
    db_session.rollback()
    assert db_session.query(BlunderOpportunityEvent).count() == 1
    compute(db_session, session)
    db_session.close()
    assert {r["outcome"] for r in private_store.rows()} == {"rolled_back", "abandoned"}
    assert report(private_store.rows())["sessions_with_successful_evidence_writes"] == 0


def test_commit_failure_is_ambiguous_not_success(private_store, db_session):
    session, _, _ = seed(db_session)
    compute(db_session, session)
    engine = db_session.get_bind()
    def fail_commit(conn):
        raise RuntimeError("synthetic commit acknowledgement failure")
    event.listen(engine, "commit", fail_commit)
    try:
        with pytest.raises(RuntimeError):
            db_session.commit()
    finally:
        event.remove(engine, "commit", fail_commit)
        db_session.rollback()
    row, = private_store.rows()
    assert row["outcome"] == "commit_unknown"


def test_missing_clock_and_sink_failure_preserve_writes(
    private_store, db_session, monkeypatch, caplog,
):
    session, _, _ = seed(db_session)
    def failed_clock(db):
        raise RuntimeError("SECRET MUST NEVER APPEAR")
    monkeypatch.setattr(telemetry, "database_clock", failed_clock)
    compute(db_session, session)
    db_session.commit()
    row, = private_store.rows()
    assert row["outcome"] == "committed" and row["database_at"] is None
    assert report(private_store.rows())["missing_completion_clocks"] == 1
    def failed_put(self, **kwargs):
        raise RuntimeError("SECRET MUST NEVER APPEAR")
    monkeypatch.setattr(telemetry.PrivateStore, "put", failed_put)
    compute(db_session, session)
    db_session.commit()
    assert db_session.query(BlunderOpportunityEvent).count() == 0
    assert "observation_missing" in caplog.text
    assert "SECRET" not in caplog.text and str(session.id) not in caplog.text


@pytest.mark.parametrize("mode", ["session", "all_sessions", "blunder", "all_blunders"])
def test_all_repair_modes_observe_actual_commits_and_precreation_deletes(
    private_store, db_session, mode,
):
    session, blunder, position = seed(db_session)
    db_session.add(SessionMove(session_id=session.id, move_number=1, color="white",
                               move_san="Ka2", fen_after=position.fen_raw))
    db_session.commit()
    if mode == "session":
        recompute_srs_opportunities(db_session, session_id=session.id, progress_every=0)
    elif mode == "all_sessions":
        recompute_srs_opportunities(db_session, progress_every=0)
    elif mode == "blunder":
        recompute_one_blunder(db_session, blunder_id=blunder.id, progress_every=0)
    else:
        recompute_all_blunders(db_session, progress_every=0)
    rows = private_store.rows()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "committed" and rows[0]["wrote"]
    assert json.loads(rows[0]["sources"]) == ["repair_" + mode]


def test_worker_coalesces_sources_and_reports_deferred_commit(private_store, db_session):
    session, _, _ = seed(db_session)
    scheduler = SessionEvidenceScheduler(
        session_factory=lambda: Session(db_session.get_bind()), auto_start=False,
        clock=lambda: 0,
    )
    scheduler.enqueue(session.id, 123, "white", [], run_opportunity=False,
                      sources={"upload_ordinary", "upload_legacy"})
    scheduler.enqueue(session.id, 123, "white", [], is_final=True,
                      sources={"upload_final", "upload_revised_line"})
    assert {r["outcome"] for r in private_store.rows()} == {"queued"}
    scheduler.run_due(now=100)
    rows = private_store.rows()
    assert {r["outcome"] for r in rows} == {"worker_finished", "committed"}
    committed = next(r for r in rows if r["outcome"] == "committed")
    assert set(json.loads(committed["sources"])) == {
        "upload_ordinary", "upload_legacy", "upload_final", "upload_revised_line",
    }
    assert report(rows)["missing_worker_observations"] == 0


def test_pending_drop_failure_and_frozen_attempt_remain_visible(private_store, db_session):
    session, _, _ = seed(db_session)
    scheduler = SessionEvidenceScheduler(auto_start=False)
    scheduler.enqueue(session.id, 123, "white", [])
    scheduler.shutdown(drain=False)
    scheduler.enqueue(session.id, 123, "white", [])
    telemetry.frozen_attempt(db_session, session)
    rows = private_store.rows()
    assert {r["outcome"] for r in rows} == {"dropped", "enqueue_rejected", "frozen"}
    assert db_session.query(BlunderOpportunityEvent).count() == 1
    assert report(rows)["candidates"][0]["frozen_late_attempts"] == 1


def test_private_expiry_does_not_extend_when_attempt_finishes(private_store, monkeypatch):
    monkeypatch.setattr(telemetry.time, "time", lambda: 1000)
    fields = dict(observation_id="test", session_id=uuid.uuid4(), sources={"worker"})
    private_store.put(**fields, outcome="pending")
    monkeypatch.setattr(telemetry.time, "time", lambda: 2000)
    private_store.put(**fields, outcome="committed")
    assert private_store.rows()[0]["expires_at"] == 1000 + telemetry.TTL_SECONDS
    assert private_store.prune(now=1000 + telemetry.TTL_SECONDS) == 1
    assert private_store.rows() == []
    assert private_store.path.stat().st_mode & 0o777 == 0o600


def test_private_path_rejects_public_permissions_and_worktrees(tmp_path):
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="0700"):
        telemetry.PrivateStore(public)
    with pytest.raises(ValueError, match="absolute"):
        telemetry.PrivateStore("relative/path")


def test_private_path_rejects_linked_worktree(tmp_path, monkeypatch):
    import subprocess
    directory = tmp_path / "linked"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(telemetry.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess([], 0, f"worktree {directory}\n", ""))
    with pytest.raises(ValueError, match="worktree"):
        telemetry.PrivateStore(directory)


def test_expired_inflight_attempt_cannot_recreate_private_row(private_store, monkeypatch):
    now = 1000
    monkeypatch.setattr(telemetry.time, "time", lambda: now)
    fields = dict(observation_id="test", session_id=uuid.uuid4(), sources={"worker"},
                  expires_at=1000 + telemetry.TTL_SECONDS)
    private_store.put(**fields, outcome="pending")
    now = 1001 + telemetry.TTL_SECONDS
    private_store.put(**fields, outcome="committed")
    assert private_store.rows() == []


def test_savepoint_rollback_is_not_a_successful_root_write(private_store, db_session):
    session, _, _ = seed(db_session)
    nested = db_session.begin_nested()
    compute(db_session, session)
    nested.rollback()
    db_session.commit()
    assert {row["outcome"] for row in private_store.rows()} == {"unsupported_savepoint"}
    assert db_session.query(BlunderOpportunityEvent).count() == 1


@pytest.mark.parametrize("exhausted", [False, True])
def test_retry_outcomes_and_drop_are_observed(private_store, db_session, monkeypatch, exhausted):
    from test_session_graph_lock import _operational_error
    session, _, _ = seed(db_session)
    attempts = 0
    def run(db, **kwargs):
        nonlocal attempts
        attempts += 1
        compute(db, session)
        if attempts == 1 or exhausted:
            raise _operational_error("55P03")
        db.commit()
    monkeypatch.setattr(session_api, "_run_graph_evidence_txn", run)
    monkeypatch.setattr(session_api, "_upsert_analysis_cache", lambda *a, **kw: None)
    session_api._run_session_move_evidence_side_effects(
        db_session, session_id=session.id, user_id=123, player_color="white",
        evidence_moves=[], move_count=0, dialect_name="postgresql",
    )
    outcomes = [row["outcome"] for row in private_store.rows()]
    assert attempts == 2 and "retry" in outcomes
    assert outcomes.count("rolled_back") == (2 if exhausted else 1)
    assert ("committed" in outcomes) is not exhausted
    assert ("dropped" in outcomes) is exhausted


def test_upload_source_does_not_invent_finality_or_revert_identity():
    legacy = SessionMovesRequest(moves=[])
    revised = SessionMovesRequest(moves=[], recompute_opportunity=True, line_revision=2)
    assert telemetry.upload_sources(legacy) == {"upload_ordinary", "upload_legacy"}
    assert telemetry.upload_sources(revised) == {"upload_ordinary", "upload_revised_line"}


def test_report_uses_distinct_session_max_and_exposes_missing_observations(private_store):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(100):
        private_store.put(observation_id=str(i), session_id="frequent", started_at=start,
                          database_at=start + timedelta(days=1), wrote=True,
                          sources={"upload_final"}, outcome="committed")
    private_store.put(observation_id="old", session_id="rare", started_at=start,
                      database_at=start + timedelta(days=40), wrote=True,
                      sources={"repair_blunder"}, outcome="committed")
    private_store.put(observation_id="crash", session_id="lost", sources={"worker"}, outcome="pending")
    result = report(private_store.rows())
    candidate, = result["candidates"]
    assert candidate["successful_session_coverage"] == .5
    assert candidate["successful_operation_coverage"] == 100 / 101
    assert candidate["late_successful_sessions"] == 1
    assert candidate["late_operation_categories"] == {"repair_blunder": 1}
    assert result["sessions_with_observation_gaps"] == 1
    assert not result["gate_b_eligible"]
    serialized = json.dumps(result)
    assert all(secret not in serialized for secret in ["frequent", "rare", "lost", "crash"])


def test_frozen_tail_prevents_censored_coverage_claim(private_store):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for key, age, outcome in [("recent", 1, "committed"), ("frozen", 40, "frozen")]:
        private_store.put(observation_id=key, session_id=key, started_at=start,
                          database_at=start + timedelta(days=age), wrote=outcome == "committed",
                          sources={"upload_final"}, outcome=outcome)
    candidate, = report(private_store.rows())["candidates"]
    assert candidate["successful_session_coverage"] == 1
    assert candidate["uncensored_session_coverage"] == .5
    assert not candidate["numeric_session_coverage_meets_999"]


def test_frozen_observation_does_not_rollback_open_sqlite_transaction(private_store, db_session):
    session, _, _ = seed(db_session)
    session.engine_elo = 1600
    db_session.flush()
    telemetry.frozen_attempt(db_session, session)
    db_session.commit()
    db_session.refresh(session)
    assert session.engine_elo == 1600


def test_idle_worker_expires_private_observations(private_store, monkeypatch):
    import threading
    private_store.put(observation_id="expired", session_id="synthetic",
                      sources={"worker"}, outcome="pending")
    with private_store.connect() as connection:
        connection.execute("UPDATE observations SET expires_at = 1")
    private_store._next_prune = 0
    expired = threading.Event()
    original = telemetry.PrivateStore.prune
    def prune(self, **kwargs):
        count = original(self, **kwargs)
        if count:
            expired.set()
        return count
    monkeypatch.setattr(telemetry.PrivateStore, "prune", prune)
    scheduler = SessionEvidenceScheduler()
    try:
        scheduler.start()
        assert expired.wait(timeout=5)
    finally:
        scheduler.shutdown()
    assert private_store.rows() == []


def test_bulk_blunder_scan_only_observes_mutated_sessions(private_store, db_session):
    session, blunder, position = seed(db_session)
    blunder.created_at = session.started_at - timedelta(days=1)
    sessions = [session]
    for _ in range(4):
        sessions.append(_session(db_session, user_id=123, started_at=session.started_at))
    for item in sessions:
        db_session.add(SessionMove(session_id=item.id, move_number=1, color="white",
                                   move_san="Ka4", fen_after="8/8/8/8/K7/8/8/4k3 w - - 0 1"))
    for square in ("1K6", "2K5", "3K4"):
        position = _position(db_session, user_id=123, active_color="white",
                             fen=f"8/8/8/8/8/8/{square}/4k3 w - - 0 1")
        other = _blunder(db_session, user_id=123, position=position)
        other.created_at = blunder.created_at
    db_session.commit()
    processed, scanned, _, _ = recompute_all_blunders(db_session, progress_every=0)
    assert (processed, scanned) == (4, 20)
    row, = private_store.rows()
    assert row["session_id"] == str(session.id)
    assert row["outcome"] == "committed" and row["wrote"]
    recompute_all_blunders(db_session, progress_every=0)
    assert len(private_store.rows()) == 1  # 4 x 5 no-op scans add no rows.


def test_collector_failure_is_cached_and_reported_once_at_startup(
    tmp_path, monkeypatch, caplog, client,
):
    from fastapi.testclient import TestClient
    from app.main import app
    calls = []
    def fail(self, directory):
        calls.append(directory)
        raise ValueError("SECRET configuration detail")
    monkeypatch.setattr(telemetry.PrivateStore, "__init__", fail)
    monkeypatch.setattr(telemetry, "_store_path", None)
    monkeypatch.setattr(telemetry, "_store", None)
    monkeypatch.setenv(telemetry.ENV, str(tmp_path))
    # client supplies the normal mocked scheduler/prewarm lifespan dependencies.
    with TestClient(app):
        assert calls == [str(tmp_path)]
        for _ in range(3):
            telemetry.emit(observation_id="test", session_id="synthetic",
                           sources={"worker"}, outcome="queued")
        assert telemetry.get_store() is None
    assert calls == [str(tmp_path)]
    assert caplog.text.count("collector_unavailable") == 1
    assert "observation_missing" not in caplog.text
    assert "SECRET" not in caplog.text and str(tmp_path) not in caplog.text


def test_enqueue_io_releases_lock_and_late_queue_cannot_erase_completion(
    private_store, monkeypatch,
):
    scheduler = SessionEvidenceScheduler(auto_start=False, clock=lambda: 0)
    original = telemetry.PrivateStore.put
    def put(self, **fields):
        assert scheduler._lock.acquire(blocking=False)
        scheduler._lock.release()
        if fields["outcome"] == "queued":
            original(self, **dict(fields, outcome="worker_finished",
                                  sources={"upload_final"}))
        original(self, **fields)
    monkeypatch.setattr(telemetry.PrivateStore, "put", put)
    scheduler.enqueue(uuid.uuid4(), 123, "white", [])
    row, = private_store.rows()
    assert row["outcome"] == "worker_finished"


def test_same_stage_out_of_order_enqueues_merge_sources(private_store):
    fields = dict(observation_id="job", session_id="synthetic", outcome="queued")
    private_store.put(**fields, sources={"upload_ordinary", "upload_final"})
    private_store.put(**fields, sources={"upload_ordinary"})
    row, = private_store.rows()
    assert set(json.loads(row["sources"])) == {"upload_ordinary", "upload_final"}


@pytest.mark.parametrize("run_opportunity", [False, True])
def test_cache_failure_does_not_invent_srs_gaps(private_store, db_session, monkeypatch, run_opportunity):
    session, _, _ = seed(db_session)
    def fail(*a, **kw):
        raise RuntimeError("synthetic cache failure")
    monkeypatch.setattr(session_api, "_upsert_analysis_cache", fail)
    scheduler = SessionEvidenceScheduler(session_factory=lambda: Session(db_session.get_bind()),
                                         auto_start=False, clock=lambda: 0)
    scheduler.enqueue(session.id, 123, "white", [], run_opportunity=run_opportunity)
    scheduler.run_due(now=100)
    result = report(private_store.rows())
    assert result["sessions_with_observation_gaps"] == 0
    assert result["missing_worker_observations"] == 0
    assert result["observed_sessions"] == int(run_opportunity)


def test_disabled_opportunity_timeouts_and_shutdown_do_not_create_gaps(
    private_store, db_session, monkeypatch,
):
    from test_session_graph_lock import _operational_error
    session, _, _ = seed(db_session)
    def timeout(*a, **kw):
        raise _operational_error("55P03")
    monkeypatch.setattr(session_api, "_run_graph_evidence_txn", timeout)
    monkeypatch.setattr(session_api, "_upsert_analysis_cache", lambda *a, **kw: None)
    scheduler = SessionEvidenceScheduler(session_factory=lambda: Session(db_session.get_bind()),
                                         auto_start=False, clock=lambda: 0)
    scheduler.enqueue(session.id, 123, "white", [], run_opportunity=False)
    scheduler.run_due(now=100)
    scheduler.enqueue(session.id, 123, "white", [], run_opportunity=False)
    scheduler.shutdown(drain=False)
    scheduler.enqueue(session.id, 123, "white", [], run_opportunity=False)
    assert set(report(private_store.rows())["outcomes"]) == {"not_requested"}


def test_failed_opportunity_job_still_exposes_gap(private_store, db_session, monkeypatch):
    session, _, _ = seed(db_session)
    def fail(*a, **kw):
        raise RuntimeError("synthetic evidence failure")
    monkeypatch.setattr(session_api, "_run_graph_evidence_txn", fail)
    scheduler = SessionEvidenceScheduler(session_factory=lambda: Session(db_session.get_bind()),
                                         auto_start=False, clock=lambda: 0)
    scheduler.enqueue(session.id, 123, "white", [])
    scheduler.run_due(now=100)
    assert report(private_store.rows())["sessions_with_observation_gaps"] == 1


def test_completion_clock_runs_after_commit_timer(private_store, db_session, monkeypatch):
    from contextlib import contextmanager
    session, _, _ = seed(db_session)
    stages = set()
    @contextmanager
    def timed(stage, **fields):
        stages.add(stage)
        try:
            yield fields
        finally:
            stages.remove(stage)
    def clock(db):
        assert "evidence_commit" not in stages
        assert not db.in_transaction()
        return datetime.now(timezone.utc)
    original_put = telemetry.PrivateStore.put
    def put(self, **fields):
        assert not stages  # neither pending nor completion I/O runs in graph stages
        return original_put(self, **fields)
    monkeypatch.setattr(session_api, "_timed_side_effect", timed)
    monkeypatch.setattr(telemetry, "database_clock", clock)
    monkeypatch.setattr(telemetry.PrivateStore, "put", put)
    session_api._run_graph_evidence_txn(
        db_session, session_id=session.id, user_id=123, player_color="white",
        evidence_moves=[], move_count=0, dialect_name="sqlite",
    )
    row, = private_store.rows()
    assert row["database_at"] is not None and row["outcome"] == "committed"


def test_report_requires_existing_spool_and_streams_rows(private_store, monkeypatch, capsys):
    from scripts.report_srs_writes import main
    typo = private_store.directory / "typo"
    typo.mkdir(mode=0o700)
    assert main(["--private-dir", str(typo)]) == 1
    assert not (typo / "observations.sqlite3").exists()
    assert capsys.readouterr().out == ""
    def fail(self):
        pytest.fail("report loaded the entire spool")
    monkeypatch.setattr(telemetry.PrivateStore, "rows", fail)
    assert main(["--private-dir", str(private_store.directory)]) == 0
    assert json.loads(capsys.readouterr().out)["observed_sessions"] == 0


def test_age_cells_need_five_distinct_sessions_even_with_many_operations(private_store):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(6):
        private_store.put(observation_id=str(i), session_id="one", started_at=start,
                          database_at=start + timedelta(days=1.2345), wrote=True,
                          sources={"repair_blunder"}, outcome="committed")
    result = report(private_store.iter_rows())
    cell = result["operation_age_days_by_source"]["repair_blunder"]
    assert cell["count"] == 6 and cell["suppressed"] and cell["maximum"] is None
    for i in range(4):
        private_store.put(observation_id=f"other-{i}", session_id=f"other-{i}", started_at=start,
                          database_at=start + timedelta(days=2), wrote=True,
                          sources={"repair_blunder"}, outcome="committed")
    cell = report(private_store.iter_rows())["operation_age_days_by_source"]["repair_blunder"]
    assert cell["count"] == 10 and not cell["suppressed"] and cell["maximum"] == 2


def test_wal_and_normal_sync_keep_expiry_checkpointed(private_store):
    with private_store.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
        private_store.put(observation_id="old", session_id="synthetic",
                          sources={"worker"}, outcome="pending")
        assert private_store.prune(now=telemetry.time.time() + telemetry.TTL_SECONDS + 1) == 1
        wal = private_store.path.with_name(private_store.path.name + "-wal")
        assert wal.stat().st_size == 0
