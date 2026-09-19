"""Aggregate private SRS observations; never export identifiers or raw records."""
from __future__ import annotations

import argparse
from array import array
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.srs_write_telemetry import PrivateStore  # noqa: E402


MIN_AGE_CELL_SESSIONS = 5


def distribution(values, *, sessions: int):
    values = sorted(values)
    if sessions < MIN_AGE_CELL_SESSIONS:
        return dict(count=len(values), mean=None, p50=None, p95=None, p99=None, maximum=None,
                    suppressed=bool(values))
    def quantile(p):
        index = (len(values) - 1) * p
        lo = int(index)
        hi = min(lo + 1, len(values) - 1)
        return values[lo] + (values[hi] - values[lo]) * (index - lo)
    return dict(count=len(values), suppressed=False, mean=sum(values) / len(values),
                p50=quantile(.5), p95=quantile(.95), p99=quantile(.99), maximum=values[-1])


def report(rows, *, candidate_days=(30.0,)) -> dict:
    """Compute both grains. Successful transaction completion is an UPPER bound.

    Session maxima include only actual committed evidence mutations, not no-op
    recomputes. Frozen attempts are separately uncensored. A coverage percentage
    with unresolved observations is explicitly insufficient for Gate B.
    """
    if any(not math.isfinite(day) or day <= 0 for day in candidate_days):
        raise ValueError("candidate days must be finite and positive")
    outcomes = Counter()
    attempted_sessions = set()
    latest = {}
    uncensored_latest = {}
    operations = array("d")
    frozen = array("d")
    frozen_sessions = set()
    categories = defaultdict(lambda: array("d"))
    category_sessions = defaultdict(set)
    late_operations = {day: Counter() for day in candidate_days}
    late_frozen = {day: Counter() for day in candidate_days}
    late_frozen_sessions = defaultdict(set)
    late_operation_counts = Counter()
    late_frozen_counts = Counter()
    invalid_clocks = 0
    missing_clock = 0
    unresolved = {"pending", "abandoned", "commit_unknown", "unsupported_savepoint",
                  "unsupported_external_transaction",
                  "queued", "worker_started", "worker_failed", "enqueue_failed",
                  "enqueue_rejected", "worker_start_failed", "dropped"}
    gap_sessions = set()
    transaction_jobs = set()
    finished_jobs = {}
    first_database_at = last_database_at = None
    # Consume the spool once. Retain compact numeric samples and session/job
    # aggregates, never a second in-memory copy of all private row dictionaries.
    for row in rows:
        outcome, session_id = row["outcome"], row["session_id"]
        outcomes[outcome] += 1
        if outcome != "not_requested":
            attempted_sessions.add(session_id)
        if outcome in unresolved:
            gap_sessions.add(session_id)
        if row.get("job_id") and outcome in {"committed", "rolled_back", "commit_unknown", "abandoned"}:
            transaction_jobs.add(row["job_id"])
        if outcome == "worker_finished":
            finished_jobs[row["id"]] = session_id
        stamp = row["database_at"]
        if stamp is not None:
            first_database_at = min(first_database_at or stamp, stamp)
            last_database_at = max(last_database_at or stamp, stamp)
        if row["outcome"] not in {"committed", "frozen"}:
            continue
        if not row["database_at"] or not row["started_at"]:
            missing_clock += 1
            gap_sessions.add(row["session_id"])
            continue
        completed = datetime.fromisoformat(row["database_at"])
        started = datetime.fromisoformat(row["started_at"])
        age = (completed - started).total_seconds() / 86400
        if age < 0:
            invalid_clocks += 1
            gap_sessions.add(row["session_id"])
            continue
        sources = json.loads(row["sources"])
        if outcome == "frozen":
            frozen.append(age)
            frozen_sessions.add(session_id)
            uncensored_latest[session_id] = max(uncensored_latest.get(session_id, 0), age)
            for days in late_frozen:
                if age >= days:
                    late_frozen_counts[days] += 1
                    late_frozen[days].update(sources)
                    late_frozen_sessions[days].add(session_id)
        elif row["wrote"]:
            operations.append(age)
            latest[session_id] = max(latest.get(session_id, 0), age)
            uncensored_latest[session_id] = max(uncensored_latest.get(session_id, 0), age)
            for source in sources:
                categories[source].append(age)
                category_sessions[source].add(session_id)
            for days in late_operations:
                if age >= days:
                    late_operation_counts[days] += 1
                    late_operations[days].update(sources)
    missing_worker_observations = finished_jobs.keys() - transaction_jobs
    gap_sessions.update(finished_jobs[job] for job in missing_worker_observations)
    candidates = []
    for days in sorted(set(candidate_days)):
        late_sessions = sum(age >= days for age in latest.values())
        covered = (len(latest) - late_sessions) / len(latest) if latest else None
        uncensored_covered = (sum(age < days for age in uncensored_latest.values())
                              / len(uncensored_latest) if uncensored_latest else None)
        candidates.append(dict(m_days=days,
            successful_session_coverage=covered, late_successful_sessions=late_sessions,
            uncensored_session_coverage=uncensored_covered,
            late_successful_operations=late_operation_counts[days],
            successful_operation_coverage=(len(operations) - late_operation_counts[days]) / len(operations)
                if operations else None,
            late_operation_categories=dict(late_operations[days]),
            frozen_late_attempts=late_frozen_counts[days],
            frozen_late_sessions=len(late_frozen_sessions[days]),
            frozen_late_categories=dict(late_frozen[days]),
            numeric_session_coverage_meets_999=uncensored_covered is not None
                and uncensored_covered >= .999 and not gap_sessions,
        ))
    return dict(schema_version=2, generated_at=datetime.now(timezone.utc).isoformat(),
        minimum_age_cell_sessions=MIN_AGE_CELL_SESSIONS,
        first_observed_database_at=first_database_at,
        last_observed_database_at=last_database_at,
        outcomes=dict(outcomes), observed_sessions=len(attempted_sessions),
        sessions_with_successful_evidence_writes=len(latest),
        sessions_without_successful_evidence_writes=len(attempted_sessions - latest.keys()),
        sessions_with_observation_gaps=len(gap_sessions),
        missing_worker_observations=len(missing_worker_observations),
        missing_completion_clocks=missing_clock, invalid_clock_ages=invalid_clocks,
        last_successful_write_age_days=distribution(latest.values(), sessions=len(latest)),
        successful_operation_age_days=distribution(operations, sessions=len(latest)),
        operation_age_days_by_source={key: distribution(ages, sessions=len(category_sessions[key]))
                                      for key, ages in sorted(categories.items())},
        frozen_attempt_age_days=distribution(frozen, sessions=len(frozen_sessions)),
        uncensored_session_age_days=distribution(uncensored_latest.values(), sessions=len(uncensored_latest)),
        candidates=candidates, gate_b_eligible=False,
        qualification_gaps=[
            "Deployment/path verification and an explicit database-time observation start are required.",
            "Verify approximately 30 representative days and complete private spools from every worker/repair host.",
            "Reconcile observation_missing logs, process deaths, clock failover and expiry gaps independently.",
            "A longer-term repair audit and explicit acceptance of the excluded tail remain required.",
            "Numeric coverage alone never authorizes a shorter deadline or activates retention.",
        ])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", required=True, action="append")
    parser.add_argument("--candidate-days", action="append", type=float, default=[])
    parser.add_argument("--expire-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        stores = [PrivateStore(path, require_existing=True) for path in args.private_dir]
        if len({store.path for store in stores}) != len(stores):
            raise ValueError("duplicate private spool")
        result = ({"expired_observations": sum(store.prune() for store in stores)}
                  if args.expire_only else report(
                      (row for store in stores for row in store.iter_rows()),
                      candidate_days=tuple({30.0, *args.candidate_days})))
    except Exception:
        print("Private SRS report failed; no report produced.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
