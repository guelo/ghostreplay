"""Backfill canonical position winners into ``position_analysis`` from analysis_cache.

Groups existing ``analysis_cache`` rows by ``normalized_fen``, picks exactly one
canonical winner per group (explicit strength-then-deterministic dominance), writes
it into ``position_analysis``, and appends best-move / mate-winner disagreements to
``position_analysis_conflicts``.

Rerunnable / idempotent: an unchanged recomputed winner is a no-op (``updated_at``
does not churn), conflicts dedupe by content signature, and a natively-written
position row (``source_cache_id IS NULL``, a Phase-3 live write) is never
overwritten.

    python scripts/backfill_position_analysis.py --dry-run
    python scripts/backfill_position_analysis.py \
        --normalized-fen "rnbqkbnr/pp2pppp/2p5/3p4/3P4/5N2/PPP1PPPP/RNBQKB1R w KQkq -"
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
from app.position_analysis_backfill import backfill_position_analysis  # noqa: E402


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
        "--dry-run", action="store_true", help="Never commit; roll back at the end."
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum cache rows to load (dry-run-only smoke). Requires --dry-run: "
        "a row cap can load a partial candidate set, so it never persists. Ignored "
        "for --normalized-fen.",
    )
    parser.add_argument(
        "--normalized-fen",
        type=str,
        help="Backfill only this normalized FEN (targeted single-position repair).",
    )
    parser.add_argument(
        "--batch-size", type=int, default=500, help="Groups per commit (real runs)."
    )
    parser.add_argument(
        "--progress-every", type=int, default=1000, help="Progress print interval."
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit is not None and not args.dry_run:
        print("error: --limit requires --dry-run (it never persists).", flush=True)
        return 2
    print(f"DATABASE_URL={_safe_database_url(DATABASE_URL)}", flush=True)
    db = SessionLocal()
    try:
        stats = backfill_position_analysis(
            db,
            normalized_fen=args.normalized_fen,
            limit=args.limit,
            batch_size=args.batch_size,
            progress_every=args.progress_every,
            dry_run=args.dry_run,
        )
        mode = "DRY RUN (rolled back)" if args.dry_run else "committed"
        print(
            f"Backfill {mode}: groups_scanned={stats.groups_scanned} "
            f"candidates_eligible={stats.candidates_eligible} "
            f"winners_inserted={stats.winners_inserted} "
            f"winners_updated={stats.winners_updated} "
            f"winners_unchanged={stats.winners_unchanged} "
            f"skipped_existing_protected={stats.skipped_existing_protected} "
            f"conflicts_recorded={stats.conflicts_recorded} "
            f"conflicts_skipped_duplicate={stats.conflicts_skipped_duplicate} "
            f"skipped_no_eligible={stats.skipped_no_eligible} "
            f"skipped_unparseable_fen={stats.skipped_unparseable_fen}"
        )
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
