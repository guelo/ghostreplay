"""Bring every blunder's opportunity summary and review basis up to date.

This is the readiness gate for the SRS retention rollout (g-srs-retention-state).
Migration 20260919_04 creates a summary for every blunder that existed when it
ran, stamped with that blunder's review basis. That is a snapshot: old
application instances keep inserting blunders and reviews until the deploy
finishes, and they write neither a summary nor a basis. Run this after the
deploy — as many times as you like, it is idempotent — until ``--check`` reports
zeros. Only then may ``readiness`` be set.

Why readiness cannot be flipped on the migration alone: after readiness the
counter reader treats a missing summary or a stale basis as a retention
invariant failure and raises. That reader is on the ghost-move path, so a
premature flip turns a rollout gap into 500s on moves.

After readiness this refuses to CREATE summaries and repairs only the review
basis of rows that already exist. Past that point a missing summary is no longer
a rollout gap; it is the loss alarm, and inserting a zero row would overwrite
folded totals and destroy the evidence that anything was lost.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal  # noqa: E402
from app.opportunity_store import (  # noqa: E402
    reconcile_review_basis,
    review_basis_gaps,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report the remaining gap and change nothing (the readiness query)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="reconcile, report what it WOULD write, then roll back",
    )
    parser.add_argument(
        "--force-create-summaries",
        action="store_true",
        help=(
            "create missing summaries even after readiness. Refused by default: "
            "after readiness a missing summary is the loss alarm, and a zero row "
            "would overwrite folded totals and erase it. Use only once you have "
            "established those blunders never folded"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    args = parse_args(argv)
    factory = session_factory or SessionLocal
    with factory() as db:
        if args.check:
            gaps = review_basis_gaps(db)
            print(
                f"missing_summaries={gaps['missing_summaries']} "
                f"basis_mismatches={gaps['basis_mismatches']}"
            )
            # A nonzero gap is a real exit code: this is meant to be the
            # readiness precondition in a deploy script, not advice.
            return 0 if gaps == {"missing_summaries": 0, "basis_mismatches": 0} else 1
        result = reconcile_review_basis(
            db, create_summaries=True if args.force_create_summaries else None
        )
        # Measured BEFORE the rollback, so a dry run reports the gap the real
        # run would leave behind rather than the gap it started from.
        gaps = review_basis_gaps(db)
        if args.dry_run:
            db.rollback()
        else:
            db.commit()
        print(
            f"summaries_created={result['summaries_created']} "
            f"basis_repaired={result['basis_repaired']} "
            f"remaining_missing={gaps['missing_summaries']} "
            f"remaining_mismatched={gaps['basis_mismatches']}"
            + (" (rolled back)" if args.dry_run else "")
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
