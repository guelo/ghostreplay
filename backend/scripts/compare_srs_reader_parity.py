"""Compare the SRS counter reader with the raw-SQL oracle, read-only.

A rollout check for g-srs-retain-rollout (step 5 of ROLLOUT_SRS_RETENTION.md).
Before anything is folded, the compact-aware reader
(``app.srs_opportunity.load_opportunity_counters``) and the unmodified raw
reader (``srs_raw_oracle.load_opportunity_counters``) must agree on all five
opportunity counters of every blunder. After a fold they are meant to differ,
because the oracle cannot see folded rows, so this refuses to compare once
anything is folded. It also refuses once freezing is on: the reader then
rejects excluding a frozen session, so the comparison set would change.

Everything runs in one transaction at one database timestamp. On PostgreSQL that
transaction is REPEATABLE READ and READ ONLY, with a 30-second statement
timeout. Each owner's blunders are compared in batches of 100. Each batch is
compared once with no exclusion, and once with each of the owner's ten most
recent sessions excluded (or all their sessions, if they have fewer than ten).

It prints aggregate counts only: no IDs, owners or counter values.
Exit codes: 0 when everything matches, 1 on any mismatch, 2 when it refuses
because something is folded.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, or_, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import (  # noqa: E402
    Blunder,
    BlunderOpportunitySummary,
    GameSession,
    OpportunityRetentionPolicy,
    UserOpportunityRetentionState,
)
from app.srs_opportunity import OpportunityCounters, load_opportunity_counters  # noqa: E402
from srs_raw_oracle import load_opportunity_counters as raw_counters  # noqa: E402

BATCH = 100
RECENT_SESSIONS = 10
COUNTERS = tuple(f.name for f in fields(OpportunityCounters))


@dataclass
class Baseline:
    summaries: int
    folded_summaries: int
    prefixes: int
    first_fold: bool
    freeze: bool

    @property
    def comparable(self) -> bool:
        return not (self.folded_summaries or self.prefixes or self.first_fold
                    or self.freeze)


@dataclass
class ParityReport:
    owners: int = 0
    blunders: int = 0
    session_exclusions: int = 0
    comparisons: int = 0
    mismatches: int = 0
    # Counter name -> how many comparisons disagreed on it. Counts only.
    by_counter: Counter = field(default_factory=Counter)


def no_fold_baseline(db: Session) -> Baseline:
    folded = or_(
        BlunderOpportunitySummary.folded_eligible_count != 0,
        BlunderOpportunitySummary.folded_opportunities_since_review != 0,
        BlunderOpportunitySummary.folded_reached_since_review != 0,
    )
    return Baseline(
        summaries=db.scalar(select(func.count()).select_from(BlunderOpportunitySummary)),
        folded_summaries=db.scalar(
            select(func.count()).select_from(BlunderOpportunitySummary).where(folded)
        ),
        prefixes=db.scalar(
            select(func.count()).select_from(UserOpportunityRetentionState).where(
                UserOpportunityRetentionState.folded_through_started_at.is_not(None)
            )
        ),
        first_fold=db.scalar(
            select(func.count()).select_from(OpportunityRetentionPolicy).where(
                OpportunityRetentionPolicy.first_fold_committed_at.is_not(None)
            )
        ) > 0,
        freeze=bool(db.scalar(select(OpportunityRetentionPolicy.freeze_enabled))),
    )


def compare(db: Session, *, now: datetime) -> ParityReport:
    report = ParityReport()
    owners = db.scalars(select(Blunder.user_id).distinct().order_by(Blunder.user_id)).all()
    for owner in owners:
        ids = db.scalars(
            select(Blunder.id).where(Blunder.user_id == owner).order_by(Blunder.id)
        ).all()
        sessions = db.scalars(
            select(GameSession.id)
            .where(GameSession.user_id == owner)
            .order_by(GameSession.started_at.desc(), GameSession.id)
            .limit(RECENT_SESSIONS)
        ).all()
        report.owners += 1
        report.blunders += len(ids)
        report.session_exclusions += len(sessions)
        for exclude in [None, *sessions]:
            for start in range(0, len(ids), BATCH):
                batch = list(ids[start:start + BATCH])
                actual = load_opportunity_counters(
                    db, batch, user_id=owner, now=now, exclude_session_id=exclude
                )
                expected = raw_counters(
                    db, batch, user_id=owner, now=now, exclude_session_id=exclude
                )
                for blunder_id in batch:
                    report.comparisons += 1
                    got = actual.get(blunder_id, OpportunityCounters())
                    want = expected.get(blunder_id, OpportunityCounters())
                    if got != want:
                        report.mismatches += 1
                        report.by_counter.update(
                            name for name in COUNTERS
                            if getattr(got, name) != getattr(want, name)
                        )
    return report


def run(db: Session, *, out=print) -> int:
    """Compare inside ``db``'s transaction, which must not have started yet."""
    postgres = db.get_bind().dialect.name == "postgresql"
    if postgres:
        # Must be the transaction's first statement. READ ONLY also makes any
        # accidental write by either reader an error instead of a change.
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout = '30s'"))
        now = db.scalar(select(func.now()))
    else:
        now = datetime.now(timezone.utc)
    try:
        baseline = no_fold_baseline(db)
        out(f"no-fold baseline: summaries={baseline.summaries} "
            f"folded_summaries={baseline.folded_summaries} "
            f"prefixes={baseline.prefixes} "
            f"first_fold={'set' if baseline.first_fold else 'never'} "
            f"freeze={'on' if baseline.freeze else 'off'}")
        if not baseline.comparable:
            out("refused: this comparison only runs before freezing and folding; "
                "after a fold the raw oracle cannot see the folded rows")
            return 2
        report = compare(db, now=now)
    finally:
        db.rollback()
    out(f"compared: owners={report.owners} blunders={report.blunders} "
        f"session_exclusions={report.session_exclusions} "
        f"comparisons={report.comparisons}")
    detail = " ".join(f"{name}={report.by_counter[name]}" for name in COUNTERS)
    out(f"mismatches={report.mismatches} ({detail})")
    return 1 if report.mismatches else 0


def main() -> int:
    from app.db import SessionLocal

    with SessionLocal() as db:
        return run(db)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
