"""Replay expiry contracts. PostgreSQL cases use barriers, never elapsed TTLs."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
import uuid
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import DateTime, delete, literal, select, text

from app import opponent_retention as retention
from app.api import game, drills
from app.models import GameSession, OpponentDecision, OpponentTargetFact, User
from app.srs_math import as_utc
from conftest import await_pg_lock, pg_required
from test_opponent_decision_record import _record_target, _post, _engine_move, AFTER_E4_FEN
from test_drill_root_confirmation import (
    _drill, _route_check, _decision_row, _graph, _TARGET,
    START_FEN, E4_FEN, E4_E5_FEN, NF3_FEN, D4_FEN,
)

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
DEADLINE = NOW + timedelta(seconds=10)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setenv("OPPONENT_DECISION_RETENTION_ENABLED", "1")
    monkeypatch.setenv("OPPONENT_DECISION_RETENTION_SECONDS", "60")
    value = [NOW]
    monkeypatch.setattr(retention, "database_clock", lambda db: literal(value[0], type_=DateTime(timezone=True)))
    return value


def assert_expired(response):
    assert response.status_code == 410, response.text
    error = response.json()["error"]
    assert error["code"] == "http_410"
    assert error["details"]["error_code"] == "OPPONENT_SESSION_EXPIRED"


@pytest.mark.parametrize("enabled,seconds,error,match", [
    ("0", "7d", ValueError, "OPPONENT_DECISION_RETENTION_SECONDS"),
    ("1", "7d", ValueError, "OPPONENT_DECISION_RETENTION_SECONDS"),
    ("0", "0", ValueError, "OPPONENT_DECISION_RETENTION_SECONDS"),
    ("0", "-1", ValueError, "OPPONENT_DECISION_RETENTION_SECONDS"),
    ("true", "60", ValueError, "OPPONENT_DECISION_RETENTION_ENABLED must be 0 or 1"),
    ("false", "60", ValueError, "OPPONENT_DECISION_RETENTION_ENABLED must be 0 or 1"),
    ("", "60", ValueError, "OPPONENT_DECISION_RETENTION_ENABLED must be 0 or 1"),
    ("1", None, RuntimeError, "without a selected duration"),
])
def test_invalid_retention_settings_prevent_startup(monkeypatch, enabled, seconds, error, match):
    from app.main import app

    monkeypatch.setenv("OPPONENT_DECISION_RETENTION_ENABLED", enabled)
    if seconds is None:
        monkeypatch.delenv("OPPONENT_DECISION_RETENTION_SECONDS", raising=False)
    else:
        monkeypatch.setenv("OPPONENT_DECISION_RETENTION_SECONDS", seconds)
    with patch("app.main.engine") as engine, patch("app.main.get_scheduler") as scheduler:
        with pytest.raises(error, match=match):
            with TestClient(app):
                pytest.fail("invalid retention settings allowed API startup")
        engine.connect.assert_not_called()
        scheduler.assert_not_called()


@pytest.mark.parametrize("enabled,seconds", [(None, None), ("0", "60"), ("1", "60")])
def test_valid_retention_settings_allow_startup(monkeypatch, request, enabled, seconds):
    for name, value in [("OPPONENT_DECISION_RETENTION_ENABLED", enabled),
                        ("OPPONENT_DECISION_RETENTION_SECONDS", seconds)]:
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    client = request.getfixturevalue("client")
    assert client.get("/health").status_code == 200


def test_both_creators_initialize_immutable_deadlines(client, auth_headers, db_session, create_game_session, monkeypatch):
    monkeypatch.setenv("OPPONENT_DECISION_RETENTION_SECONDS", "123")
    ids = [create_game_session(), _drill(client, auth_headers, target=E4_FEN, player_color="white")]
    for sid in ids:
        session = db_session.get(GameSession, uuid.UUID(sid))
        expected = as_utc(session.started_at) + timedelta(seconds=123)
        assert as_utc(session.opponent_decisions_expires_at) == expected
        monkeypatch.setenv("OPPONENT_DECISION_RETENTION_SECONDS", "999")
        retention.initialize_deadline(db_session, session)
        assert as_utc(session.opponent_decisions_expires_at) == expected


@pg_required
def test_pg_default_start_and_fresh_database_publication(pg_session_factory, monkeypatch):
    monkeypatch.setenv("OPPONENT_DECISION_RETENTION_SECONDS", "123")
    with pg_session_factory() as db:
        # Historical migration uses explicit starts; exercise the model-declared
        # server default in isolated transactional DDL without changing that schema.
        db.execute(text("CREATE SCHEMA expiry_default_test"))
        db.execute(text("SET LOCAL search_path TO expiry_default_test"))
        GameSession.__table__.create(db.connection())
        default = GameSession(user_id=123, engine_elo=1500, player_color="white", status="active")
        db.add(default)
        retention.initialize_deadline(db, default)
        assert default.opponent_decisions_expires_at == default.started_at + timedelta(seconds=123)
        db.rollback()
    sid = seed_pg(pg_session_factory)
    with pg_session_factory() as db:
        session = db.get(GameSession, sid)
        session.opponent_decisions_expires_at = None
        db.flush()
        sql = text((Path(__file__).parent / "scripts/initialize_opponent_decision_deadlines.sql").read_text())
        db.execute(sql, {"retention_seconds": 123})
        db.execute(sql, {"retention_seconds": 999})
        db.refresh(session)
        assert session.opponent_decisions_expires_at == NOW + timedelta(seconds=123)
        db.commit()
        # BEGIN's clock must not be the decision clock, even in one transaction.
        start_clock = db.scalar(text("select now()"))
        before = db.scalar(text("select clock_timestamp()"))
        _record_target(db, sid, None)
        served_at = db.scalar(select(OpponentDecision.served_at))
        after = db.scalar(text("select clock_timestamp()"))
        assert start_clock <= before < served_at <= after


@pg_required
@pytest.mark.parametrize("operation", ["admission", "publication"])
def test_pg_enforcement_uses_real_clock_after_transaction_start(pg_session_factory, monkeypatch, operation):
    monkeypatch.setenv("OPPONENT_DECISION_RETENTION_ENABLED", "1")
    monkeypatch.setenv("OPPONENT_DECISION_RETENTION_SECONDS", "60")
    sid = seed_pg(pg_session_factory)
    with pg_session_factory() as db:
        transaction_start = db.scalar(text("SELECT now()"))
        # A separate transaction moves the deadline past this transaction's now(),
        # but before its next real clock sample. No mocked clock or sleep needed.
        with pg_session_factory() as writer:
            deadline = writer.scalar(text(
                "UPDATE game_sessions SET opponent_decisions_expires_at = clock_timestamp() "
                "WHERE id = :sid RETURNING opponent_decisions_expires_at"
            ), {"sid": sid})
            writer.commit()
        assert transaction_start < deadline
        assert db.scalar(text("SELECT now()")) == transaction_start
        with pytest.raises(HTTPException) as raised:
            if operation == "admission":
                retention.check_deadline(db, sid)
            else:
                _record_target(db, sid, None)
        assert raised.value.status_code == 410
        assert raised.value.detail["error_code"] == "OPPONENT_SESSION_EXPIRED"
        assert db.query(OpponentDecision).count() == 0
        assert db.query(OpponentTargetFact).count() == 0


def test_disabled_policy_and_missing_deadline_invariant(client, db_session, create_game_session, monkeypatch):
    monkeypatch.delenv("OPPONENT_DECISION_RETENTION_ENABLED", raising=False)
    monkeypatch.delenv("OPPONENT_DECISION_RETENTION_SECONDS", raising=False)
    sid = uuid.UUID(create_game_session())
    assert db_session.get(GameSession, sid).opponent_decisions_expires_at is None
    retention.check_deadline(db_session, sid)
    monkeypatch.setenv("OPPONENT_DECISION_RETENTION_SECONDS", "60")
    monkeypatch.setenv("OPPONENT_DECISION_RETENTION_ENABLED", "1")
    with pytest.raises(RuntimeError, match="missing session deadline"):
        retention.check_deadline(db_session, sid)
    with pytest.raises(RuntimeError, match="missing session deadline"):
        _record_target(db_session, sid, None)
    assert db_session.query(OpponentDecision).count() == 0


def test_selected_duration_can_initialize_before_activation(client, db_session, create_game_session, monkeypatch):
    monkeypatch.delenv("OPPONENT_DECISION_RETENTION_ENABLED", raising=False)
    monkeypatch.setenv("OPPONENT_DECISION_RETENTION_SECONDS", "1")
    sid = uuid.UUID(create_game_session())
    session = db_session.get(GameSession, sid)
    assert session.opponent_decisions_expires_at is not None
    session.opponent_decisions_expires_at = NOW - timedelta(days=10000)
    db_session.commit()
    retention.check_deadline(db_session, sid)  # Initialization alone is not enforcement.
    monkeypatch.setenv("OPPONENT_DECISION_RETENTION_ENABLED", "1")
    with pytest.raises(HTTPException) as raised:
        retention.check_deadline(db_session, sid)
    assert raised.value.status_code == 410


def test_stored_root_result_survives_envelope_deletion(client, auth_headers, db_session, clock):
    sid = _drill(client, auth_headers, target=E4_FEN, player_color="black")
    session = db_session.get(GameSession, uuid.UUID(sid))
    session.opponent_decisions_expires_at = DEADLINE
    decision = _decision_row(sid, ply_before=0, resulting_fen=E4_FEN)
    db_session.add(decision)
    db_session.commit()
    body = {"current_fen": E4_FEN, "current_ply": 1, "decision_id": str(decision.decision_id)}
    confirmed = _route_check(client, auth_headers, sid, **body)
    assert confirmed.status_code == 200
    assert confirmed.json()["drill_root_reached_ply"] == 1
    db_session.execute(delete(OpponentDecision).where(OpponentDecision.session_id == uuid.UUID(sid)))
    db_session.commit()
    clock[0] = DEADLINE
    assert_expired(_route_check(client, auth_headers, sid, **body))
    db_session.refresh(session)
    assert session.drill_state == "root_reached"
    assert session.drill_root_reached_ply == 1
    # Invalid state and ownership retain their earlier error precedence.
    session.drill_state = "abandoned"
    db_session.commit()
    assert _route_check(client, auth_headers, sid, **body).status_code == 400
    assert _route_check(client, auth_headers, sid, user_id=456, **body).status_code == 403


@pytest.mark.parametrize("field,value", [("previous_fen", START_FEN), ("played_uci", "e2e4")])
def test_expired_drill_preserves_malformed_pair_precedence(client, auth_headers, db_session, clock, field, value):
    sid = _drill(client, auth_headers, target=E4_FEN, player_color="white")
    session = db_session.get(GameSession, uuid.UUID(sid))
    session.opponent_decisions_expires_at = DEADLINE
    db_session.commit()
    clock[0] = DEADLINE
    response = _route_check(client, auth_headers, sid, current_fen=E4_FEN, current_ply=1, **{field: value})
    assert response.status_code == 400
    assert "must be provided together" in response.json()["error"]["message"]


@pytest.mark.parametrize("state", [None, "active", "root_reached", "converted"])
def test_expiry_before_replay_preserves_state_and_exact_identity(
    client, auth_headers, db_session, create_game_session, clock, state,
):
    sid = uuid.UUID(create_game_session())
    session = db_session.get(GameSession, sid)
    session.opponent_decisions_expires_at = DEADLINE
    session.session_mode = "drill" if state else "normal"
    session.drill_state = state
    session.drill_opening_key = NF3_FEN if state else None
    session.is_rated = state in {None, "converted"}
    if state == "converted":
        session.converted_at = NOW
        session.normal_started_at = NOW
        session.rated_start_ply = 0
    db_session.commit()
    fingerprint = game._decision_fingerprint(game.normalize_fen(AFTER_E4_FEN), ["e2e4"])
    response, _ = _record_target(db_session, sid, None, fingerprint)
    replay = _post(client, auth_headers, str(sid), AFTER_E4_FEN, moves=["e2e4"])
    assert replay.status_code == 200, replay.text
    assert replay.json() == response.model_dump(mode="json")
    clock[0] = DEADLINE
    assert_expired(_post(client, auth_headers, str(sid), AFTER_E4_FEN, moves=["e2e4"]))
    db_session.expire_all()
    assert db_session.get(GameSession, sid).drill_state == state
    assert db_session.query(OpponentDecision).count() == 1


def seed_pg(factory, *, target=None, player_color="white", state="active"):
    with factory() as db:
        db.add(User(id=123))
        db.flush()
        session = GameSession(
            id=uuid.uuid4(), user_id=123, started_at=NOW, status="active",
            engine_elo=1500, player_color=player_color,
            is_rated=target is None,
            session_mode="drill" if target else "normal", drill_state=state if target else None,
            drill_opening_key=target, drill_strictness="standard" if target else None,
            opponent_decisions_expires_at=DEADLINE,
        )
        db.add(session)
        db.commit()
        sid = session.id
    if target:
        _TARGET[str(sid)] = target
    return sid


def run_at_barrier(operation, mutation, entered, release):
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(operation)
        try:
            assert entered.wait(10), "request did not reach deletion barrier"
            mutation()
        finally:
            release.set()
        return future.result(timeout=10)


def delete_envelopes(factory, sid, clock):
    with factory() as db:
        db.execute(delete(OpponentDecision).where(OpponentDecision.session_id == sid))
        db.commit()
    clock[0] = DEADLINE


@pg_required
def test_pg_replay_deleted_before_lookup_cannot_publish_replacement(pg_client, pg_session_factory, auth_headers, clock, monkeypatch):
    sid = seed_pg(pg_session_factory)
    fingerprint = game._decision_fingerprint(game.normalize_fen(AFTER_E4_FEN), ["e2e4"])
    with pg_session_factory() as db:
        _record_target(db, sid, None, fingerprint)
    entered, release = threading.Event(), threading.Event()
    original = game._replay_decision

    def paused(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(game, "_replay_decision", paused)
    with patch.object(game, "find_ghost_move", return_value=(None, None, None, None, None)), patch("app.opponent_move_controller.choose_move", return_value=_engine_move()):
        response = run_at_barrier(
            lambda: _post(pg_client, auth_headers, str(sid), AFTER_E4_FEN, moves=["e2e4"]),
            lambda: delete_envelopes(pg_session_factory, sid, clock), entered, release,
        )
    assert_expired(response)
    with pg_session_factory() as db:
        assert db.query(OpponentDecision).count() == 0
        assert db.query(OpponentTargetFact).count() == 0


@pg_required
def test_pg_conflict_winner_deleted_before_reselect_returns_503_then_410(pg_client, pg_session_factory, auth_headers, clock, monkeypatch):
    sid = seed_pg(pg_session_factory)
    with pg_session_factory() as db:
        _record_target(db, sid, None)
    entered, release = threading.Event(), threading.Event()
    original = game._replay_decision

    def paused(*args):
        # _record_decision has already committed its conflict before this read.
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(game, "_replay_decision", paused)

    def loser():
        with pg_session_factory() as db:
            with pytest.raises(HTTPException) as raised:
                _record_target(db, sid, None)
            return raised.value.status_code

    assert run_at_barrier(loser, lambda: delete_envelopes(pg_session_factory, sid, clock), entered, release) == 503
    assert_expired(_post(pg_client, auth_headers, str(sid), AFTER_E4_FEN, moves=["e2e4"]))


@pg_required
@pytest.mark.parametrize("arrival", ["opponent", "player"])
def test_pg_deleted_root_proof_fails_closed(pg_client, pg_session_factory, auth_headers, clock, monkeypatch, arrival):
    sid = seed_pg(pg_session_factory, target=NF3_FEN, player_color="black" if arrival == "opponent" else "white")
    with pg_session_factory() as db:
        decision = _decision_row(sid, ply_before=2 if arrival == "opponent" else 1,
            resulting_fen=NF3_FEN if arrival == "opponent" else E4_E5_FEN,
            history=["e2e4", "e7e5"] if arrival == "opponent" else ["e2e4"])
        # The player anchor deliberately has NO root flag.
        decision.reaches_drill_root = arrival == "opponent"
        db.add(decision)
        db.commit()
        decision_id = str(decision.decision_id)
    entered, release = threading.Event(), threading.Event()
    original = drills._confirmed_root_ply

    def paused(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(drills, "_confirmed_root_ply", paused)
    body = {"current_fen": NF3_FEN, "current_ply": 3}
    body.update({"decision_id": decision_id} if arrival == "opponent" else {"previous_fen": E4_E5_FEN, "played_uci": "g1f3"})
    def request():
        return _route_check(pg_client, auth_headers, str(sid), **body)

    response = run_at_barrier(request, lambda: delete_envelopes(pg_session_factory, sid, clock), entered, release)
    if arrival == "opponent":
        assert response.status_code == 422, response.text
    else:
        assert_expired(response)
    assert_expired(request())
    with pg_session_factory() as db:
        session = db.get(GameSession, sid)
        assert session.drill_state == "active"
        assert session.drill_root_reached_ply is None


@pg_required
@pytest.mark.parametrize("branch", ["root", "boundary", "off_route", "observed_root", "serve", "terminal_race"])
def test_pg_expiry_rechecked_after_existing_session_lock(pg_client, pg_engine, pg_session_factory, auth_headers, clock, monkeypatch, branch):
    target = NF3_FEN if branch in {"off_route", "serve"} else E4_FEN
    state = "root_reached" if branch == "boundary" else "active"
    sid = seed_pg(pg_session_factory, target=target, state=state)
    waiting, pids = threading.Event(), []
    original = retention.check_deadline

    def admission(db, session_id):
        original(db, session_id)
        if not pids:
            pids.append(db.scalar(text("select pg_backend_pid()")))
            waiting.set()

    monkeypatch.setattr(game, "check_deadline", admission)
    monkeypatch.setattr(drills, "check_deadline", admission)
    with pg_session_factory() as holder, ThreadPoolExecutor(max_workers=1) as pool:
        holder.execute(select(GameSession.id).where(GameSession.id == sid).with_for_update(key_share=True))
        with patch.object(game, "get_opening_graph", return_value=_graph()):
            if branch in {"serve", "observed_root"}:
                future = pool.submit(_post, pg_client, auth_headers, str(sid), E4_FEN, moves=["e2e4"])
            else:
                body = {"current_fen": E4_FEN, "previous_fen": START_FEN, "played_uci": "e2e4", "current_ply": 1} if branch != "off_route" else {"current_fen": D4_FEN, "previous_fen": START_FEN, "played_uci": "d2d4", "current_ply": 1}
                future = pool.submit(_route_check, pg_client, auth_headers, str(sid), **body)
            try:
                assert waiting.wait(10)
                with pg_engine.connect() as observer:
                    assert await_pg_lock(observer, pids[0])
                clock[0] = DEADLINE
                if branch == "terminal_race":
                    holder.get(GameSession, sid).drill_state = "abandoned"
                    holder.commit()
            finally:
                holder.rollback()
            response = future.result(timeout=10)
            if branch == "terminal_race":
                assert response.status_code == 400
            else:
                assert_expired(response)
    with pg_session_factory() as db:
        session = db.get(GameSession, sid)
        assert session.drill_state == ("abandoned" if branch == "terminal_race" else state)
        assert session.drill_root_reached_ply is None
        assert db.query(OpponentDecision).count() == 0


@pg_required
def test_pg_replay_and_normal_record_do_not_wait_for_upload_lock(pg_client, pg_session_factory, auth_headers, clock):
    sid = seed_pg(pg_session_factory)
    fingerprint = game._decision_fingerprint(game.normalize_fen(AFTER_E4_FEN), ["e2e4"])
    with pg_session_factory() as db:
        expected, _ = _record_target(db, sid, None, fingerprint)
    with pg_session_factory() as holder, ThreadPoolExecutor(max_workers=1) as pool:
        holder.execute(select(GameSession.id).where(GameSession.id == sid).with_for_update(key_share=True))
        try:
            response = pool.submit(_post, pg_client, auth_headers, str(sid), AFTER_E4_FEN, moves=["e2e4"]).result(timeout=5)
            assert response.status_code == 200
            assert response.json() == expected.model_dump(mode="json")
            def record():
                with pg_session_factory() as db:
                    db.execute(text("set local lock_timeout = '2s'"))
                    return _record_target(db, sid, None, "new-fingerprint")
            assert not pool.submit(record).result(timeout=5)[1]
        finally:
            holder.rollback()
