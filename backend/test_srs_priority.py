"""Unit tests for SRS priority calculation, urgency scoring, and selection scoring.

Covers:
- srs_priority = hours_since_review / (BASE_INTERVAL * 2^pass_streak)
- urgency = 1 + log2(1 + overdue)  (bounded/saturating)
- selection_score = urgency * log1p(eval_loss_cp/50) * exp(-0.35 * depth)
- Due threshold (srs_priority > 1.0) stays on linear priority
- Severity weighting with log scaling
- Exponential distance decay
- Edge cases: pass_streak=0, last_reviewed_at=NULL, MAX_INTERVAL cap
- Constants: BASE_INTERVAL=4hr, BACKOFF_FACTOR=2.0, MAX_INTERVAL=4320hrs
"""

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.srs_math import (
    BASE_INTERVAL_HOURS,
    BACKOFF_FACTOR,
    MAX_INTERVAL_HOURS,
    calculate_priority,
    calculate_urgency,
    expected_interval_hours,
)

# Import scoring components from the game module
from app.api.game import (
    DISTANCE_DECAY_RATE,
    SEVERITY_NORMALIZER_CP,
    STEERING_RADIUS,
    TOP_K,
    GhostMoveCandidate,
)

NOW = datetime(2026, 2, 19, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_base_interval_is_four_hours(self):
        assert BASE_INTERVAL_HOURS == 4.0

    def test_backoff_factor_is_two(self):
        assert BACKOFF_FACTOR == 2.0

    def test_max_interval_is_4320_hours(self):
        assert MAX_INTERVAL_HOURS == 4320.0

    def test_steering_radius_is_five(self):
        assert STEERING_RADIUS == 5

    def test_severity_normalizer_is_50(self):
        assert SEVERITY_NORMALIZER_CP == 50.0

    def test_distance_decay_rate_is_point_35(self):
        assert DISTANCE_DECAY_RATE == 0.35

    def test_top_k_is_five(self):
        assert TOP_K == 5


# ---------------------------------------------------------------------------
# expected_interval_hours
# ---------------------------------------------------------------------------

class TestExpectedInterval:
    def test_pass_streak_zero(self):
        # 4 * 2^0 = 4 hours
        assert expected_interval_hours(0) == 4.0

    def test_pass_streak_one(self):
        # 4 * 2^1 = 8 hours
        assert expected_interval_hours(1) == 8.0

    def test_pass_streak_three(self):
        # 4 * 2^3 = 32 hours
        assert expected_interval_hours(3) == 32.0

    def test_pass_streak_ten(self):
        # 4 * 2^10 = 4096 hours
        assert expected_interval_hours(10) == 4096.0

    def test_max_interval_cap(self):
        # 4 * 2^11 = 8192, capped at 4320
        assert expected_interval_hours(11) == MAX_INTERVAL_HOURS

    def test_very_high_pass_streak_stays_capped(self):
        assert expected_interval_hours(100) == MAX_INTERVAL_HOURS

    def test_negative_pass_streak_treated_as_zero(self):
        # max(-5, 0) = 0 → 4 * 2^0 = 4 hours
        assert expected_interval_hours(-5) == 4.0


# ---------------------------------------------------------------------------
# calculate_priority (linear, used for due-threshold gate)
# ---------------------------------------------------------------------------

class TestCalculatePriority:
    def test_exactly_one_interval_elapsed(self):
        # pass_streak=0, interval=4h, 4h elapsed → priority=1.0
        reviewed_at = NOW - timedelta(hours=4)
        priority = calculate_priority(
            pass_streak=0, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert priority == pytest.approx(1.0)

    def test_two_intervals_elapsed(self):
        # pass_streak=0, interval=4h, 8h elapsed → priority=2.0
        reviewed_at = NOW - timedelta(hours=8)
        priority = calculate_priority(
            pass_streak=0, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert priority == pytest.approx(2.0)

    def test_half_interval_elapsed(self):
        # pass_streak=0, interval=4h, 2h elapsed → priority=0.5
        reviewed_at = NOW - timedelta(hours=2)
        priority = calculate_priority(
            pass_streak=0, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert priority == pytest.approx(0.5)

    def test_pass_streak_increases_interval(self):
        # pass_streak=3, interval=32h, 32h elapsed → priority=1.0
        reviewed_at = NOW - timedelta(hours=32)
        priority = calculate_priority(
            pass_streak=3, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert priority == pytest.approx(1.0)

    def test_pass_streak_3_sixteen_hours_is_half_due(self):
        # pass_streak=3, interval=32h, 16h elapsed → priority=0.5
        reviewed_at = NOW - timedelta(hours=16)
        priority = calculate_priority(
            pass_streak=3, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert priority == pytest.approx(0.5)

    def test_new_blunder_uses_created_at(self):
        # last_reviewed_at=None falls back to created_at
        created = NOW - timedelta(hours=12)
        priority = calculate_priority(
            pass_streak=0, last_reviewed_at=None,
            created_at=created, now=NOW,
        )
        assert priority == pytest.approx(3.0)

    def test_last_reviewed_at_takes_precedence_over_created_at(self):
        created = NOW - timedelta(hours=10)
        reviewed = NOW - timedelta(hours=8)
        priority = calculate_priority(
            pass_streak=0, last_reviewed_at=reviewed,
            created_at=created, now=NOW,
        )
        assert priority == pytest.approx(2.0)

    def test_both_none_returns_zero(self):
        priority = calculate_priority(
            pass_streak=0, last_reviewed_at=None,
            created_at=None, now=NOW,
        )
        assert priority == 0.0

    def test_future_reference_time_clamps_to_zero(self):
        # If somehow review is in the future, hours_since should be 0
        future_review = NOW + timedelta(hours=1)
        priority = calculate_priority(
            pass_streak=0, last_reviewed_at=future_review,
            created_at=None, now=NOW,
        )
        assert priority == 0.0

    def test_max_interval_cap_affects_priority(self):
        # pass_streak=13 → 4*2^13=32768 capped at 4320
        # 4320h elapsed → priority=1.0 (exactly due)
        reviewed_at = NOW - timedelta(hours=4320)
        priority = calculate_priority(
            pass_streak=13, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert priority == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Due threshold (srs_priority > 1.0)
# ---------------------------------------------------------------------------

class TestDueThreshold:
    def test_exactly_due_at_one(self):
        reviewed_at = NOW - timedelta(hours=4)
        priority = calculate_priority(
            pass_streak=0, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert priority == pytest.approx(1.0)
        # Threshold is > 1.0, so exactly 1.0 is NOT due
        assert not (priority > 1.0)

    def test_overdue(self):
        reviewed_at = NOW - timedelta(hours=4, minutes=1)
        priority = calculate_priority(
            pass_streak=0, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert priority > 1.0

    def test_not_yet_due(self):
        reviewed_at = NOW - timedelta(hours=3, minutes=59)
        priority = calculate_priority(
            pass_streak=0, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert priority < 1.0


# ---------------------------------------------------------------------------
# calculate_urgency (bounded log2, used for scoring only)
# ---------------------------------------------------------------------------

class TestCalculateUrgency:
    def test_urgency_at_zero_overdue(self):
        # Just reviewed: overdue=0 → 1 + log2(1) = 1.0
        reviewed_at = NOW
        urgency = calculate_urgency(
            pass_streak=0, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert urgency == pytest.approx(1.0)

    def test_urgency_at_one_interval(self):
        # Exactly due: overdue=1 → 1 + log2(2) = 2.0
        reviewed_at = NOW - timedelta(hours=4)
        urgency = calculate_urgency(
            pass_streak=0, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert urgency == pytest.approx(2.0)

    def test_urgency_at_three_intervals(self):
        # 3x overdue → 1 + log2(4) = 3.0
        reviewed_at = NOW - timedelta(hours=12)
        urgency = calculate_urgency(
            pass_streak=0, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert urgency == pytest.approx(3.0)

    def test_urgency_saturates(self):
        # 100x overdue → 1 + log2(101) ≈ 7.66, not 100
        reviewed_at = NOW - timedelta(hours=400)
        urgency = calculate_urgency(
            pass_streak=0, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        expected = 1.0 + math.log2(101.0)
        assert urgency == pytest.approx(expected)
        assert urgency < 8.0

    def test_urgency_no_timestamps(self):
        urgency = calculate_urgency(
            pass_streak=0, last_reviewed_at=None,
            created_at=None, now=NOW,
        )
        assert urgency == 0.0

    def test_urgency_future_reference(self):
        # Future reference → hours_since=0, overdue=0 → urgency=1.0
        future_review = NOW + timedelta(hours=1)
        urgency = calculate_urgency(
            pass_streak=0, last_reviewed_at=future_review,
            created_at=None, now=NOW,
        )
        assert urgency == pytest.approx(1.0)

    def test_urgency_uses_created_at_fallback(self):
        created = NOW - timedelta(hours=8)
        urgency = calculate_urgency(
            pass_streak=0, last_reviewed_at=None,
            created_at=created, now=NOW,
        )
        # overdue = 8/4 = 2 → 1 + log2(3) ≈ 2.585
        assert urgency == pytest.approx(1.0 + math.log2(3.0))

    def test_urgency_monotonically_increases(self):
        # More overdue → higher urgency
        u1 = calculate_urgency(
            pass_streak=0, last_reviewed_at=NOW - timedelta(hours=5),
            created_at=None, now=NOW,
        )
        u2 = calculate_urgency(
            pass_streak=0, last_reviewed_at=NOW - timedelta(hours=20),
            created_at=None, now=NOW,
        )
        assert u2 > u1


# ---------------------------------------------------------------------------
# Severity log scaling
# ---------------------------------------------------------------------------

class TestSeverityLogScaling:
    def test_severity_50cp(self):
        # log1p(50/50) = log1p(1) ≈ 0.693
        assert math.log1p(50.0 / 50.0) == pytest.approx(math.log(2.0))

    def test_severity_200cp(self):
        # log1p(200/50) = log1p(4) ≈ 1.609
        assert math.log1p(200.0 / 50.0) == pytest.approx(math.log(5.0))

    def test_severity_ratio_sublinear(self):
        # 200cp/50cp ratio is log1p(4)/log1p(1) ≈ 2.32, not 4.0
        ratio = math.log1p(4.0) / math.log1p(1.0)
        assert ratio == pytest.approx(2.3219, abs=0.001)
        assert ratio < 4.0


# ---------------------------------------------------------------------------
# Exponential distance decay
# ---------------------------------------------------------------------------

class TestExponentialDistanceDecay:
    def test_decay_depth_0(self):
        assert math.exp(-0.35 * 0) == 1.0

    def test_decay_depth_1(self):
        assert math.exp(-0.35 * 1) == pytest.approx(0.7047, abs=0.001)

    def test_decay_depth_5(self):
        assert math.exp(-0.35 * 5) == pytest.approx(0.1738, abs=0.001)

    def test_decay_steeper_than_old_hyperbolic(self):
        # Old formula at depth 5: 1/(1+0.5) = 0.667
        # New formula at depth 5: exp(-1.75) ≈ 0.174
        old = 1.0 / (1.0 + 0.1 * 5)
        new = math.exp(-0.35 * 5)
        assert new < old


# ---------------------------------------------------------------------------
# GhostMoveCandidate.score() – selection scoring (v2)
# ---------------------------------------------------------------------------

def _candidate(
    eval_loss_cp: int = 100,
    pass_streak: int = 0,
    depth: int = 1,
    hours_ago: float = 8.0,
) -> GhostMoveCandidate:
    return GhostMoveCandidate(
        first_move="e4",
        blunder_id=1,
        depth=depth,
        eval_loss_cp=eval_loss_cp,
        pass_streak=pass_streak,
        last_reviewed_at=NOW - timedelta(hours=hours_ago),
        created_at=NOW - timedelta(days=7),
    )


class TestSelectionScore:
    def test_score_formula_manual_check(self):
        # eval_loss_cp=100, pass_streak=0, depth=1, 8h ago
        # overdue = 8/4 = 2.0 → urgency = 1 + log2(3) ≈ 2.585
        # severity = log1p(100/50) = log1p(2) ≈ 1.099
        # distance = exp(-0.35*1) ≈ 0.7047
        c = _candidate(eval_loss_cp=100, pass_streak=0, depth=1, hours_ago=8.0)
        urgency = 1.0 + math.log2(3.0)
        severity = math.log1p(2.0)
        distance = math.exp(-0.35)
        expected = urgency * severity * distance
        assert c.score(NOW) == pytest.approx(expected)

    def test_severity_weighting_200cp_vs_50cp(self):
        # 200cp vs 50cp ratio is now sublinear: log1p(4)/log1p(1) ≈ 2.32
        c200 = _candidate(eval_loss_cp=200, depth=1, hours_ago=8.0)
        c50 = _candidate(eval_loss_cp=50, depth=1, hours_ago=8.0)
        ratio = c200.score(NOW) / c50.score(NOW)
        assert ratio == pytest.approx(math.log1p(4.0) / math.log1p(1.0), abs=0.001)

    def test_distance_tiebreaker_closer_preferred(self):
        # Same eval_loss and priority, closer depth scores higher
        c_close = _candidate(eval_loss_cp=100, depth=1, hours_ago=8.0)
        c_far = _candidate(eval_loss_cp=100, depth=5, hours_ago=8.0)
        assert c_close.score(NOW) > c_far.score(NOW)

    def test_distance_weight_at_depth_zero(self):
        # depth=0 → distance_weight = exp(0) = 1.0
        c = _candidate(eval_loss_cp=100, depth=0, hours_ago=8.0)
        urgency = 1.0 + math.log2(3.0)
        severity = math.log1p(2.0)
        assert c.score(NOW) == pytest.approx(urgency * severity * 1.0)

    def test_distance_weight_at_max_steering_radius(self):
        # depth=5 → distance_weight = exp(-1.75) ≈ 0.1738
        c = _candidate(eval_loss_cp=100, depth=5, hours_ago=8.0)
        urgency = 1.0 + math.log2(3.0)
        severity = math.log1p(2.0)
        distance = math.exp(-0.35 * 5)
        assert c.score(NOW) == pytest.approx(urgency * severity * distance)

    def test_zero_eval_loss_gives_zero_score(self):
        c = _candidate(eval_loss_cp=0)
        assert c.score(NOW) == 0.0

    def test_negative_eval_loss_clamped_to_zero(self):
        c = _candidate(eval_loss_cp=-50)
        assert c.score(NOW) == 0.0

    def test_higher_priority_wins_when_severity_equal(self):
        # More overdue blunder ranks higher
        c_overdue = _candidate(eval_loss_cp=100, depth=1, hours_ago=40.0)
        c_recent = _candidate(eval_loss_cp=100, depth=1, hours_ago=4.0)
        assert c_overdue.score(NOW) > c_recent.score(NOW)

    def test_pass_streak_reduces_priority_and_score(self):
        # Same time elapsed, higher streak → lower score
        c_low = _candidate(eval_loss_cp=100, pass_streak=0, depth=1, hours_ago=8.0)
        c_high = _candidate(eval_loss_cp=100, pass_streak=3, depth=1, hours_ago=8.0)
        assert c_low.score(NOW) > c_high.score(NOW)

    def test_new_blunder_no_review_uses_created_at(self):
        c = GhostMoveCandidate(
            first_move="d4",
            blunder_id=2,
            depth=1,
            eval_loss_cp=100,
            pass_streak=0,
            last_reviewed_at=None,
            created_at=NOW - timedelta(hours=20),
        )
        # overdue = 20/4 = 5 → urgency = 1 + log2(6) ≈ 3.585
        # severity = log1p(2) ≈ 1.099
        # distance = exp(-0.35) ≈ 0.705
        urgency = 1.0 + math.log2(6.0)
        severity = math.log1p(2.0)
        distance = math.exp(-0.35)
        expected = urgency * severity * distance
        assert c.score(NOW) == pytest.approx(expected)

    def test_no_timestamps_gives_zero_score(self):
        c = GhostMoveCandidate(
            first_move="Nf3",
            blunder_id=3,
            depth=1,
            eval_loss_cp=200,
            pass_streak=0,
            last_reviewed_at=None,
            created_at=None,
        )
        assert c.score(NOW) == 0.0
