#!/usr/bin/env python3
"""Measure browser-game-v2 fleet adoption from production logs (g-bgv1-cutover §1).

This is the EVIDENCE half of the browser-game-v1 retirement. It is read-only: it
never flips a profile, never writes to the database, and never touches the
registry. Its single output is a JSON report plus an exit code that says whether
the retirement gate is met.

Three pieces, deliberately separated:

* the EMITTER lives in ``app/api/session.py`` (the ``analysis_cache_write``
  summary line, which stamps ``session_final`` and ``session_provenance``);
* the ROLLUP MATH lives in ``app/browser_provenance_metrics.py``
  (``session_v2_adoption``), which is g-mk1d's tested contract and is NOT
  reimplemented or adjusted here;
* this file is only the ADAPTER between them — Railway log retrieval, parsing,
  completeness proof, and the gate.

Why the metric is per-SESSION and not per-row or per-run is explained in
``browser_provenance_metrics``; the short version is that row-weighting lets one
long legacy game swamp the signal and run-weighting re-weights by how chatty a
client's upload loop is.

Completeness is the hard part, not the arithmetic
-------------------------------------------------
An adoption number computed from a log query that silently lost records is worse
than no number: it is biased in an unknown direction and looks authoritative. So
every retrieval path here fails CLOSED:

* Railway's CLI defaults a historical query to 500 records when ``--lines`` is
  omitted, and returns no "there was more" signal. ``--lines`` is therefore always
  passed explicitly and a shard that returns *exactly* the limit is treated as
  AMBIGUOUS, never as complete: the time window is split and re-queried until
  every leaf shard comes back under the limit. The resulting shard tree is
  recorded in the report.
* Railway drops log lines above 500/sec/replica and reports that as a log line of
  its own — which the ``side_effect=analysis_cache_write`` filter necessarily
  EXCLUDES. A filtered query therefore cannot surface its own loss signal, so a
  second, separately-filtered query per deployment looks for the drop warning and
  fails closed on any hit. That side query is also the only proof the main query
  lost nothing, so it has to be trustworthy itself: a record in its result that is
  not a drop warning means the filter is not selecting what the check assumes,
  and invalidates the deployment rather than being skipped.
* Every deployment that could have emitted a record in the window is queried, not
  just the latest one: a window spanning a deploy has its earlier half living in
  the previous container's log stream. Which deployments those are cannot be
  worked out from what Railway exposes — the list is a snapshot of where each
  deployment ENDED UP (``REMOVED`` covers both a container that served for a week
  and one cancelled mid-build) with a creation time that is when its BUILD
  started, no activation time and no removal time. So the query set is not
  narrowed by inference at all: everything created before the window closed is
  queried, back through the deployment list, and coverage is proven by EXHAUSTION
  (``deployment_query_plan`` records the refuted narrowing rules). A full-length
  (1000-record) deployment list is itself treated as possibly truncated, since
  the records dropped off the end are the oldest.
* Any output line that is not a JSON log record fails the run. A silently
  skipped record is a session missing from the DENOMINATOR, and the bias has a
  direction: odd records skew toward older legacy-client sessions, so skipping
  them pushes adoption up.
* Every Railway invocation failure — non-zero exit, missing binary, timeout —
  produces a written report and exit 2, never a traceback and exit 1 (which
  means "the number was produced and missed the bar", the opposite thing).
* The Railway CLI version is recorded, because the pagination and filter
  semantics above are version-dependent and a report without it is not
  reproducible. A version lookup that fails blocks the run rather than
  degrading to "unknown".

The window's own prerequisites are mandatory, not best-effort: the instrumentation
deploy time and the plan's log retention must both be supplied, the retention must
be a value some plan actually offers, ``--until`` may not be in the future, the
window must be recent enough to describe the fleet as it is today, and it must be
at least as long as the clean ``mixed_malformed`` period the gate asserts — a
one-day window cannot certify seven days it never queried. An optional check is
not a check: omitting ``--retention-days`` on a window that reaches past retention
yields exactly the same confident ``gate_met: true`` as a window that fits.

Every input those checks depend on is written into the report — the attested
retention, the instrumentation deploy, the freshness tolerance, the takeover
grace, the generation time — because a run with the tolerance relaxed to a year
is otherwise indistinguishable from one at the default, and a verdict whose
inputs are invisible cannot be independently doubted.

Ordering / timestamp collisions
-------------------------------
``session_v2_adoption`` keeps the latest final run per session with
``run.ts >= current.ts``, so runs sharing a maximum timestamp resolve by INPUT
ORDER. Railway timestamp collisions are not hypothetical here —
``docs/release_a_runbook.md`` §4 records Railway assigning two distinct migration
transitions the same buffered-log timestamp. Rather than perturb g-mk1d's tested
helper, this adapter detects the collision upstream: if one session's
equal-maximum-timestamp final runs disagree about the verdict, the run fails
closed instead of letting list order pick a winner. Timestamps are compared as
exact integer NANOSECONDS (not floats, which cannot represent an epoch-nanosecond
value without ~256ns of rounding, and would manufacture ties that do not exist).

Usage
-----
Link the project first (``railway link``), then::

    python scripts/report_session_v2_adoption.py \
        --service <api-service> --environment production \
        --since 2026-07-11T00:00:00Z --until 2026-07-25T00:00:00Z \
        --instrumentation-deployed-at 2026-07-10T12:00:00Z \
        --retention-days 30

``--print-commands`` prints the exact Railway invocations without querying
anything.

Exit codes: ``0`` gate met, ``1`` evidence complete but the gate is not met,
``2`` the evidence itself is incomplete or invalid (never interpret the numbers).
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.browser_provenance_metrics import (  # noqa: E402
    PROVENANCE_LEGACY,
    PROVENANCE_MIXED_MALFORMED,
    PROVENANCE_NONE,
    PROVENANCE_V2,
    AdoptionStats,
    RunVerdict,
    session_v2_adoption,
)

log = logging.getLogger("report_session_v2_adoption")

# ---------------------------------------------------------------------------
# Railway retrieval contract (re-confirm against the pinned CLI version)
# ---------------------------------------------------------------------------

# The substring the adoption query filters on. Also re-checked per parsed line:
# if a returned record lacks it, the filter did not do what we assume and the
# record counts / completeness proof are meaningless.
SIDE_EFFECT_MARKER = "side_effect=analysis_cache_write"
ADOPTION_FILTER = f'"{SIDE_EFFECT_MARKER}"'

# Railway emits "Railway rate limit of 500 logs/sec reached for replica, update
# your application to reduce the logging rate. Messages dropped: N" when it sheds
# log lines. Matched case-insensitively on EITHER half so a reworded prefix or a
# reworded suffix still trips the check — which is worth nothing unless the query
# that RETRIEVES the warning is equally forgiving. A lone '"Messages dropped"'
# query returns zero records if Railway keeps the prefix and rewords the suffix,
# and zero records is exactly what a clean sample looks like: the check fails
# open, and the wording is server-side, so pinning the CLI version does not pin
# it. So there is one query per marker and the results are unioned.
#
# Two single-phrase queries rather than one OR-ed filter because the CLI's filter
# grammar is not pinned by this script either: a filter Railway parses as a
# literal string instead of a boolean expression matches nothing, which is the
# same fail-open in a different place.
DROP_WARNING_FILTERS = ('"Messages dropped"', '"rate limit of"')
DROP_WARNING_MARKERS = ("messages dropped", "rate limit of")

# ``railway logs`` defaults a historical query to 500 records when --lines is
# omitted (cli/src/commands/logs.rs: `limit: args.lines.or(Some(500))`), and the
# documented maximum is the same 500. Always passed explicitly.
RAILWAY_LOG_LIMIT = 500

# A leaf shard narrower than this that still saturates the limit is not split
# further — it is reported as an unsplittable saturation and fails the run.
MIN_SHARD_WIDTH = timedelta(seconds=60)
MAX_SHARD_DEPTH = 24  # 14 days -> 60s needs ~15 halvings; this is the backstop.

# How far back from the window start a deployment is queried UNCONDITIONALLY,
# outside the capped backward walk. It is a convenience, NOT a coverage bound —
# nothing here proves that a deployment created within it had taken over, or
# that anything created before it had stopped. Two lags stack up and Railway
# exposes neither: created_at is when the incoming BUILD started, not when it
# began serving (build + release + healthchecks can take hours), and the outgoing
# container keeps running afterwards while it drains, its in-process evidence
# scheduler still writing cache rows and emitting these very lines.
DEFAULT_TAKEOVER_GRACE = timedelta(hours=2)

# No default budget on the backward walk: coverage is proven by querying every
# deployment created before the window (see ``deployment_query_plan``), so a
# default cap would silently make the common run incomplete. ``max_carry_in`` is
# opt-in, for cutting a long history short knowingly — and spending it is
# recorded as a coverage gap, not waved through.

# How stale the window's end may be and still describe "the fleet today". The
# criterion is about the currently deployed clients, so a clean report from an
# old window is a true statement about a fleet that has since been replaced.
DEFAULT_FRESHNESS_TOLERANCE = timedelta(hours=24)

# ``railway deployment list --limit`` is capped at 1000 by the CLI. That is a
# MAXIMUM, not a page size, and there is no cursor to continue from — so exactly
# 1000 records is ambiguous in the same way an exactly-500-record log shard is.
DEPLOYMENT_LIST_LIMIT = 1000

# No status set here on purpose. A deployment's status is a snapshot of where it
# ENDED UP, never of when it got there: SUCCESS says a container eventually
# became live, not that it was live before the window opened, and a deployment
# created three hours early can spend four of them in build, release and
# healthchecks while its predecessor keeps serving. Creation time plus a grace
# period is a guess at that same unknown. So statuses are recorded in the report
# and used for nothing — see ``deployment_query_plan`` for the bound that is
# actually provable.

# Railway's longest documented log retention (Enterprise). Hobby/Trial is 7 days
# and Pro is 30. A larger --retention-days cannot be true of any plan, and the
# flag exists precisely to bound the window, so an impossible value is rejected
# rather than allowed to authorize a window reaching past the real horizon.
MAX_DOCUMENTED_RETENTION_DAYS = 90

VALID_VERDICTS = frozenset(
    {PROVENANCE_V2, PROVENANCE_LEGACY, PROVENANCE_MIXED_MALFORMED, PROVENANCE_NONE}
)
# ``_timed_side_effect`` renders field values with %s, so booleans arrive as
# Python's repr, not lowercase JSON.
BOOL_LITERALS = {"True": True, "False": False}


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class EvidenceIncomplete(RuntimeError):
    """Base for every failure that makes the collected evidence unusable.

    They all share one property: the number that WOULD be computed is off by an
    unknown amount in an unknown direction. So none of them is a warning — each
    suppresses the gate verdict entirely and exits 2.
    """

    kind = "evidence_incomplete"


class ShardSaturationError(EvidenceIncomplete):
    """A shard hit the record limit and could not be split any further."""

    kind = "shard_saturated"


class MalformedOutputError(EvidenceIncomplete):
    """The CLI emitted output that is not a stream of JSON log records."""

    kind = "malformed_cli_output"


class RailwayCommandError(EvidenceIncomplete):
    """A Railway CLI invocation could not be run, or failed."""

    kind = "railway_command_failed"


class WarningFilterError(EvidenceIncomplete):
    """The drop-warning query returned something that is not a drop warning.

    Which means the filter is not selecting what this check assumes it selects —
    and the check is the only proof that no records were dropped.
    """

    kind = "warning_filter_unreliable"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParseAnomaly:
    """One record that could not be turned into a trustworthy verdict.

    Anomalies are never silently dropped: any anomaly fails the run, because a
    record we could not interpret is a record whose verdict is unknown, and an
    unknown verdict biases the fraction in an unknown direction.
    """

    kind: str
    detail: str
    deployment_id: str = ""
    timestamp: str = ""

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "deployment_id": self.deployment_id,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class ParsedRun:
    """One ``analysis_cache_write`` record, parsed.

    ``verdict`` is what feeds ``session_v2_adoption``; the rest is health/report
    detail that must never influence the adoption math.
    """

    verdict: RunVerdict
    status: str
    provenance_valid: int
    provenance_absent: int
    provenance_malformed: int
    deployment_id: str
    raw_timestamp: str


_RFC3339_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[Tt ](?P<time>\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d{1,9}))?"
    r"(?P<tz>[Zz]|[+-]\d{2}:?\d{2})$"
)


def parse_rfc3339_nanos(value: str) -> int:
    """Parse an RFC3339 timestamp into exact integer nanoseconds since the epoch.

    Railway stamps nanosecond precision (``2026-07-12T17:23:16.325883206Z``).
    ``datetime`` truncates to microseconds and a float epoch value rounds to
    ~256ns, either of which would MANUFACTURE ties between distinct records — and
    ties are exactly what the collision check below has to reason about. So the
    fractional digits are carried through as an integer.
    """
    match = _RFC3339_RE.match(value.strip())
    if match is None:
        raise ValueError(f"not an RFC3339 timestamp: {value!r}")
    tz = match.group("tz")
    offset = "+00:00" if tz in ("Z", "z") else (tz if ":" in tz else f"{tz[:3]}:{tz[3:]}")
    whole = datetime.fromisoformat(f"{match.group('date')}T{match.group('time')}{offset}")
    nanos = int(whole.timestamp()) * 1_000_000_000
    frac = match.group("frac")
    if frac:
        nanos += int(frac.ljust(9, "0"))
    return nanos


def parse_kv_message(message: str) -> dict[str, str]:
    """Split a log message into an EXACT-key ``k=v`` dict.

    Exact keys matter more than it looks. The line renders ``final=`` (g-dckw's
    latency cohort) BEFORE ``session_final=`` (the end-of-session bit this report
    needs), so a ``final=(\\w+)`` style regex — or any suffix match — happily reads
    the wrong field and scores mid-game uploads as completed sessions. Splitting
    on whitespace and keying on the exact token before ``=`` cannot make that
    mistake.

    First occurrence wins, so a later value that happens to contain an ``=`` can
    never shadow a real field.
    """
    fields: dict[str, str] = {}
    for token in message.split():
        key, sep, value = token.partition("=")
        if sep:
            fields.setdefault(key, value)
    return fields


def _parse_int_field(fields: dict[str, str], key: str) -> tuple[int, str | None]:
    raw = fields.get(key)
    if raw is None:
        return 0, f"missing {key}"
    try:
        return int(raw), None
    except ValueError:
        return 0, f"non-integer {key}={raw!r}"


def parse_log_event(
    event: dict, *, deployment_id: str = ""
) -> tuple[ParsedRun | None, ParseAnomaly | None]:
    """Turn one ``railway logs --json`` envelope into a :class:`ParsedRun`.

    ``railway logs --json`` emits one JSON object per record with ``message`` and
    ``timestamp`` keys plus the log's flattened attributes. The timestamp comes
    from that ENVELOPE, never from an ``asctime`` inside the message: the message
    prefix is whatever the process's log formatter produced (local time, no
    timezone, possibly no date), while the envelope value is Railway's own
    ingestion clock and is the only field the shard windows are expressed in.

    A ``status=error`` record is RETAINED when it carries a verdict: provenance is
    classified and stamped (``session.py`` §``_upsert_analysis_cache``) *before*
    ``write_analysis_cache_rows`` runs, so a writer failure does not erase the
    evidence of what the client sent.
    """
    message = event.get("message")
    if not isinstance(message, str):
        return None, ParseAnomaly(
            "missing_message", f"record has no string message: {event!r}", deployment_id
        )
    raw_ts = event.get("timestamp")
    if not isinstance(raw_ts, str):
        return None, ParseAnomaly(
            "missing_timestamp", f"record has no string timestamp: {message}", deployment_id
        )
    if SIDE_EFFECT_MARKER not in message:
        # The filter is supposed to guarantee this. If it did not, the record
        # counts underpinning the completeness proof mean something else.
        return None, ParseAnomaly(
            "unfiltered_record", f"record does not match the query filter: {message}",
            deployment_id, raw_ts,
        )
    try:
        ts = parse_rfc3339_nanos(raw_ts)
    except ValueError as err:
        return None, ParseAnomaly("bad_timestamp", str(err), deployment_id, raw_ts)

    fields = parse_kv_message(message)

    session_id = fields.get("session_id")
    if not session_id:
        return None, ParseAnomaly("missing_session_id", message, deployment_id, raw_ts)

    raw_final = fields.get("session_final")
    if raw_final is None:
        # Pre-g-mk1d instrumentation, or a truncated line. Either way the run's
        # finality is unknown and it cannot be scored.
        return None, ParseAnomaly("missing_session_final", message, deployment_id, raw_ts)
    if raw_final not in BOOL_LITERALS:
        return None, ParseAnomaly(
            "invalid_session_final", f"session_final={raw_final!r}", deployment_id, raw_ts
        )

    verdict = fields.get("session_provenance")
    if verdict is None:
        return None, ParseAnomaly(
            "missing_session_provenance", message, deployment_id, raw_ts
        )
    if verdict not in VALID_VERDICTS:
        return None, ParseAnomaly(
            "invalid_session_provenance",
            f"session_provenance={verdict!r}",
            deployment_id,
            raw_ts,
        )

    valid, valid_err = _parse_int_field(fields, "provenance_valid")
    absent, absent_err = _parse_int_field(fields, "provenance_absent")
    malformed, malformed_err = _parse_int_field(fields, "provenance_malformed")
    count_err = valid_err or absent_err or malformed_err
    if count_err is not None:
        # These are the row-weighted OPERATIONAL health counters, not the metric.
        # They are still stamped in the same block as session_provenance, so one
        # going missing means the line is not the shape this parser assumes.
        return None, ParseAnomaly(
            "bad_health_counters", f"{count_err} in: {message}", deployment_id, raw_ts
        )

    return (
        ParsedRun(
            verdict=RunVerdict(
                session_id=session_id,
                final=BOOL_LITERALS[raw_final],
                session_provenance=verdict,
                ts=ts,
            ),
            status=fields.get("status", ""),
            provenance_valid=valid,
            provenance_absent=absent,
            provenance_malformed=malformed,
            deployment_id=deployment_id,
            raw_timestamp=raw_ts,
        ),
        None,
    )


def extract_drop_warnings(records: list[dict], deployment_id: str) -> list[dict]:
    """The drop warnings in the side query's result — or a hard failure.

    A record in this result that is NOT a drop warning is not noise to skip past:
    it is evidence that the drop-warning filters did not select what this check
    assumes they select. And these queries are the ONLY proof that the adoption
    query lost nothing, so an unreliable filter has to invalidate the deployment
    rather than quietly return an empty warning list.

    The concrete failure it guards against: if the filter degrades to matching
    everything, ordinary log lines fill the 500-record result and a genuine drop
    warning sits past the end of it, unseen — leaving a truncated sample looking
    like a clean one.
    """
    warnings: list[dict] = []
    unexpected: list[str] = []
    for record in records:
        message = record.get("message")
        if isinstance(message, str) and is_drop_warning(message):
            warnings.append(
                {
                    "deployment_id": deployment_id,
                    "timestamp": record.get("timestamp", ""),
                    "message": message,
                }
            )
        else:
            unexpected.append(repr(message)[:120])
    if unexpected:
        raise WarningFilterError(
            f"the drop-warning query for {deployment_id} returned {len(unexpected)} "
            f"record(s) that are not drop warnings (e.g. {unexpected[0]}); the filter "
            "is not selecting what the loss check assumes, so a real drop warning "
            "could be sitting past the end of a result full of ordinary log lines"
        )
    return warnings


def record_identity(record: dict) -> tuple:
    """A hashable identity for one log record, whatever the CLI put in it.

    ``message`` and ``timestamp`` are strings in every documented shape, but a
    perfectly valid JSON record can hold a list or an object there, and hashing
    one raises ``TypeError`` — which is not an :class:`EvidenceIncomplete`, so it
    would escape the per-deployment handler as a traceback and exit 1: the code
    reserved for "evidence complete, gate not met", the opposite claim.

    So nothing is judged here. The value is normalised just enough to
    deduplicate, and the record goes on to be REJECTED where the rejection is
    visible — ``missing_message`` / ``missing_timestamp`` on the adoption path,
    and a filter failure on the warning side query. Dropping it here instead
    would take a session out of the denominator with nothing to show for it.
    """

    def hashable(value):
        try:
            hash(value)
        except TypeError:
            return repr(value)
        return value

    return (hashable(record.get("timestamp")), hashable(record.get("message")))


def is_drop_warning(message: str) -> bool:
    """True for Railway's log-shedding / rate-limit warning line."""
    lowered = message.lower()
    return any(marker in lowered for marker in DROP_WARNING_MARKERS)


def drop_warning_records(
    fetch_filtered,
    deployment_id: str,
    since: datetime,
    until: datetime,
    *,
    filters: tuple[str, ...] = DROP_WARNING_FILTERS,
) -> list[dict]:
    """The union of one drop-warning query PER documented marker.

    :func:`is_drop_warning` recognises a warning by either half of Railway's
    wording, so that a reworded prefix or suffix still trips the loss check. That
    tolerance is only real if retrieval has it too: query on ``Messages dropped``
    alone and a warning that kept the ``rate limit of`` prefix and reworded the
    suffix comes back as zero records — indistinguishable from a sample that lost
    nothing. The wording lives on Railway's side, so no CLI version pin covers it.

    One query per phrase, unioned, rather than one OR-ed filter: the CLI's filter
    grammar is not pinned here either, and an expression Railway matches
    literally would return nothing — the same silent pass in a different place.
    """
    seen: set[tuple] = set()
    records: list[dict] = []
    for log_filter in filters:
        for record in fetch_filtered(deployment_id, since, until, log_filter):
            key = record_identity(record)
            if key in seen:
                continue  # a warning matching both markers is one warning
            seen.add(key)
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# Latest-final grouping and collision detection
# ---------------------------------------------------------------------------


def latest_final_groups(runs: list[RunVerdict]) -> dict[str, list[RunVerdict]]:
    """Per session, every final run tied at that session's maximum timestamp.

    Normally a one-element list. A longer list means Railway stamped two distinct
    records with the same timestamp, which is the case ``session_v2_adoption``
    resolves by input order — see :func:`find_verdict_conflicts`.
    """
    groups: dict[str, list[RunVerdict]] = {}
    for run in runs:
        if not run.final:
            continue
        current = groups.get(run.session_id)
        if current is None or run.ts > current[0].ts:
            groups[run.session_id] = [run]
        elif run.ts == current[0].ts:
            current.append(run)
    return groups


def find_verdict_conflicts(groups: dict[str, list[RunVerdict]]) -> list[dict]:
    """Sessions whose equal-maximum-timestamp final runs DISAGREE.

    An agreeing tie is harmless (either winner yields the same verdict) and is
    allowed through. A disagreeing tie means the reported adoption number depends
    on the order records happened to come back from Railway, so it fails closed.
    """
    conflicts = []
    for session_id, group in sorted(groups.items()):
        verdicts = sorted({run.session_provenance for run in group})
        if len(verdicts) > 1:
            conflicts.append(
                {"session_id": session_id, "ts": group[0].ts, "verdicts": verdicts}
            )
    return conflicts


def verdict_distribution(groups: dict[str, list[RunVerdict]]) -> dict[str, int]:
    """Distinct-session counts for EVERY final verdict, ``none`` included.

    ``session_v2_adoption`` excludes ``none`` from its denominator (a session with
    no browser-eligible move is evidence of nothing), so the helper's numbers
    alone cannot answer "were there zero ``mixed_malformed`` sessions?" — the
    gate's own question. This is the full picture the report publishes so the
    gate is checkable from the report's own output.

    Safe to derive from the tie groups only because a disagreeing tie has already
    failed the run; each group is unanimous by the time this is called.
    """
    counts = {verdict: 0 for verdict in sorted(VALID_VERDICTS)}
    for group in groups.values():
        counts[group[0].session_provenance] += 1
    return counts


def malformed_sessions_since(
    groups: dict[str, list[RunVerdict]], cutoff_nanos: int
) -> list[str]:
    """Sessions whose latest final verdict is ``mixed_malformed`` at/after cutoff.

    A malformed claim anywhere in the recent tail means a deployed client is
    still emitting provenance the server rejects — retiring the v1 fallback under
    that condition would start dropping those rows outright.
    """
    return sorted(
        session_id
        for session_id, group in groups.items()
        if group[0].session_provenance == PROVENANCE_MIXED_MALFORMED
        and group[0].ts >= cutoff_nanos
    )


# ---------------------------------------------------------------------------
# Deployment coverage
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Deployment:
    id: str
    status: str
    created_at: datetime


def deployment_query_plan(
    deployments: list[Deployment],
    window_start: datetime,
    window_end: datetime,
    *,
    takeover_grace: timedelta = DEFAULT_TAKEOVER_GRACE,
) -> tuple[list[Deployment], list[Deployment]]:
    """Which deployments to query, as ``(in_range, carry_in)``.

    Railway's deployment list is a snapshot of CURRENT statuses with a creation
    time, no activation time and no removal time, and that is not enough to
    reconstruct a takeover timeline. ``REMOVED`` is the terminal state both of a
    deployment that served for a week and of one cancelled thirty seconds into
    its build; ``DEPLOYING`` has not taken over yet; and even ``SUCCESS`` only
    says a container became live EVENTUALLY — a deployment created before the
    window may have finished building, releasing and passing healthchecks well
    after the window opened, leaving its predecessor to own the opening. Any rule
    of the form "deployment N's life ends when deployment N+1 appears", and any
    rule that ends a backward search at a status, therefore drops a log stream
    that may hold records. The failure is silent: the window just returns fewer
    sessions, and they are the OLDER — that is, the legacy — ones.

    So no timeline is inferred from statuses, and no status ends anything. Two
    sets come out instead:

    ``in_range`` — created in ``[window_start - takeover_grace, window_end)``.
    All of them are queried whatever their status: created after the window
    closed a deployment cannot have logged inside it, and anything else in range
    might have (a build that failed late may still have emitted a line, and an
    unqueried source of records is an unquantified hole rather than a known-empty
    one).

    ``carry_in`` — every older deployment, NEWEST FIRST, and NOT truncated.
    Coverage here is proven by exhaustion, because every cheaper rule that has
    been tried against this data is unsound:

    * *stop at a status that proves the container ran* — ``SUCCESS`` says it
      became live eventually, not before the window opened. A deployment created
      three hours early can spend four of them in build, release and
      healthchecks, so its predecessor owned the opening.
    * *stop at the first candidate that emitted nothing* — this stream is
      filtered to ``analysis_cache_write`` lines, so it is a record of GAMEPLAY,
      not of lifecycle. A container serving through a quiet hour and an aborted
      build look identical.
    * *stop at the deployment that predates the emitter* — two separate reasons.
      ``created_at`` is build start while ``--instrumentation-deployed-at`` is
      when the emitter reached production, so the emitter's own deployment sorts
      BEFORE that timestamp and any build created (and aborted) during its
      rollout would end the walk in front of it. And pre-g-mk1d containers were
      never silent: they emitted the same ``analysis_cache_write`` line without
      ``session_final``/``session_provenance``. If one drained into the window
      those records must surface as ``missing_session_provenance`` anomalies —
      telemetry that is missing, which is a completeness failure — and skipping
      the deployment converts that into invisible absence instead.

    What is left is the deployment list itself, which is finite and already
    checked for truncation. The cost is real: every deployment created before the
    window is queried, so the walk grows with release history. ``collect`` takes
    a budget for cutting a run short KNOWINGLY, and spending it is recorded as a
    coverage gap. If Railway ever exposes per-deployment activation and removal
    times, that is the bound to switch to — it is the evidence none of the rules
    above could substitute for.
    """
    ordered = sorted(deployments, key=lambda d: (d.created_at, d.id))
    cutoff = window_start - takeover_grace
    in_range = [d for d in ordered if cutoff <= d.created_at < window_end]
    carry_in = list(reversed([d for d in ordered if d.created_at < cutoff]))
    return in_range, carry_in


def deployment_list_truncated(record_count: int, limit: int = DEPLOYMENT_LIST_LIMIT) -> bool:
    """True when the deployment list may have been cut off.

    1000 is the CLI's documented maximum and there is no continuation cursor, so
    a full-length result cannot be distinguished from a truncated one. The
    deployment missing off the end of a truncated list is the OLDEST one — which
    is exactly the one active at the window's start boundary.
    """
    return record_count >= limit


def find_coverage_gaps(required: list[Deployment], queried: set[str]) -> list[str]:
    """Deployment ids that overlap the window but were never successfully queried."""
    return sorted(d.id for d in required if d.id not in queried)


# ---------------------------------------------------------------------------
# Shard planning (the completeness proof)
# ---------------------------------------------------------------------------


@dataclass
class Shard:
    since: datetime
    until: datetime
    record_count: int
    saturated: bool
    children: list["Shard"] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "since": _iso(self.since),
            "until": _iso(self.until),
            "record_count": self.record_count,
            "saturated": self.saturated,
            "children": [child.as_dict() for child in self.children],
        }

    def leaf_count(self) -> int:
        if not self.children:
            return 1
        return sum(child.leaf_count() for child in self.children)


def collect_shard(
    fetch,
    deployment_id: str,
    since: datetime,
    until: datetime,
    *,
    limit: int = RAILWAY_LOG_LIMIT,
    min_width: timedelta = MIN_SHARD_WIDTH,
    max_depth: int = MAX_SHARD_DEPTH,
    _depth: int = 0,
) -> tuple[list[dict], Shard]:
    """Fetch ``[since, until)`` for one deployment, splitting until nothing saturates.

    ``fetch(deployment_id, since, until)`` returns the raw records for that range.
    A result of EXACTLY ``limit`` records is indistinguishable from "there were
    more", because Railway returns no truncation flag — so ``>= limit`` is treated
    as truncated and the range is halved and re-queried. Only leaves strictly
    under the limit are trusted, and the whole tree is published in the report so
    a reader can see the query was not silently capped.

    Raises :class:`ShardSaturationError` if a range at the minimum width still
    saturates: at that point the log path cannot prove completeness and no number
    should be reported.
    """
    records = list(fetch(deployment_id, since, until))
    if len(records) < limit:
        return records, Shard(since, until, len(records), saturated=False)

    if until - since <= min_width or _depth >= max_depth:
        raise ShardSaturationError(
            f"deployment {deployment_id}: {_iso(since)}..{_iso(until)} returned "
            f"{len(records)} records at the {limit}-record limit and cannot be "
            f"split below {min_width}; the query cannot be proven complete"
        )

    midpoint = since + (until - since) / 2
    left_records, left = collect_shard(
        fetch, deployment_id, since, midpoint,
        limit=limit, min_width=min_width, max_depth=max_depth, _depth=_depth + 1,
    )
    right_records, right = collect_shard(
        fetch, deployment_id, midpoint, until,
        limit=limit, min_width=min_width, max_depth=max_depth, _depth=_depth + 1,
    )
    return (
        left_records + right_records,
        Shard(since, until, len(records), saturated=True, children=[left, right]),
    )


def dedupe_records(records: list[dict]) -> tuple[list[dict], int]:
    """Drop records repeated across adjacent shards, keeping first occurrence.

    Shard boundaries are half-open here, but the CLI's ``--since``/``--until``
    inclusivity is not contractually pinned, so a record exactly on a boundary
    could come back from both halves. Duplicates would not change the adoption
    fraction (the rollup is idempotent for identical runs) but they WOULD inflate
    the per-shard record counts that the completeness proof rests on, so they are
    removed and counted.
    """
    seen: set[tuple] = set()
    unique = []
    duplicates = 0
    for record in records:
        key = record_identity(record)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append(record)
    return unique, duplicates


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateThresholds:
    """The retirement criterion. Defaults are the bead's PROPOSED starting point.

    They are deliberately arguments, not constants: the bead requires the observed
    distribution to be measured first and the finally-adopted numbers recorded
    alongside the report that justified them.
    """

    min_fraction: float = 0.95
    max_without_final_share: float = 0.05
    min_sessions: int = 200
    malformed_clean_days: int = 7

    def __post_init__(self) -> None:
        """Reject thresholds that cannot express a real criterion.

        NaN is the dangerous one and it is reachable straight from the command
        line: ``float("nan")`` parses fine, and EVERY comparison against NaN is
        False — so ``--min-fraction nan`` makes a 0%-adoption sample sail through
        a gate that reports itself as met. Validated in ``__post_init__`` rather
        than in the argument parser so no construction path can skip it.
        """
        for name in ("min_fraction", "max_without_final_share"):
            value = getattr(self, name)
            if not isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(
                    f"{name} must be a finite fraction in [0, 1]; got {value!r}"
                )
        if self.min_sessions < 1:
            raise ValueError(
                f"min_sessions must be at least 1; got {self.min_sessions!r} "
                "(a gate that accepts an empty sample is not a gate)"
            )
        if self.malformed_clean_days < 1:
            raise ValueError(
                f"malformed_clean_days must be at least 1; got {self.malformed_clean_days!r}"
            )


def evaluate_gate(
    *,
    stats: AdoptionStats,
    distribution: dict[str, int],
    recent_malformed_sessions: list[str],
    thresholds: GateThresholds,
) -> list[str]:
    """Reasons the gate is NOT met; empty means met.

    Only reached once the evidence is proven complete — a threshold check on
    lossy input is theatre, so completeness failures short-circuit before here.
    """
    reasons = []
    if stats.sessions_considered < thresholds.min_sessions:
        reasons.append(
            f"sample too small: sessions_considered={stats.sessions_considered} "
            f"< {thresholds.min_sessions}"
        )
    if stats.fraction < thresholds.min_fraction:
        reasons.append(
            f"v2 session share {stats.fraction:.4f} < {thresholds.min_fraction}"
        )
    # This one biases the fraction UP, so it is a hard gate rather than a note: a
    # client too old to send terminal_action is also too old to send provenance,
    # which makes the excluded sessions both invisible AND certainly non-adopted.
    if stats.sessions_considered:
        without_final_share = stats.sessions_without_final / stats.sessions_considered
        if without_final_share > thresholds.max_without_final_share:
            reasons.append(
                f"sessions_without_final share {without_final_share:.4f} > "
                f"{thresholds.max_without_final_share} — the fraction overstates adoption"
            )
    elif stats.sessions_without_final:
        reasons.append(
            f"no session was considered but {stats.sessions_without_final} emitted "
            "runs without ever reporting a final one"
        )
    if recent_malformed_sessions:
        reasons.append(
            f"{len(recent_malformed_sessions)} session(s) reported mixed_malformed "
            f"in the final {thresholds.malformed_clean_days} days: "
            f"{', '.join(recent_malformed_sessions[:10])}"
        )
    if distribution.get(PROVENANCE_V2, 0) + distribution.get(PROVENANCE_LEGACY, 0) == 0:
        reasons.append("no session reported a v2 or legacy verdict — empty sample")
    return reasons


def validate_window(
    *,
    window_start: datetime,
    window_end: datetime,
    instrumentation_deployed_at: datetime | None,
    retention_days: int | None,
    now: datetime,
    malformed_clean_days: int = GateThresholds.malformed_clean_days,
    freshness_tolerance: timedelta = DEFAULT_FRESHNESS_TOLERANCE,
) -> list[str]:
    """Reasons the requested window cannot produce trustworthy evidence.

    Checked before a single record is fetched, and each prerequisite is REQUIRED
    rather than skipped-if-absent. An optional check is not a check: omitting
    ``--retention-days`` on a window that reaches past retention produces exactly
    the same confident ``gate_met: true`` as a window that fits, computed from
    however much of the log store had not yet been discarded.

    * **Instrumentation bound.** Records predating g-mk1d carry no
      ``session_provenance`` at all. Their absence reads as a smaller sample
      rather than as missing data, so a window that opens too early quietly
      measures a different population than the one it claims to.
    * **Retention bound.** Railway retention is plan-dependent (Hobby 7d / Pro 30d
      / Enterprise up to 90d); past it the log store has already dropped part of
      the answer.
    * **Not in the future.** ``--until`` beyond now claims coverage of time that
      has not happened. Those shards return few records, come back under the
      limit, and are recorded as complete.
    * **Trailing, not historical.** The criterion is about the fleet as it is now.
      A clean window from two months ago is a true statement about a fleet that
      has since been replaced.
    * **Long enough for its own clean-period claim.** The gate asserts no
      ``mixed_malformed`` session in the final ``malformed_clean_days`` days; a
      window shorter than that asserts it about days it never queried.
    """
    problems = []
    if window_end <= window_start:
        problems.append(f"empty window: {_iso(window_start)}..{_iso(window_end)}")
    elif window_end - window_start < timedelta(days=malformed_clean_days):
        problems.append(
            f"window spans {window_end - window_start} but the gate asserts a clean "
            f"{malformed_clean_days}-day mixed_malformed period; the "
            f"{timedelta(days=malformed_clean_days) - (window_end - window_start)} "
            "that were never queried would be counted as clean"
        )

    if instrumentation_deployed_at is None:
        problems.append(
            "--instrumentation-deployed-at was not supplied; without it a window "
            "reaching back before the g-mk1d deploy cannot be detected, and records "
            "that never carried session_provenance would shrink the sample silently"
        )
    elif window_start < instrumentation_deployed_at:
        problems.append(
            f"window starts {_iso(window_start)}, before the g-mk1d instrumentation "
            f"deploy at {_iso(instrumentation_deployed_at)}"
        )

    if retention_days is None:
        problems.append(
            "--retention-days was not supplied; without the plan's log retention "
            "(Hobby 7 / Pro 30 / Enterprise up to 90) a window that reaches past it "
            "cannot be distinguished from one that fits"
        )
    elif not 1 <= retention_days <= MAX_DOCUMENTED_RETENTION_DAYS:
        # The flag's whole job is to bound the window, so a value no plan offers
        # bounds nothing: --retention-days 10000 waves through a window reaching
        # arbitrarily far past the real horizon.
        problems.append(
            f"--retention-days {retention_days} is outside Railway's documented "
            f"range (1..{MAX_DOCUMENTED_RETENTION_DAYS} days: Hobby/Trial 7, Pro 30, "
            "Enterprise up to 90), so it cannot describe any plan's retention"
        )
    else:
        horizon = now - timedelta(days=retention_days)
        if window_start < horizon:
            problems.append(
                f"window starts {_iso(window_start)}, outside the {retention_days}-day "
                f"retention horizon ({_iso(horizon)}); logs before it are already gone. "
                "If the needed window exceeds retention, add a durable "
                "provenance-observation table before starting a fresh window."
            )

    if window_end > now:
        problems.append(
            f"window ends {_iso(window_end)}, in the future (now {_iso(now)}); the "
            "un-elapsed part would be recorded as queried and complete"
        )
    elif now - window_end > freshness_tolerance:
        problems.append(
            f"window ends {_iso(window_end)}, more than {freshness_tolerance} before "
            f"now ({_iso(now)}); the retirement criterion is about the CURRENT fleet, "
            "so a stale window cannot authorize a flip today"
        )
    return problems


# ---------------------------------------------------------------------------
# Railway CLI invocation
# ---------------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_deployment_list_command(
    railway_bin: str,
    *,
    service: str | None,
    environment: str | None,
    project: str | None,
    limit: int = 1000,
) -> list[str]:
    """``railway deployment list`` — EVERY deployment, not just the latest.

    ``--limit`` defaults to 20 in the CLI and caps at 1000; the explicit maximum
    is used so a busy service's older deployments are not quietly cut off (a
    truncated list is a coverage gap that would never be detected, because the
    missing deployments are also missing from the "required" set).
    """
    cmd = [railway_bin, "deployment", "list", "--json", "--limit", str(limit)]
    if service:
        cmd += ["--service", service]
    if environment:
        cmd += ["--environment", environment]
    if project:
        cmd += ["--project", project]
    return cmd


def build_logs_command(
    railway_bin: str,
    deployment_id: str,
    since: datetime,
    until: datetime,
    *,
    log_filter: str,
    lines: int = RAILWAY_LOG_LIMIT,
) -> list[str]:
    """``railway logs <deployment_id>`` for one shard.

    The deployment id is POSITIONAL; omitting it silently defaults to the most
    recent successful deployment, which is exactly the single-deployment blind
    spot the coverage check exists to prevent. ``--deployment`` selects the
    deployment (application) log stream rather than build/http/network logs, and
    ``--lines`` is always explicit — see :func:`collect_shard`.
    """
    return [
        railway_bin,
        "logs",
        deployment_id,
        "--deployment",
        "--json",
        "--lines",
        str(lines),
        "--filter",
        log_filter,
        "--since",
        _iso(since),
        "--until",
        _iso(until),
    ]


def run_command(cmd: list[str], *, timeout: int = 300) -> str:
    """Run a Railway command, raising :class:`RailwayCommandError` on any failure.

    Every failure mode — non-zero exit, a missing binary, a timeout — is one
    class, because they all mean the same thing to the caller: the records this
    command was supposed to return are missing, and the report must say so rather
    than compute a fraction over what did arrive.
    """
    log.debug("running: %s", " ".join(cmd))
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as err:
        raise RailwayCommandError(f"could not run {' '.join(cmd)}: {err}") from err
    if completed.returncode != 0:
        raise RailwayCommandError(
            f"command failed ({completed.returncode}): {' '.join(cmd)}\n"
            f"{completed.stderr.strip()}"
        )
    return completed.stdout


def parse_json_records(text: str) -> list[dict]:
    """Parse the CLI's JSON output as either a JSON array or NDJSON.

    Every non-blank line must be a JSON object, and anything else fails closed.
    Skipping unparseable lines looks harmless — "it wasn't a log record anyway" —
    but a truncated or corrupt record is exactly a record we were meant to count,
    and dropping it removes a session from the DENOMINATOR without leaving a
    trace. Worse, the bias has a direction: the odd records come
    disproportionately from the older, longer-running, legacy-client sessions,
    so silent skipping pushes the adoption fraction UP.
    """
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as err:
            raise MalformedOutputError(f"CLI output is not valid JSON: {err}") from err
        if not isinstance(payload, list):
            raise MalformedOutputError(
                f"CLI JSON output is not an array of records: {type(payload).__name__}"
            )
        for item in payload:
            if not isinstance(item, dict):
                raise MalformedOutputError(f"non-object record in CLI output: {item!r}")
        return payload
    records = []
    for number, raw_line in enumerate(stripped.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as err:
            raise MalformedOutputError(
                f"line {number} of CLI output is not a JSON record ({err}): {line[:200]}"
            ) from err
        if not isinstance(item, dict):
            raise MalformedOutputError(
                f"line {number} of CLI output is not a JSON object: {line[:200]}"
            )
        records.append(item)
    return records


def parse_deployments(records: list[dict]) -> tuple[list[Deployment], list[ParseAnomaly]]:
    """Normalize ``railway deployment list --json`` output.

    Key spelling differs between CLI versions (``createdAt`` vs ``created_at``),
    so both are accepted; a record with neither is an anomaly, never a skip —
    a deployment silently dropped here is a coverage gap that hides itself.
    """
    deployments: list[Deployment] = []
    anomalies: list[ParseAnomaly] = []
    for record in records:
        deployment_id = record.get("id") or record.get("deploymentId")
        raw_created = record.get("createdAt") or record.get("created_at")
        if not deployment_id or not isinstance(raw_created, str):
            anomalies.append(
                ParseAnomaly("bad_deployment_record", json.dumps(record, sort_keys=True))
            )
            continue
        try:
            created_at = datetime.fromtimestamp(
                parse_rfc3339_nanos(raw_created) / 1e9, tz=timezone.utc
            )
        except ValueError as err:
            anomalies.append(
                ParseAnomaly("bad_deployment_timestamp", str(err), str(deployment_id))
            )
            continue
        deployments.append(
            Deployment(
                id=str(deployment_id),
                status=str(record.get("status", "")),
                created_at=created_at,
            )
        )
    return deployments, anomalies


def railway_cli_version(railway_bin: str) -> str:
    """The CLI version, recorded in the report.

    Raises rather than degrading to ``"unknown"``. Pagination and filter semantics
    are version-dependent, so a report that cannot name the CLI it was produced
    with is not reproducible — and an unreproducible report is not evidence for
    an irreversible cutover, however good its numbers look.
    """
    return run_command([railway_bin, "--version"], timeout=30).strip()


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceInputs:
    """The operator-supplied and tuned values every completeness check depends on.

    Recorded in the report verbatim, because without them the report cannot be
    independently checked. ``--freshness-tolerance-hours 10000`` produces output
    that is indistinguishable from a run at the 24-hour default unless the
    tolerance itself is written down, and the same is true of the retention the
    operator attested to and the instrumentation deploy the window is measured
    against. A reader has to be able to see the inputs to distrust the verdict.

    The Railway selectors are here for the same reason: nothing else in the
    report says WHICH fleet was measured. A run against a staging environment
    answers a question nobody asked, and its output is otherwise shaped exactly
    like a production one. They are recorded rather than validated because a
    Railway environment can be named anything — rejecting every name that is not
    "production" would block correct runs while still passing a staging
    environment that happens to be called production.
    """

    generated_at: datetime
    instrumentation_deployed_at: datetime | None
    retention_days: int | None
    freshness_tolerance: timedelta
    service: str | None = None
    environment: str | None = None
    project: str | None = None
    takeover_grace: timedelta = DEFAULT_TAKEOVER_GRACE
    log_record_limit: int = RAILWAY_LOG_LIMIT
    deployment_list_limit: int = DEPLOYMENT_LIST_LIMIT
    carry_in_budget: int | None = None

    def as_dict(self) -> dict:
        return {
            "generated_at": _iso(self.generated_at),
            # `null` means "whatever `railway link` points at", which is exactly
            # the case where the reader most needs to check the deployment ids.
            "service": self.service,
            "environment": self.environment,
            "project": self.project,
            "instrumentation_deployed_at": (
                _iso(self.instrumentation_deployed_at)
                if self.instrumentation_deployed_at
                else None
            ),
            # Operator-attested: nothing in the CLI reports the plan's retention,
            # so this is a claim the report carries, not a measurement.
            "retention_days_attested": self.retention_days,
            "freshness_tolerance_hours": self.freshness_tolerance.total_seconds() / 3600,
            "takeover_grace_minutes": self.takeover_grace.total_seconds() / 60,
            "log_record_limit": self.log_record_limit,
            "deployment_list_limit": self.deployment_list_limit,
            # `null` = every pre-window deployment was queried, which is what
            # makes the coverage claim an exhaustion rather than an inference.
            "carry_in_budget": self.carry_in_budget,
        }


def build_report(
    *,
    window_start: datetime,
    window_end: datetime,
    inputs: EvidenceInputs,
    thresholds: GateThresholds,
    parsed_runs: list[ParsedRun],
    anomalies: list[ParseAnomaly],
    shard_trees: dict[str, Shard],
    per_deployment_records: dict[str, int],
    duplicates_removed: int,
    coverage_gaps: list[str],
    drop_warnings: list[dict],
    window_problems: list[str],
    required_deployments: list[Deployment],
    cli_version: str,
) -> dict:
    """Assemble the evidence report and its verdict.

    Ordering of the two verdict layers is the point: COMPLETENESS first, gate
    second. A threshold evaluated over records we cannot prove we received is a
    number with an unknown bias, so any completeness failure suppresses the gate
    verdict rather than reporting it alongside.
    """
    verdicts = [run.verdict for run in parsed_runs]
    groups = latest_final_groups(verdicts)
    conflicts = find_verdict_conflicts(groups)
    stats = session_v2_adoption(verdicts)
    distribution = verdict_distribution(groups)
    malformed_cutoff = (
        window_end - timedelta(days=thresholds.malformed_clean_days)
    ).timestamp() * 1e9
    recent_malformed = malformed_sessions_since(groups, int(malformed_cutoff))

    blocking = list(window_problems)
    if not blocking and not required_deployments:
        # An unlinked project or a service typo returns an empty deployment list
        # without failing, which would otherwise read as "zero sessions" — a gate
        # failure — rather than as "the query never reached production".
        blocking.append(
            "no deployment overlaps the window; the service/environment selection "
            "or the project link is wrong"
        )
    if anomalies:
        blocking.append(
            f"{len(anomalies)} retrieval or parsing anomaly(ies): "
            + ", ".join(sorted({a.kind for a in anomalies}))
        )
    if coverage_gaps:
        blocking.append(
            f"{len(coverage_gaps)} deployment(s) overlapping the window were not "
            f"queried: {', '.join(coverage_gaps)}"
        )
    if drop_warnings:
        blocking.append(
            f"{len(drop_warnings)} Railway log-drop/rate-limit warning(s) in the window"
        )
    if conflicts:
        blocking.append(
            f"{len(conflicts)} session(s) have conflicting equal-timestamp final "
            "verdicts; adoption would depend on record order"
        )

    gate_reasons = (
        evaluate_gate(
            stats=stats,
            distribution=distribution,
            recent_malformed_sessions=recent_malformed,
            thresholds=thresholds,
        )
        if not blocking
        else ["not evaluated: the evidence is incomplete (see evidence_problems)"]
    )

    return {
        "window": {
            "since": _iso(window_start),
            "until": _iso(window_end),
            "span_days": (window_end - window_start).total_seconds() / 86400,
        },
        "railway_cli_version": cli_version,
        "completeness_inputs": inputs.as_dict(),
        "thresholds": {
            "min_fraction": thresholds.min_fraction,
            "max_without_final_share": thresholds.max_without_final_share,
            "min_sessions": thresholds.min_sessions,
            "malformed_clean_days": thresholds.malformed_clean_days,
        },
        "adoption": {
            "fraction": stats.fraction,
            "sessions_considered": stats.sessions_considered,
            "sessions_v2": stats.sessions_v2,
            "sessions_without_final": stats.sessions_without_final,
            # Distinct-session counts for every verdict, `none` included, so the
            # zero-mixed_malformed gate is checkable from this report alone.
            "final_verdict_distribution": distribution,
            "recent_mixed_malformed_sessions": recent_malformed,
        },
        # Row-weighted OPERATIONAL health only. Never the adoption metric: a
        # single long legacy game contributes hundreds of rows here.
        "row_health": {
            "provenance_valid": sum(r.provenance_valid for r in parsed_runs),
            "provenance_absent": sum(r.provenance_absent for r in parsed_runs),
            "provenance_malformed": sum(r.provenance_malformed for r in parsed_runs),
            "runs_total": len(parsed_runs),
            "runs_status_error": sum(1 for r in parsed_runs if r.status == "error"),
        },
        "coverage": {
            "deployments_required": [
                {"id": d.id, "status": d.status, "created_at": _iso(d.created_at)}
                for d in required_deployments
            ],
            "deployments_queried": sorted(per_deployment_records),
            "coverage_gaps": coverage_gaps,
            "records_per_deployment": per_deployment_records,
            "shard_leaves_per_deployment": {
                dep: tree.leaf_count() for dep, tree in sorted(shard_trees.items())
            },
            "shard_tree": {dep: tree.as_dict() for dep, tree in sorted(shard_trees.items())},
            "duplicates_removed": duplicates_removed,
            "drop_warnings": drop_warnings,
        },
        "anomalies": [a.as_dict() for a in anomalies],
        "verdict_conflicts": conflicts,
        "evidence_problems": blocking,
        "gate_failures": gate_reasons,
        "gate_met": not blocking and not gate_reasons,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_moment(value: str) -> datetime:
    return datetime.fromtimestamp(parse_rfc3339_nanos(value) / 1e9, tz=timezone.utc)


def empty_collection() -> dict:
    """A zero-record collection, so a failure before retrieval still reports."""
    return {
        "required": [],
        "parsed_runs": [],
        "anomalies": [],
        "shard_trees": {},
        "per_deployment_records": {},
        "duplicates_removed": 0,
        "drop_warnings": [],
        "coverage_gaps": [],
    }


def collect(
    *,
    fetch_logs,
    fetch_warnings,
    in_range: list[Deployment],
    window_start: datetime,
    window_end: datetime,
    carry_in: list[Deployment] | None = None,
    limit: int = RAILWAY_LOG_LIMIT,
    max_carry_in: int | None = None,
) -> dict:
    """Query the planned deployments and return the raw collection result.

    ``in_range`` is queried in full, then every ``carry_in`` candidate in turn,
    newest first. Nothing here decides that a deployment is settled: not its
    status (a snapshot of where it ended up, not of when) and not its silence.

    Silence especially. This log stream is filtered down to
    ``analysis_cache_write`` lines, which makes it a record of GAMEPLAY, not of
    lifecycle: a container serving through a quiet hour emits nothing, an aborted
    build emits nothing, and they are indistinguishable here. So "this candidate
    emitted nothing, therefore everything older than it had stopped" is not an
    inference the data supports — and getting it wrong drops a still-draining
    older container whose sessions are exactly the legacy ones being counted.

    ``max_carry_in`` is an optional budget, not a bound on what mattered.
    Spending it leaves deployments that could have emitted in the window
    unqueried, which is a coverage gap like any other rather than something to
    assume away.

    Injected fetchers keep this — the part with the completeness logic — testable
    without a network or a CLI.
    """
    parsed_runs: list[ParsedRun] = []
    anomalies: list[ParseAnomaly] = []
    shard_trees: dict[str, Shard] = {}
    per_deployment_records: dict[str, int] = {}
    drop_warnings: list[dict] = []
    duplicates_removed = 0
    queried: set[str] = set()
    required: list[Deployment] = list(in_range)

    def query(deployment: Deployment) -> int | None:
        """Fetch one deployment's records; ``None`` if it could not be read."""
        nonlocal duplicates_removed
        # SEPARATE, differently-filtered query, and it runs FIRST: the adoption
        # filter cannot return Railway's drop warning, because the warning is not
        # an analysis_cache_write line. Without this the query hides its own loss.
        # If the warning query itself fails we cannot prove nothing was dropped,
        # so the deployment is abandoned before its records are trusted.
        try:
            warning_records = list(fetch_warnings(deployment.id, window_start, window_end))
            drop_warnings.extend(extract_drop_warnings(warning_records, deployment.id))
            records, tree = collect_shard(
                fetch_logs, deployment.id, window_start, window_end, limit=limit
            )
        except EvidenceIncomplete as err:
            # Never added to ``queried``, so this also surfaces as a coverage gap:
            # the deployment is both an anomaly and an unqueried log source.
            anomalies.append(ParseAnomaly(err.kind, str(err), deployment.id))
            return None

        records, duplicates = dedupe_records(records)
        duplicates_removed += duplicates
        shard_trees[deployment.id] = tree
        per_deployment_records[deployment.id] = len(records)
        queried.add(deployment.id)

        for record in records:
            run, anomaly = parse_log_event(record, deployment_id=deployment.id)
            if anomaly is not None:
                anomalies.append(anomaly)
            elif run is not None:
                parsed_runs.append(run)
        return len(records)

    for deployment in in_range:
        query(deployment)

    candidates = list(carry_in or [])

    def unread_from(index: int) -> None:
        """Record everything from ``index`` on as required-but-never-queried.

        The walk ends where the evidence ends, and EVERY deployment behind that
        point is unread — not just the one it stopped on. Listing only the next
        candidate would understate the hole in exactly the direction that makes a
        report look complete.
        """
        required.extend(candidates[index:])

    for index, deployment in enumerate(candidates):
        required.append(deployment)
        if max_carry_in is not None and index >= max_carry_in:
            # Listed as required but never queried, so it reads as the coverage
            # gap it is: this deployment and every older one may hold window
            # records, and nothing here can tell whether they do.
            remaining = len(candidates) - index
            unread_from(index + 1)
            anomalies.append(
                ParseAnomaly(
                    "carry_in_unbounded",
                    f"--max-carry-in-deployments {max_carry_in} stopped the backward "
                    f"walk with {remaining} pre-window deployment(s) unread, so a "
                    "container still serving when the window opened may never have been "
                    "queried; drop the budget to read them all (a few queries each, and "
                    "they normally return nothing)",
                    deployment.id,
                )
            )
            break
        if query(deployment) is None:
            # Already an anomaly and a gap; skipping past it would hide it, and
            # everything behind it is unread for the same reason.
            unread_from(index + 1)
            break

    return {
        "required": required,
        "parsed_runs": parsed_runs,
        "anomalies": anomalies,
        "shard_trees": shard_trees,
        "per_deployment_records": per_deployment_records,
        "duplicates_removed": duplicates_removed,
        "drop_warnings": drop_warnings,
        "coverage_gaps": find_coverage_gaps(required, queried),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--service", help="Railway service name or id (the API service).")
    parser.add_argument("--environment", default="production")
    parser.add_argument("--project")
    parser.add_argument("--railway-bin", default="railway")
    parser.add_argument(
        "--since", required=True, help="Window start, RFC3339 (e.g. 2026-07-11T00:00:00Z)."
    )
    parser.add_argument("--until", required=True, help="Window end, RFC3339.")
    # Not argparse-`required` so --print-commands stays usable without them, but
    # omitting either is an evidence problem: the run writes a report and exits 2.
    parser.add_argument(
        "--instrumentation-deployed-at",
        help="REQUIRED for a real run. RFC3339 time the g-mk1d session_provenance "
        "instrumentation reached production. The window must start at or after it.",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        help="REQUIRED for a real run. Railway log retention for the plan (Hobby 7 / "
        "Pro 30 / Enterprise up to 90). The window must fit entirely inside it.",
    )
    parser.add_argument(
        "--max-carry-in-deployments",
        type=int,
        default=None,
        help="Optional budget: stop the backward walk after this many pre-window "
        "deployments. Unset (the default) queries every one of them, which is what "
        "makes coverage provable — a container that was still serving when the "
        "window opened cannot be identified from Railway's deployment list, only "
        "queried. Setting this records the unread deployments as a coverage gap.",
    )
    parser.add_argument(
        "--freshness-tolerance-hours",
        type=int,
        default=int(DEFAULT_FRESHNESS_TOLERANCE.total_seconds() // 3600),
        help="How stale --until may be and still describe the CURRENT fleet.",
    )
    parser.add_argument("--min-fraction", type=float, default=GateThresholds.min_fraction)
    parser.add_argument(
        "--max-without-final-share",
        type=float,
        default=GateThresholds.max_without_final_share,
    )
    parser.add_argument("--min-sessions", type=int, default=GateThresholds.min_sessions)
    parser.add_argument(
        "--malformed-clean-days", type=int, default=GateThresholds.malformed_clean_days
    )
    parser.add_argument(
        "--report-out",
        type=Path,
        default=BACKEND_ROOT / "session_v2_adoption_report.json",
    )
    parser.add_argument(
        "--print-commands",
        action="store_true",
        help="Print the Railway commands that would run and exit. No queries.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Bad arguments exit 2 via parser.error, the same code as unusable evidence —
    # both mean "no number was produced", as opposed to exit 1's "the number was
    # produced and missed the bar".
    try:
        window_start = _parse_moment(args.since)
        window_end = _parse_moment(args.until)
        instrumentation_at = (
            _parse_moment(args.instrumentation_deployed_at)
            if args.instrumentation_deployed_at
            else None
        )
    except ValueError as err:
        parser.error(str(err))
    if args.freshness_tolerance_hours < 0:
        parser.error("--freshness-tolerance-hours must not be negative")
    try:
        thresholds = GateThresholds(
            min_fraction=args.min_fraction,
            max_without_final_share=args.max_without_final_share,
            min_sessions=args.min_sessions,
            malformed_clean_days=args.malformed_clean_days,
        )
    except ValueError as err:
        parser.error(str(err))

    list_cmd = build_deployment_list_command(
        args.railway_bin,
        service=args.service,
        environment=args.environment,
        project=args.project,
    )
    if args.print_commands:
        print(" ".join(list_cmd))
        print(
            " ".join(
                build_logs_command(
                    args.railway_bin,
                    "<deployment-id>",
                    window_start,
                    window_end,
                    log_filter=ADOPTION_FILTER,
                )
            )
        )
        for warning_filter in DROP_WARNING_FILTERS:
            print(
                " ".join(
                    build_logs_command(
                        args.railway_bin,
                        "<deployment-id>",
                        window_start,
                        window_end,
                        log_filter=warning_filter,
                    )
                )
            )
        return 0

    generated_at = datetime.now(timezone.utc)
    inputs = EvidenceInputs(
        generated_at=generated_at,
        instrumentation_deployed_at=instrumentation_at,
        retention_days=args.retention_days,
        freshness_tolerance=timedelta(hours=args.freshness_tolerance_hours),
        service=args.service,
        environment=args.environment,
        project=args.project,
        carry_in_budget=args.max_carry_in_deployments,
    )
    problems = validate_window(
        window_start=window_start,
        window_end=window_end,
        instrumentation_deployed_at=instrumentation_at,
        retention_days=args.retention_days,
        now=generated_at,
        malformed_clean_days=thresholds.malformed_clean_days,
        freshness_tolerance=inputs.freshness_tolerance,
    )

    # Everything below is best-effort: a retrieval failure must still produce a
    # written report and exit 2 ("the evidence is unusable"), never a traceback
    # and exit 1 ("evidence complete, gate not met"). The two exit codes mean
    # opposite things to an operator deciding whether to flip a profile.
    cli_version = "unavailable"
    required: list[Deployment] = []
    deployment_anomalies: list[ParseAnomaly] = []
    collected = empty_collection()

    if problems:
        # Nothing below could be trusted anyway, so do not spend a query on it.
        for problem in problems:
            log.error("window rejected: %s", problem)
    else:
        def fetch_logs(deployment_id: str, since: datetime, until: datetime) -> list[dict]:
            return parse_json_records(
                run_command(
                    build_logs_command(
                        args.railway_bin, deployment_id, since, until,
                        log_filter=ADOPTION_FILTER,
                    )
                )
            )

        def fetch_filtered(
            deployment_id: str, since: datetime, until: datetime, log_filter: str
        ) -> list[dict]:
            return parse_json_records(
                run_command(
                    build_logs_command(
                        args.railway_bin, deployment_id, since, until, log_filter=log_filter
                    )
                )
            )

        def fetch_warnings(deployment_id: str, since: datetime, until: datetime) -> list[dict]:
            return drop_warning_records(fetch_filtered, deployment_id, since, until)

        try:
            cli_version = railway_cli_version(args.railway_bin)
            log.info("railway CLI: %s", cli_version)

            deployment_records = parse_json_records(run_command(list_cmd))
            if deployment_list_truncated(len(deployment_records)):
                problems.append(
                    f"the deployment list returned {len(deployment_records)} records at "
                    f"the CLI's {DEPLOYMENT_LIST_LIMIT}-record maximum, so it may be "
                    "truncated; the oldest deployments are dropped first, which is "
                    "where the window's start boundary lives. Narrow the service scope."
                )
            deployments, deployment_anomalies = parse_deployments(deployment_records)
            in_range, carry_in = deployment_query_plan(
                deployments,
                window_start,
                window_end,
                takeover_grace=inputs.takeover_grace,
            )
            required = in_range + carry_in
            log.info(
                "%d deployment(s) listed: %d created in range, %d created earlier "
                "(all queried unless --max-carry-in-deployments cuts the walk short)",
                len(deployments),
                len(in_range),
                len(carry_in),
            )

            if not problems:
                collected = collect(
                    fetch_logs=fetch_logs,
                    fetch_warnings=fetch_warnings,
                    in_range=in_range,
                    carry_in=carry_in,
                    window_start=window_start,
                    window_end=window_end,
                    max_carry_in=args.max_carry_in_deployments,
                )
                required = collected["required"]
        except EvidenceIncomplete as err:
            log.error("%s: %s", err.kind, err)
            problems.append(f"{err.kind}: {err}")

    report = build_report(
        window_start=window_start,
        window_end=window_end,
        inputs=inputs,
        thresholds=thresholds,
        parsed_runs=collected["parsed_runs"],
        anomalies=deployment_anomalies + collected["anomalies"],
        shard_trees=collected["shard_trees"],
        per_deployment_records=collected["per_deployment_records"],
        duplicates_removed=collected["duplicates_removed"],
        coverage_gaps=collected["coverage_gaps"],
        drop_warnings=collected["drop_warnings"],
        window_problems=problems,
        required_deployments=required,
        cli_version=cli_version,
    )

    # The report IS the deliverable — the numbers an operator checks before
    # flipping a profile. If it could not be written there is nothing to check,
    # so this exits 2 ("unusable evidence") even for a run that met the gate.
    # Uncaught, an unwritable path would leave a traceback and exit 1, which is
    # the code reserved for "evidence complete, gate not met" — the opposite
    # claim, and the more dangerous direction to be wrong in.
    try:
        args.report_out.write_text(json.dumps(report, indent=2, sort_keys=True))
    except (OSError, TypeError, ValueError) as err:
        log.error("could not write the report to %s: %s", args.report_out, err)
        return 2
    log.info("Wrote report to %s", args.report_out)

    adoption = report["adoption"]
    log.info(
        "sessions_considered=%d sessions_v2=%d fraction=%.4f without_final=%d",
        adoption["sessions_considered"],
        adoption["sessions_v2"],
        adoption["fraction"],
        adoption["sessions_without_final"],
    )
    log.info("final verdict distribution: %s", adoption["final_verdict_distribution"])

    if report["evidence_problems"]:
        for problem in report["evidence_problems"]:
            log.error("evidence incomplete: %s", problem)
        return 2
    if not report["gate_met"]:
        for reason in report["gate_failures"]:
            log.warning("gate not met: %s", reason)
        return 1
    log.info("Gate MET. Record these numbers on g-bgv1-cutover before flipping.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
