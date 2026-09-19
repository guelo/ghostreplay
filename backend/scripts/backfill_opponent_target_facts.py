"""Backfill compact targeting facts and verify coverage before reader handoff.

Default: read-only aggregate verification. --apply commits the grouped-MAX
backfill, then verifies in a fresh repeatable-read, read-only snapshot. This
PostgreSQL operator tool never changes the reader setting or initializes deadlines;
--apply is refused once any retention deadline has been initialized.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import and_, func, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import OpponentDecision, OpponentTargetFact  # noqa: E402
from app.opponent_target_facts import backfill_target_facts  # noqa: E402
from app.srs_opportunity import targeted_counters_query  # noqa: E402


@dataclass(frozen=True)
class CoverageReport:
    checked_at: datetime
    decision_pairs: int
    fact_pairs: int
    missing_pairs: int
    timestamp_mismatches: int
    extra_pairs: int
    counter_mismatches: int

    @property
    def matches(self) -> bool:
        return not (self.missing_pairs or self.timestamp_mismatches
                    or self.extra_pairs or self.counter_mismatches)


def compare_target_facts(db: Session, *, now: datetime) -> CoverageReport:
    """Aggregate-only report; caller must hold one consistent snapshot.

    Exact full-history MAX equality is deliberately stronger than current counter
    equality. This is a pre-pruning coverage check, not a post-pruning oracle.
    """
    original = select(
        OpponentDecision.session_id.label("session_id"),
        OpponentDecision.target_blunder_id.label("blunder_id"),
        func.max(OpponentDecision.served_at).label("last_served_at"),
    ).where(OpponentDecision.target_blunder_id.is_not(None)).group_by(
        OpponentDecision.session_id, OpponentDecision.target_blunder_id,
    ).subquery()
    fact = OpponentTargetFact.__table__
    same_pair = and_(original.c.session_id == fact.c.session_id,
                     original.c.blunder_id == fact.c.blunder_id)
    missing = db.scalar(select(func.count()).select_from(
        original.outerjoin(fact, same_pair),
    ).where(fact.c.session_id.is_(None)))
    mismatches = db.scalar(select(func.count()).select_from(
        original.join(fact, same_pair),
    ).where(original.c.last_served_at != fact.c.last_served_at))
    extra = db.scalar(select(func.count()).select_from(
        fact.outerjoin(original, same_pair),
    ).where(original.c.session_id.is_(None)))
    counters = []
    for source in ("decisions", "facts"):
        counters.append({
            row.blunder_id: (int(row.targeted_30d), int(row.targeted_reached_30d))
            for row in targeted_counters_query(
                db, cutoff=now - timedelta(days=30), source=source,
            )
        })
    raw, compact = counters
    return CoverageReport(
        checked_at=now,
        decision_pairs=db.scalar(select(func.count()).select_from(original)),
        fact_pairs=db.scalar(select(func.count()).select_from(fact)),
        missing_pairs=missing, timestamp_mismatches=mismatches, extra_pairs=extra,
        counter_mismatches=sum(raw.get(key) != compact.get(key) for key in raw.keys() | compact.keys()),
    )


def run(engine, *, apply: bool = False) -> tuple[int, CoverageReport]:
    if engine.dialect.name != "postgresql":
        raise ValueError("Target fact backfill verification requires PostgreSQL")
    changed = 0
    if apply:
        with Session(engine) as db, db.begin():
            changed = backfill_target_facts(db)
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="REPEATABLE READ")
        with conn.begin():
            conn.execute(text("SET TRANSACTION READ ONLY"))
            now = conn.scalar(select(func.clock_timestamp()))
            with Session(bind=conn) as db:
                report = compare_target_facts(db, now=now)
    return changed, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit MAX backfill before checking")
    args = parser.parse_args()
    from app.db import engine

    try:
        changed, report = run(engine, apply=args.apply)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({
        **asdict(report), "checked_at": report.checked_at.isoformat(),
        "changed_pairs": changed, "matches": report.matches,
    }, sort_keys=True))
    return 0 if report.matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
