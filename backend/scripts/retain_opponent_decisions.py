"""Prune expired opponent replay envelopes and stale targeting facts.

Default: no deletion. It takes the same short row locks and evaluates the same
fresh-clock predicates as a real run — which is what makes its report the rows a
real run would remove rather than an estimate — and issues no DELETE. Those locks
block nothing: envelopes are insert-only, so no live request locks one, and plain
reads never wait on a row lock. ``--apply`` additionally requires the retention
policy to be enabled, ``OPPONENT_TARGET_SOURCE=facts`` so the counters no longer
read the rows being deleted, ``OPPONENT_DECISION_CLEANUP_ENABLED=1`` and a
recorded ``OPPONENT_DECISION_CLEANUP_NOT_BEFORE`` the database clock has already
passed — the rollout's activation + 7 days. See RETAIN_OPPONENT_DECISIONS.md.

Finite by construction: the run stops at its row or time budget and exits. It
keeps no cursor, file or lock between invocations, so the hourly Railway job can
be killed, redeployed or run twice without losing or double-counting work.

Exit status: 0 healthy, 1 alerting (backlog lag or a missing-deadline invariant),
2 refused (misconfiguration, wrong dialect, or deletion not yet authorized). The
two non-zero statuses are kept apart on purpose: 1 means a sweep ran and reported
something, 2 means no sweep happened and an operator has to change something.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy.orm import Session  # noqa: E402

from app.opponent_cleanup import (  # noqa: E402
    BATCH_BYTES, BATCH_ROWS, RUN_ROWS, RUN_SECONDS, SESSION_PAGE,
    CleanupRefused, SweepReport, sweep,
)

REFUSED = 2


def run(engine, *, apply: bool = False, **budget) -> SweepReport:
    """Sweep the primary database. PostgreSQL only: the locking IS the contract.

    ``SKIP LOCKED`` and row-level ``FOR UPDATE OF`` are what keep this job off a
    live request's path. SQLite compiles both away, so a sweep there would be a
    different algorithm wearing the same name.
    """
    if engine.dialect.name != "postgresql":
        raise CleanupRefused("Opponent decision cleanup requires PostgreSQL")
    return sweep(lambda: Session(engine), apply=apply, **budget)


def report_json(report: SweepReport) -> str:
    payload = asdict(report)
    expiry = report.oldest_overdue_expiry
    payload["oldest_overdue_expiry"] = None if expiry is None else expiry.isoformat()
    payload["healthy"] = report.healthy
    return json.dumps(payload, sort_keys=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Delete instead of reporting")
    parser.add_argument("--session-page", type=int, default=SESSION_PAGE)
    parser.add_argument("--batch-rows", type=int, default=BATCH_ROWS)
    parser.add_argument("--batch-bytes", type=int, default=BATCH_BYTES)
    parser.add_argument("--run-rows", type=int, default=RUN_ROWS)
    parser.add_argument("--run-seconds", type=float, default=RUN_SECONDS)
    args = parser.parse_args()
    from app.db import engine

    try:
        report = run(
            engine, apply=args.apply, session_page=args.session_page,
            batch_rows=args.batch_rows, batch_bytes=args.batch_bytes,
            run_rows=args.run_rows, run_seconds=args.run_seconds,
        )
    except CleanupRefused as refusal:
        print(json.dumps({"refused": str(refusal)}), file=sys.stderr)
        return REFUSED
    print(report_json(report))
    return 0 if report.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
