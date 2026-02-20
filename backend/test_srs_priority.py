"""Unit tests for SRS priority calculation and selection scoring.

Covers:
- srs_priority = hours_since_review / (BASE_INTERVAL * 2^pass_streak)
- selection_score = srs_priority * (eval_loss_cp / 50) / (1 + 0.1 * distance)
- Due threshold (srs_priority > 1.0)
- Severity weighting (200cp scores 4x vs 50cp)
- Distance tiebreaker (closer blunders preferred)
- Edge cases: pass_streak=0, last_reviewed_at=NULL, MAX_INTERVAL cap
- Constants: BASE_INTERVAL=1hr, BACKOFF_FACTOR=2.0, MAX_INTERVAL=4320hrs
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.srs_math import (
    BASE_INTERVAL_HOURS,
    BACKOFF_FACTOR,
    MAX_INTERVAL_HOURS,
    calculate_priority,
    expected_interval_hours,
)

# Import scoring components from the game module
from app.api.game import (
    DISTANCE_WEIGHT_SLOPE,
    SEVERITY_NORMALIZER_CP,
    STEERING_RADIUS,
    GhostMoveCandidate,
)

NOW = datetime(2026, 2, 19, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_base_interval_is_one_hour(self):
        assert BASE_INTERVAL_HOURS == 1.0

    def test_backoff_factor_is_two(self):
        assert BACKOFF_FACTOR == 2.0

    def test_max_interval_is_4320_hours(self):
        assert MAX_INTERVAL_HOURS == 4320.0

    def test_steering_radius_is_five(self):
        assert STEERING_RADIUS == 5

    def test_severity_normalizer_is_50(self):
        assert SEVERITY_NORMALIZER_CP == 50.0

    def test_distance_weight_slope_is_point_one(self):
        assert DISTANCE_WEIGHT_SLOPE == 0.1


# ---------------------------------------------------------------------------
# expected_interval_hours
# ---------------------------------------------------------------------------

class TestExpectedInterval:
    def test_pass_streak_zero(self):
        # 1 * 2^0 = 1 hour
        assert expected_interval_hours(0) == 1.0

    def test_pass_streak_one(self):
        # 1 * 2^1 = 2 hours
        assert expected_interval_hours(1) == 2.0

    def test_pass_streak_three(self):
        # 1 * 2^3 = 8 hours
        assert expected_interval_hours(3) == 8.0

    def test_pass_streak_ten(self):
        # 1 * 2^10 = 1024 hours
        assert expected_interval_hours(10) == 1024.0

    def test_max_interval_cap(self):
        # 1 * 2^13 = 8192, capped at 4320
        assert expected_interval_hours(13) == MAX_INTERVAL_HOURS

    def test_very_high_pass_streak_stays_capped(self):
        assert expected_interval_hours(100) == MAX_INTERVAL_HOURS

    def test_negative_pass_streak_treated_as_zero(self):
        # max(-5, 0) = 0 → 1 * 2^0 = 1 hour
        assert expected_interval_hours(-5) == 1.0


# ---------------------------------------------------------------------------
# calculate_priority
# ---------------------------------------------------------------------------

class TestCalculatePriority:
    def test_exactly_one_interval_elapsed(self):
        # pass_streak=0, interval=1h, 1h elapsed → priority=1.0
        reviewed_at = NOW - timedelta(hours=1)
        priority = calculate_priority(
            pass_streak=0, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert priority == pytest.approx(1.0)

    def test_two_intervals_elapsed(self):
        # pass_streak=0, interval=1h, 2h elapsed → priority=2.0
        reviewed_at = NOW - timedelta(hours=2)
        priority = calculate_priority(
            pass_streak=0, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert priority == pytest.approx(2.0)

    def test_half_interval_elapsed(self):
        # pass_streak=0, interval=1h, 30min elapsed → priority=0.5
        reviewed_at = NOW - timedelta(minutes=30)
        priority = calculate_priority(
            pass_streak=0, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert priority == pytest.approx(0.5)

    def test_pass_streak_increases_interval(self):
        # pass_streak=3, interval=8h, 8h elapsed → priority=1.0
        reviewed_at = NOW - timedelta(hours=8)
        priority = calculate_priority(
            pass_streak=3, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert priority == pytest.approx(1.0)

    def test_pass_streak_3_four_hours_is_half_due(self):
        # pass_streak=3, interval=8h, 4h elapsed → priority=0.5
        reviewed_at = NOW - timedelta(hours=4)
        priority = calculate_priority(
            pass_streak=3, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert priority == pytest.approx(0.5)

    def test_new_blunder_uses_created_at(self):
        # last_reviewed_at=None falls back to created_at
        created = NOW - timedelta(hours=3)
        priority = calculate_priority(
            pass_streak=0, last_reviewed_at=None,
            created_at=created, now=NOW,
        )
        assert priority == pytest.approx(3.0)

    def test_last_reviewed_at_takes_precedence_over_created_at(self):
        created = NOW - timedelta(hours=10)
        reviewed = NOW - timedelta(hours=2)
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
        # pass_streak=13 → 2^13=8192 capped at 4320
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
        reviewed_at = NOW - timedelta(hours=1)
        priority = calculate_priority(
            pass_streak=0, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert priority == pytest.approx(1.0)
        # Threshold is > 1.0, so exactly 1.0 is NOT due
        assert not (priority > 1.0)

    def test_overdue(self):
        reviewed_at = NOW - timedelta(hours=1, minutes=1)
        priority = calculate_priority(
            pass_streak=0, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert priority > 1.0

    def test_not_yet_due(self):
        reviewed_at = NOW - timedelta(minutes=59)
        priority = calculate_priority(
            pass_streak=0, last_reviewed_at=reviewed_at,
            created_at=None, now=NOW,
        )
        assert priority < 1.0


# ---------------------------------------------------------------------------
# GhostMoveCandidate.score() – selection scoring
# ---------------------------------------------------------------------------

def _candidate(
    eval_loss_cp: int = 100,
    pass_streak: int = 0,
    depth: int = 1,
    hours_ago: float = 2.0,
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
        # eval_loss_cp=100, pass_streak=0, depth=1, 2h ago
        # priority = 2 / 1 = 2.0
        # severity = 100 / 50 = 2.0
        # distance = 1 / (1 + 0.1*1) = 1/1.1 ≈ 0.9091
        # score = 2.0 * 2.0 * 0.9091 ≈ 3.6364
        c = _candidate(eval_loss_cp=100, pass_streak=0, depth=1, hours_ago=2.0)
        expected = 2.0 * 2.0 * (1.0 / 1.1)
        assert c.score(NOW) == pytest.approx(expected)

    def test_severity_weighting_200cp_vs_50cp(self):
        # 200cp blunder should score 4x vs 50cp at same priority/distance
        c200 = _candidate(eval_loss_cp=200, depth=1, hours_ago=2.0)
        c50 = _candidate(eval_loss_cp=50, depth=1, hours_ago=2.0)
        assert c200.score(NOW) == pytest.approx(4.0 * c50.score(NOW))

    def test_distance_tiebreaker_closer_preferred(self):
        # Same eval_loss and priority, closer depth scores higher
        c_close = _candidate(eval_loss_cp=100, depth=1, hours_ago=2.0)
        c_far = _candidate(eval_loss_cp=100, depth=5, hours_ago=2.0)
        assert c_close.score(NOW) > c_far.score(NOW)

    def test_distance_weight_at_depth_zero(self):
        # depth=0 → distance_weight = 1/(1+0) = 1.0
        c = _candidate(eval_loss_cp=100, depth=0, hours_ago=2.0)
        priority = 2.0  # 2h / 1h
        severity = 100 / 50.0
        assert c.score(NOW) == pytest.approx(priority * severity * 1.0)

    def test_distance_weight_at_max_steering_radius(self):
        # depth=5 → distance_weight = 1/(1+0.5) = 1/1.5
        c = _candidate(eval_loss_cp=100, depth=5, hours_ago=2.0)
        priority = 2.0
        severity = 2.0
        distance = 1.0 / 1.5
        assert c.score(NOW) == pytest.approx(priority * severity * distance)

    def test_zero_eval_loss_gives_zero_score(self):
        c = _candidate(eval_loss_cp=0)
        assert c.score(NOW) == 0.0

    def test_negative_eval_loss_clamped_to_zero(self):
        c = _candidate(eval_loss_cp=-50)
        assert c.score(NOW) == 0.0

    def test_higher_priority_wins_when_severity_equal(self):
        # More overdue blunder ranks higher
        c_overdue = _candidate(eval_loss_cp=100, depth=1, hours_ago=10.0)
        c_recent = _candidate(eval_loss_cp=100, depth=1, hours_ago=1.0)
        assert c_overdue.score(NOW) > c_recent.score(NOW)

    def test_pass_streak_reduces_priority_and_score(self):
        # Same time elapsed, higher streak → lower score
        c_low = _candidate(eval_loss_cp=100, pass_streak=0, depth=1, hours_ago=2.0)
        c_high = _candidate(eval_loss_cp=100, pass_streak=3, depth=1, hours_ago=2.0)
        assert c_low.score(NOW) > c_high.score(NOW)

    def test_new_blunder_no_review_uses_created_at(self):
        c = GhostMoveCandidate(
            first_move="d4",
            blunder_id=2,
            depth=1,
            eval_loss_cp=100,
            pass_streak=0,
            last_reviewed_at=None,
            created_at=NOW - timedelta(hours=5),
        )
        # priority = 5 / 1 = 5.0
        # severity = 100/50 = 2.0
        # distance = 1/1.1
        expected = 5.0 * 2.0 * (1.0 / 1.1)
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
