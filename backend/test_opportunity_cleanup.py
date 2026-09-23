"""Scheduling contracts use virtual time; transfer/eligibility use real SQL."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from conftest import engine
from app import opportunity_cleanup as cleanup, opportunity_fold as fold
from app.models import OpponentTargetFact
from app.opportunity_cleanup import Backlog
from app.opportunity_fold import FoldOutcome, FoldResult
from app.session_activity import known_inflight
from app.opportunity_retention import database_clock
from app.srs_math import as_utc
from test_opportunity_compaction import (_blunder, _session, _event, _enable_folding,
                                        _simple, PATIENT)


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert 0 < seconds <= 1
        self.now += seconds


@pytest.fixture
def schedule(db_session, monkeypatch):
    _enable_folding(db_session)
    db_session.commit()
    clock = Clock()
    state = {1: Backlog(200, 12 * 3600)}
    calls = []
    active = {1: True}
    outcome = [FoldOutcome.SKIPPED_USER_BUSY]
    monkeypatch.setattr(cleanup, 'eligible_users', lambda *a, **k: list(state))
    monkeypatch.setattr(cleanup, 'backlog', lambda e, uid: state[uid])
    monkeypatch.setattr(cleanup, 'recently_active', lambda e, uid, **k: active.get(uid, True))

    def attempt(e, *, user_id, activity_check, **kw):
        if activity_check and activity_check():
            return FoldResult(FoldOutcome.DEFERRED_ACTIVITY, user_id)
        calls.append((user_id, clock(), activity_check, kw['max_pairs']))
        return FoldResult(outcome[0], user_id)

    monkeypatch.setattr(cleanup, 'fold_user_batch', attempt)
    return SimpleNamespace(clock=clock, state=state, calls=calls, active=active,
                           outcome=outcome,
                           run=lambda **kw: cleanup.scheduled_sweep(
                               engine, clock=clock, sleep=clock.sleep, **kw))


@pytest.mark.parametrize('lag,first,attempts', [
    (12 * 3600 - .001, None, 0), (12 * 3600, 60, 3),
    (18 * 3600 - .001, 60, 3), (18 * 3600, 0, 3),
])
def test_exact_escalation_boundaries_and_one_quiet_recheck(schedule, lag, first, attempts):
    schedule.state[1] = Backlog(200, lag)
    report = schedule.run(max_attempts=3)
    assert len(schedule.calls) == attempts
    if attempts:
        assert schedule.calls[0][1] == first
        assert [c[1] for c in schedule.calls] == [first, first + 1, first + 2]
        assert all(c[2] is None for c in schedule.calls)
        assert report.capped == [1]
    else:
        assert report.deferred == [1]
    assert report.remaining[1] == Backlog(200, lag)


@pytest.mark.parametrize('outcome', [FoldOutcome.SKIPPED_PUBLICATION,
    FoldOutcome.SKIPPED_USER_BUSY, FoldOutcome.DEFERRED_ROW_BUSY,
    FoldOutcome.STALE_REPREPARE, FoldOutcome.EXPORT_FAILED, FoldOutcome.NOTHING_ELIGIBLE])
def test_every_unsuccessful_attempt_counts_without_false_completion(schedule, outcome):
    schedule.outcome[0] = outcome
    report = schedule.run(max_attempts=3)
    assert report.sweep.outcomes == {outcome.value: 3}
    assert report.remaining[1].pairs == 200
    assert report.capped == [1]


def test_charged_time_includes_cooldown_but_not_quiet_recheck(schedule):
    report = schedule.run(max_seconds=2)
    assert [c[1] for c in schedule.calls] == [60, 61]
    assert report.capped == [1]


def test_saturated_users_rotate_and_quiet_users_do_not_block_others(schedule):
    schedule.state.update({2: Backlog(50, 18 * 3600), 3: Backlog(50, 24 * 3600)})
    report = schedule.run(max_attempts=2)
    assert [c[0] for c in schedule.calls] == [2, 3, 2, 3, 1, 1]
    assert report.alert_users == [3]
    assert len(report.capped) == 3


def test_each_user_gets_120_seconds_of_attempts_and_cooldown(schedule, monkeypatch):
    schedule.state.update({uid: Backlog(200, 18 * 3600) for uid in (1, 2, 3)})
    def attempt(e, *, user_id, stop_check, **kw):
        assert not stop_check()
        schedule.calls.append(user_id)
        schedule.clock.now += 1  # One second of work plus one second cooldown.
        assert not stop_check()
        return FoldResult(FoldOutcome.SKIPPED_USER_BUSY, user_id)
    monkeypatch.setattr(cleanup, 'fold_user_batch', attempt)
    report = schedule.run()
    assert schedule.calls == [1, 2, 3] * 60
    assert schedule.clock() == 181  # Includes the final user's cooldown.
    assert report.capped == [1, 2, 3]
    assert report.remaining == schedule.state


def test_attempt_budget_uses_prior_charge_and_current_work(schedule, monkeypatch):
    schedule.state[1] = Backlog(200, 18 * 3600)
    def attempt(e, *, user_id, stop_check, **kw):
        assert not stop_check()
        schedule.calls.append(schedule.clock())
        schedule.clock.now += 2
        exhausted = stop_check()
        assert exhausted == (len(schedule.calls) == 2)
        return FoldResult(FoldOutcome.DEFERRED_BUDGET if exhausted
                          else FoldOutcome.SKIPPED_USER_BUSY, user_id)
    monkeypatch.setattr(cleanup, 'fold_user_batch', attempt)
    report = schedule.run(max_seconds=5)
    assert schedule.calls == [0, 3]
    assert report.capped == [1]


def test_partial_progress_preserves_forced_visit_and_stops_only_below_12h(schedule, monkeypatch):
    def attempt(e, *, user_id, activity_check, **kw):
        assert activity_check is None
        schedule.calls.append(schedule.clock())
        # Activity never grants another quiet recheck; partial folds keep age.
        schedule.state[1] = Backlog(100, 12 * 3600 if len(schedule.calls) == 1 else 43199)
        return FoldResult(FoldOutcome.FOLDED, 1, rows_deleted=25, blunders=1)
    monkeypatch.setattr(cleanup, 'fold_user_batch', attempt)
    report = schedule.run()
    assert schedule.calls == [60, 61]
    assert report.sweep.rows_deleted == 50
    assert report.remaining[1] == Backlog(100, 43199)
    assert not report.capped


def test_normal_activity_return_stops_revisits(schedule, monkeypatch):
    schedule.state[1] = Backlog(100, 3600)
    schedule.active[1] = False
    def attempt(e, *, activity_check, **kw):
        if activity_check():
            return FoldResult(FoldOutcome.DEFERRED_ACTIVITY, 1)
        schedule.active[1] = True
        return FoldResult(FoldOutcome.FOLDED, 1, rows_deleted=25)
    monkeypatch.setattr(cleanup, 'fold_user_batch', attempt)
    report = schedule.run()
    assert report.sweep.batches == 1
    assert report.deferred == [1]
    assert report.remaining[1].pairs == 100


def test_normal_mode_checks_activity_once_before_preparation(schedule, monkeypatch):
    schedule.state[1] = Backlog(100, 3600)
    checks = []
    def active(*a, **kw):
        checks.append(True)
        return False
    monkeypatch.setattr(cleanup, 'recently_active', active)
    schedule.run(max_attempts=1)
    assert checks == [True]


def test_policy_disabled_mid_sweep_stops_attempts_but_measures_backlog(schedule):
    schedule.state.update({uid: Backlog(200, 18 * 3600) for uid in (1, 2, 3)})
    schedule.outcome[0] = FoldOutcome.DISABLED
    report = schedule.run()
    assert [c[0] for c in schedule.calls] == [1]
    assert report.disabled
    assert report.remaining == schedule.state


def test_shutdown_skips_final_discovery_and_backlog_reads(schedule, monkeypatch):
    stopped = [False]
    def attempt(e, *, user_id, **kw):
        stopped[0] = True
        monkeypatch.setattr(cleanup, 'eligible_users', lambda *a, **k: pytest.fail('discovery'))
        monkeypatch.setattr(cleanup, 'backlog', lambda *a, **k: pytest.fail('backlog'))
        return FoldResult(FoldOutcome.DEFERRED_BUDGET, user_id)
    monkeypatch.setattr(cleanup, 'fold_user_batch', attempt)
    report = schedule.run(stopped=lambda: stopped[0])
    assert not report.remaining


def test_shutdown_between_final_backlog_reads_stops_remaining_reads(schedule, monkeypatch):
    schedule.state[2] = Backlog(200, 18 * 3600)
    stopped = [False]
    def read(e, uid):
        if len(schedule.calls) == 2:
            assert uid == 1
            stopped[0] = True
        return schedule.state[uid]
    monkeypatch.setattr(cleanup, 'backlog', read)
    report = schedule.run(max_attempts=1, stopped=lambda: stopped[0])
    assert list(report.remaining) == [1]


def test_user_failure_isolated_and_backlog_error_never_reported_as_empty(schedule, monkeypatch):
    schedule.state[2] = Backlog(3, 90000)
    def read(e, uid):
        if uid == 1:
            raise RuntimeError('read failed')
        return schedule.state[uid]
    monkeypatch.setattr(cleanup, 'backlog', read)
    report = schedule.run(max_attempts=1)
    assert 1 not in report.remaining
    assert len(report.sweep.errors) == 2
    assert report.alert_users == [2]


def test_disabled_schedule_does_not_discover_or_claim_empty(db_session, monkeypatch):
    monkeypatch.setattr(cleanup, 'eligible_users', lambda *a, **k: pytest.fail('discovery'))
    report = cleanup.scheduled_sweep(engine)
    assert report.disabled and not report.remaining


def test_backlog_uses_pin_expiry_and_legacy_time_not_just_session_age(db_session, monkeypatch):
    monkeypatch.setenv('OPPONENT_TARGET_SOURCE', 'facts')
    now = as_utc(db_session.scalar(select(database_clock(db_session))))
    b = _blunder(db_session, user_id=41)
    s = _session(db_session, user_id=41, started_at=now - timedelta(days=90))
    _event(db_session, blunder=b, game_session=s)
    db_session.add(OpponentTargetFact(session_id=s.id, blunder_id=b.id,
                                    last_served_at=now - timedelta(days=30, hours=2)))
    _enable_folding(db_session)
    db_session.commit()
    actual = cleanup.backlog(engine, 41)
    assert actual.pairs == 1
    assert actual.lag_seconds == pytest.approx(7200, abs=1)
    fact = db_session.get(OpponentTargetFact, (s.id, b.id))
    fact.last_served_at = now - timedelta(days=29)
    db_session.commit()
    assert cleanup.backlog(engine, 41).pairs == 0
    fact.last_served_at = now - timedelta(days=40)
    from app.models import BlunderOpportunityEvent
    event = db_session.query(BlunderOpportunityEvent).one()
    event.occurred_at = None
    event.created_at = now - timedelta(minutes=20)
    db_session.commit()
    assert cleanup.backlog(engine, 41).lag_seconds == pytest.approx(1200, abs=1)
    event.created_at = now + timedelta(days=1)
    db_session.commit()
    assert cleanup.backlog(engine, 41).pairs == 0
    assert fold.fold_user_batch(engine, user_id=41).outcome == FoldOutcome.NOTHING_ELIGIBLE


def test_new_activity_after_export_discards_export_without_folding(db_session, monkeypatch, tmp_path):
    monkeypatch.setenv('GHOSTREPLAY_SRS_FOLD_EXPORT_DIR', str(tmp_path / 'exports'))
    _simple(db_session, user_id=42)
    checks = iter([False, True])
    result = fold.fold_user_batch(engine, user_id=42, limits=PATIENT,
                                  activity_check=lambda: next(checks))
    assert result.outcome == FoldOutcome.DEFERRED_ACTIVITY
    assert cleanup.backlog(engine, 42).pairs == 3
    assert not list(tmp_path.rglob('*.json'))


def test_activity_unknown_and_coalescing_tolerance(db_session):
    _blunder(db_session, user_id=43)
    now = as_utc(db_session.scalar(select(database_clock(db_session))))
    s = _session(db_session, user_id=43, started_at=now - timedelta(days=90))
    db_session.commit()
    assert cleanup.recently_active(engine, 43)
    s.last_activity_at = now - timedelta(seconds=3600)
    db_session.commit()
    assert cleanup.recently_active(engine, 43)
    s.last_activity_at = now - timedelta(seconds=3661)
    db_session.commit()
    assert not cleanup.recently_active(engine, 43)
    from app.session_activity import session_work
    with session_work(43):
        assert cleanup.recently_active(engine, 43)
    assert not known_inflight(43)


def test_expiry_runs_even_if_sweep_fails(monkeypatch):
    from app import opportunity_cleanup_job as job
    expired = []
    def fail(*a, **k):
        raise RuntimeError('failed')
    monkeypatch.setattr(job, 'scheduled_sweep', fail)
    monkeypatch.setattr(job, 'expire_fold_artifacts', expired.append)
    with pytest.raises(RuntimeError):
        job.run_once(engine)
    assert expired == [engine]


def test_shutdown_during_sweep_skips_artifact_expiry(monkeypatch):
    from app import opportunity_cleanup_job as job
    stopped = [False]
    def sweep(*a, **kw):
        stopped[0] = True
        return cleanup.CleanupReport()
    monkeypatch.setattr(job, 'scheduled_sweep', sweep)
    monkeypatch.setattr(job, 'expire_fold_artifacts', lambda *a: pytest.fail('expiry'))
    job.run_once(engine, stopped=lambda: stopped[0])


def test_default_100_attempt_cap_and_next_hour_revisit(schedule):
    first = schedule.run()
    assert len(schedule.calls) == 100
    assert first.capped == [1]
    assert first.remaining[1].lag_seconds == 43200
    schedule.clock.now += 3600
    schedule.run(max_attempts=1)
    assert len(schedule.calls) == 101


def test_visit_budget_expiring_during_export_does_not_acquire_fold_locks(
    db_session, monkeypatch, tmp_path,
):
    monkeypatch.setenv('GHOSTREPLAY_SRS_FOLD_EXPORT_DIR', str(tmp_path / 'exports'))
    _simple(db_session, user_id=45)
    expired = [False]
    real = fold.write_export
    def export(*a, **kw):
        result = real(*a, **kw)
        expired[0] = True
        return result
    monkeypatch.setattr(fold, 'write_export', export)
    monkeypatch.setattr(fold, '_transfer', lambda *a, **k: pytest.fail('budget expired'))
    result = fold.fold_user_batch(engine, user_id=45, stop_check=lambda: expired[0])
    assert result.outcome == FoldOutcome.DEFERRED_BUDGET
    assert cleanup.backlog(engine, 45).pairs == 3
    assert not list(tmp_path.rglob('*.json'))


def test_backlog_remains_measurable_after_cleanup_disabled(db_session):
    from app.models import OpportunityRetentionPolicy
    _simple(db_session, user_id=46)
    policy = db_session.get(OpportunityRetentionPolicy, 1)
    policy.cleanup_enabled = False
    db_session.commit()
    assert cleanup.backlog(engine, 46).pairs == 3


def test_activity_before_preparation_never_opens_a_fold_connection(monkeypatch):
    fake_engine = SimpleNamespace(connect=lambda: pytest.fail('active user connected'))
    result = fold.fold_user_batch(fake_engine, user_id=1, activity_check=lambda: True)
    assert result.outcome == FoldOutcome.DEFERRED_ACTIVITY


def test_hourly_job_is_opt_in_and_uses_hourly_cadence(monkeypatch):
    from app import opportunity_cleanup_job as job
    monkeypatch.delenv('GHOSTREPLAY_SRS_CLEANUP_JOB_ENABLED', raising=False)
    assert job.start_cleanup_job(engine) is None
    clock = Clock()
    monkeypatch.setattr(job, 'time', SimpleNamespace(monotonic=clock))
    calls = []
    def run_once(*a, **kw):
        calls.append(clock())
        clock.now += 120
    monkeypatch.setattr(job, 'run_once', run_once)
    worker = job.CleanupJob(engine)
    class Stop:
        def is_set(self):
            return len(calls) == 2
        def wait(self, seconds):
            assert seconds == 3480
            clock.now += seconds
    worker.stop = Stop()
    worker._run()
    assert calls == [0, 3600]
