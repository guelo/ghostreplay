#!/usr/bin/env python3
"""Backfill cached opening score snapshots from historical evidence."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db import DATABASE_URL
from app.opening_cache import (
    get_latest_opening_score_batch,
    list_opening_score_candidate_pairs,
    recompute_opening_scores,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill_opening_scores")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill cached opening score snapshots.")
    parser.add_argument(
        "--database-url",
        default=DATABASE_URL,
        help=f"SQLAlchemy database URL (default: {DATABASE_URL})",
    )
    parser.add_argument("--user-id", type=int, default=None, help="Only backfill one user.")
    parser.add_argument(
        "--player-color",
        choices=("white", "black"),
        default=None,
        help="Only backfill one player color.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit candidate pairs.")
    parser.add_argument("--dry-run", action="store_true", help="Show candidate pairs without writing.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recompute even when a cached batch already exists.",
    )
    args = parser.parse_args()

    engine = create_engine(args.database_url, pool_pre_ping=True)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    with session_local() as db:
        pairs = list_opening_score_candidate_pairs(
            db,
            user_id=args.user_id,
            player_color=args.player_color,
            limit=args.limit,
        )

        if args.dry_run:
            for pair_user_id, pair_color in pairs:
                print(f"{pair_user_id}\t{pair_color}")
            log.info("Dry run found %d candidate pairs", len(pairs))
            return

        recomputed = 0
        skipped = 0

        for pair_user_id, pair_color in pairs:
            latest = get_latest_opening_score_batch(db, pair_user_id, pair_color)
            if latest is not None and not args.force:
                skipped += 1
                continue
            recompute_opening_scores(db, pair_user_id, pair_color)
            recomputed += 1

        log.info(
            "Opening score backfill complete: %d recomputed, %d skipped, %d candidates",
            recomputed,
            skipped,
            len(pairs),
        )


if __name__ == "__main__":
    main()
