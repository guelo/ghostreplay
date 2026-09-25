"""Actual publication/fold transfer races, with independent connections/barriers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import chess
import pytest

from conftest import LIMITS, pg_required
from app import opportunity_fold, opponent_retention, srs_target_admission
from app.api import game as game_api
from app.maia3_client import Maia3Error
from app.models import (
    Blunder, BlunderOpportunityEvent, BlunderOpportunitySummary, OpponentDecision, OpponentTargetFact, OpportunityRetentionPolicy,
)
from app.opportunity_fold import FoldOutcome, fold_user_batch
from test_srs_target_publication_pg import (
    AFTER_E4_FEN, USER_ID, _record_target, _seed,
)

pytestmark = pg_required


def _history(factory, clock, monkeypatch):
    # Only the clock expression is substituted. All admission, SQL, locking,
    # insertion, pin selection, folding and fallback code runs unmodified.
    for module in (srs_target_admission, opponent_retention):
        monkeypatch.setattr(module, 'database_clock', opportunity_fold.database_clock)
    started = clock[0] - timedelta(days=60) + timedelta(seconds=1)
    with factory() as db:
        sid, bid = _seed(db, started_at=started)
        db.get(Blunder, bid).created_at = started - timedelta(days=1)
        db.add(BlunderOpportunitySummary(blunder_id=bid))
        db.add(BlunderOpportunityEvent(session_id=sid, blunder_id=bid,
            occurred_at=started, opportunity=True, reached=True))
        policy = db.get(OpportunityRetentionPolicy, 1)
        policy.readiness = policy.freeze_enabled = policy.cleanup_enabled = True
        db.commit()
    return sid, bid


@pytest.mark.parametrize('finish', ['commit', 'rollback', 'disconnect'])
def test_pg_final_stamp_interlocks_with_real_fold_past_grace(
    pg_engine, pg_session_factory, qualification_clock, monkeypatch, finish,
):
    clock = qualification_clock
    sid, bid = _history(pg_session_factory, clock, monkeypatch)
    stamped, release = Event(), Event()
    real_fact = game_api.publish_target_fact

    def pause(db, **kwargs):
        real_fact(db, **kwargs)
        stamped.set()
        assert release.wait(4)
        if finish == 'disconnect':
            db.invalidate()
            raise RuntimeError('injected disconnect')
        if finish == 'rollback':
            raise RuntimeError('injected rollback')

    monkeypatch.setattr(game_api, 'publish_target_fact', pause)
    def publish():
        with pg_session_factory() as db:
            if finish == 'commit':
                return _record_target(db, sid, bid)
            with pytest.raises(RuntimeError, match='injected'):
                _record_target(db, sid, bid)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(publish)
        try:
            assert stamped.wait(4)
            clock[0] += timedelta(hours=1, seconds=2)
            # A committed global shortening cannot retroactively cancel the
            # version under which this already-stamped publication was admitted.
            with pg_session_factory() as db:
                policy = db.get(OpportunityRetentionPolicy, 1)
                policy.mutation_window_days = 30
                policy.version += 1
                db.commit()
            # Request spans M and G while holding the real publication lock.
            result = fold_user_batch(pg_engine, user_id=USER_ID, limits=LIMITS)
            assert result.outcome == FoldOutcome.SKIPPED_PUBLICATION
        finally:
            release.set()
        pending.result(timeout=10)
    result = fold_user_batch(pg_engine, user_id=USER_ID, limits=LIMITS)
    if finish == 'commit':
        assert result.outcome == FoldOutcome.NOTHING_ELIGIBLE
        with pg_session_factory() as db:
            winner = db.query(OpponentDecision).one()
            assert winner.served_at < clock[0] - timedelta(hours=1)
            replay = game_api._replay_decision(db, sid, winner.request_fingerprint)
            assert replay.decision_id == winner.decision_id
            assert replay.target_blunder_id == bid
            assert db.query(OpponentTargetFact).count() == 1
            assert db.query(BlunderOpportunityEvent).count() == 1
        clock[0] += timedelta(days=30)
        assert fold_user_batch(pg_engine, user_id=USER_ID, limits=LIMITS).rows_deleted == 1
    else:
        assert result.rows_deleted == 1
        with pg_session_factory() as db:
            assert db.query(OpponentDecision).count() == 0
            assert db.query(OpponentTargetFact).count() == 0


def test_pg_computation_outliving_grace_falls_back_after_real_fold(
    pg_client, pg_engine, pg_session_factory, qualification_clock, monkeypatch,
    auth_headers, caplog,
):
    clock = qualification_clock
    sid, bid = _history(pg_session_factory, clock, monkeypatch)
    computed, release = Event(), Event()
    original_search = game_api.find_ghost_move

    def search(*args, **kwargs):
        result = original_search(*args, **kwargs)
        assert result[1] == bid
        computed.set()
        assert release.wait(4)
        return result

    monkeypatch.setattr(game_api, 'find_ghost_move', search)
    def unavailable(*args, **kwargs):
        raise Maia3Error('qualification outage')
    monkeypatch.setattr('app.opponent_move_controller.choose_move', unavailable)
    request = dict(session_id=str(sid), fen=AFTER_E4_FEN, moves=['e2e4'])
    def post():
        return pg_client.post('/api/game/next-opponent-move', json=request,
                              headers=auth_headers(user_id=USER_ID))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(post)
        try:
            assert computed.wait(4)
            clock[0] += timedelta(minutes=30)
            assert fold_user_batch(pg_engine, user_id=USER_ID, limits=LIMITS).outcome == FoldOutcome.NOTHING_ELIGIBLE
            clock[0] += timedelta(hours=1)
            assert fold_user_batch(pg_engine, user_id=USER_ID, limits=LIMITS).rows_deleted == 1
            # Increasing M cannot reopen the prefix already consumed by folding.
            with pg_session_factory() as db:
                policy = db.get(OpportunityRetentionPolicy, 1)
                policy.mutation_window_days = 180
                policy.version += 1
                db.commit()
        finally:
            release.set()
        response = pending.result(timeout=10)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['target_blunder_id'] is None
    assert 'targeting after fold' in caplog.text
    board = chess.Board(AFTER_E4_FEN)
    assert chess.Move.from_uci(body['move']['uci']) in board.legal_moves
    board.push_uci(body['move']['uci'])
    assert post().json() == body
    with pg_session_factory() as db:
        winner = db.query(OpponentDecision).one()
        assert winner.resulting_fen == board.fen()
        assert winner.target_blunder_id is None
        assert db.query(OpponentTargetFact).count() == 0
        assert db.query(BlunderOpportunityEvent).count() == 0
        # A subsequent evidence repair cannot recreate the folded contribution.
        from scripts.recompute_srs_opportunities import recompute_srs_opportunities
        assert recompute_srs_opportunities(db, session_id=sid).frozen_sessions == 1
        assert db.query(BlunderOpportunityEvent).count() == 0


@pytest.mark.parametrize('wait_kind', ['short', 'timeout'])
def test_pg_endpoint_waits_for_actual_fold_then_rechecks_or_degrades(
    pg_client, pg_engine, pg_session_factory, qualification_clock, monkeypatch,
    auth_headers, caplog, wait_kind,
):
    from sqlalchemy import text
    from conftest import await_pg_lock
    from test_opportunity_compaction import _session

    clock = qualification_clock
    sid, bid = _history(pg_session_factory, clock, monkeypatch)
    with pg_session_factory() as db:
        old = _session(db, user_id=USER_ID, started_at=clock[0]-timedelta(days=200))
        db.add(BlunderOpportunityEvent(session_id=old.id, blunder_id=bid,
            occurred_at=old.started_at, opportunity=True, reached=False))
        # Both policy and clock change while publication waits.
        db.get(OpportunityRetentionPolicy, 1).mutation_window_days = 180
        db.commit()
    held, release, entering = Event(), Event(), Event()
    pids = {}
    real_fold_lock = opportunity_fold.lock_state_for_fold
    real_share = srs_target_admission._lock_state_row

    def hold(db, **kwargs):
        result = real_fold_lock(db, **kwargs)
        assert result
        pids['fold'] = db.scalar(text('SELECT pg_backend_pid()'))
        held.set()
        assert release.wait(4)
        return result

    def share(db, user_id):
        pids['publication'] = db.scalar(text('SELECT pg_backend_pid()'))
        entering.set()
        return real_share(db, user_id)

    monkeypatch.setattr(opportunity_fold, 'lock_state_for_fold', hold)
    monkeypatch.setattr(srs_target_admission, '_lock_state_row', share)
    def unavailable(*args, **kwargs):
        raise Maia3Error('qualification outage')
    monkeypatch.setattr('app.opponent_move_controller.choose_move', unavailable)
    request = dict(session_id=str(sid), fen=AFTER_E4_FEN, moves=['e2e4'])
    def post():
        return pg_client.post('/api/game/next-opponent-move', json=request,
                              headers=auth_headers(user_id=USER_ID))

    with ThreadPoolExecutor(max_workers=2) as pool:
        folding = pool.submit(fold_user_batch, pg_engine, user_id=USER_ID, limits=LIMITS)
        try:
            assert held.wait(4)
            publishing = pool.submit(post)
            assert entering.wait(4)
            with pg_engine.connect() as observer:
                await_pg_lock(observer, pids['publication'], pids['fold'])
            if wait_kind == 'short':
                with pg_session_factory() as db:
                    policy = db.get(OpportunityRetentionPolicy, 1)
                    policy.mutation_window_days = 60
                    policy.version += 1
                    db.commit()
                clock[0] += timedelta(seconds=2)
                release.set()
            response = publishing.result(timeout=4)
            assert response.status_code == 200, response.text
        finally:
            release.set()
        fold_result = folding.result(timeout=5)
        if wait_kind == 'short':
            assert fold_result.outcome == FoldOutcome.STALE_REPREPARE
            monkeypatch.setattr(opportunity_fold, 'lock_state_for_fold', real_fold_lock)
            assert fold_user_batch(pg_engine, user_id=USER_ID, limits=LIMITS).rows_deleted == 1
        else:
            assert fold_result.rows_deleted == 1
    body = response.json()
    assert body['target_blunder_id'] is None
    reason = 'mutation_window_expired' if wait_kind == 'short' else 'state_lock_timeout'
    assert reason in caplog.text
    assert chess.Move.from_uci(body['move']['uci']) in chess.Board(AFTER_E4_FEN).legal_moves
    assert post().json() == body
    with pg_session_factory() as db:
        assert db.query(OpponentDecision).count() == 1
        assert db.query(OpponentTargetFact).count() == 0
        assert srs_target_admission.lock_state_for_fold(db, user_id=USER_ID)


@pytest.mark.parametrize('damage', ['policy', 'targeted_history'])
def test_pg_missing_authority_serves_legal_replay_without_maia(
    pg_client, pg_session_factory, qualification_clock, monkeypatch, auth_headers,
    caplog, damage,
):
    from sqlalchemy import delete
    from app.models import UserOpportunityRetentionState

    clock = qualification_clock
    sid, _ = _history(pg_session_factory, clock, monkeypatch)
    with pg_session_factory() as db:
        if damage == 'policy':
            db.execute(delete(OpportunityRetentionPolicy))
        else:
            db.get(UserOpportunityRetentionState, USER_ID).targeted_discarded_max_served_at = clock[0]
        db.commit()
    def unavailable(*args, **kwargs):
        raise Maia3Error('qualification outage')
    monkeypatch.setattr('app.opponent_move_controller.choose_move', unavailable)
    request = dict(session_id=str(sid), fen=AFTER_E4_FEN, moves=['e2e4'])
    response = pg_client.post('/api/game/next-opponent-move', json=request,
                             headers=auth_headers(user_id=USER_ID))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['target_blunder_id'] is None
    assert chess.Move.from_uci(body['move']['uci']) in chess.Board(AFTER_E4_FEN).legal_moves
    assert pg_client.post('/api/game/next-opponent-move', json=request,
                          headers=auth_headers(user_id=USER_ID)).json() == body
    assert ('missing_retention_policy' if damage == 'policy' else 'counters unavailable') in caplog.text
    with pg_session_factory() as db:
        assert db.query(OpponentDecision).count() == 1
        assert db.query(OpponentTargetFact).count() == 0


def test_pg_evidence_worker_commit_outliving_grace_is_not_lost(
    pg_engine, pg_session_factory, qualification_clock, monkeypatch,
):
    from sqlalchemy import delete
    from app import opportunity_store
    from app.api.session import _run_graph_evidence_txn
    from app.models import SessionMove
    from app.srs_opportunity import load_opportunity_counters
    from test_srs_target_publication_pg import AFTER_E4_E5_PLAYED_FEN

    clock = qualification_clock
    # Keep actual host-time trigger predicates away from a deadline: this test
    # advances only the application SQL clock, not the server or host clocks.
    clock[0] += timedelta(days=1)
    sid, bid = _history(pg_session_factory, clock, monkeypatch)
    with pg_session_factory() as db:
        db.execute(delete(BlunderOpportunityEvent))
        db.add(SessionMove(session_id=sid, move_number=1, color='black', move_san='e5',
                           fen_before=AFTER_E4_FEN, fen_after=AFTER_E4_E5_PLAYED_FEN))
        db.commit()
    admitted, release = Event(), Event()
    real_frozen = opportunity_store.session_evidence_frozen
    def pause(db, **kwargs):
        frozen = real_frozen(db, **kwargs)
        assert not frozen
        admitted.set()
        assert release.wait(4)
        return frozen
    monkeypatch.setattr(opportunity_store, 'session_evidence_frozen', pause)
    def worker():
        with pg_session_factory() as db:
            _run_graph_evidence_txn(db, session_id=sid, user_id=USER_ID,
                player_color='white', evidence_moves=[], move_count=1,
                dialect_name='postgresql', run_opportunity=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(worker)
        try:
            assert admitted.wait(4)
            clock[0] += timedelta(hours=1, seconds=2)
            # No candidate exists yet; a second old pair makes the real fold
            # attempt reach the advisory lock rather than stop at preparation.
            with pg_session_factory() as db:
                from test_opportunity_compaction import _session
                old = _session(db, user_id=USER_ID, started_at=clock[0]-timedelta(days=100))
                db.add(BlunderOpportunityEvent(session_id=old.id, blunder_id=bid,
                    occurred_at=old.started_at, opportunity=False, reached=False))
                db.commit()
            assert fold_user_batch(pg_engine, user_id=USER_ID, limits=LIMITS).outcome == FoldOutcome.SKIPPED_USER_BUSY
        finally:
            release.set()
        pending.result(timeout=10)
    monkeypatch.setattr(opportunity_store, 'session_evidence_frozen', real_frozen)
    with pg_session_factory() as db:
        before = load_opportunity_counters(db, [bid], user_id=USER_ID, now=clock[0])
        assert before[bid].event_count == 1
        db.commit()
    assert fold_user_batch(pg_engine, user_id=USER_ID, limits=LIMITS).rows_deleted == 2
    with pg_session_factory() as db:
        assert load_opportunity_counters(db, [bid], user_id=USER_ID, now=clock[0]) == before


def test_pg_missing_state_is_created_before_target_publication(
    pg_client, pg_session_factory, qualification_clock, monkeypatch, auth_headers,
):
    from sqlalchemy import delete
    from app.models import UserOpportunityRetentionState

    with pg_session_factory() as db:
        sid, bid = _seed(db)
        # A new owner has no folded/raw state to purge. Remove the helper's
        # migration-style state before installing their first summary.
        db.execute(delete(UserOpportunityRetentionState))
        db.commit()
        db.add(BlunderOpportunitySummary(blunder_id=bid))
        policy = db.get(OpportunityRetentionPolicy, 1)
        policy.readiness = policy.freeze_enabled = True
        db.commit()
    def no_engine(*args, **kwargs):
        pytest.fail('new-user state initialization must preserve the selected target')
    monkeypatch.setattr('app.opponent_move_controller.choose_move', no_engine)
    response = pg_client.post('/api/game/next-opponent-move',
        json=dict(session_id=str(sid), fen=AFTER_E4_FEN, moves=['e2e4']),
        headers=auth_headers(user_id=USER_ID))
    assert response.status_code == 200, response.text
    assert response.json()['target_blunder_id'] == bid
    with pg_session_factory() as db:
        assert db.get(UserOpportunityRetentionState, USER_ID) is not None
        assert db.query(OpponentTargetFact).count() == 1


def test_pg_state_creation_failure_rolls_back_before_legal_fallback(
    pg_client, pg_session_factory, pg_engine, monkeypatch, auth_headers, caplog,
):
    """Inject a real SQL integrity failure, not a mocked admission verdict."""
    from sqlalchemy import delete, text
    from app.models import UserOpportunityRetentionState

    with pg_session_factory() as db:
        sid, _ = _seed(db)
        db.execute(delete(UserOpportunityRetentionState))
        db.commit()
    with pg_engine.begin() as conn:
        conn.execute(text("""
            CREATE FUNCTION srs_qualification_reject_state() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                RAISE EXCEPTION 'qualification: state creation unavailable'
                    USING ERRCODE = '23503';
            END $$
        """))
        conn.execute(text("""
            CREATE TRIGGER srs_qualification_reject_state
            BEFORE INSERT ON user_opportunity_retention_state
            FOR EACH ROW EXECUTE FUNCTION srs_qualification_reject_state()
        """))
    try:
        def unavailable(*args, **kwargs):
            raise Maia3Error('qualification outage')
        monkeypatch.setattr('app.opponent_move_controller.choose_move', unavailable)
        request = dict(session_id=str(sid), fen=AFTER_E4_FEN, moves=['e2e4'])
        def post():
            return pg_client.post('/api/game/next-opponent-move', json=request,
                                  headers=auth_headers(user_id=USER_ID))
        response = post()
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['target_blunder_id'] is None
        assert 'missing_retention_state' in caplog.text
        assert chess.Move.from_uci(body['move']['uci']) in chess.Board(AFTER_E4_FEN).legal_moves
        assert post().json() == body
        with pg_session_factory() as db:
            assert db.query(OpponentDecision).count() == 1
            assert db.query(OpponentTargetFact).count() == 0
            assert db.get(UserOpportunityRetentionState, USER_ID) is None
    finally:
        with pg_engine.begin() as conn:
            conn.execute(text('DROP TRIGGER srs_qualification_reject_state ON user_opportunity_retention_state'))
            conn.execute(text('DROP FUNCTION srs_qualification_reject_state()'))
