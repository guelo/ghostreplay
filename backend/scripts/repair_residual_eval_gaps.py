"""Repair historical eval gaps from trustworthy retained analysis evidence.

Dry-run classification is the default and performs no writes:

    python scripts/repair_residual_eval_gaps.py

Applying requires an explicit flag:

    python scripts/repair_residual_eval_gaps.py --apply

Output is aggregate-only. It never prints session ids, users, FENs, moves, or
evaluation values.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import DATABASE_URL, SessionLocal  # noqa: E402
from app.residual_eval_gap_backfill import run_backfill  # noqa: E402


def _safe_database_url(database_url: str) -> str:
    parts = urlsplit(database_url)
    netloc = parts.netloc
    if "@" in netloc:
        credentials, host = netloc.rsplit("@", 1)
        username = credentials.split(":", 1)[0]
        netloc = f"{username}:***@{host}" if username else f"***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit guarded repairs. Omit for the read-only dry-run plan.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(f"DATABASE_URL={_safe_database_url(DATABASE_URL)}", flush=True)
    run = run_backfill(SessionLocal, apply=args.apply)
    plan = run.plan
    outcome = run.outcome
    print(
        "Historical eval-gap plan: "
        f"ended_visible_null_sessions={plan.ended_visible_null_sessions} "
        f"sessions_with_eval_gaps={plan.sessions_with_eval_gaps} "
        f"missing_eval_rows={plan.missing_eval_rows} "
        f"trustworthy_retained_rows={plan.trustworthy_retained_rows} "
        f"unrecoverable_rows={plan.unrecoverable_rows} "
        f"fully_recoverable_sessions={plan.fully_recoverable_sessions}",
        flush=True,
    )
    print(
        f"Historical eval-gap outcome ({'applied' if run.applied else 'dry-run'}): "
        f"sessions_attempted={outcome.sessions_attempted} "
        f"rows_filled_actual={outcome.rows_filled_actual} "
        f"sessions_moved_off_none={outcome.sessions_moved_off_none} "
        f"sessions_still_none={outcome.sessions_still_none} "
        f"evidence_sessions_bumped={outcome.evidence_sessions_bumped}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
