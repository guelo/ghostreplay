"""Reconstruct legacy ``game_sessions.drill_root_reached_ply`` values.

This is a legacy-only repair. An all-session run requires an explicit, frozen
``--started-before`` timestamp so rerunning the command cannot bypass live
root-confirmation for sessions created after boundary activation.

Examples::

    python scripts/backfill_drill_evidence_boundaries.py \
      --all-sessions --started-before 2026-08-01T00:00:00Z --limit 100
    python scripts/backfill_drill_evidence_boundaries.py \
      --all-sessions --started-before 2026-08-01T00:00:00Z \
      --after-session-id 01234567-89ab-cdef-0123-456789abcdef --limit 100
    python scripts/backfill_drill_evidence_boundaries.py \
      --session-id 01234567-89ab-cdef-0123-456789abcdef
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import DATABASE_URL, SessionLocal  # noqa: E402
from app.evidence_boundary_backfill import run_boundary_backfill  # noqa: E402


def _safe_database_url(database_url: str) -> str:
    parts = urlsplit(database_url)
    netloc = parts.netloc
    if "@" in netloc:
        credentials, host = netloc.rsplit("@", 1)
        username = credentials.split(":", 1)[0]
        netloc = f"{username}:***@{host}" if username else f"***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _utc_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be an ISO-8601 timestamp with a UTC offset"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--all-sessions",
        action="store_true",
        help="Process the frozen legacy drill cohort.",
    )
    mode.add_argument(
        "--session-id",
        type=uuid.UUID,
        help="Process one explicitly selected legacy drill session.",
    )
    parser.add_argument(
        "--started-before",
        type=_utc_datetime,
        help="Exclusive UTC creation cutoff; required with --all-sessions.",
    )
    parser.add_argument(
        "--after-session-id",
        type=uuid.UUID,
        help="Resume after this last committed UUID (all-session mode only).",
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        help="Maximum sessions in this committed page (all-session mode only).",
    )
    parser.add_argument(
        "--progress-every",
        type=_non_negative_int,
        default=100,
        help="Progress interval; 0 disables intermediate output.",
    )
    args = parser.parse_args(argv)
    if args.all_sessions and args.started_before is None:
        parser.error("--started-before is required with --all-sessions")
    if args.session_id is not None and args.started_before is not None:
        parser.error("--started-before is only valid with --all-sessions")
    if args.session_id is not None and (
        args.after_session_id is not None or args.limit is not None
    ):
        parser.error("--after-session-id and --limit require --all-sessions")
    return args


def main(
    argv: list[str] | None = None,
    *,
    session_factory=None,
) -> int:
    args = parse_args(argv)
    factory = session_factory or SessionLocal
    db = factory()
    try:
        print(f"DATABASE_URL={_safe_database_url(DATABASE_URL)}", flush=True)
        report = run_boundary_backfill(
            db,
            session_id=args.session_id,
            started_before=args.started_before,
            after_session_id=args.after_session_id,
            limit=args.limit,
            progress_every=args.progress_every,
        )
        print(
            "Boundary backfill committed: "
            f"cohort_sessions={report.cohort_sessions} "
            f"selected_sessions={report.selected_sessions} "
            f"stamped={report.stamped} "
            f"already_stamped={report.already_stamped} "
            f"missing_target={report.missing_target} "
            f"invalid_target={report.invalid_target} "
            f"target_not_observed={report.target_not_observed} "
            f"unreconstructable={report.unreconstructable} "
            f"remaining_null={report.remaining_null} "
            f"last_session_id={report.last_session_id}",
            flush=True,
        )
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
