#!/usr/bin/env python3
"""Ingest JeffML/eco.json scores.json into the analysis_cache table.

scores.json maps FEN positions (after a move) to Stockfish eval in pawns.
This script cross-references those FENs against eco.json to recover
(fen_before, move_uci) pairs, converts evals to centipawns, and upserts
rows with source='jeffml-scores'.

Only played_eval is populated — best_move/best_eval/eval_delta are left NULL
since scores.json doesn't provide those.

Usage:
    python scripts/ingest_scores.py
    python scripts/ingest_scores.py --database-url sqlite:///analysis_cache.db
    python scripts/ingest_scores.py --dry-run -v
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.request
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ingest_scores")

import chess
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.models import AnalysisCache, Base

JEFF_ECO_COMMIT = "f398993004c7a84701e24691573af3c9bd196ffd"
SCORES_URL = f"https://raw.githubusercontent.com/JeffML/eco.json/{JEFF_ECO_COMMIT}/scores.json"

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/ghostreplay"
DEFAULT_ECO_PATH = PROJECT_ROOT / "public" / "data" / "openings" / "eco.json"
BATCH_SIZE = 100
SOURCE = "jeffml-scores"


def download_scores(url: str) -> dict[str, float]:
    """Download scores.json and return FEN → eval (pawns) mapping."""
    log.info("Downloading scores.json from %s", url)
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read().decode())
    log.info("Downloaded %d scored positions", len(data))
    return data


def fen_to_epd(fen: str) -> str:
    """Strip halfmove and fullmove counters from a FEN to get EPD-like key."""
    return " ".join(fen.split()[:4])


def build_fen_to_move_map(eco_path: Path) -> dict[str, list[tuple[str, str, str]]]:
    """Walk eco.json and build a map from resulting-position-EPD to (fen_before, move_uci, move_san).

    Multiple eco entries may reach the same position via different move orders,
    so each EPD maps to a list (though we only need one).
    """
    with open(eco_path) as f:
        data = json.load(f)

    # Map: EPD-after-move → [(fen_before, move_uci, move_san), ...]
    result: dict[str, list[tuple[str, str, str]]] = {}

    for entry in data["entries"]:
        uci_moves = entry["uci"].split()
        board = chess.Board()

        for uci_str in uci_moves:
            fen_before = board.fen()
            move = chess.Move.from_uci(uci_str)
            san = board.san(move)
            board.push(move)

            epd_after = fen_to_epd(board.fen())
            if epd_after not in result:
                result[epd_after] = []
                result[epd_after].append((fen_before, uci_str, san))

    return result


def match_scores(
    scores: dict[str, float],
    fen_map: dict[str, list[tuple[str, str, str]]],
) -> list[dict]:
    """Cross-reference scores with eco positions. Returns upsert-ready rows."""
    rows = []
    matched = 0
    unmatched = 0
    seen: set[tuple[str, str]] = set()

    for score_fen, eval_pawns in scores.items():
        epd = fen_to_epd(score_fen)
        entries = fen_map.get(epd)

        if not entries:
            unmatched += 1
            continue

        matched += 1
        # Convert pawns to centipawns (white-relative, scores.json is already white-relative)
        cp = round(eval_pawns * 100)

        for fen_before, move_uci, move_san in entries:
            key = (fen_before, move_uci)
            if key in seen:
                continue
            seen.add(key)

            rows.append({
                "fen_before": fen_before,
                "move_uci": move_uci,
                "move_san": move_san,
                "best_move_uci": None,
                "best_move_san": None,
                "played_eval": cp,
                "best_eval": None,
                "eval_delta": None,
                "source": SOURCE,
            })

    log.info("Matched %d / %d scored positions (%d unmatched)", matched, len(scores), unmatched)
    log.info("Generated %d unique (fen_before, move_uci) rows", len(rows))
    return rows


def upsert_rows(db: Session, rows: list[dict]) -> int:
    """Upsert rows into analysis_cache. Returns number of rows affected."""
    if not rows:
        return 0

    dialect_name = db.bind.dialect.name if db.bind else ""

    if dialect_name == "sqlite":
        make_insert = sqlite_insert
    elif dialect_name == "postgresql":
        make_insert = postgresql_insert
    else:
        # Generic fallback — only insert new rows, skip existing
        for val in rows:
            existing = db.query(AnalysisCache).filter(
                AnalysisCache.fen_before == val["fen_before"],
                AnalysisCache.move_uci == val["move_uci"],
            ).first()
            if not existing:
                db.add(AnalysisCache(**val))
        db.commit()
        return len(rows)

    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        stmt = make_insert(AnalysisCache).values(batch)
        # Skip rows that already exist from richer sources (precomputed/game)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[AnalysisCache.fen_before, AnalysisCache.move_uci],
        )
        db.execute(stmt)
        total += len(batch)

    db.commit()
    return total


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest JeffML scores.json into analysis_cache."
    )
    parser.add_argument(
        "--database-url",
        default=DEFAULT_DATABASE_URL,
        help=f"SQLAlchemy database URL (default: {DEFAULT_DATABASE_URL})",
    )
    parser.add_argument(
        "--eco-path",
        type=Path,
        default=DEFAULT_ECO_PATH,
        help=f"Path to eco.json (default: {DEFAULT_ECO_PATH})",
    )
    parser.add_argument(
        "--scores-url",
        default=SCORES_URL,
        help=f"URL to scores.json (default: pinned JeffML commit)",
    )
    parser.add_argument(
        "--scores-path",
        type=Path,
        default=None,
        help="Path to local scores.json (skips download if provided)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Match positions without writing to DB.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging.",
    )
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    # Load scores
    if args.scores_path:
        log.info("Loading scores from %s", args.scores_path)
        with open(args.scores_path) as f:
            scores = json.load(f)
        log.info("Loaded %d scored positions", len(scores))
    else:
        scores = download_scores(args.scores_url)

    # Build FEN→move map from eco.json
    log.info("Loading eco.json from %s", args.eco_path)
    fen_map = build_fen_to_move_map(args.eco_path)
    log.info("Built map with %d unique resulting positions", len(fen_map))

    # Match and build rows
    rows = match_scores(scores, fen_map)

    if args.dry_run:
        log.info("Dry run — skipping database writes.")
        return

    if not rows:
        log.info("No rows to write.")
        return

    # Write to DB
    start = time.time()
    engine = create_engine(args.database_url)
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        count = upsert_rows(db, rows)

    elapsed = time.time() - start
    log.info("Upserted %d rows in %.1fs", count, elapsed)


if __name__ == "__main__":
    main()
