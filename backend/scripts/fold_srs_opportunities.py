"""Drive the SRS opportunity compactor and its finite recovery by hand.

Four verbs, and the order they matter in:

    status   what has been folded, and how long recovery still has to run
    sweep    fold bounded batches for users that have foldable evidence
    restore  put a batch's raw rows back, exactly, inside the window
    expire   delete manifests and artifacts past seven days, plus orphans

This is the manual entry point — for the rollback rehearsal, for a canary, and
for an operator who needs to answer "can we still roll back?" from a prompt.
RECURRING scheduling, activity avoidance and the 12h/18h escalation belong to
``g-srs-cleanup-schedule`` and are deliberately not here: a cron that called this
would be that scheduler, built by accident and without its guarantees.

Nothing folds until ``cleanup_enabled`` is set, which ``g-srs-retain-rollout``
owns. Until then ``sweep`` reports ``disabled`` and changes nothing, which is the
correct behaviour on every deployment today.

    cd backend && source .venv/bin/activate
    python scripts/fold_srs_opportunities.py status
    python scripts/fold_srs_opportunities.py sweep --max-batches 5
    python scripts/fold_srs_opportunities.py restore --user 1234
    python scripts/fold_srs_opportunities.py expire

See scripts/RECOVER_SRS_FOLD.md for the rehearsal and the seven-day boundary.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy.orm import Session  # noqa: E402

from app.db import engine  # noqa: E402
from app.opportunity_fold import (  # noqa: E402
    FoldLimits,
    eligible_users,
    sweep,
)
from app.opportunity_fold_export import export_dir  # noqa: E402
from app.opportunity_retention import load_policy  # noqa: E402
from app.opportunity_fold_recovery import (  # noqa: E402
    RecoveryExpired,
    RecoveryRefused,
    expire_fold_artifacts,
    fold_status,
    restore_folded_evidence,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = parser.add_subparsers(dest="verb", required=True)

    verbs.add_parser("status", help="what has been folded and how long recovery has left")

    folding = verbs.add_parser("sweep", help="fold bounded batches")
    folding.add_argument("--user", type=int, action="append", dest="users",
                         help="restrict to this user id; repeatable")
    folding.add_argument("--max-batches", type=int, default=50,
                         help="stop after this many committed batches (default 50)")
    folding.add_argument("--max-pairs", type=int, default=FoldLimits.max_pairs,
                         help="starting batch size in pairs; it only ever shrinks")
    folding.add_argument("--max-blunders", type=int, default=FoldLimits.max_blunders,
                         help="distinct blunders per batch")

    recovery = verbs.add_parser("restore", help="put deleted rows back, inside the window")
    recovery.add_argument("--user", type=int, action="append", dest="users",
                          help="restrict to this user id; repeatable")
    recovery.add_argument("--batch", action="append", dest="batches",
                          help="restrict to this batch id; repeatable")

    verbs.add_parser("expire", help="delete expired manifests, artifacts and orphans")
    return parser.parse_args(argv)


def _print_status() -> int:
    status = fold_status(engine)
    print(f"export directory: {export_dir()}")
    print(f"database clock:   {status.now}")
    if status.first_fold_committed_at is None:
        print("first fold:       never — nothing has been deleted")
    else:
        print(f"first fold:       {status.first_fold_committed_at}")
        print(f"recovery until:   {status.deadline}")
        state = "CLOSED" if status.expired else f"open, {status.remaining} left"
        print(f"recovery window:  {state}")
    print(f"batches:          {status.unrestored_batches} unrestored, "
          f"{status.restored_batches} restored, {status.expiring_batches} past expiry")
    # Read the switch rather than inferring it from an empty list: "nothing left
    # to fold" and "folding is turned off" are the same zero and a very different
    # thing to be told during a rollout.
    with Session(bind=engine) as db:
        enabled = load_policy(db).cleanup_enabled
    print(f"foldable users:   {len(eligible_users(engine, limit=1000))}"
          f" (cleanup is {'enabled' if enabled else 'DISABLED'})")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.verb == "status":
        return _print_status()

    if args.verb == "sweep":
        limits = FoldLimits(max_pairs=args.max_pairs, max_blunders=args.max_blunders)
        report = sweep(engine, user_ids=args.users, limits=limits,
                       max_batches=args.max_batches)
        print(f"batches={report.batches} rows_deleted={report.rows_deleted} "
              f"candidates={report.candidates} "
              f"max_lock={report.max_lock_seconds:.3f}s")
        for outcome, count in sorted(report.outcomes.items()):
            print(f"  {outcome}: {count}")
        for failure in report.errors:
            print(f"  ERROR {failure}", file=sys.stderr)
        # Per-user failures do not fail the sweep — one user must not stop the
        # rest — but they must not be invisible to a caller either.
        return 1 if report.errors else 0

    if args.verb == "restore":
        batches = [uuid.UUID(value) for value in (args.batches or [])] or None
        try:
            report = restore_folded_evidence(engine, user_ids=args.users,
                                             batch_ids=batches)
        except (RecoveryExpired, RecoveryRefused) as refusal:
            print(f"refused: {refusal}", file=sys.stderr)
            return 2
        print(f"batches={report.batches} rows_restored={report.rows_restored} "
              f"skipped_missing_parent={report.rows_skipped_missing_parent} "
              f"skipped_present={report.rows_skipped_present} "
              f"summaries_adjusted={report.summaries_adjusted} "
              f"summaries_missing={report.summaries_missing} "
              f"since_review_left_alone={report.since_review_left_alone}")
        if report.anchor_cleared:
            print("recovery anchor cleared: nothing is folded any more, so the "
                  "seven-day clock has stopped and the next fold starts a new one")
        elif not report.failures and fold_status(engine).first_fold_committed_at:
            # Everything asked for came back and the clock is still running. Either
            # something was folded again while this ran, or the clear could not get
            # its lock on the manifest table within the bound — a whole-user purge
            # or an account deletion holds that table for its whole transaction.
            # Neither costs anything but a re-run, and neither used to say so.
            print("recovery anchor still set: either something is folded again, or "
                  "the manifest table was locked for too long to check. The deadline "
                  "keeps running until a restore clears it — re-run this to try again")
        for batch_id, reason in report.failures:
            print(f"  FAILED {batch_id}: {reason}", file=sys.stderr)
        # A batch that could not be restored is still deleted. The rest of the
        # rollback went through — that is why they are reported rather than
        # raised — but the run did not do what it was asked.
        return 1 if report.failures else 0

    report = expire_fold_artifacts(engine)
    print(f"manifests_deleted={report.manifests_deleted} "
          f"artifacts_deleted={report.artifacts_deleted} "
          f"orphans_deleted={report.orphans_deleted}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
