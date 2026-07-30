"""Repair historical row-short sessions from their own verified terminal PGN.

Dry-run classification is the default and performs no writes:

    python scripts/repair_short_move_rows.py

Applying requires an explicit flag:

    python scripts/repair_short_move_rows.py --apply

Derived rows carry NULL evaluations, so after applying run the eval repairs in
this order to heal what their own evidence rules allow:

    python scripts/repair_residual_eval_gaps.py [--apply]
    python scripts/backfill_checkmate_final_ply_evals.py [--apply]
    python scripts/backfill_draw_final_ply_evals.py [--apply]

Output is aggregate-only. It never prints session ids, users, FENs, moves, or
evaluation values: the backfill disables logging around planning/applying
(``chess.pgn`` logs SAN/FEN/headers for malformed movetext; serving-path
helpers log session UUIDs), and a failure is reported by exception type only —
SQLAlchemy error text can embed statement parameters, i.e. row data.
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
from app.short_move_row_backfill import run_backfill  # noqa: E402


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
    try:
        run = run_backfill(SessionLocal, apply=args.apply)
    except Exception as exc:  # noqa: BLE001 — aggregate-only output, no row data
        print(
            f"Short-move-row repair aborted: {type(exc).__name__} "
            "(detail withheld: SQL error text can embed row data; every "
            "applied session committed atomically, rerun after diagnosing)",
            flush=True,
        )
        return 1
    plan = run.plan
    outcome = run.outcome
    print(
        "Short-move-row plan: "
        f"ended_visible_null_sessions={plan.ended_visible_null_sessions} "
        f"sessions_rows_short={plan.sessions_rows_short} "
        f"missing_tail_rows={plan.missing_tail_rows} "
        f"verified_sessions={plan.verified_sessions} "
        f"unverifiable_sessions={plan.unverifiable_sessions}",
        flush=True,
    )
    print(
        f"Short-move-row outcome ({'applied' if run.applied else 'dry-run'}): "
        f"sessions_attempted={outcome.sessions_attempted} "
        f"rows_inserted_actual={outcome.rows_inserted_actual} "
        f"sessions_moved_off_none={outcome.sessions_moved_off_none} "
        f"sessions_still_none={outcome.sessions_still_none} "
        f"evidence_sessions_bumped={outcome.evidence_sessions_bumped}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
