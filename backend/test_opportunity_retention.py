"""Shared SRS opportunity retention policy, freeze decisions and folded counters.

Scope note: this file covers the POLICY and STORAGE lifecycle owned by
g-srs-retention-state. It never folds anything itself — the fold transfer and
its recovery path belong to g-srs-fold-recovery — so folded totals here are
written directly into the summary to stand in for evidence that was physically
deleted, which is exactly what a reader can and cannot distinguish.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.api.blunder import _upsert_blunder_target
from app.fen import fen_hash
from app.models import (
    Blunder,
    BlunderOpportunityEvent,
    BlunderOpportunitySummary,
    BlunderReview,
    GameSession,
    OpportunityRetentionPolicy,
    Position,
    User,
    UserOpportunityRetentionState,
)
from app.opportunity_retention import (
    DEFAULT_GRACE_SECONDS,
    DEFAULT_MUTATION_WINDOW_DAYS,
    RetentionInvariantError,
    RetentionPolicy,
    ensure_retention_policy_row,
    frozen_by_age,
    load_policy,
    require_targeted_window,
)
from app.opportunity_store import (
    ensure_retention_state,
    ensure_summary,
    load_folded_counters,
    reconcile_review_basis,
    review_basis_gaps,
    session_evidence_frozen,
)
from app.srs_opportunity import load_opportunity_counters


FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def _user(db_session, user_id: int) -> User:
    user = db_session.get(User, user_id)
    if user is None:
        user = User(id=user_id, username=None, is_anonymous=True)
        db_session.add(user)
        db_session.flush()
    return user


def _session(db_session, *, user_id: int, started_at: datetime) -> GameSession:
    game_session = GameSession(
        id=uuid.uuid4(),
        user_id=user_id,
        started_at=started_at,
        status="completed",
        engine_elo=1500,
        player_color="white",
    )
    db_session.add(game_session)
    db_session.flush()
    return game_session


def _blunder(db_session, *, user_id: int, fen: str = FEN) -> Blunder:
    position = Position(
        user_id=user_id, fen_hash=fen_hash(fen), fen_raw=fen, active_color="w"
    )
    db_session.add(position)
    db_session.flush()
    blunder = Blunder(
        user_id=user_id,
        position_id=position.id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
        created_at=datetime.now(timezone.utc) - timedelta(days=365),
    )
    db_session.add(blunder)
    db_session.flush()
    return blunder


def _set_policy(db_session, **fields) -> None:
    row = db_session.get(OpportunityRetentionPolicy, 1)
    for key, value in fields.items():
        setattr(row, key, value)
    db_session.flush()


# --------------------------------------------------------------------------
# Policy arithmetic
# --------------------------------------------------------------------------


def test_policy_defaults_are_the_decided_horizon_with_every_switch_off(db_session):
    """A fresh install carries the decided horizon and still does nothing.

    Behaviour is unchanged from before this bead because the switches are off,
    not because M is small. Seeding the decided M = 60 days and G = 1 hour is
    what stops an activation that forgets to set the policy from freezing at a
    horizon nobody approved.
    """
    policy = load_policy(db_session)

    assert policy.mutation_window_days == DEFAULT_MUTATION_WINDOW_DAYS == 60
    assert policy.grace_seconds == DEFAULT_GRACE_SECONDS == 3600
    assert policy.freeze_enabled is False
    assert policy.cleanup_enabled is False
    assert policy.readiness is False


def test_grace_is_a_drain_gap_after_the_freeze_not_an_extension_of_it():
    """Freeze at M, fold no earlier than M + G, so writers have G to drain.

    If G extended mutability instead, freeze and fold-eligibility would land on
    the same instant and the compactor would be racing a write it had just
    authorized. The gap is the whole reason G exists.
    """
    without_grace = RetentionPolicy(mutation_window_days=30, grace_seconds=0)
    with_grace = RetentionPolicy(mutation_window_days=30, grace_seconds=3600)

    assert with_grace.mutable_age == without_grace.mutable_age
    assert with_grace.foldable_age - with_grace.mutable_age == timedelta(seconds=3600)
    assert without_grace.foldable_age == without_grace.mutable_age


def test_freeze_boundary_is_inclusive_and_ignores_grace():
    """A session exactly at the M cutoff is frozen, whatever G says.

    The prefix is inclusive by construction (MAX(started_at) of folded pairs), so
    the age test must be inclusive too; otherwise one session is mutable via the
    age path and immutable via the prefix path.
    """
    now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    policy = RetentionPolicy(mutation_window_days=30, grace_seconds=60)
    edge = now - policy.mutable_age

    assert frozen_by_age(edge, now=now, policy=policy) is True
    assert frozen_by_age(edge + timedelta(microseconds=1), now=now, policy=policy) is False
    assert frozen_by_age(edge - timedelta(microseconds=1), now=now, policy=policy) is True


# --------------------------------------------------------------------------
# Freeze decisions against the database clock
# --------------------------------------------------------------------------


def test_age_alone_freezes_nothing_until_freezing_is_approved(db_session):
    """M is a published policy, not a side effect of deploying this code.

    P0-B approval activates the immutable-session window; shipping the storage
    must leave a 90-day-old session exactly as writable as it is today.
    """
    user = _user(db_session, 9000)
    ensure_retention_state(db_session, user.id)
    old = _session(
        db_session, user_id=user.id, started_at=datetime.now(timezone.utc) - timedelta(days=900)
    )

    assert load_policy(db_session).freeze_enabled is False
    assert session_evidence_frozen(db_session, session_id=old.id, user_id=user.id) is False


def test_old_session_is_frozen_and_recent_session_is_not(db_session):
    user = _user(db_session, 9001)
    _set_policy(db_session, readiness=True, freeze_enabled=True)
    ensure_retention_state(db_session, user.id)
    old = _session(
        db_session, user_id=user.id, started_at=datetime.now(timezone.utc) - timedelta(days=90)
    )
    fresh = _session(
        db_session, user_id=user.id, started_at=datetime.now(timezone.utc) - timedelta(hours=1)
    )

    assert session_evidence_frozen(db_session, session_id=old.id, user_id=user.id) is True
    assert session_evidence_frozen(db_session, session_id=fresh.id, user_id=user.id) is False


def test_increasing_m_cannot_reopen_evidence_below_the_fold_prefix(db_session):
    """The prefix arm does not move when the window widens.

    Folding under a short M and then lengthening it is the exact shape that would
    silently reopen evidence whose raw rows no longer exist, letting a writer
    recreate them and double-count against a summary that already absorbed them.
    """
    user = _user(db_session, 9002)
    ensure_retention_state(db_session, user.id)
    started_at = datetime.now(timezone.utc) - timedelta(days=5)
    session = _session(db_session, user_id=user.id, started_at=started_at)

    # Under a short M this session is frozen by age, so it folds.
    _set_policy(db_session, readiness=True, freeze_enabled=True, mutation_window_days=1)
    assert session_evidence_frozen(db_session, session_id=session.id, user_id=user.id) is True

    state = db_session.get(UserOpportunityRetentionState, user.id)
    state.folded_through_started_at = started_at  # inclusive: equal start counts
    db_session.flush()

    # M now covers the session again; only the prefix keeps it frozen.
    _set_policy(db_session, mutation_window_days=365)
    assert frozen_by_age(started_at, now=datetime.now(timezone.utc),
                         policy=load_policy(db_session)) is False
    assert session_evidence_frozen(db_session, session_id=session.id, user_id=user.id) is True


def test_a_session_above_the_prefix_stays_mutable_under_a_long_window(db_session):
    """The prefix only forbids; it must not freeze sessions newer than itself."""
    user = _user(db_session, 9003)
    ensure_retention_state(db_session, user.id)
    _set_policy(db_session, readiness=True, freeze_enabled=True, mutation_window_days=365)
    folded_through = datetime.now(timezone.utc) - timedelta(days=10)
    state = db_session.get(UserOpportunityRetentionState, user.id)
    state.folded_through_started_at = folded_through
    db_session.flush()

    newer = _session(
        db_session, user_id=user.id, started_at=folded_through + timedelta(seconds=1)
    )

    assert session_evidence_frozen(db_session, session_id=newer.id, user_id=user.id) is False


def test_a_missing_retention_state_row_leaves_the_age_arm_to_decide(db_session):
    """No prefix contributes no freeze; it must not fail open OR closed by itself."""
    user = _user(db_session, 9004)
    _set_policy(db_session, readiness=True, freeze_enabled=True)
    old = _session(
        db_session, user_id=user.id, started_at=datetime.now(timezone.utc) - timedelta(days=90)
    )
    fresh = _session(
        db_session, user_id=user.id, started_at=datetime.now(timezone.utc)
    )

    assert db_session.get(UserOpportunityRetentionState, user.id) is None
    assert session_evidence_frozen(db_session, session_id=old.id, user_id=user.id) is True
    assert session_evidence_frozen(db_session, session_id=fresh.id, user_id=user.id) is False


def test_another_users_session_is_not_reported_frozen(db_session):
    """Ownership is the caller's check; conflating it with freezing misleads."""
    owner = _user(db_session, 9005)
    other = _user(db_session, 9006)
    _set_policy(db_session, readiness=True, freeze_enabled=True)
    session = _session(
        db_session, user_id=owner.id, started_at=datetime.now(timezone.utc) - timedelta(days=90)
    )

    assert session_evidence_frozen(db_session, session_id=session.id, user_id=other.id) is False


@pytest.mark.parametrize(
    ("skew_year", "expected_old", "expected_fresh"),
    [(2000, True, False), (2200, True, False)],
    ids=["clock-behind", "clock-ahead"],
)
def test_freeze_decisions_ignore_the_application_clock_in_both_directions(
    db_session, monkeypatch, skew_year, expected_old, expected_fresh
):
    """Worker, app and compactor clocks disagree; only the database decides.

    A host 26 years behind would call every session unborn; one 174 years ahead
    would call every session frozen. Holding database time fixed, both must
    reach the same verdicts.
    """
    user = _user(db_session, 9007)
    _set_policy(db_session, readiness=True, freeze_enabled=True)
    ensure_retention_state(db_session, user.id)
    old = _session(
        db_session, user_id=user.id, started_at=datetime.now(timezone.utc) - timedelta(days=90)
    )
    fresh = _session(
        db_session, user_id=user.id, started_at=datetime.now(timezone.utc)
    )

    class _SkewedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(skew_year, 1, 1, tzinfo=tz or timezone.utc)

    monkeypatch.setattr("app.opportunity_retention.datetime", _SkewedDatetime)

    assert session_evidence_frozen(
        db_session, session_id=old.id, user_id=user.id
    ) is expected_old
    assert session_evidence_frozen(
        db_session, session_id=fresh.id, user_id=user.id
    ) is expected_fresh


# --------------------------------------------------------------------------
# Eager summary initialization
# --------------------------------------------------------------------------


def test_ensure_summary_is_insert_if_absent_and_never_overwrites(db_session):
    """A concurrent review's values must survive a later initialization attempt."""
    user = _user(db_session, 9008)
    blunder = _blunder(db_session, user_id=user.id)
    ensure_summary(db_session, blunder.id)

    summary = db_session.get(BlunderOpportunitySummary, blunder.id)
    summary.folded_eligible_count = 7
    summary.folded_opportunities_since_review = 3
    summary.folded_reached_since_review = 1
    db_session.flush()

    ensure_summary(db_session, blunder.id)
    db_session.expire_all()

    assert db_session.get(BlunderOpportunitySummary, blunder.id).folded_eligible_count == 7


def test_ensure_retention_state_starts_with_no_prefix(db_session):
    """Seeding a timestamp would freeze history that was never folded."""
    user = _user(db_session, 9009)
    ensure_retention_state(db_session, user.id)

    state = db_session.get(UserOpportunityRetentionState, user.id)
    assert state.folded_through_started_at is None
    assert state.targeted_discarded_max_served_at is None
    assert state.sweep_progress_started_at is None


def test_the_blunder_insertion_helper_creates_a_zero_summary(db_session):
    """Eager creation at the ONE insertion helper is what makes loss detectable.

    Both the auto and manual recording routes funnel through
    ``_upsert_blunder_target``, so creating the summary there — and nowhere else
    — is what keeps the two surfaces from drifting.

    Only the summary. The per-user retention row is NOT created here: recording
    a blunder must not start depending on a ``users`` row it otherwise never
    needs, and a missing retention row is unambiguous (no prefix, nothing
    frozen) in a way a missing summary is not.
    """
    user = _user(db_session, 9010)
    position = Position(
        user_id=user.id, fen_hash=fen_hash(FEN), fen_raw=FEN, active_color="w"
    )
    db_session.add(position)
    db_session.flush()

    blunder_id, is_new = _upsert_blunder_target(
        db_session,
        user_id=user.id,
        position_id=position.id,
        user_move="a3",
        best_move="e4",
        eval_loss=220,
    )

    assert is_new is True
    summary = db_session.get(BlunderOpportunitySummary, blunder_id)
    assert summary is not None
    assert summary.folded_eligible_count == 0
    assert summary.folded_opportunities_since_review == 0
    assert summary.folded_reached_since_review == 0
    assert db_session.get(UserOpportunityRetentionState, user.id) is None


def test_returning_an_existing_blunder_does_not_reset_its_totals(db_session):
    """Re-recording the same position must not erase what was already folded."""
    user = _user(db_session, 9020)
    position = Position(
        user_id=user.id, fen_hash=fen_hash(FEN), fen_raw=FEN, active_color="w"
    )
    db_session.add(position)
    db_session.flush()
    blunder_id, _ = _upsert_blunder_target(
        db_session, user_id=user.id, position_id=position.id,
        user_move="a3", best_move="e4", eval_loss=220,
    )
    summary = db_session.get(BlunderOpportunitySummary, blunder_id)
    summary.folded_eligible_count = 12
    summary.folded_opportunities_since_review = 12
    summary.folded_reached_since_review = 5
    db_session.flush()

    again_id, is_new = _upsert_blunder_target(
        db_session, user_id=user.id, position_id=position.id,
        user_move="a3", best_move="e4", eval_loss=220,
    )
    db_session.expire_all()

    assert (again_id, is_new) == (blunder_id, False)
    refreshed = db_session.get(BlunderOpportunitySummary, blunder_id)
    assert refreshed.folded_eligible_count == 12
    assert refreshed.folded_reached_since_review == 5


# --------------------------------------------------------------------------
# Exact retained counters
# --------------------------------------------------------------------------


def _event(db_session, *, blunder, session, reached=False, opportunity=True, occurred_at=None):
    event = BlunderOpportunityEvent(
        blunder_id=blunder.id,
        session_id=session.id,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        opportunity=opportunity,
        reached=reached,
    )
    db_session.add(event)
    db_session.flush()
    return event


def test_counters_add_folded_totals_to_live_rows(db_session):
    """Folded evidence does not stop existing because its rows were deleted."""
    user = _user(db_session, 9011)
    blunder = _blunder(db_session, user_id=user.id)
    ensure_summary(db_session, blunder.id)
    session = _session(db_session, user_id=user.id, started_at=datetime.now(timezone.utc))
    _event(db_session, blunder=blunder, session=session, reached=True)

    summary = db_session.get(BlunderOpportunitySummary, blunder.id)
    summary.folded_eligible_count = 10
    summary.folded_opportunities_since_review = 6
    summary.folded_reached_since_review = 2
    db_session.flush()

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=user.id)[blunder.id]

    assert counters.event_count == 11
    assert counters.opportunities_since_review == 7
    assert counters.reached_since_review == 3


def test_a_fully_folded_blunder_still_reports_its_counters(db_session):
    """Driving the query off blunders, not off event rows, is what keeps this row."""
    user = _user(db_session, 9012)
    blunder = _blunder(db_session, user_id=user.id)
    ensure_summary(db_session, blunder.id)
    summary = db_session.get(BlunderOpportunitySummary, blunder.id)
    summary.folded_eligible_count = 4
    summary.folded_opportunities_since_review = 4
    summary.folded_reached_since_review = 1
    db_session.flush()

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=user.id)[blunder.id]

    assert counters.event_count == 4
    assert counters.opportunities_since_review == 4
    assert counters.reached_since_review == 1


def test_excluding_a_session_touches_only_raw_evidence(db_session):
    """A current session cannot have been folded, so exclusion cannot reduce totals."""
    user = _user(db_session, 9013)
    blunder = _blunder(db_session, user_id=user.id)
    ensure_summary(db_session, blunder.id)
    current = _session(db_session, user_id=user.id, started_at=datetime.now(timezone.utc))
    _event(db_session, blunder=blunder, session=current, reached=True)

    summary = db_session.get(BlunderOpportunitySummary, blunder.id)
    summary.folded_eligible_count = 5
    summary.folded_opportunities_since_review = 5
    summary.folded_reached_since_review = 2
    db_session.flush()

    counters = load_opportunity_counters(
        db_session, [blunder.id], user_id=user.id, exclude_session_id=current.id
    )[blunder.id]

    assert counters.event_count == 5
    assert counters.opportunities_since_review == 5
    assert counters.reached_since_review == 2


def test_excluding_the_only_session_keeps_the_blunder_in_the_result(db_session):
    """As a WHERE filter this dropped the blunder, taking its folded half with it."""
    user = _user(db_session, 9014)
    blunder = _blunder(db_session, user_id=user.id)
    ensure_summary(db_session, blunder.id)
    current = _session(db_session, user_id=user.id, started_at=datetime.now(timezone.utc))
    _event(db_session, blunder=blunder, session=current, reached=True)
    summary = db_session.get(BlunderOpportunitySummary, blunder.id)
    summary.folded_eligible_count = 3
    summary.folded_opportunities_since_review = 3
    db_session.flush()

    counters = load_opportunity_counters(
        db_session, [blunder.id], user_id=user.id, exclude_session_id=current.id
    )

    assert counters[blunder.id].event_count == 3


def test_folded_counters_do_not_vary_with_now(db_session):
    """Broad totals are windowless; a different ``now`` must not move them."""
    user = _user(db_session, 9015)
    blunder = _blunder(db_session, user_id=user.id)
    ensure_summary(db_session, blunder.id)
    summary = db_session.get(BlunderOpportunitySummary, blunder.id)
    summary.folded_eligible_count = 9
    summary.folded_opportunities_since_review = 9
    summary.folded_reached_since_review = 4
    db_session.flush()

    now = datetime.now(timezone.utc)
    early = load_opportunity_counters(
        db_session, [blunder.id], user_id=user.id, now=now - timedelta(days=400)
    )[blunder.id]
    late = load_opportunity_counters(
        db_session, [blunder.id], user_id=user.id, now=now + timedelta(days=400)
    )[blunder.id]

    assert early.event_count == late.event_count == 9
    assert early.reached_since_review == late.reached_since_review == 4


def test_load_folded_counters_distinguishes_absent_from_zero(db_session):
    """A row of zeros and no row at all are different facts after readiness."""
    user = _user(db_session, 9016)
    with_summary = _blunder(db_session, user_id=user.id)
    without_summary = _blunder(db_session, user_id=user.id, fen=FEN.replace("w KQkq", "b KQkq"))
    ensure_summary(db_session, with_summary.id)

    folded = load_folded_counters(db_session, [with_summary.id, without_summary.id])

    assert folded[with_summary.id].present is True
    assert folded[with_summary.id].folded_eligible_count == 0
    assert without_summary.id not in folded


# --------------------------------------------------------------------------
# Readiness turns a rollout gap into an invariant failure
# --------------------------------------------------------------------------


def test_missing_summary_is_tolerated_before_readiness(db_session):
    user = _user(db_session, 9017)
    blunder = _blunder(db_session, user_id=user.id)

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=user.id)

    assert counters[blunder.id].event_count == 0


def test_missing_summary_fails_explicitly_after_readiness(db_session):
    """Serving silently smaller counters would change dueness and hide the loss."""
    user = _user(db_session, 9018)
    blunder = _blunder(db_session, user_id=user.id)
    _set_policy(db_session, readiness=True)

    with pytest.raises(RetentionInvariantError, match="no opportunity summary"):
        load_opportunity_counters(db_session, [blunder.id], user_id=user.id)


def test_stale_review_basis_fails_explicitly_after_readiness(db_session):
    """A lagging basis means folded since-review counts belong to another window."""
    user = _user(db_session, 9019)
    blunder = _blunder(db_session, user_id=user.id)
    ensure_summary(db_session, blunder.id)
    session = _session(db_session, user_id=user.id, started_at=datetime.now(timezone.utc))
    review = BlunderReview(
        blunder_id=blunder.id,
        session_id=session.id,
        reviewed_at=datetime.now(timezone.utc),
        passed=True,
        move_played_san="e4",
        eval_delta_cp=0,
    )
    db_session.add(review)
    db_session.flush()
    _set_policy(db_session, readiness=True)

    with pytest.raises(RetentionInvariantError, match="folded review basis"):
        load_opportunity_counters(db_session, [blunder.id], user_id=user.id)

    summary = db_session.get(BlunderOpportunitySummary, blunder.id)
    summary.latest_review_id = review.id
    db_session.flush()

    assert load_opportunity_counters(db_session, [blunder.id], user_id=user.id)


# --------------------------------------------------------------------------
# Rollout controls
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "expected_constraint"),
    [
        ({"freeze_enabled": True}, "ready_before_freeze"),
        ({"readiness": True, "cleanup_enabled": True}, "freeze_before_cleanup"),
    ],
)
def test_the_rollout_ladder_cannot_be_inverted(db_session, fields, expected_constraint):
    """readiness -> freeze -> cleanup, enforced by the database, not by callers.

    Folding evidence that writers can still rewrite would lose writes, and
    freezing before every summary exists would freeze undetectable holes.
    """
    from sqlalchemy.exc import IntegrityError

    row = db_session.get(OpportunityRetentionPolicy, 1)
    for key, value in fields.items():
        setattr(row, key, value)

    with pytest.raises(IntegrityError, match=expected_constraint):
        db_session.flush()
    db_session.rollback()


def test_freezing_and_cleanup_are_disabled_by_default(db_session):
    """This bead ships inert; nothing here may turn either switch on."""
    policy = load_policy(db_session)

    assert policy.freeze_enabled is False
    assert policy.cleanup_enabled is False


def test_turning_freezing_back_off_does_not_unfreeze_folded_evidence(db_session):
    """Deleted raw rows do not come back, so the prefix arm cannot be switched off.

    A writer allowed to rewrite a folded session would recreate rows the summary
    has already absorbed and double-count them for good.
    """
    user = _user(db_session, 9021)
    ensure_retention_state(db_session, user.id)
    started_at = datetime.now(timezone.utc) - timedelta(days=2)
    session = _session(db_session, user_id=user.id, started_at=started_at)
    state = db_session.get(UserOpportunityRetentionState, user.id)
    state.folded_through_started_at = started_at
    db_session.flush()

    _set_policy(db_session, readiness=False, freeze_enabled=False, cleanup_enabled=False)

    assert session_evidence_frozen(db_session, session_id=session.id, user_id=user.id) is True


# --------------------------------------------------------------------------
# Reviews: basis, reset and acceptance at every session age
# --------------------------------------------------------------------------


def _review_blunder(db_session, *, user_id: int) -> Blunder:
    position = Position(
        user_id=user_id,
        fen_hash=f"review-fen-{user_id}",
        fen_raw="8/8/8/8/8/8/8/8 w - - 0 1",
        active_color="white",
    )
    db_session.add(position)
    db_session.flush()
    blunder = Blunder(
        user_id=user_id,
        position_id=position.id,
        bad_move_san="Qh5",
        best_move_san="Nf3",
        eval_loss_cp=120,
        created_at=datetime.now(timezone.utc) - timedelta(days=400),
    )
    db_session.add(blunder)
    db_session.flush()
    ensure_summary(db_session, blunder.id)
    summary = db_session.get(BlunderOpportunitySummary, blunder.id)
    summary.folded_eligible_count = 20
    summary.folded_opportunities_since_review = 8
    summary.folded_reached_since_review = 3
    db_session.commit()
    return blunder


def test_a_new_review_zeroes_folded_since_review_and_keeps_the_lifetime_total(
    client, auth_headers, create_game_session, db_session
):
    """Every already-folded event predates this review, so the window restarts.

    The lifetime total is not a window and must survive: it is what routes
    ``srs_priority`` between the dueness branch and the time-based schedule.
    """
    user_id = 9101
    session_id = create_game_session(user_id=user_id)
    blunder = _review_blunder(db_session, user_id=user_id)

    response = client.post(
        "/api/srs/review",
        headers=auth_headers(user_id=user_id),
        json={
            "session_id": session_id,
            "blunder_id": blunder.id,
            "passed": True,
            "user_move": "Nf3",
            "eval_delta": 20,
        },
    )
    assert response.status_code == 200, response.text

    db_session.expire_all()
    summary = db_session.get(BlunderOpportunitySummary, blunder.id)
    assert summary.folded_opportunities_since_review == 0
    assert summary.folded_reached_since_review == 0
    assert summary.folded_eligible_count == 20
    assert summary.latest_review_id is not None
    assert summary.latest_review_session_id == uuid.UUID(session_id)


def test_an_idempotent_retry_does_not_reset_a_second_time(
    client, auth_headers, create_game_session, db_session
):
    """The retry echoes the original outcome; it must not restart the window again."""
    user_id = 9102
    session_id = create_game_session(user_id=user_id)
    blunder = _review_blunder(db_session, user_id=user_id)
    payload = {
        "session_id": session_id,
        "blunder_id": blunder.id,
        "passed": True,
        "user_move": "Nf3",
        "eval_delta": 20,
        "idempotency_key": "retry-key",
    }
    assert client.post(
        "/api/srs/review", headers=auth_headers(user_id=user_id), json=payload
    ).status_code == 200

    db_session.expire_all()
    summary = db_session.get(BlunderOpportunitySummary, blunder.id)
    summary.folded_opportunities_since_review = 5
    summary.folded_reached_since_review = 2
    db_session.commit()

    assert client.post(
        "/api/srs/review", headers=auth_headers(user_id=user_id), json=payload
    ).status_code == 200

    db_session.expire_all()
    refreshed = db_session.get(BlunderOpportunitySummary, blunder.id)
    assert refreshed.folded_opportunities_since_review == 5
    assert refreshed.folded_reached_since_review == 2


def test_a_review_succeeds_against_a_session_far_past_the_freeze_boundary(
    client, auth_headers, create_game_session, db_session
):
    """Reviews are accepted at EVERY age; freezing governs evidence, not grading.

    A frozen session still names where the grade happened, and refusing the
    review would make old library practice silently stop counting.
    """
    user_id = 9103
    session_id = create_game_session(user_id=user_id)
    game_session = db_session.get(GameSession, uuid.UUID(session_id))
    game_session.started_at = datetime.now(timezone.utc) - timedelta(days=900)
    blunder = _review_blunder(db_session, user_id=user_id)
    ensure_retention_state(db_session, user_id)
    state = db_session.get(UserOpportunityRetentionState, user_id)
    state.folded_through_started_at = datetime.now(timezone.utc)
    _set_policy(db_session, readiness=True, freeze_enabled=True, mutation_window_days=1)
    db_session.commit()

    assert session_evidence_frozen(
        db_session, session_id=game_session.id, user_id=user_id
    ) is True

    response = client.post(
        "/api/srs/review",
        headers=auth_headers(user_id=user_id),
        json={
            "session_id": session_id,
            "blunder_id": blunder.id,
            "passed": False,
            "user_move": "Qh5",
            "eval_delta": 300,
        },
    )

    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert db_session.get(BlunderOpportunitySummary, blunder.id).latest_review_id is not None


def test_a_review_before_readiness_initializes_its_missing_summary(
    client, auth_headers, create_game_session, db_session
):
    """During rollout the backfill may not have reached this blunder yet."""
    user_id = 9104
    session_id = create_game_session(user_id=user_id)
    blunder = _review_blunder(db_session, user_id=user_id)
    db_session.delete(db_session.get(BlunderOpportunitySummary, blunder.id))
    db_session.commit()

    response = client.post(
        "/api/srs/review",
        headers=auth_headers(user_id=user_id),
        json={
            "session_id": session_id,
            "blunder_id": blunder.id,
            "passed": True,
            "user_move": "Nf3",
            "eval_delta": 20,
        },
    )
    assert response.status_code == 200, response.text

    db_session.expire_all()
    summary = db_session.get(BlunderOpportunitySummary, blunder.id)
    assert summary is not None
    assert summary.folded_eligible_count == 0
    assert summary.latest_review_id is not None


def test_a_review_after_readiness_does_not_paper_over_a_missing_summary(
    client, auth_headers, create_game_session, db_session
):
    """Recreating the row would erase the only evidence that folding lost data."""
    user_id = 9105
    session_id = create_game_session(user_id=user_id)
    blunder = _review_blunder(db_session, user_id=user_id)
    db_session.delete(db_session.get(BlunderOpportunitySummary, blunder.id))
    _set_policy(db_session, readiness=True)
    db_session.commit()

    client.post(
        "/api/srs/review",
        headers=auth_headers(user_id=user_id),
        json={
            "session_id": session_id,
            "blunder_id": blunder.id,
            "passed": True,
            "user_move": "Nf3",
            "eval_delta": 20,
        },
    )

    db_session.expire_all()
    assert db_session.get(BlunderOpportunitySummary, blunder.id) is None


def test_reviewing_restores_basis_agreement_for_the_reader(
    client, auth_headers, create_game_session, db_session
):
    """The post-review state must satisfy the invariant the reader enforces."""
    user_id = 9106
    session_id = create_game_session(user_id=user_id)
    blunder = _review_blunder(db_session, user_id=user_id)

    assert client.post(
        "/api/srs/review",
        headers=auth_headers(user_id=user_id),
        json={
            "session_id": session_id,
            "blunder_id": blunder.id,
            "passed": True,
            "user_move": "Nf3",
            "eval_delta": 20,
        },
    ).status_code == 200

    db_session.expire_all()
    _set_policy(db_session, readiness=True)
    db_session.commit()

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=user_id)
    assert counters[blunder.id].opportunities_since_review == 0
    assert counters[blunder.id].event_count == 20


# --------------------------------------------------------------------------
# Targeted-only historical availability
# --------------------------------------------------------------------------


def test_no_targeted_watermark_leaves_every_window_available(db_session):
    """Nothing has been discarded, so no window can be reaching past anything."""
    user = _user(db_session, 9201)
    ensure_retention_state(db_session, user.id)

    require_targeted_window(
        db_session, user_id=user.id, cutoff=datetime(1990, 1, 1, tzinfo=timezone.utc)
    )


def test_a_cutoff_at_the_discarded_time_is_rejected_and_a_later_one_passes(db_session):
    """Equal counts as inside: the watermark is the newest DISCARDED served_at.

    A silently smaller denominator is worse than an error, because a shrinking
    denominator inflates p_reach instead of obviously failing.
    """
    user = _user(db_session, 9202)
    ensure_retention_state(db_session, user.id)
    discarded_through = datetime.now(timezone.utc) - timedelta(days=60)
    state = db_session.get(UserOpportunityRetentionState, user.id)
    state.targeted_discarded_max_served_at = discarded_through
    db_session.flush()

    with pytest.raises(RetentionInvariantError, match="discarded targeting history"):
        require_targeted_window(db_session, user_id=user.id, cutoff=discarded_through)
    with pytest.raises(RetentionInvariantError):
        require_targeted_window(
            db_session, user_id=user.id, cutoff=discarded_through - timedelta(seconds=1)
        )

    require_targeted_window(
        db_session, user_id=user.id, cutoff=discarded_through + timedelta(microseconds=1)
    )


def test_advancing_the_fold_prefix_does_not_advance_the_targeted_watermark(db_session):
    """Untargeted folding has nothing to say about targeted availability."""
    user = _user(db_session, 9203)
    ensure_retention_state(db_session, user.id)
    state = db_session.get(UserOpportunityRetentionState, user.id)
    state.folded_through_started_at = datetime.now(timezone.utc)
    db_session.flush()

    assert state.targeted_discarded_max_served_at is None
    require_targeted_window(
        db_session, user_id=user.id, cutoff=datetime(1990, 1, 1, tzinfo=timezone.utc)
    )


def test_the_broad_reader_refuses_a_window_into_discarded_targeting(db_session):
    """The guard runs before the aggregate, not after it has produced a number."""
    user = _user(db_session, 9204)
    blunder = _blunder(db_session, user_id=user.id)
    ensure_summary(db_session, blunder.id)
    ensure_retention_state(db_session, user.id)
    state = db_session.get(UserOpportunityRetentionState, user.id)
    state.targeted_discarded_max_served_at = datetime.now(timezone.utc)
    db_session.flush()

    with pytest.raises(RetentionInvariantError, match="discarded targeting history"):
        load_opportunity_counters(db_session, [blunder.id], user_id=user.id)


# --------------------------------------------------------------------------
# The evidence write choke point
# --------------------------------------------------------------------------


def test_the_evidence_writer_skips_a_frozen_session_without_touching_its_rows(
    db_session
):
    """Recompute is full replacement, so a skip has to happen BEFORE the delete.

    An unguarded run would delete this session's rows (the summary has already
    absorbed equivalents) and then recreate them, double-counting forever. The
    explicit False return is what the worker and the repair CLI report as a
    policy skip rather than a failure to retry.
    """
    from app.api.session import _compute_blunder_opportunity_events

    user = _user(db_session, 9301)
    blunder = _blunder(db_session, user_id=user.id)
    ensure_summary(db_session, blunder.id)
    started_at = datetime.now(timezone.utc) - timedelta(days=5)
    session = _session(db_session, user_id=user.id, started_at=started_at)
    _event(db_session, blunder=blunder, session=session, reached=True)
    ensure_retention_state(db_session, user.id)
    state = db_session.get(UserOpportunityRetentionState, user.id)
    state.folded_through_started_at = started_at
    db_session.flush()

    wrote = _compute_blunder_opportunity_events(
        db_session, session_id=session.id, user_id=user.id, player_color="white"
    )

    assert wrote is False
    surviving = db_session.query(BlunderOpportunityEvent).filter(
        BlunderOpportunityEvent.session_id == session.id
    ).all()
    assert len(surviving) == 1
    assert surviving[0].reached is True


def test_the_evidence_writer_still_runs_for_an_unfrozen_session(db_session):
    """The guard must not become a blanket refusal; normal uploads still write."""
    from app.api.session import _compute_blunder_opportunity_events

    user = _user(db_session, 9302)
    session = _session(
        db_session, user_id=user.id, started_at=datetime.now(timezone.utc)
    )
    ensure_retention_state(db_session, user.id)

    assert _compute_blunder_opportunity_events(
        db_session, session_id=session.id, user_id=user.id, player_color="white"
    ) is True


# --------------------------------------------------------------------------
# Readiness reconciliation
# --------------------------------------------------------------------------


def _review(db_session, *, blunder, session, reviewed_at=None):
    review = BlunderReview(
        blunder_id=blunder.id,
        session_id=session.id,
        reviewed_at=reviewed_at or datetime.now(timezone.utc),
        passed=True,
        move_played_san="e4",
        eval_delta_cp=0,
    )
    db_session.add(review)
    db_session.flush()
    return review


def test_readiness_does_not_break_a_blunder_reviewed_before_the_summary_existed(
    db_session,
):
    """The regression that made flipping readiness a 500 on the ghost-move path.

    A blunder reviewed before this bead shipped has a review but no summary. If
    the backfill creates that summary with a NULL basis, the reader compares it
    to the live latest review, disagrees, and raises — for every such blunder,
    on a path that must never fail a move. The reconciler is what closes that.
    """
    user = _user(db_session, 9401)
    blunder = _blunder(db_session, user_id=user.id)
    session = _session(db_session, user_id=user.id, started_at=datetime.now(timezone.utc))
    review = _review(db_session, blunder=blunder, session=session)

    assert review_basis_gaps(db_session)["missing_summaries"] == 1
    reconcile_review_basis(db_session)
    _set_policy(db_session, readiness=True)

    summary = db_session.get(BlunderOpportunitySummary, blunder.id)
    assert summary.latest_review_id == review.id
    assert load_opportunity_counters(db_session, [blunder.id], user_id=user.id)


def test_reconcile_repairs_a_basis_an_old_instance_left_behind(db_session):
    """Deploys are not atomic: old instances write reviews with no basis.

    The migration's snapshot cannot cover a review that lands after it, so the
    reconciler has to be re-runnable rather than a one-shot backfill.
    """
    user = _user(db_session, 9402)
    blunder = _blunder(db_session, user_id=user.id)
    ensure_summary(db_session, blunder.id)
    session = _session(db_session, user_id=user.id, started_at=datetime.now(timezone.utc))
    review = _review(db_session, blunder=blunder, session=session)
    # Folded evidence attributed to the window the stale basis describes.
    summary = db_session.get(BlunderOpportunitySummary, blunder.id)
    summary.folded_eligible_count = 5
    summary.folded_opportunities_since_review = 5
    summary.folded_reached_since_review = 2
    db_session.flush()

    assert review_basis_gaps(db_session) == {
        "missing_summaries": 0,
        "basis_mismatches": 1,
    }
    result = reconcile_review_basis(db_session)

    assert result == {"summaries_created": 0, "basis_repaired": 1}
    db_session.expire(summary)
    assert summary.latest_review_id == review.id
    # The new review opened a new window; folded counts from the old one do not
    # belong in it. The LIFETIME total is untouched, because those events happened.
    assert summary.folded_opportunities_since_review == 0
    assert summary.folded_reached_since_review == 0
    assert summary.folded_eligible_count == 5


def test_reconcile_is_idempotent_and_leaves_no_gap(db_session):
    user = _user(db_session, 9403)
    reviewed = _blunder(db_session, user_id=user.id)
    unreviewed = _blunder(
        db_session, user_id=user.id, fen=FEN.replace(" w ", " b ")
    )
    session = _session(db_session, user_id=user.id, started_at=datetime.now(timezone.utc))
    _review(db_session, blunder=reviewed, session=session)

    first = reconcile_review_basis(db_session)
    second = reconcile_review_basis(db_session)

    assert first["summaries_created"] == 2
    assert second == {"summaries_created": 0, "basis_repaired": 0}
    assert review_basis_gaps(db_session) == {
        "missing_summaries": 0,
        "basis_mismatches": 0,
    }
    # A blunder that was never reviewed has a basis of NULL, which AGREES with
    # its absent latest review. Absent is a valid basis, not a gap.
    assert db_session.get(BlunderOpportunitySummary, unreviewed.id).latest_review_id is None


def test_a_deleted_review_leaves_a_gap_the_reconciler_closes(db_session):
    """A basis pointing at a review that no longer exists is also a mismatch."""
    user = _user(db_session, 9404)
    blunder = _blunder(db_session, user_id=user.id)
    ensure_summary(db_session, blunder.id)
    session = _session(db_session, user_id=user.id, started_at=datetime.now(timezone.utc))
    review = _review(db_session, blunder=blunder, session=session)
    reconcile_review_basis(db_session)

    db_session.delete(review)
    db_session.flush()

    assert review_basis_gaps(db_session)["basis_mismatches"] == 1
    reconcile_review_basis(db_session)
    assert review_basis_gaps(db_session)["basis_mismatches"] == 0
    assert db_session.get(BlunderOpportunitySummary, blunder.id).latest_review_id is None


def test_excluding_a_frozen_session_is_refused_rather_than_answered(db_session):
    """An exclusion can only suppress RAW rows, so a frozen session cannot be one.

    Its share may already live in the summary, where it cannot be subtracted.
    Answering anyway would return a number that still contains the session the
    caller asked to remove.
    """
    user = _user(db_session, 9405)
    blunder = _blunder(db_session, user_id=user.id)
    ensure_summary(db_session, blunder.id)
    old = _session(
        db_session,
        user_id=user.id,
        started_at=datetime.now(timezone.utc) - timedelta(days=400),
    )
    _set_policy(db_session, readiness=True, freeze_enabled=True)

    with pytest.raises(RetentionInvariantError, match="cannot be excluded"):
        load_opportunity_counters(
            db_session, [blunder.id], user_id=user.id, exclude_session_id=old.id
        )


def test_excluding_a_live_session_is_still_accepted_while_freezing_is_on(db_session):
    """The one live caller excludes the game it is steering, which is never old."""
    user = _user(db_session, 9406)
    blunder = _blunder(db_session, user_id=user.id)
    ensure_summary(db_session, blunder.id)
    current = _session(
        db_session, user_id=user.id, started_at=datetime.now(timezone.utc)
    )
    _set_policy(db_session, readiness=True, freeze_enabled=True)

    counters = load_opportunity_counters(
        db_session, [blunder.id], user_id=user.id, exclude_session_id=current.id
    )

    assert counters[blunder.id].event_count == 0


def test_the_reconciler_refuses_to_invent_a_summary_after_readiness(db_session):
    """After readiness a missing summary is the loss alarm, not a rollout gap.

    Creating a zero row would overwrite folded totals with zeros and destroy the
    only evidence that anything was lost — the exact failure eager summaries
    exist to make detectable.
    """
    user = _user(db_session, 9407)
    blunder = _blunder(db_session, user_id=user.id)
    _set_policy(db_session, readiness=True)

    with pytest.raises(RetentionInvariantError, match="no opportunity summary"):
        reconcile_review_basis(db_session)

    assert db_session.get(BlunderOpportunitySummary, blunder.id) is None
    # Forced, for a recovery that has separately established nothing folded.
    assert reconcile_review_basis(db_session, create_summaries=True) == {
        "summaries_created": 1,
        "basis_repaired": 0,
    }


def test_the_reconciler_still_repairs_a_basis_after_readiness(db_session):
    """Rewriting an existing row's review pointer destroys nothing."""
    user = _user(db_session, 9408)
    blunder = _blunder(db_session, user_id=user.id)
    ensure_summary(db_session, blunder.id)
    session = _session(db_session, user_id=user.id, started_at=datetime.now(timezone.utc))
    review = _review(db_session, blunder=blunder, session=session)
    _set_policy(db_session, readiness=True)

    assert reconcile_review_basis(db_session) == {
        "summaries_created": 0,
        "basis_repaired": 1,
    }
    assert db_session.get(BlunderOpportunitySummary, blunder.id).latest_review_id == review.id


def test_a_create_all_database_can_be_given_the_seeded_policy_row():
    """A model-built database must end up with the row a migration seeds.

    Target publication refuses to pin against a policy it cannot read, so a
    create_all database without row 1 serves every drill untargeted — which is
    how the seeded e2e review-position flow broke. The helper closes that gap for
    the e2e seed database and the PostgreSQL gate's post-TRUNCATE restore, at the
    decided horizon, without disturbing a row that is already there.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.models import Base

    engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            assert db.get(OpportunityRetentionPolicy, 1) is None

        ensure_retention_policy_row(engine)

        with Session(engine) as db:
            # Exactly the fallback load_policy would have returned, which is the
            # decided horizon with every switch off.
            assert load_policy(db) == RetentionPolicy()
            db.get(OpportunityRetentionPolicy, 1).readiness = True
            db.commit()

        # Idempotent, and not a reset: re-seeding must not reverse an operator's
        # readiness flip, because the rollout ladder is one-directional.
        ensure_retention_policy_row(engine)
        with Session(engine) as db:
            assert load_policy(db).readiness is True
    finally:
        engine.dispose()
