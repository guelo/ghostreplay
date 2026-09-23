"""Real activity row contention, fold rechecks, SQL age and additive migration."""
from datetime import timedelta

from alembic import command
from sqlalchemy import create_engine, event, inspect, select, update

from conftest import pg_required
from app import opportunity_fold as fold
from app.models import GameSession, OpponentTargetFact
from app.opportunity_cleanup import backlog, recently_active, scheduled_sweep
from app.opportunity_fold import FoldOutcome
from app.opportunity_retention import database_clock
from app.session_activity import stamp_activity
from test_opportunity_compaction_pg import _seed, USER_ID
from test_opportunity_compaction_migration import config

pytestmark = pg_required


def test_pg_activity_hint_is_database_stamped_coalesced_and_never_waits_for_upload(
    pg_engine, pg_session_factory, caplog,
):
    _seed(pg_session_factory)
    with pg_session_factory() as db:
        sid = db.scalar(select(GameSession.id).limit(1))
        before = db.scalar(select(database_clock(db)))
    with pg_session_factory() as upload:
        session = upload.scalar(select(GameSession).where(GameSession.id == sid).with_for_update())
        session.pgn = 'committed core work survives a missed hint'
        upload.flush()
        assert not stamp_activity(pg_engine, session_id=sid, user_id=USER_ID)
        upload.commit()
    assert stamp_activity(pg_engine, session_id=sid, user_id=USER_ID)
    assert not stamp_activity(pg_engine, session_id=sid, user_id=USER_ID)
    with pg_session_factory() as db:
        session = db.get(GameSession, sid)
        assert before <= session.last_activity_at <= db.scalar(select(database_clock(db)))
        assert session.pgn == 'committed core work survives a missed hint'

    # A coalesced hint must not try to lock even when an upload owns the row.
    caplog.clear()
    with pg_session_factory() as upload:
        upload.scalar(select(GameSession).where(GameSession.id == sid).with_for_update())
        assert not stamp_activity(pg_engine, session_id=sid, user_id=USER_ID)
    assert 'session_activity_hint_failed' not in caplog.text


    # A foreground request may hold the ONLY ordinary pool slot. Its hint uses
    # a separate bounded pool, and a saturated hint pool drops work immediately.
    from app import session_activity as activity
    tiny = create_engine(pg_engine.url, pool_size=1, max_overflow=0, pool_timeout=0)
    try:
        connect_args = {}
        event.listen(activity._hint_engine(tiny), 'do_connect',
                     lambda dialect, record, args, kwargs: connect_args.update(kwargs))
        with pg_session_factory() as db:
            db.execute(update(GameSession).where(GameSession.id == sid).values(last_activity_at=None))
            db.commit()
        with tiny.connect():
            assert stamp_activity(tiny, session_id=sid, user_id=USER_ID)
        assert {key: connect_args[key] for key in activity.PG_CONNECT_ARGS} == activity.PG_CONNECT_ARGS
        assert connect_args['connect_timeout'] == 2
        with activity._hint_engine(tiny).connect():
            assert not stamp_activity(tiny, session_id=sid, user_id=USER_ID)
    finally:
        activity.dispose_activity_engine(tiny)
        tiny.dispose()


def test_pg_activity_arriving_during_export_defers_normal_but_not_forced_transfer(
    pg_engine, pg_session_factory, monkeypatch, tmp_path,
):
    _seed(pg_session_factory)
    monkeypatch.setenv('GHOSTREPLAY_SRS_FOLD_EXPORT_DIR', str(tmp_path / 'exports'))
    with pg_session_factory() as db:
        db.execute(update(GameSession).values(last_activity_at=database_clock(db) - timedelta(hours=2)))
        sid = db.scalar(select(GameSession.id).limit(1))
        db.commit()
    real_export = fold.write_export
    def export(*a, **kw):
        result = real_export(*a, **kw)
        assert stamp_activity(pg_engine, session_id=sid, user_id=USER_ID)
        return result
    monkeypatch.setattr(fold, 'write_export', export)
    result = fold.fold_user_batch(pg_engine, user_id=USER_ID,
        activity_check=lambda: recently_active(pg_engine, USER_ID))
    assert result.outcome == FoldOutcome.DEFERRED_ACTIVITY
    assert backlog(pg_engine, USER_ID).pairs == 2
    assert not list(tmp_path.rglob('*.json'))
    monkeypatch.setattr(fold, 'write_export', real_export)
    report = scheduled_sweep(pg_engine, max_attempts=1)
    assert report.sweep.rows_deleted == 2
    assert report.remaining[USER_ID].pairs == 0


def test_pg_backlog_age_uses_same_pin_and_legacy_predicates_as_fold(
    pg_engine, pg_session_factory, monkeypatch,
):
    monkeypatch.setenv('OPPONENT_TARGET_SOURCE', 'facts')
    bid = _seed(pg_session_factory, rows=1)
    with pg_session_factory() as db:
        now = db.scalar(select(database_clock(db)))
        sid = db.scalar(select(GameSession.id))
        db.add(OpponentTargetFact(session_id=sid, blunder_id=bid,
                                 last_served_at=now - timedelta(days=30, hours=13)))
        db.commit()
    result = backlog(pg_engine, USER_ID)
    assert result.pairs == 1
    assert 13 * 3600 <= result.lag_seconds < 13 * 3600 + 5
    with pg_session_factory() as db:
        db.execute(update(OpponentTargetFact).values(last_served_at=database_clock(db)))
        db.commit()
    assert backlog(pg_engine, USER_ID).pairs == 0
    assert fold.fold_user_batch(pg_engine, user_id=USER_ID).outcome == FoldOutcome.NOTHING_ELIGIBLE


def test_pg_activity_migration_is_nullable_indexed_and_reversible(pg_migration_db, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', pg_migration_db)
    cfg = config()
    command.upgrade(cfg, '20260920_01')
    engine = create_engine(pg_migration_db)
    try:
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users(id, is_anonymous) VALUES (9021, true)"))
            conn.execute(text("INSERT INTO game_sessions(id, user_id, status, engine_elo, started_at) "
                              "VALUES ('00000000-0000-0000-0000-000000009021', 9021, 'active', 1500, clock_timestamp())"))
        command.upgrade(cfg, '20260923_01')
        columns = {c['name']: c for c in inspect(engine).get_columns('game_sessions')}
        assert columns['last_activity_at']['nullable']
        model_column = GameSession.__table__.c.last_activity_at
        assert model_column.nullable and model_column.type.timezone
        indexes = {i['name']: i['column_names'] for i in inspect(engine).get_indexes('game_sessions')}
        assert indexes['idx_game_sessions_user_activity'] == ['user_id', 'last_activity_at']
        with engine.connect() as conn:
            assert conn.scalar(text('SELECT last_activity_at FROM game_sessions')) is None
        command.downgrade(cfg, '20260920_01')
        assert 'last_activity_at' not in {c['name'] for c in inspect(engine).get_columns('game_sessions')}
        command.upgrade(cfg, '20260923_01')
    finally:
        engine.dispose()
