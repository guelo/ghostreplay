"""The browser-game-v1 retirement criterion's math (g-mk1d §0.6 / §2.4.1).

The point of these tests is the GRAIN. The cutover decision is gated on this
number, and the two ways to get it wrong — weighting by rows (long games dominate)
or by runs (chatty uploaders dominate) — both look plausible in a dashboard. So
the rollup is asserted against the real helper, not described in prose.
"""

from app.browser_provenance_metrics import (
    PROVENANCE_LEGACY,
    PROVENANCE_MIXED_MALFORMED,
    PROVENANCE_NONE,
    PROVENANCE_V2,
    RunVerdict,
    session_provenance_verdict,
    session_v2_adoption,
)


def test_verdict_requires_a_validated_row_and_no_malformed_one():
    assert session_provenance_verdict(valid=3, absent=0, malformed=0) == PROVENANCE_V2
    assert session_provenance_verdict(valid=3, absent=2, malformed=0) == PROVENANCE_V2


def test_malformed_dominates_so_a_client_bug_cannot_hide_in_an_adopted_session():
    assert (
        session_provenance_verdict(valid=9, absent=0, malformed=1)
        == PROVENANCE_MIXED_MALFORMED
    )


def test_all_absent_is_legacy_and_no_eligible_rows_is_neither():
    assert session_provenance_verdict(valid=0, absent=4, malformed=0) == PROVENANCE_LEGACY
    assert session_provenance_verdict(valid=0, absent=0, malformed=0) == PROVENANCE_NONE


def test_many_runs_for_one_session_collapse_to_its_latest_final_verdict():
    # A session emits many coalesced runs (each incremental upload can trigger
    # one). Counting runs would re-weight adoption by upload frequency.
    stats = session_v2_adoption(
        [
            RunVerdict("s1", final=False, session_provenance=PROVENANCE_LEGACY, ts=1),
            RunVerdict("s1", final=True, session_provenance=PROVENANCE_V2, ts=2),
        ]
    )
    assert (stats.sessions_considered, stats.sessions_v2) == (1, 1)
    assert stats.fraction == 1.0


def test_a_later_final_run_supersedes_an_earlier_one():
    stats = session_v2_adoption(
        [
            RunVerdict("s1", final=True, session_provenance=PROVENANCE_V2, ts=5),
            RunVerdict("s1", final=True, session_provenance=PROVENANCE_LEGACY, ts=9),
        ]
    )
    assert (stats.sessions_considered, stats.sessions_v2) == (1, 0)


def test_a_session_with_no_final_run_is_excluded_not_counted_as_non_adopted():
    # Still in progress: it is not evidence of non-adoption.
    stats = session_v2_adoption(
        [RunVerdict("s1", final=False, session_provenance=PROVENANCE_V2, ts=1)]
    )
    assert stats.sessions_considered == 0
    assert stats.fraction == 0.0


def test_adoption_is_one_vote_per_session_regardless_of_game_length():
    # s1 is a 200-move legacy game that emitted 20 runs; s2/s3/s4 are short v2
    # games with one run each. Row- or run-weighting would report near-zero
    # adoption; the correct answer is 3/4.
    runs = [
        RunVerdict("s1", final=True, session_provenance=PROVENANCE_LEGACY, ts=i)
        for i in range(20)
    ]
    runs += [
        RunVerdict(sid, final=True, session_provenance=PROVENANCE_V2, ts=1)
        for sid in ("s2", "s3", "s4")
    ]
    stats = session_v2_adoption(runs)
    assert (stats.sessions_considered, stats.sessions_v2) == (4, 3)
    assert stats.fraction == 0.75


def test_mixed_malformed_counts_against_adoption():
    stats = session_v2_adoption(
        [
            RunVerdict("s1", final=True, session_provenance=PROVENANCE_MIXED_MALFORMED, ts=1),
            RunVerdict("s2", final=True, session_provenance=PROVENANCE_V2, ts=1),
        ]
    )
    assert (stats.sessions_considered, stats.sessions_v2) == (2, 1)


def test_sessions_with_no_browser_eligible_move_leave_the_denominator_alone():
    stats = session_v2_adoption(
        [
            RunVerdict("s1", final=True, session_provenance=PROVENANCE_NONE, ts=1),
            RunVerdict("s2", final=True, session_provenance=PROVENANCE_V2, ts=1),
        ]
    )
    assert (stats.sessions_considered, stats.sessions_v2) == (1, 1)


def test_empty_input_does_not_divide_by_zero():
    stats = session_v2_adoption([])
    assert (stats.sessions_considered, stats.sessions_v2, stats.fraction) == (0, 0, 0.0)


def test_sessions_that_never_reported_a_final_run_are_counted_not_hidden():
    # A session with only non-final runs is excluded from the ratio (it may still
    # be in progress) but MUST surface in sessions_without_final. The exclusion is
    # not neutral: a client too old to send terminal_action is also too old to send
    # provenance, so silently dropping these inflates the adoption fraction toward
    # 1.0 exactly when the legacy fleet is largest.
    stats = session_v2_adoption(
        [
            RunVerdict("done", final=True, session_provenance=PROVENANCE_V2, ts=1),
            RunVerdict("old", final=False, session_provenance=PROVENANCE_LEGACY, ts=1),
            RunVerdict("old", final=False, session_provenance=PROVENANCE_LEGACY, ts=2),
        ]
    )
    assert (stats.sessions_considered, stats.sessions_v2) == (1, 1)
    assert stats.fraction == 1.0
    # ...and the caveat that makes that 1.0 untrustworthy is visible.
    assert stats.sessions_without_final == 1


def test_a_session_with_any_final_run_is_not_counted_as_missing_one():
    stats = session_v2_adoption(
        [
            RunVerdict("s1", final=False, session_provenance=PROVENANCE_LEGACY, ts=1),
            RunVerdict("s1", final=True, session_provenance=PROVENANCE_V2, ts=2),
        ]
    )
    assert stats.sessions_without_final == 0


def test_a_final_none_session_is_excluded_from_both_the_ratio_and_the_caveat():
    # `none` sessions DID report finality; they simply carry no browser-eligible
    # move. They belong in neither bucket.
    stats = session_v2_adoption(
        [RunVerdict("s1", final=True, session_provenance=PROVENANCE_NONE, ts=1)]
    )
    assert (stats.sessions_considered, stats.sessions_without_final) == (0, 0)
