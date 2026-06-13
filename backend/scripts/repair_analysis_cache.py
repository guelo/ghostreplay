#!/usr/bin/env python3
"""Audit and repair the analysis_cache table (g-repair-drill-cache).

The canonical precompute (``precompute_openings.py``) REGENERATES authoritative
rows for opening positions. This companion tool handles the other half of the
repair: it identifies legacy / incomplete / contaminated rows the write guard
would reject today and invalidates them explicitly, so they can no longer feed
the eval-delta / win-chance fallbacks that drive drill pass/fail, regular-game
classification/recording, and SRS grading.

Deployment order (see the bead): write protection (g-guard-cache-writes) and the
canonical precompute must be deployed/run FIRST. Regenerating or invalidating
before write protection is active is unsafe. Its default mode is a
non-destructive audit.

Modes
-----
* default (audit / dry-run): classify every row, print per-category counts, and
  write a JSON report. No writes — and no schema DDL.
* ``--apply``: delete the rows selected for invalidation. Each candidate is
  RE-CLASSIFIED under a row lock inside the deletion transaction, so a row
  repaired in place by an overlapping precompute (same id, now canonical) is no
  longer eligible and is left alone. Streamed in bounded batches, so memory is
  O(batch) regardless of table size, and resumable — re-running after an
  interruption simply continues.
* ``--verify``: re-scan and exit non-zero if any invalidation-eligible row
  remains. Safe to run before and after ``--apply``.

Every run writes a sidecar report (default
``backend/repair_analysis_cache_report.json``) and logs per-category metrics.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("repair_analysis_cache")

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.analysis_cache_audit import (
    AuditReport,
    Category,
    classify_row,
    should_invalidate,
)
from app.analysis_cache_repo import _row_to_dict
from app.models import AnalysisCache

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/ghostreplay"
SCAN_BATCH = 1000
DELETE_BATCH = 500


def _is_postgres(engine) -> bool:
    return engine.dialect.name == "postgresql"


def audit(db: Session, *, include_legacy_null: bool) -> AuditReport:
    """Scan the whole table once and return memory-bounded per-category counts.

    Streams by ascending primary key (O(batch) memory) and retains only tallies,
    never per-row ids/keys. ``include_legacy_null`` only affects the derived
    invalidate count, not the categorization.
    """
    report = AuditReport()
    last_id = 0
    while True:
        rows = (
            db.query(AnalysisCache)
            .filter(AnalysisCache.id > last_id)
            .order_by(AnalysisCache.id)
            .limit(SCAN_BATCH)
            .all()
        )
        if not rows:
            break
        for row in rows:
            last_id = row.id
            report.record(classify_row(_row_to_dict(row)))
    return report


def apply_invalidation(engine, *, include_legacy_null: bool) -> int:
    """Stream the table and delete still-eligible rows under a row lock.

    Each batch is its own transaction. Candidate rows are locked
    (``FOR UPDATE`` on PostgreSQL) and RE-CLASSIFIED inside that transaction
    before deletion, so a row that an overlapping precompute repaired in place
    (writer mutates in place, preserving the id) is re-evaluated as canonical and
    skipped. Memory is O(batch); an interruption leaves a consistent table and
    the next run resumes from a fresh scan.
    """
    deleted = 0
    last_id = 0
    while True:
        with Session(engine) as db:
            query = (
                db.query(AnalysisCache)
                .filter(AnalysisCache.id > last_id)
                .order_by(AnalysisCache.id)
                .limit(DELETE_BATCH)
            )
            if _is_postgres(engine):
                query = query.with_for_update(of=AnalysisCache)
            rows = query.all()
            if not rows:
                break
            for row in rows:
                last_id = row.id
                category = classify_row(_row_to_dict(row))
                if should_invalidate(category, include_legacy_null=include_legacy_null):
                    db.delete(row)
                    deleted += 1
            db.commit()
        log.info("Invalidation progress: %d deleted (through id %d)", deleted, last_id)
    return deleted


def _write_report(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    log.info("Wrote report to %s", path)


def _log_metrics(report: AuditReport, *, include_legacy_null: bool) -> None:
    log.info("analysis_cache audit: %d rows total", report.total)
    for category in Category:
        log.info("  %-26s %d", category.value, report.counts.get(category.value, 0))
    log.info(
        "  -> selected for invalidation: %d",
        report.invalidate_count(include_legacy_null=include_legacy_null),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete the rows selected for invalidation (default is dry-run audit).",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Exit non-zero if any invalidation-eligible row remains.",
    )
    parser.add_argument(
        "--include-legacy-null",
        action="store_true",
        help="Also invalidate profile-less rows the guard rejects (null/unsatisfied contract).",
    )
    parser.add_argument(
        "--report-out",
        type=Path,
        default=BACKEND_ROOT / "repair_analysis_cache_report.json",
    )
    args = parser.parse_args()

    start = time.time()
    engine = create_engine(args.database_url)

    # Never create schema here: a dry-run audit must not require DDL privileges
    # and must not silently materialize an empty table when migrations are missing.
    if not inspect(engine).has_table(AnalysisCache.__tablename__):
        log.error(
            "Table %r does not exist. Run migrations first; this tool never "
            "creates schema.",
            AnalysisCache.__tablename__,
        )
        return 2

    with Session(engine) as db:
        report = audit(db, include_legacy_null=args.include_legacy_null)
    _log_metrics(report, include_legacy_null=args.include_legacy_null)

    mode = "apply" if args.apply else ("verify" if args.verify else "audit")
    payload = {
        "mode": mode,
        "include_legacy_null": args.include_legacy_null,
        **report.as_dict(include_legacy_null=args.include_legacy_null),
    }

    if args.apply:
        deleted = apply_invalidation(engine, include_legacy_null=args.include_legacy_null)
        with Session(engine) as db:
            post = audit(db, include_legacy_null=args.include_legacy_null)
        remaining = post.invalidate_count(include_legacy_null=args.include_legacy_null)
        payload["deleted"] = deleted
        payload["post_invalidate_remaining"] = remaining
        _write_report(args.report_out, payload)
        log.info("Done (apply) in %.1fs. Deleted %d, %d remaining.",
                 time.time() - start, deleted, remaining)
        return 0 if remaining == 0 else 1

    _write_report(args.report_out, payload)

    if args.verify:
        remaining = report.invalidate_count(include_legacy_null=args.include_legacy_null)
        log.info("Verify: %d invalidation-eligible rows remain.", remaining)
        return 0 if remaining == 0 else 1

    log.info("Done (audit) in %.1fs. Re-run with --apply to invalidate %d rows.",
             time.time() - start,
             report.invalidate_count(include_legacy_null=args.include_legacy_null))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
