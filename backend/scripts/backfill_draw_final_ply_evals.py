"""Backfill historical draw final-ply evals (data repair for g-hs78 / g-c60b).

Repairs VISIBLE draw-ended sessions whose final ``session_moves`` row persisted with
``eval_cp IS NULL AND eval_mate IS NULL`` — a gap the g-hs78 forward fix prevents for new
games but cannot restore historically. Each candidate is verified by replaying the
session's COMPLETE stored chain from the standard start (python-chess) and accepting only
a genuine terminal draw; on success the row is filled ``eval_cp=0, eval_mate=null,
eval_delta=null`` and the affected ``(user_id, player_color)`` evidence sequence is bumped
in the same transaction. Anything that cannot be positively verified is left null.

Threefold repetition and the fifty-move rule are history-dependent, so verification needs
the whole game from an established start — see ``app.draw_final_ply_backfill``, where all
the business logic lives; this is a thin CLI.

No ordering gate (the fill's ``eval_cp=0`` is sign-neutral), but confirm the g-hs78
forward fix is live in the target env so the cohort is closed behind the run.

    python scripts/backfill_draw_final_ply_evals.py --dry-run
    python scripts/backfill_draw_final_ply_evals.py
    python scripts/backfill_draw_final_ply_evals.py --session-id <uuid>
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

from app.db import DATABASE_URL, SessionLocal  # noqa: E402
from app.draw_final_ply_backfill import run_backfill  # noqa: E402


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
    # exactly the "session factory from the DB URL" the module requires: each call returns
    # a fresh Session, and the module owns every session's commit/rollback/close.
    report = run_backfill(SessionLocal, dry_run=args.dry_run, session_id=session_id)

    s = report.sizing
    o = report.outcome
    mode = "DRY RUN (rolled back)" if report.dry_run else "committed"

    # Phase A snapshot forecast (measured via the guarded game_accuracy_for_rows
    # before/after the verified in-memory fill).
    print(
        "Phase A forecast (snapshot): "
        f"total_draw_sessions={s.total_draw_sessions} "
        f"hidden_draw_sessions_excluded={s.hidden_draw_sessions_excluded} "
        f"final_ply_missing_eval={s.final_ply_missing_eval} "
        f"moved_off_none={s.moved_off_none} "
        f"repaired_accuracy_already_non_null={s.repaired_accuracy_already_non_null} "
        f"residual_remains_none={s.residual_remains_none} "
        f"rows_rejected_verification={s.rows_rejected_verification} "
        f"final_ply_eval_cp_zero={s.final_ply_eval_cp_zero} "
        f"reconciles={s.reconciles()}",
        flush=True,
    )
    # Verified-draw subtypes: independent flags (a draw can be several at once), so these
    # do NOT sum to the verified total. Ops report only, no behavioral effect.
    print(
        "Verified draw subtypes: "
        f"stalemate={s.verified_stalemate} "
        f"insufficient_material={s.verified_insufficient_material} "
        f"fifty_move={s.verified_fifty_move} "
        f"threefold={s.verified_threefold}",
        flush=True,
    )
    # Actual Phase B writes (may be < forecast if a candidate was concurrently resolved or
    # demoted by an appended row).
    print(
        f"Phase B actual ({mode}): "
        f"rows_filled_actual={o.rows_filled_actual} "
        f"evidence_groups_bumped_actual={o.evidence_groups_bumped_actual}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
