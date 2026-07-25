"""The adapter between the prod log stream and the adoption rollup (g-bgv1-cutover §1).

Scope discipline: the ROLLUP MATH is g-mk1d's contract and is tested in
``test_browser_provenance_metrics.py``; nothing here re-asserts it. What is tested
here is everything BETWEEN the log line and that helper — the two places a
retirement decision can be made on a confident-looking wrong number:

* **misreading a line** (``final=`` vs ``session_final=`` is the headline case);
* **not receiving all the lines** (a capped query, an unqueried deployment, a
  Railway log drop) while the report still prints a fraction.

All pure functions: no CLI, no network, no database.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.api.session import _timed_side_effect
from app.browser_provenance_metrics import (
    PROVENANCE_LEGACY,
    PROVENANCE_MIXED_MALFORMED,
    PROVENANCE_NONE,
    PROVENANCE_V2,
    RunVerdict,
    session_v2_adoption,
)
from scripts.report_session_v2_adoption import (
    DROP_WARNING_FILTERS,
    RAILWAY_LOG_LIMIT,
    Deployment,
    EvidenceIncomplete,
    EvidenceInputs,
    GateThresholds,
    MalformedOutputError,
    RailwayCommandError,
    ShardSaturationError,
    WarningFilterError,
    build_logs_command,
    build_report,
    collect,
    collect_shard,
    dedupe_records,
    deployment_list_truncated,
    deployment_query_plan,
    drop_warning_records,
    evaluate_gate,
    extract_drop_warnings,
    find_coverage_gaps,
    find_verdict_conflicts,
    is_drop_warning,
    latest_final_groups,
    main,
    parse_deployments,
    parse_json_records,
    parse_kv_message,
    parse_log_event,
    parse_rfc3339_nanos,
    validate_window,
    verdict_distribution,
)

WINDOW_START = datetime(2026, 7, 11, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 7, 25, tzinfo=timezone.utc)


def log_message(
    *,
    session_id="s1",
    session_final=True,
    final=True,
    session_provenance=PROVENANCE_V2,
    status="ok",
    valid=1,
    absent=0,
    malformed=0,
    cache_row_count=1,
):
    """A production ``analysis_cache_write`` line, prefix and field order included.

    The ``%(asctime)s %(levelname)s`` prefix (app/logging_config.py) is real and
    carries no timezone, which is why the parser takes its time from the Railway
    envelope instead. Field order matters too: ``final=`` really does precede
    ``session_final=`` on the wire.
    """
    return (
        "2026-07-20 11:22:33 INFO upsert_session_moves "
        "side_effect=analysis_cache_write "
        f"session_id={session_id} user_id=7 move_count=42 "
        f"cache_row_count={cache_row_count} final={final} session_final={session_final} "
        f"kind=live status={status} provenance_valid={valid} provenance_absent={absent} "
        f"provenance_malformed={malformed} session_provenance={session_provenance} "
        "elapsed_ms=12.3"
    )


def event(message, timestamp="2026-07-20T11:22:33.325883206Z"):
    return {"message": message, "timestamp": timestamp}


# ---------------------------------------------------------------------------
# Line parsing
# ---------------------------------------------------------------------------


def test_finality_comes_from_session_final_not_the_latency_cohort_final():
    # THE regression this file exists for. `final=` is g-dckw's latency cohort and
    # tracks run_opportunity, which the revert upload and every pre-g-y90g client
    # set True mid-game. Reading it as "the session ended" would score abandoned
    # and reverted sessions as completed — and it appears FIRST on the line, so a
    # sloppy suffix/regex match lands on it.
    run, anomaly = parse_log_event(event(log_message(final=True, session_final=False)))
    assert anomaly is None
    assert run.verdict.final is False


def test_a_genuinely_final_run_is_read_as_final():
    run, anomaly = parse_log_event(event(log_message(final=False, session_final=True)))
    assert anomaly is None
    assert run.verdict.final is True


def test_exact_key_matching_survives_the_full_line():
    fields = parse_kv_message(log_message())
    assert fields["final"] == "True"
    assert fields["session_final"] == "True"
    assert fields["session_provenance"] == PROVENANCE_V2
    assert fields["side_effect"] == "analysis_cache_write"


def test_missing_session_provenance_is_an_anomaly_not_a_silent_drop():
    # A dropped record would shrink the sample invisibly; an anomaly fails closed.
    message = log_message().replace(f" session_provenance={PROVENANCE_V2}", "")
    run, anomaly = parse_log_event(event(message))
    assert run is None
    assert anomaly.kind == "missing_session_provenance"


def test_missing_session_final_is_an_anomaly():
    message = log_message().replace(" session_final=True", "")
    run, anomaly = parse_log_event(event(message))
    assert run is None
    assert anomaly.kind == "missing_session_final"


def test_an_unknown_verdict_value_is_an_error():
    run, anomaly = parse_log_event(event(log_message(session_provenance="v3")))
    assert run is None
    assert anomaly.kind == "invalid_session_provenance"


def test_a_non_boolean_session_final_is_an_error():
    run, anomaly = parse_log_event(event(log_message(session_final="true")))
    assert run is None
    assert anomaly.kind == "invalid_session_final"


def test_a_status_error_run_still_carries_its_verdict():
    # Provenance is classified and stamped BEFORE write_analysis_cache_rows runs,
    # so a writer failure does not erase the evidence of what the client sent.
    run, anomaly = parse_log_event(
        event(log_message(status="error", session_provenance=PROVENANCE_LEGACY, valid=0, absent=3))
    )
    assert anomaly is None
    assert run.status == "error"
    assert run.verdict.session_provenance == PROVENANCE_LEGACY
    assert run.provenance_absent == 3


def test_a_record_the_filter_should_have_excluded_is_an_anomaly():
    # If a non-matching record comes back, the filter is not doing what the
    # per-shard record counts assume, so the completeness proof is void.
    run, anomaly = parse_log_event(event("2026-07-20 11:22:33 INFO some other line"))
    assert run is None
    assert anomaly.kind == "unfiltered_record"


def test_the_envelope_timestamp_is_used_not_the_message_asctime():
    run, _ = parse_log_event(event(log_message(), timestamp="2026-07-24T09:00:00Z"))
    assert run.verdict.ts == parse_rfc3339_nanos("2026-07-24T09:00:00Z")
    assert run.raw_timestamp == "2026-07-24T09:00:00Z"


def test_nanosecond_timestamps_keep_full_precision():
    # Railway stamps nanoseconds (release_a_runbook §4). Rounding through a float
    # epoch would collapse neighbouring records into a tie and hand the
    # collision check ties that never happened.
    earlier = parse_rfc3339_nanos("2026-07-12T17:23:16.325883206Z")
    later = parse_rfc3339_nanos("2026-07-12T17:23:16.325883207Z")
    assert later - earlier == 1
    assert parse_rfc3339_nanos("2026-07-12T17:23:16Z") % 1_000_000_000 == 0
    assert parse_rfc3339_nanos("2026-07-12T17:23:16.5Z") % 1_000_000_000 == 500_000_000


def test_offset_timestamps_normalize_to_the_same_instant():
    assert parse_rfc3339_nanos("2026-07-12T17:23:16Z") == parse_rfc3339_nanos(
        "2026-07-12T19:23:16+02:00"
    )


def test_a_malformed_timestamp_is_an_anomaly():
    run, anomaly = parse_log_event({"message": log_message(), "timestamp": "yesterday"})
    assert run is None
    assert anomaly.kind == "bad_timestamp"


def test_the_parser_matches_what_the_emitter_actually_renders(caplog):
    """Pin the adapter to the real emitter, not to this file's fixture.

    ``_timed_side_effect`` renders its fields with ``%s`` in insertion order; if
    that rendering ever changes shape (booleans, ordering, separators) this test
    fails here rather than silently in production, where the only symptom would
    be an adoption number computed from fewer records than were emitted.
    """
    session_id = uuid.uuid4()
    with caplog.at_level("INFO"):
        with _timed_side_effect(
            "analysis_cache_write",
            session_id=session_id,
            user_id=7,
            move_count=42,
            cache_row_count=0,
            final=True,
            session_final=False,
            kind="final",
            status="error",
        ) as fields:
            fields["cache_row_count"] = 3
            fields["provenance_valid"] = 3
            fields["provenance_absent"] = 0
            fields["provenance_malformed"] = 0
            fields["session_provenance"] = PROVENANCE_V2
            fields["status"] = "ok"

    emitted = [
        record.getMessage()
        for record in caplog.records
        if "side_effect=analysis_cache_write" in record.getMessage()
    ]
    assert len(emitted) == 1
    run, anomaly = parse_log_event(event(emitted[0]))
    assert anomaly is None
    assert run.verdict.session_id == str(session_id)
    assert run.verdict.final is False  # session_final, despite final=True
    assert run.verdict.session_provenance == PROVENANCE_V2
    assert (run.status, run.provenance_valid) == ("ok", 3)


# ---------------------------------------------------------------------------
# Equal-timestamp collisions
# ---------------------------------------------------------------------------


def _verdict(session_id, verdict, ts, *, final=True):
    return RunVerdict(session_id=session_id, final=final, session_provenance=verdict, ts=ts)


def test_conflicting_equal_max_timestamp_verdicts_fail_closed():
    # session_v2_adoption resolves ts ties by input order (run.ts >= current.ts),
    # so which verdict wins would depend on the order Railway returned records.
    groups = latest_final_groups(
        [
            _verdict("s1", PROVENANCE_LEGACY, 100),
            _verdict("s1", PROVENANCE_V2, 100),
        ]
    )
    conflicts = find_verdict_conflicts(groups)
    assert len(conflicts) == 1
    assert conflicts[0]["session_id"] == "s1"
    assert conflicts[0]["verdicts"] == [PROVENANCE_LEGACY, PROVENANCE_V2]
    # ...and the ordering ambiguity is real, not theoretical:
    assert session_v2_adoption(
        [_verdict("s1", PROVENANCE_LEGACY, 100), _verdict("s1", PROVENANCE_V2, 100)]
    ).sessions_v2 == 1
    assert session_v2_adoption(
        [_verdict("s1", PROVENANCE_V2, 100), _verdict("s1", PROVENANCE_LEGACY, 100)]
    ).sessions_v2 == 0


def test_an_agreeing_equal_timestamp_pair_passes():
    groups = latest_final_groups(
        [_verdict("s1", PROVENANCE_V2, 100), _verdict("s1", PROVENANCE_V2, 100)]
    )
    assert find_verdict_conflicts(groups) == []


def test_only_the_maximum_timestamp_group_can_conflict():
    # An earlier disagreeing run is simply superseded — not a collision.
    groups = latest_final_groups(
        [
            _verdict("s1", PROVENANCE_LEGACY, 100),
            _verdict("s1", PROVENANCE_V2, 200),
        ]
    )
    assert find_verdict_conflicts(groups) == []
    assert groups["s1"][0].session_provenance == PROVENANCE_V2


def test_non_final_runs_never_enter_the_tie_groups():
    groups = latest_final_groups(
        [
            _verdict("s1", PROVENANCE_V2, 100, final=True),
            _verdict("s1", PROVENANCE_LEGACY, 100, final=False),
        ]
    )
    assert find_verdict_conflicts(groups) == []


def test_the_distribution_reports_none_sessions_the_rollup_excludes():
    # The gate asks "were there zero mixed_malformed sessions?", which the rollup's
    # own numbers cannot answer — it excludes `none` from the denominator and
    # folds legacy/mixed_malformed together as "not v2".
    groups = latest_final_groups(
        [
            _verdict("a", PROVENANCE_V2, 1),
            _verdict("b", PROVENANCE_LEGACY, 1),
            _verdict("c", PROVENANCE_MIXED_MALFORMED, 1),
            _verdict("d", PROVENANCE_NONE, 1),
        ]
    )
    assert verdict_distribution(groups) == {
        PROVENANCE_V2: 1,
        PROVENANCE_LEGACY: 1,
        PROVENANCE_MIXED_MALFORMED: 1,
        PROVENANCE_NONE: 1,
    }


# ---------------------------------------------------------------------------
# Shard splitting (the completeness proof)
# ---------------------------------------------------------------------------


def _fetcher(count_for):
    """Fetcher whose record count per range is decided by ``count_for(since, until)``."""
    calls = []

    def fetch(deployment_id, since, until):
        calls.append((since, until))
        return [event(log_message(session_id=f"s{i}")) for i in range(count_for(since, until))]

    fetch.calls = calls
    return fetch


def test_a_shard_returning_exactly_the_limit_is_split_not_trusted():
    # Railway returns no truncation flag, so exactly-500 is indistinguishable from
    # "there was more". Trusting it is how a query silently caps itself.
    def count_for(since, until):
        return RAILWAY_LOG_LIMIT if (until - since) > timedelta(days=7) else 3

    fetch = _fetcher(count_for)
    records, tree = collect_shard(fetch, "dep", WINDOW_START, WINDOW_END)
    assert tree.saturated is True
    assert len(tree.children) == 2
    assert all(child.saturated is False for child in tree.children)
    assert len(records) == 6


def test_splitting_recurses_until_every_leaf_is_under_the_limit():
    # One busy hour inside a two-week window: the split has to keep going down
    # that branch while the quiet branches stop immediately.
    busy_start = WINDOW_START + timedelta(days=3)
    busy_end = busy_start + timedelta(hours=1)

    def count_for(since, until):
        overlaps_busy = since < busy_end and until > busy_start
        if overlaps_busy and (until - since) > timedelta(minutes=30):
            return RAILWAY_LOG_LIMIT
        return 2

    fetch = _fetcher(count_for)
    _, tree = collect_shard(fetch, "dep", WINDOW_START, WINDOW_END)

    def leaves(node):
        if not node.children:
            yield node
            return
        for child in node.children:
            yield from leaves(child)

    assert all(leaf.record_count < RAILWAY_LOG_LIMIT for leaf in leaves(tree))
    assert all(not leaf.saturated for leaf in leaves(tree))
    assert tree.leaf_count() > 2


def test_an_unsplittable_saturated_shard_fails_closed():
    # Below the minimum width the log path cannot prove completeness, so it must
    # refuse rather than report a capped count as a real one.
    fetch = _fetcher(lambda since, until: RAILWAY_LOG_LIMIT)
    with pytest.raises(ShardSaturationError):
        collect_shard(fetch, "dep", WINDOW_START, WINDOW_START + timedelta(minutes=5))


def test_an_under_limit_shard_is_fetched_once():
    fetch = _fetcher(lambda since, until: 4)
    records, tree = collect_shard(fetch, "dep", WINDOW_START, WINDOW_END)
    assert len(fetch.calls) == 1
    assert (len(records), tree.record_count, tree.children) == (4, 4, [])


def test_boundary_duplicates_are_removed_and_counted():
    duplicated = event(log_message(session_id="s1"))
    records, removed = dedupe_records([duplicated, dict(duplicated), event(log_message(session_id="s2"))])
    assert (len(records), removed) == (2, 1)


# ---------------------------------------------------------------------------
# Deployment coverage
# ---------------------------------------------------------------------------


def _deployment(dep_id, offset_days, status="SUCCESS"):
    return Deployment(dep_id, status, WINDOW_START + timedelta(days=offset_days))


def _plan(deployments, **kwargs):
    return deployment_query_plan(deployments, WINDOW_START, WINDOW_END, **kwargs)


def _selected(deployments, **kwargs):
    """Ids of everything the plan would query, in-range first then carry-in order."""
    in_range, carry_in = _plan(deployments, **kwargs)
    return [d.id for d in in_range] + [d.id for d in carry_in]


def test_the_deployment_active_when_the_window_opened_is_a_carry_in_candidate():
    # It was created before the window, so a "created inside the window" filter
    # would miss it — and it owns the log stream for the window's first half.
    in_range, carry_in = _plan([_deployment("new", 5), _deployment("old", -3)])
    assert [d.id for d in in_range] == ["new"]
    assert [d.id for d in carry_in] == ["old"]


def test_deployments_created_after_the_window_closed_are_excluded():
    inside = _deployment("inside", 2)
    future = _deployment("future", 30)
    # It cannot have logged inside a window that had already closed.
    assert _selected([inside, future]) == ["inside"]


def test_the_takeover_grace_moves_a_deployment_between_sets_but_never_out():
    # Two lags stack and Railway exposes neither: created_at is when the incoming
    # BUILD started, not when it took over, and the outgoing container keeps
    # draining afterwards — with its in-process evidence scheduler still emitting
    # these very lines. So the grace is a convenience, not a coverage bound:
    # shrinking it to nothing must still leave both deployments queried, just via
    # the capped backward walk instead of unconditionally.
    outgoing = _deployment("outgoing", -1)
    incoming = Deployment("incoming", "SUCCESS", WINDOW_START - timedelta(minutes=5))
    in_range, carry_in = _plan([outgoing, incoming])
    assert ([d.id for d in in_range], [d.id for d in carry_in]) == (["incoming"], ["outgoing"])

    in_range, carry_in = _plan([outgoing, incoming], takeover_grace=timedelta(0))
    assert ([d.id for d in in_range], [d.id for d in carry_in]) == ([], ["incoming", "outgoing"])


def test_a_failed_build_does_not_end_the_live_deployments_lifetime():
    # THE deployment-list trap. A deployment list is not a list of things that
    # ran: a build that fails leaves the previous deployment live and serving.
    # Ending each deployment's life at the next record's creation time would
    # evict the genuinely-live container from the query set and query the failed
    # one instead — which has no application logs at all, so the window's records
    # would simply vanish from the denominator.
    live = _deployment("live", -2, status="SUCCESS")
    failed_build = _deployment("failed", 1, status="FAILED")
    selected = _selected([live, failed_build])
    assert "live" in selected
    # ...and the failed one is still queried, since a late failure may have
    # emitted a line or two before giving up.
    assert "failed" in selected


def test_an_aborted_deployment_removed_before_it_ran_does_not_evict_the_live_one():
    # REMOVED is the terminal state BOTH of a deployment that served for a week
    # and of one cancelled thirty seconds into its build, and the status snapshot
    # cannot tell them apart. Treating it as proof of takeover meant a build
    # someone cancelled could push the genuinely-live SUCCESS deployment out of
    # the query set, taking every record with it.
    live = _deployment("live", -10, status="SUCCESS")
    aborted = _deployment("aborted", -5, status="REMOVED")
    assert _selected([live, aborted]) == ["aborted", "live"]


def test_a_deployment_still_deploying_does_not_evict_its_predecessor():
    # DEPLOYING means building/releasing. It is not serving anything yet, so the
    # predecessor is still the one writing to the log stream.
    live = _deployment("live", -10, status="SUCCESS")
    in_flight = _deployment("in-flight", -5, status="DEPLOYING")
    assert _selected([live, in_flight]) == ["in-flight", "live"]


def test_statuses_that_never_ran_never_bound_the_backward_walk():
    live = _deployment("live", -10, status="SUCCESS")
    noise = [
        _deployment("queued", -8, status="QUEUED"),
        _deployment("building", -7, status="BUILDING"),
        _deployment("skipped", -6, status="SKIPPED"),
        _deployment("removing", -5, status="REMOVING"),
    ]
    assert "live" in _selected([live, *noise])


def test_a_status_proven_deployment_does_not_end_the_backward_walk():
    # SUCCESS says a container became live EVENTUALLY, never when. This one was
    # created three hours before the window and spent four of them in build,
    # release and healthchecks, so it took over AFTER the window opened and its
    # predecessor owned the opening — with the legacy sessions that go with it.
    # A status snapshot cannot tell that history from a clean early takeover, so
    # no status ends the walk.
    late_starter = Deployment("late-starter", "SUCCESS", WINDOW_START - timedelta(hours=3))
    predecessor = _deployment("predecessor", -4, status="REMOVED")
    assert _selected([predecessor, late_starter]) == ["late-starter", "predecessor"]


def test_the_instrumentation_time_does_not_bound_the_walk():
    # It cannot. `created_at` is BUILD START while --instrumentation-deployed-at
    # is when the emitter reached production, so the emitter's own deployment
    # sorts before that timestamp — and `aborted-during-rollout`, a build created
    # and cancelled while the emitter was still rolling out, sorts between them.
    # Stopping at "the first deployment created before the instrumentation time"
    # therefore stops one short of the deployment holding the records.
    emitter_deploy = Deployment("emitter-deploy", "SUCCESS", WINDOW_START - timedelta(days=9))
    aborted = Deployment("aborted-during-rollout", "REMOVED", WINDOW_START - timedelta(days=8))
    later = _deployment("later", -5, status="REMOVED")
    assert _selected([emitter_deploy, aborted, later]) == [
        "later",
        "aborted-during-rollout",
        "emitter-deploy",
    ]


def test_a_pre_instrumentation_container_is_queried_not_assumed_silent():
    # Pre-g-mk1d containers were never silent: they emitted the same
    # analysis_cache_write line, just without session_final/session_provenance
    # (git show b52aad4^:backend/app/api/session.py). One draining into the window
    # is missing telemetry, and querying it says so — skipping it as "too old to
    # have the emitter" would turn a completeness failure into silent absence.
    old = _deployment("pre-instrumentation", -20, status="REMOVED")
    message = log_message().replace(f" session_provenance={PROVENANCE_V2}", "")
    queried, collected = _walk([old], {"pre-instrumentation": [event(message)]})
    assert queried == ["pre-instrumentation"]
    assert [a.kind for a in collected["anomalies"]] == ["missing_session_provenance"]
    assert _report(collected, required=collected["required"])["gate_met"] is False


def test_a_history_of_removed_deployments_is_still_searchable():
    # The COMMON case, and the reason the search cannot be driven by status: once
    # a deploy inside the window supersedes it, the container that was live when
    # the window opened reads REMOVED, and so does every deployment before it.
    # A rule that needs a status-proven predecessor would give up here.
    history = [_deployment(f"old{i}", -(i + 2), status="REMOVED") for i in range(50)]
    in_range, carry_in = _plan(
        [*history, _deployment("deployed-mid-window", 1, status="SUCCESS")]
    )
    assert [d.id for d in in_range] == ["deployed-mid-window"]
    assert [d.id for d in carry_in][:2] == ["old0", "old1"]  # newest first


def test_an_unrecognised_status_cannot_narrow_the_query_set():
    # Nothing reads a status any more, so one Railway adds later cannot change
    # what is queried — the failure mode that needed a drift check is gone.
    live = _deployment("live", -10, status="SUCCESS")
    assert _selected([live, _deployment("odd", -5, status="TELEPORTING")]) == ["odd", "live"]


def test_a_full_length_deployment_list_is_treated_as_possibly_truncated():
    # 1000 is the CLI's documented maximum with no continuation cursor, and the
    # records dropped off a truncated list are the OLDEST — which is where the
    # window's start boundary lives.
    assert deployment_list_truncated(1000) is True
    assert deployment_list_truncated(999) is False


def test_an_unqueried_overlapping_deployment_is_a_coverage_gap():
    required = [_deployment("a", 1), _deployment("b", 2)]
    assert find_coverage_gaps(required, {"a"}) == ["b"]
    assert find_coverage_gaps(required, {"a", "b"}) == []


def test_deployment_records_missing_a_timestamp_become_anomalies():
    # A silently skipped deployment is a coverage gap that hides itself: it would
    # be absent from BOTH the required set and the queried set.
    deployments, anomalies = parse_deployments(
        [
            {"id": "a", "status": "SUCCESS", "createdAt": "2026-07-12T00:00:00Z"},
            {"id": "b", "status": "SUCCESS", "created_at": "2026-07-13T00:00:00Z"},
            {"id": "c", "status": "SUCCESS"},
        ]
    )
    assert [d.id for d in deployments] == ["a", "b"]
    assert [a.kind for a in anomalies] == ["bad_deployment_record"]


def test_the_logs_command_names_the_deployment_and_pins_the_line_limit():
    # Omitting the positional id silently defaults to the latest successful
    # deployment; omitting --lines silently defaults to 500 records.
    cmd = build_logs_command(
        "railway", "dep-1", WINDOW_START, WINDOW_END, log_filter='"x"', lines=500
    )
    assert cmd[:3] == ["railway", "logs", "dep-1"]
    assert "--lines" in cmd and cmd[cmd.index("--lines") + 1] == "500"
    assert cmd[cmd.index("--since") + 1] == "2026-07-11T00:00:00Z"
    assert cmd[cmd.index("--until") + 1] == "2026-07-25T00:00:00Z"
    assert "--json" in cmd


# ---------------------------------------------------------------------------
# Drop warnings (the filtered query's blind spot)
# ---------------------------------------------------------------------------


def test_railways_drop_warning_is_recognised():
    assert is_drop_warning(
        "Railway rate limit of 500 logs/sec reached for replica, update your "
        "application to reduce the logging rate. Messages dropped: 50"
    )
    assert not is_drop_warning(log_message())


def test_a_reworded_drop_warning_is_still_retrieved():
    # The half-and-half recogniser is worth nothing if retrieval is stricter than
    # it is. Railway keeps the "rate limit of" prefix and rewords the suffix, and
    # a lone '"Messages dropped"' query comes back with zero records — which is
    # precisely what a sample that lost nothing looks like. The wording is
    # server-side, so no CLI version pin covers this.
    reworded = event("Railway rate limit of 500 logs/sec reached. 50 lines discarded")
    queried = []

    def fetch_filtered(deployment_id, since, until, log_filter):
        queried.append(log_filter)
        return [reworded] if "rate limit" in log_filter else []

    records = drop_warning_records(fetch_filtered, "dep", WINDOW_START, WINDOW_END)
    assert queried == list(DROP_WARNING_FILTERS)
    assert extract_drop_warnings(records, "dep")[0]["message"] == reworded["message"]


def test_a_json_record_with_a_structured_message_fails_closed_on_both_paths():
    # {"message": []} is valid JSON and unhashable, so deduplicating it by
    # (timestamp, message) raised TypeError — not an EvidenceIncomplete, so it
    # escaped the per-deployment handler as a traceback and exit 1, the code
    # that means "evidence complete, gate not met". Both queries must instead
    # reach the checks that reject the record where a reader can see it.
    junk = {"message": [], "timestamp": {"seconds": 1}}

    collected = collect(
        fetch_logs=lambda *a: [junk],
        fetch_warnings=lambda *a: [],
        in_range=[_deployment("dep", 1)],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )
    assert [a.kind for a in collected["anomalies"]] == ["missing_message"]
    assert _report(collected)["gate_met"] is False

    warnings = collect(
        fetch_logs=lambda *a: [],
        fetch_warnings=lambda *a: [junk],
        in_range=[_deployment("dep", 1)],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )
    assert [a.kind for a in warnings["anomalies"]] == ["warning_filter_unreliable"]
    assert warnings["coverage_gaps"] == ["dep"]

    # And the side query's own union step is where the warning path hashed first.
    assert len(drop_warning_records(lambda *a: [junk], "dep", WINDOW_START, WINDOW_END)) == 1


def test_a_warning_matching_both_markers_is_counted_once():
    # The usual case: both queries return the same line, and it is one warning.
    warning = event("Railway rate limit of 500 logs/sec reached. Messages dropped: 50")
    records = drop_warning_records(
        lambda *args: [warning], "dep", WINDOW_START, WINDOW_END
    )
    assert len(records) == 1


def test_a_drop_warning_in_the_side_query_fails_the_run_closed():
    # The adoption filter cannot return this line — it is not an
    # analysis_cache_write record — which is exactly why a second query exists.
    def fetch_logs(deployment_id, since, until):
        return [event(log_message(session_id="s1"))]

    def fetch_warnings(deployment_id, since, until):
        return [event("Railway rate limit of 500 logs/sec reached. Messages dropped: 50")]

    collected = collect(
        fetch_logs=fetch_logs,
        fetch_warnings=fetch_warnings,
        in_range=[_deployment("dep", 1)],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )
    assert len(collected["drop_warnings"]) == 1

    report = _report(collected, thresholds=GateThresholds(min_sessions=1))
    assert report["gate_met"] is False
    assert any("log-drop" in problem for problem in report["evidence_problems"])
    assert report["gate_failures"] == [
        "not evaluated: the evidence is incomplete (see evidence_problems)"
    ]


def test_an_unrelated_line_in_the_side_query_invalidates_the_deployment():
    # Skipping it looks right — "that isn't a drop warning" — but this query is
    # the ONLY proof the adoption query lost nothing, and a record it should not
    # have matched means the filter is not selecting what the check assumes. If
    # the filter degrades to matching everything, ordinary lines fill the
    # 500-record result and a real drop warning sits past the end of it, unseen.
    with pytest.raises(WarningFilterError, match="not selecting"):
        extract_drop_warnings(
            [event("2026-07-20 11:22:33 INFO application started")], "dep"
        )
    assert issubclass(WarningFilterError, EvidenceIncomplete)


def test_a_broken_warning_filter_cannot_produce_a_green_gate():
    # End to end: a healthy-looking 200-session sample plus one stray record in
    # the warning query must not clear the gate.
    def fetch_warnings(deployment_id, since, until):
        return [event("2026-07-20 11:22:33 INFO GET /api/health 200")]

    collected = collect(
        fetch_logs=lambda *a: [
            event(log_message(session_id=f"v{i}")) for i in range(200)
        ],
        fetch_warnings=fetch_warnings,
        in_range=[_deployment("dep", 1)],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )
    assert [a.kind for a in collected["anomalies"]] == ["warning_filter_unreliable"]
    assert collected["coverage_gaps"] == ["dep"]
    assert collected["parsed_runs"] == []

    report = _report(collected, thresholds=GateThresholds(min_sessions=1))
    assert report["gate_met"] is False


# ---------------------------------------------------------------------------
# CLI output parsing and command failures
# ---------------------------------------------------------------------------


def test_valid_ndjson_and_json_array_output_both_parse():
    assert parse_json_records('{"message":"a","timestamp":"t"}\n{"message":"b","timestamp":"t"}') == [
        {"message": "a", "timestamp": "t"},
        {"message": "b", "timestamp": "t"},
    ]
    assert parse_json_records('[{"id":"a"},{"id":"b"}]') == [{"id": "a"}, {"id": "b"}]
    assert parse_json_records("   ") == []


def test_a_truncated_ndjson_record_fails_closed_instead_of_being_skipped():
    # Skipping it looks harmless — "it wasn't a log record anyway" — but a corrupt
    # record is exactly a record we were meant to count, and dropping it removes a
    # session from the DENOMINATOR with no trace. The bias has a direction too:
    # odd records skew toward older, longer-running legacy sessions, so silent
    # skipping pushes the adoption fraction UP.
    with pytest.raises(MalformedOutputError, match="line 2"):
        parse_json_records('{"message":"a","timestamp":"t"}\n{"message":"b","time')


def test_non_object_cli_output_fails_closed():
    with pytest.raises(MalformedOutputError):
        parse_json_records('{"message":"a"}\n"just a string"')
    with pytest.raises(MalformedOutputError):
        parse_json_records('[{"id":"a"}, "not an object"]')
    with pytest.raises(MalformedOutputError):
        parse_json_records("Error: not logged in")


def test_a_malformed_response_for_one_deployment_becomes_an_anomaly_and_a_gap():
    # Both signals, deliberately: the deployment is an unreadable record source
    # AND an unqueried one.
    def fetch_logs(deployment_id, since, until):
        raise MalformedOutputError("line 3 of CLI output is not a JSON record")

    collected = collect(
        fetch_logs=fetch_logs,
        fetch_warnings=lambda *a: [],
        in_range=[_deployment("dep", 1)],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )
    assert [a.kind for a in collected["anomalies"]] == ["malformed_cli_output"]
    assert collected["coverage_gaps"] == ["dep"]


def test_a_failed_railway_call_for_one_deployment_becomes_an_anomaly_and_a_gap():
    def fetch_logs(deployment_id, since, until):
        raise RailwayCommandError("command failed (1): railway logs dep")

    collected = collect(
        fetch_logs=fetch_logs,
        fetch_warnings=lambda *a: [],
        in_range=[_deployment("dep", 1)],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )
    assert [a.kind for a in collected["anomalies"]] == ["railway_command_failed"]
    assert collected["coverage_gaps"] == ["dep"]


def test_a_failing_drop_warning_query_abandons_the_deployment():
    # Without the warning query there is no proof nothing was dropped, so the
    # deployment's records must not be counted as if there were.
    def fetch_warnings(deployment_id, since, until):
        raise RailwayCommandError("command failed (1): railway logs dep")

    collected = collect(
        fetch_logs=lambda *a: [event(log_message(session_id="s1"))],
        fetch_warnings=fetch_warnings,
        in_range=[_deployment("dep", 1)],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )
    assert collected["parsed_runs"] == []
    assert collected["coverage_gaps"] == ["dep"]


def test_an_unsplittable_shard_is_reported_as_an_anomaly_not_a_crash():
    def fetch_logs(deployment_id, since, until):
        return [event(log_message(session_id=f"s{i}")) for i in range(RAILWAY_LOG_LIMIT)]

    collected = collect(
        fetch_logs=fetch_logs,
        fetch_warnings=lambda *a: [],
        in_range=[_deployment("dep", 1)],
        window_start=WINDOW_START,
        window_end=WINDOW_START + timedelta(minutes=5),
    )
    assert [a.kind for a in collected["anomalies"]] == ["shard_saturated"]
    assert collected["coverage_gaps"] == ["dep"]


# ---------------------------------------------------------------------------
# Finding the container that was serving when the window opened
# ---------------------------------------------------------------------------


def _walk(carry_in, records_by_deployment, **kwargs):
    """Run the carry-in search with a fetcher that knows which deployments logged."""
    queried = []

    def fetch_logs(deployment_id, since, until):
        queried.append(deployment_id)
        return records_by_deployment.get(deployment_id, [])

    collected = collect(
        fetch_logs=fetch_logs,
        fetch_warnings=lambda *a: [],
        in_range=[],
        carry_in=carry_in,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        **kwargs,
    )
    return queried, collected


def test_a_silent_deployment_does_not_end_the_search():
    # THE unsound shortcut, and it looked so reasonable: "this candidate emitted
    # nothing, so the chain of containers overlapping the window has ended".
    # It has not. This log stream is filtered to analysis_cache_write lines, which
    # makes it a record of GAMEPLAY, not of lifecycle — an aborted build and a
    # container that served through a quiet hour are indistinguishable in it. Here
    # the silent one is an aborted build sitting between two deployments that both
    # emitted, and stopping at it drops `older-draining` entirely.
    carry_in = [
        _deployment("new-live", -2, status="REMOVED"),
        _deployment("aborted-between", -3, status="REMOVED"),
        _deployment("older-draining", -4, status="REMOVED"),
    ]
    queried, collected = _walk(
        carry_in,
        {
            "new-live": [event(log_message(session_id="v1", session_provenance=PROVENANCE_V2))],
            "older-draining": [
                event(log_message(session_id="l1", session_provenance=PROVENANCE_LEGACY))
            ],
        },
    )
    assert queried == ["new-live", "aborted-between", "older-draining"]
    # And the bias has a direction: the dropped container is the OLD one, so its
    # sessions are exactly the legacy ones the fraction exists to count.
    assert sorted(r.verdict.session_provenance for r in collected["parsed_runs"]) == [
        PROVENANCE_LEGACY,
        PROVENANCE_V2,
    ]


def test_the_search_walks_past_aborted_deployments_that_emitted_nothing():
    # Two builds cancelled before they ever ran, then the container that was
    # genuinely live. Stopping at the first silent candidate would have missed it.
    carry_in = [
        _deployment("aborted-1", -2, status="REMOVED"),
        _deployment("aborted-2", -3, status="REMOVED"),
        _deployment("live", -4, status="SUCCESS"),
    ]
    queried, collected = _walk(carry_in, {"live": [event(log_message(session_id="s1"))]})
    assert queried == ["aborted-1", "aborted-2", "live"]
    assert collected["coverage_gaps"] == []
    assert len(collected["parsed_runs"]) == 1


def test_no_status_ends_the_search_short_of_the_planned_candidates():
    # The plan decides which deployments are candidates; collect queries all of
    # them and does not second-guess it from a status. `live` reaching SUCCESS says
    # nothing about WHEN it took over, so `superseded` may still have been
    # draining into the window.
    carry_in = [
        _deployment("live", -2, status="SUCCESS"),
        _deployment("superseded", -3, status="REMOVED"),
    ]
    queried, collected = _walk(
        carry_in, {"superseded": [event(log_message(session_id="s1"))]}
    )
    assert queried == ["live", "superseded"]
    assert len(collected["parsed_runs"]) == 1


def test_the_whole_pre_window_history_is_read_unless_a_budget_says_otherwise():
    # No default budget: coverage here is proven by exhausting the deployment
    # list, so a cap that defaulted to some number would quietly make the
    # ordinary run incomplete.
    carry_in = [_deployment(f"old{i}", -(i + 2), status="REMOVED") for i in range(6)]
    queried, collected = _walk(carry_in, {})
    assert queried == [d.id for d in carry_in]
    assert collected["coverage_gaps"] == []


def test_an_unfinished_search_is_a_coverage_gap_not_an_assumption():
    # Nothing found after the budget: the serving container may be the next
    # candidate or something older still, so the sample cannot be trusted.
    carry_in = [_deployment(f"old{i}", -(i + 2), status="REMOVED") for i in range(6)]
    queried, collected = _walk(carry_in, {}, max_carry_in=3)
    assert queried == ["old0", "old1", "old2"]
    assert [a.kind for a in collected["anomalies"]] == ["carry_in_unbounded"]
    # Every deployment behind the stopping point, not just the next one: listing
    # one would understate the hole in the direction that looks complete.
    assert collected["coverage_gaps"] == ["old3", "old4", "old5"]
    assert _report(collected, required=collected["required"])["gate_met"] is False


def test_a_failed_carry_in_query_stops_the_search_rather_than_skipping_past_it():
    # Skipping it would silently promote the next candidate to "the one that was
    # serving", on no evidence — the failed query is exactly the missing evidence.
    def fetch_logs(deployment_id, since, until):
        raise RailwayCommandError("command failed (1): railway logs old0")

    collected = collect(
        fetch_logs=fetch_logs,
        fetch_warnings=lambda *a: [],
        in_range=[],
        carry_in=[_deployment("old0", -2, status="REMOVED"), _deployment("old1", -3)],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )
    assert collected["coverage_gaps"] == ["old0", "old1"]
    assert [d.id for d in collected["required"]] == ["old0", "old1"]


def test_every_retrieval_failure_shares_one_base_class():
    # collect() catches the base, so a new failure mode cannot escape as an
    # uncaught exception and turn exit 2 into a traceback and exit 1.
    for error in (ShardSaturationError, MalformedOutputError, RailwayCommandError):
        assert issubclass(error, EvidenceIncomplete)


# ---------------------------------------------------------------------------
# Report + gate
# ---------------------------------------------------------------------------


def _inputs(**overrides):
    kwargs = {
        "generated_at": WINDOW_END,
        "instrumentation_deployed_at": WINDOW_START - timedelta(days=1),
        "retention_days": 30,
        "freshness_tolerance": timedelta(hours=24),
    }
    kwargs.update(overrides)
    return EvidenceInputs(**kwargs)


def _report(collected, *, thresholds=None, required=None, window_problems=None, inputs=None):
    return build_report(
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        inputs=inputs or _inputs(),
        thresholds=thresholds or GateThresholds(),
        parsed_runs=collected["parsed_runs"],
        anomalies=collected["anomalies"],
        shard_trees=collected["shard_trees"],
        per_deployment_records=collected["per_deployment_records"],
        duplicates_removed=collected["duplicates_removed"],
        coverage_gaps=collected["coverage_gaps"],
        drop_warnings=collected["drop_warnings"],
        window_problems=window_problems or [],
        required_deployments=[_deployment("dep", 1)] if required is None else required,
        cli_version="railway 4.10.0",
    )


def _collected(records, deployments=None):
    return collect(
        fetch_logs=lambda dep, since, until: records,
        fetch_warnings=lambda dep, since, until: [],
        in_range=[_deployment("dep", 1)] if deployments is None else deployments,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )


def test_a_clean_sample_that_clears_the_thresholds_meets_the_gate():
    records = [
        event(log_message(session_id=f"v{i}", session_provenance=PROVENANCE_V2))
        for i in range(19)
    ]
    records.append(event(log_message(session_id="legacy", session_provenance=PROVENANCE_LEGACY)))
    report = _report(
        _collected(records), thresholds=GateThresholds(min_fraction=0.95, min_sessions=20)
    )
    assert report["evidence_problems"] == []
    assert report["gate_met"] is True
    assert report["adoption"]["sessions_considered"] == 20
    assert report["adoption"]["fraction"] == 0.95
    assert report["railway_cli_version"] == "railway 4.10.0"


def test_a_parse_anomaly_suppresses_the_gate_verdict_entirely():
    # Not "gate met with a caveat": a record we could not read is a verdict we do
    # not have, biasing the fraction in an unknown direction.
    records = [event(log_message(session_id=f"v{i}")) for i in range(19)]
    records.append(event(log_message(session_id="bad", session_provenance="v3")))
    report = _report(_collected(records), thresholds=GateThresholds(min_sessions=1))
    assert report["gate_met"] is False
    assert any("anomaly" in problem for problem in report["evidence_problems"])


def test_a_coverage_gap_suppresses_the_gate_verdict():
    required = [_deployment("queried", 1), _deployment("missed", 2)]
    collected = _collected(
        [event(log_message(session_id="s1"))], deployments=[_deployment("queried", 1)]
    )
    collected["coverage_gaps"] = ["missed"]
    report = _report(collected, thresholds=GateThresholds(min_sessions=1), required=required)
    assert report["gate_met"] is False
    assert any("were not queried" in p for p in report["evidence_problems"])


def test_recent_mixed_malformed_sessions_fail_the_gate():
    # A deployed client still emitting provenance the server rejects: retiring the
    # v1 fallback under that condition starts dropping those rows outright.
    recent = (WINDOW_END - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    records = [
        event(log_message(session_id=f"v{i}"), timestamp=recent) for i in range(19)
    ]
    records.append(
        event(
            log_message(session_id="bug", session_provenance=PROVENANCE_MIXED_MALFORMED, malformed=1),
            timestamp=recent,
        )
    )
    report = _report(_collected(records), thresholds=GateThresholds(min_sessions=1, min_fraction=0.5))
    assert report["evidence_problems"] == []
    assert report["gate_met"] is False
    assert report["adoption"]["recent_mixed_malformed_sessions"] == ["bug"]


def test_an_old_mixed_malformed_session_does_not_fail_the_recency_check():
    old = (WINDOW_END - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    records = [
        event(log_message(session_id="bug", session_provenance=PROVENANCE_MIXED_MALFORMED, malformed=1), timestamp=old),
        event(log_message(session_id="ok")),
    ]
    report = _report(_collected(records), thresholds=GateThresholds(min_sessions=1, min_fraction=0.5))
    assert report["adoption"]["recent_mixed_malformed_sessions"] == []
    assert report["gate_met"] is True


def test_row_health_counts_are_reported_but_never_drive_the_gate():
    # One 400-row legacy game against nineteen short v2 games: row-weighting would
    # read as ~5% adoption, the session grain reads 95%.
    records = [event(log_message(session_id=f"v{i}", valid=1)) for i in range(19)]
    records.append(
        event(log_message(session_id="long", session_provenance=PROVENANCE_LEGACY, valid=0, absent=400))
    )
    report = _report(_collected(records), thresholds=GateThresholds(min_sessions=20))
    assert report["row_health"]["provenance_absent"] == 400
    assert report["row_health"]["provenance_valid"] == 19
    assert report["adoption"]["fraction"] == 0.95
    assert report["gate_met"] is True


def test_too_many_sessions_without_a_final_run_fail_the_gate():
    # This exclusion biases the fraction UP: a client too old to send
    # terminal_action is also too old to send provenance.
    records = [event(log_message(session_id=f"v{i}")) for i in range(10)]
    records += [
        event(log_message(session_id=f"old{i}", session_final=False, session_provenance=PROVENANCE_LEGACY))
        for i in range(5)
    ]
    report = _report(_collected(records), thresholds=GateThresholds(min_sessions=1, min_fraction=0.5))
    assert report["adoption"]["sessions_without_final"] == 5
    assert report["gate_met"] is False
    assert any("overstates adoption" in reason for reason in report["gate_failures"])


def test_a_small_sample_fails_the_gate_even_at_full_adoption():
    report = _report(
        _collected([event(log_message(session_id="s1"))]),
        thresholds=GateThresholds(min_sessions=200),
    )
    assert report["adoption"]["fraction"] == 1.0
    assert report["gate_met"] is False
    assert any("sample too small" in reason for reason in report["gate_failures"])


def test_an_empty_sample_never_reads_as_adopted():
    report = _report(_collected([]), thresholds=GateThresholds(min_sessions=1))
    assert report["adoption"]["fraction"] == 0.0
    assert report["gate_met"] is False


def test_reaching_no_deployment_at_all_is_an_evidence_problem_not_a_gate_failure():
    # A wrong --service or an unlinked project returns an empty deployment list
    # without erroring; read as "zero sessions" it would look like a gate failure
    # rather than a query that never reached production.
    report = _report(_collected([], deployments=[]), required=[])
    assert any("no deployment overlaps" in p for p in report["evidence_problems"])


def test_the_shard_tree_is_published_so_completeness_is_checkable():
    report = _report(_collected([event(log_message(session_id="s1"))]))
    tree = report["coverage"]["shard_tree"]["dep"]
    assert tree["record_count"] == 1
    assert tree["saturated"] is False
    assert report["coverage"]["records_per_deployment"] == {"dep": 1}


def test_the_report_records_the_inputs_its_completeness_claims_rest_on():
    # A run with the freshness tolerance relaxed to a year is otherwise
    # indistinguishable from one at the 24-hour default: the checks all pass
    # either way and nothing in the output says which was used. A verdict whose
    # inputs are invisible cannot be independently doubted.
    default = _report(_collected([]))["completeness_inputs"]
    assert default["freshness_tolerance_hours"] == 24
    assert default["retention_days_attested"] == 30
    assert default["instrumentation_deployed_at"] == "2026-07-10T00:00:00Z"
    assert default["generated_at"] == "2026-07-25T00:00:00Z"
    assert default["takeover_grace_minutes"] == 120
    assert default["log_record_limit"] == RAILWAY_LOG_LIMIT

    relaxed = _report(
        _collected([]), inputs=_inputs(freshness_tolerance=timedelta(days=365))
    )["completeness_inputs"]
    assert relaxed["freshness_tolerance_hours"] == 8760


def test_the_report_records_which_railway_fleet_it_measured():
    # Nothing else in the report says which fleet the numbers came from, and a
    # staging run is shaped exactly like a production one.
    recorded = _report(
        _collected([]),
        inputs=_inputs(service="api", environment="staging", project="ghostreplay"),
    )["completeness_inputs"]
    assert (recorded["service"], recorded["environment"], recorded["project"]) == (
        "api",
        "staging",
        "ghostreplay",
    )


def test_the_report_records_the_window_span_it_certifies():
    assert _report(_collected([]))["window"]["span_days"] == 14


# ---------------------------------------------------------------------------
# Window validation
# ---------------------------------------------------------------------------


def _window(**overrides):
    kwargs = {
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "instrumentation_deployed_at": WINDOW_START - timedelta(days=1),
        "retention_days": 30,
        "now": WINDOW_END,
    }
    kwargs.update(overrides)
    return validate_window(**kwargs)


def test_a_window_reaching_before_the_instrumentation_deploy_is_rejected():
    # Records predating session_provenance are ABSENT, not legacy; counting the
    # window as merely smaller would hide that the answer is partly missing.
    problems = _window(instrumentation_deployed_at=WINDOW_START + timedelta(days=1))
    assert any("instrumentation" in p for p in problems)


def test_a_window_outside_log_retention_is_rejected():
    assert any("retention" in p for p in _window(retention_days=7))


def test_a_retention_claim_no_plan_offers_is_rejected():
    # --retention-days exists to BOUND the window, so a value no plan offers
    # bounds nothing: 10000 waves through a window reaching arbitrarily far past
    # the real horizon, where the logs are already gone.
    problems = _window(
        retention_days=10000,
        window_start=WINDOW_START - timedelta(days=100),
        instrumentation_deployed_at=WINDOW_START - timedelta(days=200),
    )
    assert any("documented" in p for p in problems)
    assert _window(retention_days=0) != []


def test_a_window_shorter_than_the_clean_period_it_certifies_is_rejected():
    # The gate asserts no mixed_malformed session in the final N days. A window
    # shorter than N asserts it about days it never queried.
    problems = _window(
        window_start=WINDOW_END - timedelta(days=1), malformed_clean_days=7
    )
    assert any("mixed_malformed" in p for p in problems)
    # Matching the clean period exactly is enough.
    assert _window(window_start=WINDOW_END - timedelta(days=7), malformed_clean_days=7) == []


def test_an_omitted_instrumentation_bound_is_itself_an_evidence_problem():
    # An optional check is not a check: skipping it when the flag is absent gives
    # a too-early window the same confident gate_met as a correct one.
    problems = _window(instrumentation_deployed_at=None)
    assert any("--instrumentation-deployed-at" in p for p in problems)


def test_an_omitted_retention_bound_is_itself_an_evidence_problem():
    problems = _window(retention_days=None)
    assert any("--retention-days" in p for p in problems)


def test_a_window_ending_in_the_future_is_rejected():
    # The un-elapsed shards come back small, under the limit, and are recorded as
    # complete — coverage of time that has not happened yet.
    problems = _window(now=WINDOW_END - timedelta(days=2))
    assert any("in the future" in p for p in problems)


def test_a_stale_historical_window_cannot_authorize_a_flip_today():
    # A clean two-month-old window is a true statement about a fleet that has
    # since been replaced.
    problems = _window(now=WINDOW_END + timedelta(days=60))
    assert any("CURRENT fleet" in p for p in problems)


def test_a_window_within_the_freshness_tolerance_is_accepted():
    assert _window(now=WINDOW_END + timedelta(hours=6)) == []


def test_a_window_inside_retention_and_after_instrumentation_is_accepted():
    assert _window() == []


def test_an_inverted_window_is_rejected():
    problems = _window(window_start=WINDOW_END, window_end=WINDOW_START, now=WINDOW_END)
    assert any("empty window" in p for p in problems)


# ---------------------------------------------------------------------------
# Threshold validation
# ---------------------------------------------------------------------------


def test_a_nan_threshold_cannot_be_constructed():
    # The dangerous one, and it is reachable straight from the command line:
    # float("nan") parses fine and EVERY comparison against it is False, so
    # `--min-fraction nan` would let a 0%-adoption sample through a gate that
    # reports itself as met.
    with pytest.raises(ValueError, match="finite fraction"):
        GateThresholds(min_fraction=float("nan"))
    with pytest.raises(ValueError, match="finite fraction"):
        GateThresholds(max_without_final_share=float("nan"))


def test_a_nan_threshold_would_otherwise_pass_a_zero_adoption_sample():
    # Demonstrates why the constructor guard matters rather than asserting it twice.
    from app.browser_provenance_metrics import AdoptionStats

    assert not (0.0 < float("nan"))  # the comparison evaluate_gate makes
    assert (
        evaluate_gate(
            stats=AdoptionStats(sessions_considered=500, sessions_v2=0),
            distribution={PROVENANCE_V2: 0, PROVENANCE_LEGACY: 500},
            recent_malformed_sessions=[],
            thresholds=GateThresholds(min_fraction=0.95),
        )
        != []
    )


def test_out_of_range_and_infinite_thresholds_are_rejected():
    with pytest.raises(ValueError, match="finite fraction"):
        GateThresholds(min_fraction=1.5)
    with pytest.raises(ValueError, match="finite fraction"):
        GateThresholds(min_fraction=float("inf"))
    with pytest.raises(ValueError, match="finite fraction"):
        GateThresholds(max_without_final_share=-0.1)


def test_a_zero_sample_size_gate_is_rejected():
    with pytest.raises(ValueError, match="at least 1"):
        GateThresholds(min_sessions=0)
    with pytest.raises(ValueError, match="at least 1"):
        GateThresholds(malformed_clean_days=0)


def test_the_gate_helper_reports_every_independent_failure_at_once():
    # An operator re-running after each single fix would burn a window per attempt.
    from app.browser_provenance_metrics import AdoptionStats

    reasons = evaluate_gate(
        stats=AdoptionStats(sessions_considered=10, sessions_v2=5, sessions_without_final=9),
        distribution={PROVENANCE_V2: 5, PROVENANCE_LEGACY: 5},
        recent_malformed_sessions=["s1"],
        thresholds=GateThresholds(),
    )
    assert len(reasons) == 4


# ---------------------------------------------------------------------------
# The CLI's exit-code contract (0 gate met / 1 gate missed / 2 unusable evidence)
# ---------------------------------------------------------------------------


def _cli(tmp_path, *extra):
    """A run that stops at window validation, so it never touches the CLI."""
    return [
        "--since", "2026-07-11T00:00:00Z",
        "--until", "2026-07-25T00:00:00Z",
        "--report-out", str(tmp_path / "report.json"),
        *extra,
    ]


def test_a_report_that_cannot_be_written_exits_2_rather_than_1(tmp_path, caplog):
    # Exit 1 means "evidence complete, gate not met" — a claim about the fleet.
    # An uncaught write error made every failure here say exactly that, with a
    # traceback where the numbers should be, and the report IS the artifact an
    # operator checks before flipping a profile.
    argv = _cli(tmp_path / "no-such-directory")
    with caplog.at_level(logging.ERROR):
        assert main(argv) == 2
    assert any("could not write the report" in r.message for r in caplog.records)


def test_a_run_blocked_before_retrieval_still_writes_its_report(tmp_path):
    # Exit 2 with no file would leave nothing to explain the refusal.
    assert main(_cli(tmp_path)) == 2
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["gate_met"] is False
    assert report["evidence_problems"]


def test_print_commands_shows_every_query_the_run_would_make(tmp_path, capsys):
    assert main(_cli(tmp_path, "--print-commands")) == 0
    printed = capsys.readouterr().out
    # Both drop-warning queries, not just the one: an operator reproducing the
    # loss check by hand with a single filter would reproduce the blind spot.
    for warning_filter in DROP_WARNING_FILTERS:
        assert warning_filter in printed
