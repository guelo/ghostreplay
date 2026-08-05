"""Durable L2 contract for per-session opening replay products."""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import chess
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError

import app.game_phase as game_phase
import app.opening_evidence as opening_evidence
from app.models import OpeningSessionReplayCache
from app.opening_evidence import EvidenceOverlay, PhaseSample, ReplayCacheStats
from app.opening_graph import OpeningGraph, OpeningGraphNode, _fen_from_board


USER_ID = 901
COLOR = "white"
ROOT = _fen_from_board(chess.Board())


def _graph() -> OpeningGraph:
    board = chess.Board()
    board.push_uci("e2e4")
    e4 = _fen_from_board(board)
    nodes = {
        ROOT: OpeningGraphNode(ROOT, "white"),
        e4: OpeningGraphNode(e4, "black"),
    }
    nodes[ROOT].children["e2e4"] = e4
    nodes[e4].parents.add((ROOT, "e2e4"))
    graph = OpeningGraph(nodes, ROOT)
    graph.freeze()
    return graph


def _seed_user(db) -> None:
    db.execute(
        text(
            "INSERT INTO users (id, username, is_anonymous) VALUES (:id, :username, 1)"
        ),
        {"id": USER_ID, "username": f"replay-{USER_ID}"},
    )
    db.commit()


def _seed_line(db, moves: tuple[str, ...] = ("e2e4", "e7e5")) -> str:
    sid = str(uuid.uuid4())
    db.execute(
        text(
            "INSERT INTO game_sessions "
            "(id, user_id, started_at, ended_at, status, engine_elo, "
            " player_color, is_rated, session_mode) "
            "VALUES (:sid, :user_id, :started_at, :ended_at, 'ended', 1500, "
            " :color, 1, 'normal')"
        ),
        {
            "sid": sid,
            "user_id": USER_ID,
            "started_at": "2026-08-01 10:00:00+00:00",
            "ended_at": "2026-08-01 10:05:00+00:00",
            "color": COLOR,
        },
    )
    board = chess.Board()
    for ply, uci in enumerate(moves):
        move = chess.Move.from_uci(uci)
        san = board.san(move)
        fen_before = board.fen()
        color = "white" if board.turn == chess.WHITE else "black"
        board.push(move)
        db.execute(
            text(
                "INSERT INTO session_moves "
                "(session_id, move_number, color, move_san, fen_before, fen_after, "
                " eval_delta, eval_cp, best_move_eval_cp, segment) "
                "VALUES (:sid, :move_number, :color, :san, :before, :after, "
                " :delta, :eval_cp, :best_eval, 'normal')"
            ),
            {
                "sid": sid,
                "move_number": ply // 2 + 1,
                "color": color,
                "san": san,
                "before": fen_before,
                "after": board.fen(),
                "delta": 20 + ply,
                "eval_cp": None if ply == 0 else 5,
                "best_eval": None if ply == 0 else 15,
            },
        )
    db.commit()
    return sid


def _semantic(overlay: EvidenceOverlay) -> tuple:
    return (
        overlay.nodes,
        overlay.edges,
        overlay.source_counts,
        overlay.excluded_sessions,
        overlay.phase_samples,
        overlay.shared_scope,
    )


def _raise_if_called(*args, **kwargs):
    raise AssertionError("raw replay function ran during persisted bootstrap")


def _migration_module():
    path = (
        Path(__file__).parent
        / "alembic"
        / "versions"
        / "20260802_01_create_opening_session_replay_cache.py"
    )
    spec = importlib.util.spec_from_file_location(
        "opening_replay_cache_migration", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("timestamp", "canonical_timestamp"),
    [
        (
            datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 8, 1, 10, 0),
            datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(
                2026,
                8,
                1,
                10,
                0,
                tzinfo=timezone(timedelta(hours=5, minutes=30)),
            ),
            datetime(
                2026,
                8,
                1,
                10,
                0,
                tzinfo=timezone(timedelta(hours=5, minutes=30)),
            ),
        ),
    ],
    ids=("utc", "sqlite-naive", "non-utc-offset"),
)
def test_payload_round_trip_is_strict_and_storage_only(timestamp, canonical_timestamp):
    move = opening_evidence._CachedMove(
        session_id="source-session",
        move_number=1,
        color="white",
        norm_before=ROOT,
        norm_after="after",
        fen_before_raw=chess.Board().fen(),
        move_san="e4",
        uci="e2e4",
        eval_delta=12,
        eval_cp=None,
        best_move_eval_cp=34,
        session_ts=timestamp,
    )
    original = opening_evidence._CachedSession(
        moves=(move,),
        phase_sample=PhaseSample(1, None, None),
        excluded=False,
        exclusion_msg="",
    )

    payload = opening_evidence._encode_cached_session(original, timestamp)
    decoded = opening_evidence._decode_cached_session("hydrated-session", payload, 1)
    raw = json.loads(payload)

    assert payload == json.dumps(raw, sort_keys=True, separators=(",", ":"))
    assert set(raw) == opening_evidence._SESSION_PAYLOAD_KEYS
    assert "session_id" not in payload
    assert "quality" not in payload
    assert decoded.moves[0].session_id == "hydrated-session"
    assert decoded.moves[0].session_ts == canonical_timestamp
    assert decoded.moves[0].eval_cp is None
    assert decoded.phase_sample == original.phase_sample

    excluded = opening_evidence._CachedSession(
        moves=(),
        phase_sample=None,
        excluded=True,
        exclusion_msg="broken continuity",
    )
    excluded_payload = opening_evidence._encode_cached_session(excluded, timestamp)
    assert (
        opening_evidence._decode_cached_session("excluded-session", excluded_payload, 0)
        == excluded
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"unknown": 1}),
        lambda value: value.pop("exclusion_msg"),
        lambda value: value.__setitem__("moves", "not-an-array"),
        lambda value: value.__setitem__("moves", []),
        lambda value: value.__setitem__("excluded", 1),
        lambda value: value.__setitem__("phase_sample", None),
        lambda value: value.__setitem__("session_ts", "not-a-time"),
    ],
)
def test_payload_decoder_fails_closed_on_shape_type_and_invariant_errors(mutation):
    timestamp = datetime(2026, 8, 1, tzinfo=timezone.utc)
    value = opening_evidence._CachedSession(
        moves=(
            opening_evidence._CachedMove(
                session_id="s",
                move_number=1,
                color="white",
                norm_before=ROOT,
                norm_after="after",
                fen_before_raw=chess.Board().fen(),
                move_san="e4",
                uci="e2e4",
                eval_delta=None,
                eval_cp=None,
                best_move_eval_cp=None,
                session_ts=timestamp,
            ),
        ),
        phase_sample=PhaseSample(1, None, None),
        excluded=False,
        exclusion_msg="",
    )
    raw = json.loads(opening_evidence._encode_cached_session(value, timestamp))
    mutation(raw)

    with pytest.raises(ValueError):
        opening_evidence._decode_cached_session(
            "s", json.dumps(raw, sort_keys=True, separators=(",", ":")), 1
        )


def test_l2_select_sql_preserves_the_postgres_uuid_primary_key():
    postgres_sql = opening_evidence._session_replay_l2_select_sql("postgresql")
    sqlite_sql = opening_evidence._session_replay_l2_select_sql("sqlite")

    assert "session_id = ANY(CAST(:sids AS UUID[]))" in postgres_sql
    assert "CAST(session_id AS TEXT) IN" not in postgres_sql
    assert "CAST(session_id AS TEXT) IN :sids" in sqlite_sql


def test_restart_hydrates_every_session_without_raw_fetch_or_replay(
    db_session, monkeypatch
):
    _seed_user(db_session)
    for _ in range(3):
        _seed_line(db_session)
    graph = _graph()

    cold = opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)
    db_session.commit()  # SQLite L2 shares the caller transaction by design.
    assert cold.replay_cache_stats == ReplayCacheStats(
        build_count=1,
        probed_sessions=3,
        raw_derivations=3,
        persisted_upserts=3,
    )

    opening_evidence.reset_session_evidence_cache()
    monkeypatch.setattr(
        opening_evidence, "reconstruct_board_sequence", _raise_if_called
    )
    monkeypatch.setattr(opening_evidence, "divide", _raise_if_called)

    counts = {"raw": 0, "l2_read": 0}
    engine = db_session.get_bind()

    def count_sql(conn, cursor, statement, parameters, context, executemany):
        if "sm.session_id IN" in statement:
            counts["raw"] += 1
        if "FROM opening_session_replay_cache" in statement:
            counts["l2_read"] += 1

    event.listen(engine, "before_cursor_execute", count_sql)
    try:
        restarted = opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)
        assert _semantic(restarted) == _semantic(cold)
        assert restarted.replay_cache_stats == ReplayCacheStats(
            build_count=1,
            probed_sessions=3,
            l2_hits=3,
        )
        assert counts == {"raw": 0, "l2_read": 1}

        memory_warm = opening_evidence.overlay_evidence(
            db_session, USER_ID, COLOR, graph
        )
        assert _semantic(memory_warm) == _semantic(cold)
        assert memory_warm.replay_cache_stats == ReplayCacheStats(
            build_count=1,
            probed_sessions=3,
            l1_hits=3,
        )
        assert counts == {"raw": 0, "l2_read": 1}
    finally:
        event.remove(engine, "before_cursor_execute", count_sql)


@pytest.mark.parametrize("version_name", ["divider", "inputs"])
def test_semantic_version_bump_replays_repairs_and_then_bootstraps(
    db_session, monkeypatch, version_name
):
    _seed_user(db_session)
    _seed_line(db_session)
    graph = _graph()
    opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)
    db_session.commit()

    if version_name == "divider":
        monkeypatch.setattr(game_phase, "DIVIDER_VERSION", "divider-test-next")
    else:
        monkeypatch.setattr(
            opening_evidence,
            "OPENING_EVIDENCE_INPUTS_VERSION",
            "inputs-test-next",
        )

    real = opening_evidence.reconstruct_board_sequence
    calls = {"n": 0}

    def counting(moves):
        calls["n"] += 1
        return real(moves)

    monkeypatch.setattr(opening_evidence, "reconstruct_board_sequence", counting)
    opening_evidence.reset_session_evidence_cache()
    repaired = opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)
    db_session.commit()
    assert calls["n"] == 1
    assert repaired.replay_cache_stats.raw_derivations == 1

    stored = db_session.execute(
        text("SELECT divider_version, inputs_version FROM opening_session_replay_cache")
    ).one()
    assert stored.divider_version == game_phase.DIVIDER_VERSION
    assert stored.inputs_version == opening_evidence.OPENING_EVIDENCE_INPUTS_VERSION

    calls["n"] = 0
    opening_evidence.reset_session_evidence_cache()
    hydrated = opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)
    assert calls["n"] == 0
    assert hydrated.replay_cache_stats.l2_hits == 1


def test_payload_version_mismatch_is_repaired(db_session, monkeypatch):
    _seed_user(db_session)
    _seed_line(db_session)
    graph = _graph()
    baseline = opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)
    db_session.commit()
    db_session.execute(
        text("UPDATE opening_session_replay_cache SET payload_version = 99")
    )
    db_session.commit()

    real = opening_evidence.reconstruct_board_sequence
    calls = {"n": 0}

    def counting(moves):
        calls["n"] += 1
        return real(moves)

    monkeypatch.setattr(opening_evidence, "reconstruct_board_sequence", counting)
    opening_evidence.reset_session_evidence_cache()
    repaired = opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)
    db_session.commit()

    assert calls["n"] == 1
    assert _semantic(repaired) == _semantic(baseline)
    row = db_session.execute(
        text("SELECT payload_version, payload FROM opening_session_replay_cache")
    ).one()
    assert row.payload_version == opening_evidence.SESSION_REPLAY_PAYLOAD_VERSION


def test_corrupt_payload_with_current_versions_is_repaired(
    db_session, monkeypatch, caplog
):
    _seed_user(db_session)
    _seed_line(db_session)
    graph = _graph()
    baseline = opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)
    db_session.commit()
    original = db_session.execute(
        text(
            "SELECT content_hash, divider_version, inputs_version, "
            "payload_version FROM opening_session_replay_cache"
        )
    ).one()
    db_session.execute(
        text("UPDATE opening_session_replay_cache SET payload = :payload"),
        {"payload": "{"},
    )
    db_session.commit()

    real = opening_evidence.reconstruct_board_sequence
    calls = {"n": 0}

    def counting(moves):
        calls["n"] += 1
        return real(moves)

    monkeypatch.setattr(opening_evidence, "reconstruct_board_sequence", counting)
    opening_evidence.reset_session_evidence_cache()
    with caplog.at_level("WARNING", logger="app.opening_evidence"):
        repaired = opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)
    db_session.commit()

    assert calls["n"] == 1
    assert _semantic(repaired) == _semantic(baseline)
    assert repaired.replay_cache_stats.l2_hits == 0
    assert repaired.replay_cache_stats.raw_derivations == 1
    assert (
        sum(
            "malformed opening-session replay cache row" in record.getMessage()
            for record in caplog.records
        )
        == 1
    )
    row = db_session.execute(
        text(
            "SELECT content_hash, divider_version, inputs_version, "
            "payload_version, payload FROM opening_session_replay_cache"
        )
    ).one()
    assert row.content_hash == original.content_hash
    assert row.divider_version == original.divider_version
    assert row.inputs_version == original.inputs_version
    assert row.payload_version == original.payload_version
    assert row.payload != "{"


def test_stale_l2_hash_is_never_served_and_is_replaced(db_session, monkeypatch):
    _seed_user(db_session)
    _seed_line(db_session)
    graph = _graph()
    opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)
    db_session.commit()
    db_session.execute(
        text("UPDATE opening_session_replay_cache SET content_hash = :stale_hash"),
        {"stale_hash": "b" * 40},
    )
    db_session.commit()
    opening_evidence.reset_session_evidence_cache()

    replays = {"count": 0}
    real_reconstruct = opening_evidence.reconstruct_board_sequence

    def count_reconstruction(*args, **kwargs):
        replays["count"] += 1
        return real_reconstruct(*args, **kwargs)

    monkeypatch.setattr(
        opening_evidence, "reconstruct_board_sequence", count_reconstruction
    )
    repaired = opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)

    assert repaired.replay_cache_stats.l2_hits == 0
    assert repaired.replay_cache_stats.raw_derivations == 1
    assert repaired.replay_cache_stats.persisted_upserts == 1
    assert replays["count"] == 1
    assert (
        db_session.execute(
            text("SELECT content_hash FROM opening_session_replay_cache")
        ).scalar_one()
        != "b" * 40
    )


def test_quality_is_recomputed_after_a_persisted_hit(db_session, monkeypatch):
    _seed_user(db_session)
    _seed_line(db_session, ("e2e4",))
    graph = _graph()
    opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)
    db_session.commit()
    opening_evidence.reset_session_evidence_cache()

    monkeypatch.setattr(
        opening_evidence,
        "move_quality",
        lambda **kwargs: (0.125, "test-live-quality"),
    )
    monkeypatch.setattr(
        opening_evidence, "reconstruct_board_sequence", _raise_if_called
    )
    hydrated = opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)

    assert hydrated.nodes[ROOT].quality_sum == pytest.approx(0.125)
    assert hydrated.source_counts["test-live-quality"] == 1
    assert hydrated.replay_cache_stats.l2_hits == 1


def test_excluded_session_hydrates_and_warns_once_in_the_new_process(
    db_session, monkeypatch, caplog
):
    _seed_user(db_session)
    sid = _seed_line(db_session)
    db_session.execute(
        text(
            "UPDATE session_moves SET fen_before = :wrong "
            "WHERE session_id = :sid AND color = 'black'"
        ),
        {"sid": sid, "wrong": chess.Board().fen()},
    )
    db_session.commit()
    graph = _graph()

    with caplog.at_level("WARNING", logger="app.opening_evidence"):
        cold = opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)
    db_session.commit()
    assert cold.excluded_sessions == 1

    caplog.clear()
    opening_evidence.reset_session_evidence_cache()
    monkeypatch.setattr(
        opening_evidence, "reconstruct_board_sequence", _raise_if_called
    )
    monkeypatch.setattr(opening_evidence, "divide", _raise_if_called)
    with caplog.at_level("WARNING", logger="app.opening_evidence"):
        restarted = opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)
        memory_warm = opening_evidence.overlay_evidence(
            db_session, USER_ID, COLOR, graph
        )

    warnings = [
        record
        for record in caplog.records
        if "excluding session" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert restarted.excluded_sessions == 1
    assert memory_warm.excluded_sessions == 1
    assert restarted.replay_cache_stats.l2_hits == 1
    assert memory_warm.replay_cache_stats.l1_hits == 1


def test_real_sqlite_l2_read_and_write_errors_leave_caller_session_usable(
    db_session,
):
    _seed_user(db_session)
    _seed_line(db_session)
    graph = _graph()
    db_session.execute(text("DROP TABLE opening_session_replay_cache"))

    overlay = opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)

    # ``_apply_cache_fallbacks`` runs after both swallowed DBAPI errors. Reaching
    # this assertion proves the caller Session was not left pending rollback.
    assert overlay.nodes[ROOT].live_attempts == 1
    assert overlay.replay_cache_stats.raw_derivations == 1
    assert overlay.replay_cache_stats.l2_read_failed is True
    assert overlay.replay_cache_stats.l2_write_failed is True
    assert (
        db_session.execute(text("SELECT count(*) FROM session_moves")).scalar_one() == 2
    )


def test_real_sqlite_l2_write_error_degrades_after_a_successful_read(db_session):
    _seed_user(db_session)
    _seed_line(db_session)
    graph = _graph()
    db_session.execute(
        text(
            "CREATE TRIGGER fail_opening_replay_insert "
            "BEFORE INSERT ON opening_session_replay_cache "
            "BEGIN SELECT RAISE(ABORT, 'forced L2 write failure'); END"
        )
    )

    overlay = opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)

    assert overlay.nodes[ROOT].live_attempts == 1
    assert overlay.replay_cache_stats.raw_derivations == 1
    assert overlay.replay_cache_stats.l2_read_failed is False
    assert overlay.replay_cache_stats.l2_write_failed is True
    assert (
        db_session.execute(text("SELECT count(*) FROM session_moves")).scalar_one() == 2
    )


def test_independent_connection_acquisition_failure_is_best_effort():
    class BrokenEngine:
        def connect(self):
            raise TimeoutError("pool exhausted")

        def begin(self):
            raise TimeoutError("pool exhausted")

    class FakeSession:
        def get_bind(self):
            return SimpleNamespace(
                dialect=SimpleNamespace(name="postgresql"),
                engine=BrokenEngine(),
            )

    fake = FakeSession()
    assert opening_evidence._load_persisted_sessions(
        fake, {str(uuid.uuid4()): "hash"}
    ) == ({}, True)

    cached = opening_evidence._CachedSession(
        moves=(),
        phase_sample=None,
        excluded=True,
        exclusion_msg="excluded",
    )
    count, failed = opening_evidence._upsert_persisted_sessions(
        fake,
        [
            opening_evidence._PersistedSession(
                session_id=str(uuid.uuid4()),
                content_hash="a" * 40,
                session_ts=datetime(2026, 8, 1, tzinfo=timezone.utc),
                value=cached,
            )
        ],
    )
    assert count == 0
    assert failed is True


def test_concurrent_delete_fk_violation_is_best_effort():
    class BrokenConnection:
        def execute(self, statement, params):
            raise IntegrityError(
                "cache source session disappeared",
                params,
                RuntimeError("foreign key violation"),
            )

    class BrokenTransaction:
        def __enter__(self):
            return BrokenConnection()

        def __exit__(self, exc_type, exc, traceback):
            return False

    class BrokenEngine:
        def begin(self):
            return BrokenTransaction()

    class FakeSession:
        def get_bind(self):
            return SimpleNamespace(
                dialect=SimpleNamespace(name="postgresql"),
                engine=BrokenEngine(),
            )

    cached = opening_evidence._CachedSession(
        moves=(),
        phase_sample=None,
        excluded=True,
        exclusion_msg="source session deleted",
    )
    count, failed = opening_evidence._upsert_persisted_sessions(
        FakeSession(),
        [
            opening_evidence._PersistedSession(
                session_id=str(uuid.uuid4()),
                content_hash="a" * 40,
                session_ts=datetime(2026, 8, 1, tzinfo=timezone.utc),
                value=cached,
            )
        ],
    )

    assert count == 0
    assert failed is True


def test_l1_eviction_hydrates_from_l2_without_replay(db_session, monkeypatch):
    _seed_user(db_session)
    _seed_line(db_session, ("e2e4",))
    _seed_line(db_session, ("e2e4",))
    graph = _graph()
    baseline = opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)
    db_session.commit()
    opening_evidence.reset_session_evidence_cache()
    monkeypatch.setattr(opening_evidence, "_SESSION_CACHE_MAX_ROWS", 1)
    monkeypatch.setattr(
        opening_evidence, "reconstruct_board_sequence", _raise_if_called
    )

    first = opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)
    second = opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, graph)

    assert _semantic(first) == _semantic(baseline)
    assert _semantic(second) == _semantic(baseline)
    assert first.replay_cache_stats.l2_hits == 2
    assert second.replay_cache_stats.l2_hits == 1
    assert first.replay_cache_stats.raw_derivations == 0
    assert second.replay_cache_stats.raw_derivations == 0


def test_session_delete_cascades_persisted_replay(db_session):
    _seed_user(db_session)
    sid = _seed_line(db_session)
    opening_evidence.overlay_evidence(db_session, USER_ID, COLOR, _graph())
    db_session.commit()
    assert (
        db_session.execute(
            text("SELECT count(*) FROM opening_session_replay_cache")
        ).scalar_one()
        == 1
    )

    db_session.execute(text("DELETE FROM game_sessions WHERE id = :sid"), {"sid": sid})
    db_session.commit()
    assert (
        db_session.execute(
            text("SELECT count(*) FROM opening_session_replay_cache")
        ).scalar_one()
        == 0
    )


def test_model_and_migration_have_one_row_per_session_without_extra_index():
    table = OpeningSessionReplayCache.__table__
    assert set(table.c.keys()) == {
        "session_id",
        "content_hash",
        "divider_version",
        "inputs_version",
        "payload_version",
        "move_count",
        "payload",
        "updated_at",
    }
    assert table.primary_key.columns.keys() == ["session_id"]
    assert not table.indexes
    fk = next(iter(table.c.session_id.foreign_keys))
    assert fk.target_fullname == "game_sessions.id"
    assert fk.ondelete == "CASCADE"
    assert {constraint.name for constraint in table.constraints} >= {
        "ck_opening_session_replay_cache_move_count"
    }

    revision = _migration_module()
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        model_default = str(
            table.c.updated_at.server_default.arg.compile(dialect=dialect)
        )
        migration_default = str(revision.statement_timestamp().compile(dialect=dialect))
        assert model_default == migration_default


def test_migration_upgrades_populated_schema_without_backfill_and_downgrades(
    monkeypatch,
):
    revision = _migration_module()
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.execute(text("CREATE TABLE game_sessions (id UUID PRIMARY KEY)"))
        sid = str(uuid.uuid4())
        connection.execute(
            text("INSERT INTO game_sessions (id) VALUES (:sid)"),
            {"sid": sid},
        )
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(revision, "op", operations)
        revision.upgrade()
        inspector = inspect(connection)
        assert inspector.has_table("opening_session_replay_cache")
        assert {
            column["name"]
            for column in inspector.get_columns("opening_session_replay_cache")
        } == {
            "session_id",
            "content_hash",
            "divider_version",
            "inputs_version",
            "payload_version",
            "move_count",
            "payload",
            "updated_at",
        }
        foreign_keys = inspector.get_foreign_keys("opening_session_replay_cache")
        assert len(foreign_keys) == 1
        assert foreign_keys[0]["referred_table"] == "game_sessions"
        assert foreign_keys[0]["constrained_columns"] == ["session_id"]
        assert foreign_keys[0]["options"]["ondelete"] == "CASCADE"
        checks = inspector.get_check_constraints("opening_session_replay_cache")
        assert {
            (constraint["name"], constraint["sqltext"]) for constraint in checks
        } == {
            (
                "ck_opening_session_replay_cache_move_count",
                "move_count >= 0",
            )
        }
        assert inspector.get_indexes("opening_session_replay_cache") == []
        assert (
            connection.execute(
                text("SELECT count(*) FROM opening_session_replay_cache")
            ).scalar_one()
            == 0
        )

        params = {
            "sid": sid,
            "content_hash": "a" * 40,
            "divider_version": "divider",
            "inputs_version": "inputs",
            "payload_version": 1,
            "payload": "{}",
        }
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO opening_session_replay_cache "
                        "(session_id, content_hash, divider_version, "
                        "inputs_version, payload_version, move_count, payload) "
                        "VALUES (:sid, :content_hash, :divider_version, "
                        ":inputs_version, :payload_version, -1, :payload)"
                    ),
                    params,
                )
        connection.execute(
            text(
                "INSERT INTO opening_session_replay_cache "
                "(session_id, content_hash, divider_version, inputs_version, "
                "payload_version, move_count, payload) "
                "VALUES (:sid, :content_hash, :divider_version, :inputs_version, "
                ":payload_version, 0, :payload)"
            ),
            params,
        )
        connection.execute(
            text("DELETE FROM game_sessions WHERE id = :sid"), {"sid": sid}
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM opening_session_replay_cache")
            ).scalar_one()
            == 0
        )
        revision.downgrade()
        assert not inspect(connection).has_table("opening_session_replay_cache")
    engine.dispose()
