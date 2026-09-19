"""Read-only aggregate census of ORIGINAL SRS evidence and decision storage.

One REPEATABLE READ snapshot and one database as-of. No policy is changed and no
shorter deadline is selected. See OBSERVE_SRS_WRITES.md before production use.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402


BASE = """
WITH sessions AS (
  SELECT id, user_id, started_at, status,
    CASE WHEN drill_state = 'converted' THEN 'converted'
         WHEN session_mode = 'drill' THEN 'drill' ELSE 'normal' END AS cohort
  FROM game_sessions
), targets AS (
  SELECT d.session_id, d.target_blunder_id AS blunder_id,
    count(*) AS decisions,
    max(d.served_at) FILTER (WHERE d.served_at >= b.created_at) AS last_eligible
  FROM opponent_decisions d JOIN blunders b ON b.id = d.target_blunder_id
  GROUP BY d.session_id, d.target_blunder_id
), events AS (
  SELECT e.*, s.started_at, s.cohort, s.status,
    coalesce(e.occurred_at, e.created_at) AS effective_at,
    e.opportunity AND coalesce(e.occurred_at, e.created_at)
      >= coalesce(b.created_at, e.created_at) AS broad_eligible,
    t.session_id IS NOT NULL AS ever_targeted,
    coalesce(t.last_eligible >= :as_of - interval '30 days', false) AS pinned,
    t.last_eligible,
    e.occurred_at IS NOT NULL AND e.occurred_at = s.started_at
      AND e.occurred_at < :as_of AS timestamp_audited
  FROM blunder_opportunity_events e
  JOIN sessions s ON s.id = e.session_id JOIN blunders b ON b.id = e.blunder_id
  LEFT JOIN targets t ON t.session_id = e.session_id AND t.blunder_id = e.blunder_id
)
"""


def _rows(connection, query, parameters):
    return [dict(row) for row in connection.execute(text(query), parameters).mappings()]


def census(engine, *, candidate_days=(), grace_hours=1.0) -> dict:
    """Candidates are caller-supplied measurements; 30d is always a baseline scenario.

    Legacy NULL/mismatched timestamps are counted and conservatively excluded
    from foldability until their explicit audit is resolved. The source is the
    original decision envelopes, so run BEFORE their independent pruning.
    """
    deadlines = sorted(set([30.0, *candidate_days]))
    if any(not math.isfinite(day) or day <= 0 for day in deadlines):
        raise ValueError("candidate days must be finite and positive")
    if not math.isfinite(grace_hours) or grace_hours < 0:
        raise ValueError("grace must be finite and nonnegative")
    if engine.dialect.name != "postgresql":
        raise ValueError("storage census requires PostgreSQL")
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        with conn.begin():
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SET LOCAL statement_timeout = '60s'"))
            as_of = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
            parameters = {"as_of": as_of, "grace": grace_hours / 24}
            diagnostics = _rows(conn, BASE + """
              SELECT count(*) AS rows,
                count(*) FILTER (WHERE effective_at < :as_of - interval '60 days') AS older_60d,
                count(*) FILTER (WHERE effective_at < :as_of - interval '30 days'
                  AND NOT ever_targeted) AS older_30d_never_targeted,
                count(*) FILTER (WHERE effective_at < :as_of - interval '30 days'
                  AND NOT pinned) AS older_30d_not_currently_targeted,
                count(*) FILTER (WHERE occurred_at IS NULL) AS null_occurred_at,
                count(*) FILTER (WHERE effective_at != started_at) AS mismatched_time,
                count(*) FILTER (WHERE effective_at > :as_of OR started_at > :as_of) AS future_time,
                count(*) FILTER (WHERE NOT timestamp_audited) AS timestamp_audit_required,
                count(*) FILTER (WHERE NOT broad_eligible AND opportunity AND reached AND pinned)
                  AS broad_ineligible_targeted_reached,
                count(*) FILTER (WHERE ever_targeted) AS ever_targeted_event_pairs,
                count(*) FILTER (WHERE pinned) AS current_target_event_pairs
              FROM events
            """, parameters)[0]
            fanout = _rows(conn, BASE + """, event_fanout AS (
              SELECT session_id, count(*) AS broad_pairs,
                count(*) FILTER (WHERE ever_targeted) AS target_event_pairs
              FROM events GROUP BY session_id
            ), target_fanout AS (
              SELECT session_id, count(*) AS target_pairs,
                count(*) FILTER (WHERE last_eligible >= :as_of - interval '30 days') AS current_pairs
              FROM targets GROUP BY session_id
            ), per_session AS (
              SELECT s.id, s.cohort, s.started_at, s.status,
                coalesce(e.broad_pairs, 0) AS broad_pairs,
                coalesce(t.target_pairs, 0) AS target_pairs,
                coalesce(e.target_event_pairs, 0) AS target_event_pairs,
                coalesce(t.current_pairs, 0) AS current_pairs
              FROM sessions s
              LEFT JOIN event_fanout e ON e.session_id = s.id
              LEFT JOIN target_fanout t ON t.session_id = s.id
            ) SELECT cohort, count(*) AS sessions,
                count(*) FILTER (WHERE status = 'active') AS active_sessions,
                count(*) FILTER (WHERE started_at >= :as_of - interval '30 days'
                  AND started_at <= :as_of) AS sessions_started_30d,
                avg(broad_pairs) AS broad_pairs_mean,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY broad_pairs) AS broad_pairs_p95,
                max(broad_pairs) AS broad_pairs_max,
                avg(broad_pairs) FILTER (WHERE status = 'active') AS active_broad_pairs_mean,
                max(broad_pairs) FILTER (WHERE status = 'active') AS active_broad_pairs_max,
                avg(target_pairs) AS target_pairs_mean,
                percentile_cont(ARRAY[0.5,0.95,0.99]) WITHIN GROUP (ORDER BY target_pairs)
                  AS target_pairs_p50_p95_p99,
                max(target_pairs) AS target_pairs_max,
                avg(target_event_pairs) AS target_event_pairs_mean,
                percentile_cont(ARRAY[0.5,0.95,0.99]) WITHIN GROUP (ORDER BY target_event_pairs)
                  AS target_event_pairs_p50_p95_p99,
                max(target_event_pairs) AS target_event_pairs_max,
                sum(target_pairs) AS ever_targeted_pairs,
                sum(current_pairs) AS current_targeted_pairs,
                sum(target_pairs - target_event_pairs) AS targets_without_event,
                avg(broad_pairs) FILTER (WHERE started_at >= :as_of - interval '30 days'
                  AND started_at <= :as_of) AS recent_broad_pairs_mean,
                avg(target_event_pairs) FILTER (WHERE started_at >= :as_of - interval '30 days'
                  AND started_at <= :as_of) AS recent_target_event_pairs_mean
              FROM per_session GROUP BY cohort ORDER BY cohort
            """, parameters)
            policies = []
            for days in deadlines:
                parameters["days"] = days
                policy = _rows(conn, BASE + """
                  SELECT count(*) FILTER (WHERE started_at <= :as_of - :days * interval '1 day')
                      AS sessions_past_m_event_rows,
                    count(*) FILTER (WHERE started_at <= :as_of - (:days + :grace) * interval '1 day'
                      AND timestamp_audited AND NOT pinned) AS foldable_union,
                    count(*) FILTER (WHERE started_at <= :as_of - (:days + :grace) * interval '1 day'
                      AND timestamp_audited AND pinned) AS solely_target_pinned_pairs,
                    count(*) FILTER (WHERE pinned AND started_at < :as_of - :days * interval '1 day')
                      AS old_session_current_pins,
                    percentile_cont(ARRAY[0.5,0.95,0.99,1.0]) WITHIN GROUP
                      (ORDER BY extract(epoch FROM last_eligible - started_at) / 86400.0)
                      FILTER (WHERE last_eligible IS NOT NULL) AS last_target_age_days,
                    percentile_cont(ARRAY[0.5,0.95,0.99,1.0]) WITHIN GROUP
                      (ORDER BY greatest(0, extract(epoch FROM
                        least(:as_of, last_eligible + interval '30 days') - started_at) / 86400.0
                        - :days - :grace)) FILTER (WHERE last_eligible IS NOT NULL)
                      AS observed_extra_residence_days
                  FROM events
                """, parameters)[0]
                policy.update(m_days=days, grace_days=parameters["grace"],
                              retained_rows=diagnostics["rows"] - policy["foldable_union"],
                              retained_fraction=(diagnostics["rows"] - policy["foldable_union"])
                              / diagnostics["rows"] if diagnostics["rows"] else None,
                              foldable_fraction=policy["foldable_union"] / diagnostics["rows"]
                              if diagnostics["rows"] else None)
                policies.append(policy)
            decisions = _rows(conn, BASE + """
              SELECT s.cohort, d.target_blunder_id IS NOT NULL AS targeted,
                count(*) AS rows, count(DISTINCT d.session_id) AS sessions,
                sum(octet_length(d.response_payload)) AS payload_bytes,
                sum(pg_column_size(d)) AS tuple_bytes,
                count(*) FILTER (WHERE served_at >= :as_of - interval '30 days'
                  AND served_at <= :as_of) AS served_30d,
                sum(octet_length(d.response_payload)) FILTER (
                  WHERE served_at >= :as_of - interval '30 days' AND served_at <= :as_of)
                  AS payload_bytes_served_30d,
                count(*) FILTER (WHERE served_at > :as_of) AS future_served_at
              FROM opponent_decisions d JOIN sessions s ON s.id = d.session_id
              GROUP BY s.cohort, d.target_blunder_id IS NOT NULL ORDER BY s.cohort, targeted
            """, parameters)
            repeated = _rows(conn, BASE + """
              SELECT count(*) AS targeted_pairs, coalesce(sum(decisions - 1), 0) AS repeated_decisions,
                max(decisions) AS max_decisions_per_pair,
                percentile_cont(ARRAY[0.5,0.95,0.99]) WITHIN GROUP (ORDER BY decisions)
                  AS decisions_per_pair_p50_p95_p99
              FROM targets
            """, parameters)[0]
            storage = _rows(conn, """
              SELECT c.relname AS relation, pg_total_relation_size(c.oid) AS total_bytes,
                pg_indexes_size(c.oid) AS index_bytes,
                pg_relation_size(c.oid) AS heap_bytes,
                CASE WHEN c.reltoastrelid = 0 THEN 0
                  ELSE pg_total_relation_size(c.reltoastrelid) END AS toast_bytes
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = current_schema()
                AND c.relname IN ('blunder_opportunity_events', 'opponent_decisions')
              ORDER BY c.relname
            """, parameters)
            retained_blunders = conn.execute(text("SELECT count(*) FROM blunders")).scalar_one()
            target_timing = _rows(conn, BASE + """, last_by_session AS (
              SELECT session_id, max(last_eligible) AS last_eligible FROM targets GROUP BY session_id
            ) SELECT s.cohort, count(*) AS targeted_sessions,
              percentile_cont(ARRAY[0.5,0.95,0.99,1.0]) WITHIN GROUP
                (ORDER BY extract(epoch FROM t.last_eligible - s.started_at) / 86400.0)
                AS last_eligible_target_age_days
              FROM last_by_session t JOIN sessions s ON s.id = t.session_id
              WHERE t.last_eligible IS NOT NULL GROUP BY s.cohort ORDER BY s.cohort
            """, parameters)
            active_users = conn.execute(text("""SELECT count(DISTINCT user_id) FROM game_sessions
              WHERE started_at >= :as_of - interval '30 days' AND started_at <= :as_of
            """), parameters).scalar_one()
    event_bytes = next(item["total_bytes"] for item in storage
                       if item["relation"] == "blunder_opportunity_events")
    bytes_per_row = event_bytes / diagnostics["rows"] if diagnostics["rows"] else None
    decision_storage = next(item for item in storage if item["relation"] == "opponent_decisions")
    decision_count = sum(row["rows"] for row in decisions)
    decision_projections = []
    for row in decisions:
        row["payload_bytes_per_session"] = row["payload_bytes"] / row["sessions"]
        row["rows_per_session"] = row["rows"] / row["sessions"]
        row["allocated_bytes_per_session_estimate"] = {
            key: decision_storage[key] * row["rows"] / decision_count / row["sessions"]
            for key in ("total_bytes", "index_bytes", "toast_bytes")
        }
        for scale in (1, 10, 100):
            daily_rows = scale * row["served_30d"] / 30
            decision_projections.append(dict(
                cohort=row["cohort"], targeted=row["targeted"], scale=scale,
                arrival_rows_per_day=daily_rows,
                arrival_payload_bytes_per_day=scale * float(row["payload_bytes_served_30d"] or 0) / 30,
                allocated_bytes_per_day_estimate=daily_rows * decision_storage["total_bytes"] / decision_count,
            ))
    projections = []
    for policy in policies:
        for cohort in fanout:
            rate = cohort["sessions_started_30d"] / 30
            f = float(cohort["recent_broad_pairs_mean"] or 0)
            te = float(cohort["recent_target_event_pairs_mean"] or 0)
            for scale in (1, 10, 100):
                rows = scale * rate * (f * (policy["m_days"] + parameters["grace"]) + te * 30 + f)
                projections.append(dict(cohort=cohort["cohort"], scale=scale,
                    m_days=policy["m_days"], raw_rows_with_24h_cleanup_lag=rows,
                    raw_bytes_at_current_allocation=rows * bytes_per_row if bytes_per_row else None))
    return dict(schema_version=1, as_of=as_of.isoformat(), source="original_decisions",
                diagnostics=diagnostics, fanout=fanout, policies=policies,
                decisions=decisions, decision_projections=decision_projections,
                repeated_decisions=repeated, target_timing=target_timing, storage=storage,
                retained_blunders=retained_blunders, active_users_30d=active_users,
                projections=projections, gate_b_eligible=False,
                limitations=[
                    "Diagnostic cohorts overlap; only foldable_union is deduplicated.",
                    "NULL/mismatched/future timestamps require audit before folding.",
                    "Original envelopes must be intact; independent decision pruning invalidates this census.",
                    "Physical relation sizes can change during the logical snapshot; allocation is not compacted size.",
                    "Recent sessions have incomplete lifetime fanout; projections are scenarios, not a capacity qualification.",
                    "Projections exclude unimplemented summary/state/recovery storage; Gate A needs an equivalent compacted fixture.",
                    "Decision served_30d is retained arrival volume, not measured net growth; compare two censuses.",
                    "Decision allocation by cohort is prorated by rows; index/TOAST allocation cannot be attributed exactly to sessions.",
                ])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-days", type=float, action="append", default=[])
    args = parser.parse_args(argv)
    from app.db import engine
    try:
        report = census(engine, candidate_days=args.candidate_days)
    except Exception:
        # SQL exceptions may include parameters or connection credentials.
        print("SRS census failed; inspect privately. No report produced.", file=sys.stderr)
        return 1
    print(json.dumps(report, default=_json_value, sort_keys=True))
    return 0


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return float(value)


if __name__ == "__main__":
    raise SystemExit(main())
