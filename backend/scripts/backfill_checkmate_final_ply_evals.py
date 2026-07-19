"""Backfill historical checkmate final-ply evals (data repair for g-hs78 / g-eh2w).

Repairs checkmate-ended sessions whose final ``session_moves`` row persisted with
``eval_cp IS NULL AND eval_mate IS NULL`` — a gap the g-hs78 forward fix prevents for
new games but cannot restore historically. Each candidate final ply is verified by
replaying ``fen_before + move_san`` (python-chess); on a verified checkmate the row is
filled ``eval_mate=0, eval_cp=+10000, eval_delta=0`` (mover-relative) and the affected
``(user_id, player_color)`` evidence sequence is bumped in the same transaction.

Ordering gate: run only AFTER g-1l4p (mate-0 sign fix) is deployed — confirm live in
the target env first. All business logic lives in
``app.checkmate_final_ply_backfill``; this is a thin CLI.

    python scripts/backfill_checkmate_final_ply_evals.py --dry-run
    python scripts/backfill_checkmate_final_ply_evals.py
    python scripts/backfill_checkmate_final_ply_evals.py --session-id <uuid>
"""
from __future__ import annotations

import argparse
import logging
import uuid
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.checkmate_final_ply_backfill import run_backfill  # noqa: E402
from app.db import DATABASE_URL, SessionLocal  # noqa: E402


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
        "--dry-run",
        action="store_true",
        help="Never commit; each group's transaction is rolled back.",
    )
    parser.add_argument(
        "--session-id",
        type=str,
        help="Scope the backfill to a single game session (UUID).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session_id = uuid.UUID(args.session_id) if args.session_id else None

    # Required, not cosmetic: the module logs every rejected session/move id and each
    # group's outcome at INFO. A fresh CLI process has no handler configured, so the root
    # logger's default WARNING level would silently drop exactly the per-session detail an
    # operator needs to act on a run — leaving only the totals printed below. basicConfig
    # rather than app.logging_config.configure_logging(), which is the API server's setup
    # (uvicorn loggers) and has no business in a one-off ops script.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    print(f"DATABASE_URL={_safe_database_url(DATABASE_URL)}", flush=True)

    # SessionLocal is a sessionmaker bound to the engine built from DATABASE_URL, i.e.
    # exactly the "session factory from the DB URL" the module requires: each call
    # returns a fresh Session, and the module owns every session's commit/rollback/close.
    report = run_backfill(SessionLocal, dry_run=args.dry_run, session_id=session_id)

    s = report.sizing
    o = report.outcome
    mode = "DRY RUN (rolled back)" if report.dry_run else "committed"

    # Phase A snapshot forecast (measured via the guarded game_accuracy_for_rows
    # before/after the verified in-memory fill).
    print(
        "Phase A forecast (snapshot): "
        f"total_checkmate_sessions={s.total_checkmate_sessions} "
        f"final_ply_missing_eval={s.final_ply_missing_eval} "
        f"moved_off_none={s.moved_off_none} "
        f"repaired_accuracy_already_non_null={s.repaired_accuracy_already_non_null} "
        f"residual_remains_none={s.residual_remains_none} "
        f"rows_rejected_verification={s.rows_rejected_verification} "
        f"mate0_persisted={s.mate0_persisted} "
        f"reconciles={s.reconciles()}",
        flush=True,
    )
    # Actual Phase B writes (may be < forecast if a candidate was concurrently resolved).
    print(
        f"Phase B actual ({mode}): "
        f"rows_filled_actual={o.rows_filled_actual} "
        f"evidence_groups_bumped_actual={o.evidence_groups_bumped_actual}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
