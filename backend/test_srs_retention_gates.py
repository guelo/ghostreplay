"""Combined retention qualification; synthetic facts only, no production data."""
from __future__ import annotations

import json
import random
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from conftest import LIMITS, _create_test_schema, pg_required
from app import opportunity_fold_recovery, opportunity_retention
from app.models import (
    Blunder, BlunderOpportunityEvent, BlunderReview,
    GameSession, OpponentDecision, OpportunityFoldBatch, OpportunityRetentionPolicy,
    Position, SessionMove, User,
)
from app.opponent_target_facts import backfill_target_facts
from app.opportunity_fold import FoldOutcome, fold_user_batch
from app.opportunity_store import load_policy, record_review_basis, session_evidence_frozen
from app.srs_opportunity import load_opportunity_counters, practice_priority_score
from srs_raw_oracle import load_opportunity_counters as raw_counters
from test_opportunity_compaction import (
    DISTINCT_FENS, _blunder, _decision, _enable_folding, _event, _session,
)

USER = 9751
TABLES = (
    'blunder_opportunity_events', 'blunder_opportunity_summaries',
    'user_opportunity_retention_state', 'opportunity_retention_policy',
    'opportunity_fold_batches',
)


def _mirror_original_facts(source, oracle):
    """Append/update original facts only; NEVER delete facts absent after folding.

    Mutable repair replacements are applied explicitly by the caller. No
    summaries, manifests, prefixes or compactor outputs enter the oracle.
    """
    for model in (User, Position, Blunder, GameSession, BlunderReview,
                  OpponentDecision, BlunderOpportunityEvent):
        table = model.__table__
        keys = [column.name for column in table.primary_key]
        for row in source.execute(select(table)).mappings():
            stmt = sqlite_insert(table).values(dict(row))
            oracle.execute(stmt.on_conflict_do_update(
                index_elements=keys,
                set_={c.name: stmt.excluded[c.name] for c in table.c if c.name not in keys},
            ))
    oracle.commit()


@pg_required
@pytest.mark.parametrize('seed', [7, 29])
def test_pg_randomized_commits_preserve_the_raw_oracle(
    pg_engine, pg_session_factory, qualification_clock, monkeypatch, seed,
):
    rng = random.Random(seed)
    now = qualification_clock
    oracle_engine = create_engine('sqlite://')
    with oracle_engine.begin() as conn:
        _create_test_schema(conn)
    try:
        with pg_session_factory() as db, Session(oracle_engine) as oracle:
            blunders = [_blunder(db, user_id=USER, fen=fen,
                created_at=now[0] - timedelta(days=400)) for fen in DISTINCT_FENS]
            ids = [b.id for b in blunders]
            sessions = []
            for age in (150, 100, 61, 59, 1, 0):
                game = _session(db, user_id=USER, started_at=now[0] - timedelta(days=age))
                sessions.append(game)
                for b in blunders:
                    _event(db, blunder=b, game_session=game,
                           reached=rng.choice([True, False]))
                # Bootstrap targets include a still-live pin in an old session.
                _decision(db, game_session=game, blunder=blunders[0],
                          served_at=now[0] - timedelta(days=min(age, 29)))
            from app.api.game import NextOpponentMoveResponse
            for decision in db.scalars(select(OpponentDecision)):
                decision.response_payload = NextOpponentMoveResponse(
                    mode='ghost', move={'uci': 'e7e5', 'san': 'e5'},
                    target_blunder_id=decision.target_blunder_id,
                    decision_id=decision.decision_id, decision_source='ghost_path',
                ).model_dump_json()
            _enable_folding(db)
            db.commit()
            _mirror_original_facts(db, oracle)
            db.commit()
            deleted = 0
            # Every seed exercises every operation, in a different commit order.
            operations = ['fold', 'review', 'upload', 'repair', 'policy', 'clock', 'replay'] * 5
            rng.shuffle(operations)
            for step, operation in enumerate(operations):
                game = rng.choice(sessions)
                b = rng.choice(blunders)
                if operation == 'fold':
                    db.commit()
                    result = fold_user_batch(pg_engine, user_id=USER, limits=LIMITS)
                    assert result.outcome in (FoldOutcome.FOLDED, FoldOutcome.NOTHING_ELIGIBLE)
                    deleted += result.rows_deleted
                    db.expire_all()
                elif operation == 'review':
                    # Same lock and summary reset contract as the review writer;
                    # old sessions remain valid review sources after folding.
                    db.execute(select(Blunder.id).where(Blunder.id == b.id).with_for_update(key_share=True))
                    review = BlunderReview(blunder_id=b.id, session_id=game.id,
                        reviewed_at=now[0], passed=True, move_played_san='good', eval_delta_cp=0)
                    db.add(review)
                    db.flush()
                    record_review_basis(db, blunder_id=b.id, review_id=review.id,
                        reviewed_at=review.reviewed_at, session_id=game.id, policy=load_policy(db))
                    db.commit()
                elif operation in ('upload', 'repair'):
                    from scripts.recompute_srs_opportunities import recompute_srs_opportunities
                    # The actual evidence/repair writer; an empty upload retires
                    # mutable stale rows, while frozen sessions must be skipped.
                    frozen = session_evidence_frozen(db, user_id=USER, session_id=game.id)
                    report = recompute_srs_opportunities(db, session_id=game.id, progress_every=0)
                    assert report.frozen_sessions == int(frozen)
                    if not frozen:
                        # An empty mutable history intentionally retires its
                        # evidence; apply that supported deletion to the oracle.
                        oracle.execute(delete(BlunderOpportunityEvent).where(
                            BlunderOpportunityEvent.session_id == game.id))
                        oracle.commit()
                    if operation == 'upload':
                        fresh = _session(db, user_id=USER, started_at=now[0])
                        sessions.append(fresh)
                        db.add(SessionMove(session_id=fresh.id, move_number=1,
                            color='black', move_san='move', fen_after=db.get(Position, b.position_id).fen_raw))
                        db.commit()
                        assert recompute_srs_opportunities(db, session_id=fresh.id,
                            progress_every=0).processed_sessions == 1
                elif operation == 'policy':
                    policy = db.get(OpportunityRetentionPolicy, 1)
                    policy.mutation_window_days = rng.choice([60, 90, 180])
                    policy.version += 1
                    db.commit()
                elif operation == 'clock':
                    now[0] += timedelta(days=31, microseconds=1)
                    db.commit()
                else:
                    # Replay is read-only: neither served_at nor facts are renewed.
                    from app.api.game import _replay_decision
                    decision = db.scalar(select(OpponentDecision).limit(1))
                    if decision is not None:
                        stamp = decision.served_at
                        _replay_decision(db, decision.session_id, decision.request_fingerprint)
                        assert decision.served_at == stamp
                    db.commit()
                if operation != 'fold':
                    _mirror_original_facts(db, oracle)
                # Test both backfilled facts and original decisions against the
                # SAME immutable pre-retention SQL reader, after EVERY commit.
                backfill_target_facts(db)
                db.commit()
                for target_source in ('decisions', 'facts'):
                    monkeypatch.setenv('OPPONENT_TARGET_SOURCE', target_source)
                    actual = load_opportunity_counters(db, ids, user_id=USER, now=now[0])
                    expected = raw_counters(oracle, ids, user_id=USER, now=now[0])
                    assert actual == expected, (seed, step, operation, target_source)
                    for bid in ids:
                        kwargs = dict(eval_loss_cp=200, pass_streak=2,
                                      last_reviewed_at=None, created_at=now[0]-timedelta(days=400), now=now[0])
                        assert practice_priority_score(counters=actual[bid], **kwargs) == practice_priority_score(counters=expected[bid], **kwargs)
                    db.commit()
                # Excluding a permanently frozen session must fail explicitly.
                if session_evidence_frozen(db, user_id=USER, session_id=game.id):
                    with pytest.raises(opportunity_retention.RetentionInvariantError, match='frozen'):
                        load_opportunity_counters(db, ids, user_id=USER, now=now[0], exclude_session_id=game.id)
                else:
                    assert load_opportunity_counters(db, ids, user_id=USER, now=now[0], exclude_session_id=game.id) == raw_counters(oracle, ids, user_id=USER, now=now[0], exclude_session_id=game.id)
                db.commit()
            assert deleted > 0
    finally:
        oracle_engine.dispose()


def _relation_bytes(engine):
    with engine.connect() as conn:
        return {name: int(conn.scalar(text('SELECT pg_total_relation_size(CAST(:name AS regclass))'), {'name': name})) for name in TABLES}


def _packed_bytes(engine):
    # Only in this disposable synthetic test DB. Rewrites are measurement, never
    # an operational recommendation or a claim that ordinary DELETE shrinks disk.
    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
        for name in TABLES:
            conn.execute(text(f'VACUUM FULL ANALYZE {name}'))
    return _relation_bytes(engine)


@pg_required
def test_pg_two_turnovers_account_for_net_storage(
    pg_engine, pg_session_factory, qualification_clock, tmp_path,
):
    now = qualification_clock
    reports = []
    with pg_session_factory() as db:
        blunders = [_blunder(db, user_id=USER, fen=fen,
            created_at=now[0]-timedelta(days=1000)) for fen in DISTINCT_FENS]
        _enable_folding(db)
        db.commit()
        for turnover in range(2):
            # 4,000 old pairs and 800 young pairs per turnover. Half carry high
            # targeting, half have none; no reviews preserves lifetime extremes.
            for i in range(1200):
                game = _session(db, user_id=USER, started_at=now[0]-timedelta(days=61 if i < 1000 else 1))
                for index, b in enumerate(blunders):
                    _event(db, blunder=b, game_session=game, reached=(i % 2 == 0))
                    if index < 2:
                        _decision(db, game_session=game, blunder=b,
                                  served_at=now[0]-timedelta(days=31 if i < 1000 else 0.5))
            db.commit()
            baseline = _packed_bytes(pg_engine)
            before = load_opportunity_counters(db, [b.id for b in blunders], user_id=USER, now=now[0])
            db.commit()
            for _ in range(49):
                result = fold_user_batch(pg_engine, user_id=USER, limits=LIMITS)
                if result.outcome == FoldOutcome.NOTHING_ELIGIBLE:
                    break
                assert result.outcome == FoldOutcome.FOLDED
            else:
                pytest.fail('small fixture did not drain within 49 batches')
            db.expire_all()
            assert db.query(BlunderOpportunityEvent).count() == 800
            assert load_opportunity_counters(db, [b.id for b in blunders], user_id=USER, now=now[0]) == before
            assert all(b.event_count == (turnover+1)*1200 for b in before.values())
            db.commit()
            allocated = _relation_bytes(pg_engine)
            packed = _packed_bytes(pg_engine)
            exports = sum(path.stat().st_size for path in (tmp_path/'exports').rglob('*') if path.is_file())
            export_allocated = sum(path.stat().st_blocks * 512 for path in (tmp_path/'exports').rglob('*') if path.is_file())
            assert exports > 0
            # The existing recovery remains present in this worst-case sample.
            assert db.query(OpportunityFoldBatch).count() > 0
            db.commit()
            now[0] += timedelta(days=8)
            opportunity_fold_recovery.expire_fold_artifacts(pg_engine)
            after_expiry = _packed_bytes(pg_engine)
            assert not list((tmp_path/'exports').glob('*.json'))
            report = dict(turnover=turnover+1, baseline=baseline, allocated_after_delete=allocated,
                          packed_before_expiry=packed, export_bytes=exports, export_allocated_bytes=export_allocated, packed_after_expiry=after_expiry,
                          pre_expiry_total=sum(packed.values())+exports,
                          post_expiry_total=sum(after_expiry.values()))
            reports.append(report)
            # Parent Gate A is explicitly AFTER catch-up and artifact expiry.
            assert report['post_expiry_total'] <= baseline['blunder_opportunity_events'] * .5, report
            now[0] += timedelta(days=60, hours=1)
        version = db.scalar(text('SHOW server_version'))
        db.commit()
    print('SRS_STORAGE_REPORT=' + json.dumps(dict(server_version=version, turnovers=reports), sort_keys=True))
