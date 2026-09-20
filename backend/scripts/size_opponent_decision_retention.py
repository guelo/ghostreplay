"""Bounded synthetic sizing and plan evidence for opponent decision retention.

Not a benchmark framework and not a production measurement: it builds one
census-shaped synthetic population in a SCRATCH database, records relation /
index / TOAST bytes and the sweep's plans before and after pruning, and prints
one JSON report. The rollout child compares production's real census against it.

Refuses any database that holds application data at all, BEFORE ``--reset`` is
allowed to drop anything, and takes the URL as an explicit argument rather than
from the application environment, so it cannot be pointed at production by
inheriting a variable.

    python scripts/size_opponent_decision_retention.py \
        --database-url postgresql+psycopg://user@localhost:5433/ghostreplay_sizing
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import statistics
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, func, insert, inspect, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import opponent_cleanup as cleanup  # noqa: E402
from app.models import (  # noqa: E402
    Base, Blunder, GameSession, OpponentDecision, OpponentTargetFact, Position, User,
)

# The 2026-08-21 census shape recorded in g-retain-decisions: 607 sessions, 152
# normal and 455 drills, ~27.5 envelopes per normal session. Drills are longer
# and carry the bigger payloads, which is what the byte budget exists for.
NORMAL_SESSIONS = 152
DRILL_SESSIONS = 455
NORMAL_DECISIONS = 28
DRILL_DECISIONS = 12
# Sized so the whole population lands near the 10,657,792 bytes g-prod-db-growth
# measured for ~9k real rows on 2026-08-21, making these figures comparable to
# that census rather than to an invented workload.
NORMAL_PAYLOAD = 700
DRILL_PAYLOAD = 1400
# Two thirds already past R + D, the rest still live: a first-run backlog with a
# steady-state remainder, so both plans are exercised on one population.
EXPIRED_FRACTION = 2 / 3
TARGETS = 200

RELATIONS = ("opponent_decisions", "opponent_target_facts", "game_sessions")
# The single row a previous run of this harness leaves behind; see _scratch_guard.
SIZING_USERNAME = "sizing"


def _scratch_guard(engine) -> None:
    """Refuse any database that is not this harness's own scratch database.

    The ordering is the entire point. ``--reset`` drops the schema, so this runs
    BEFORE the drop: checked afterwards it would inspect a database the mistyped
    URL had already emptied, and pass. Every table the application owns must be
    empty, except for the one user row a previous run of this harness seeded.
    """
    inspector = inspect(engine)
    present = [t for t in Base.metadata.sorted_tables if inspector.has_table(t.name)]
    if not present:
        return
    occupied = []
    with Session(engine) as db:
        for table in present:
            rows = int(db.scalar(select(func.count()).select_from(table)))
            if not rows:
                continue
            if table.name == User.__tablename__ and rows == 1 and db.scalar(
                select(func.count()).select_from(User)
                .where(User.username == SIZING_USERNAME)
            ):
                continue  # A previous run of this harness, and nothing else.
            occupied.append(f"{table.name} ({rows} rows)")
    if occupied:
        raise SystemExit(
            "Refusing a database that holds application data; this harness only runs "
            f"against an empty scratch database: {', '.join(sorted(occupied))}"
        )


def _payload(size: int) -> str:
    """Incompressible filler.

    A repeated character would let pglz shrink a 2.6 KB payload to almost
    nothing and report a table several times smaller than production's, and
    nothing would ever reach TOAST. Real serialized responses are JSON with UUIDs
    and FENs in them: compressible, but not by two orders of magnitude.
    """
    filler = "".join(uuid.uuid4().hex for _ in range(size // 32 + 1))
    return filler[:size]


def _relation_bytes(db: Session) -> dict[str, dict[str, int]]:
    rows = db.execute(text("""
        SELECT c.relname,
               pg_table_size(c.oid) AS table_bytes,
               pg_indexes_size(c.oid) AS index_bytes,
               COALESCE(pg_total_relation_size(t.oid), 0) AS toast_bytes,
               COALESCE(s.n_live_tup, 0) AS live_rows
        FROM pg_class c
        LEFT JOIN pg_class t ON t.oid = c.reltoastrelid
        LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
        WHERE c.relname = ANY(:names)
    """), {"names": list(RELATIONS)}).all()
    sizes = {
        row.relname: {
            "table_bytes": int(row.table_bytes), "index_bytes": int(row.index_bytes),
            "toast_bytes": int(row.toast_bytes), "live_rows": int(row.live_rows),
        }
        for row in rows
    }
    # Allocated bytes barely move on DELETE: the space is reusable, not returned.
    # The live payload total is what actually shrinks, and what a later rewrite
    # would reclaim, so report both rather than claiming a volume saving.
    sizes["opponent_decisions"]["live_payload_bytes"] = int(db.scalar(
        select(func.coalesce(func.sum(func.octet_length(
            OpponentDecision.response_payload)), 0))
    ))
    return sizes


def _explain(db: Session, label: str, statement) -> dict[str, object]:
    compiled = statement.compile(db.get_bind(), compile_kwargs={"literal_binds": True})
    plan = db.execute(text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {compiled}")).scalar_one()
    node = plan[0]["Plan"]
    return {
        "query": label,
        "top_node": node["Node Type"],
        "execution_ms": round(plan[0]["Execution Time"], 3),
        "shared_hit_blocks": node.get("Shared Hit Blocks", 0) + node.get("Shared Read Blocks", 0),
        "relations": sorted({
            found for found in _relations_in(node)
        }),
        "text": "\n".join(_flatten(node)),
    }


def _relations_in(node) -> list[str]:
    found = [node["Relation Name"]] if "Relation Name" in node else []
    for child in node.get("Plans", []):
        found.extend(_relations_in(child))
    return found


def _flatten(node, depth: int = 0) -> list[str]:
    detail = node.get("Index Name") or node.get("Relation Name") or ""
    lines = [f"{'  ' * depth}{node['Node Type']}{f' [{detail}]' if detail else ''}"]
    for child in node.get("Plans", []):
        lines.extend(_flatten(child, depth + 1))
    return lines


def _seed(db: Session, *, now: datetime) -> None:
    db.add(User(id=1, username="sizing"))
    db.flush()
    # One position per blunder: (user_id, position_id) is unique on blunders.
    positions = [
        Position(user_id=1, fen_hash=f"sizing-{index}",
                 fen_raw="8/8/8/8/8/8/K7/4k3 w - - 0 1", active_color="white")
        for index in range(TARGETS)
    ]
    db.add_all(positions)
    db.flush()
    blunders = [
        Blunder(user_id=1, position_id=position.id, bad_move_san="bad", best_move_san="good",
                eval_loss_cp=200, created_at=now - timedelta(days=200))
        for position in positions
    ]
    db.add_all(blunders)
    db.flush()
    target_ids = [blunder.id for blunder in blunders]

    sessions, decisions, facts = [], [], []
    plan = [(NORMAL_SESSIONS, NORMAL_DECISIONS, NORMAL_PAYLOAD, "normal"),
            (DRILL_SESSIONS, DRILL_DECISIONS, DRILL_PAYLOAD, "drill")]
    index = 0
    for count, per_session, payload_size, mode in plan:
        for offset in range(count):
            index += 1
            expired = offset < int(count * EXPIRED_FRACTION)
            session_id = uuid.uuid4()
            started = now - timedelta(days=40 if expired else 1, minutes=index)
            sessions.append({
                "id": session_id, "user_id": 1, "started_at": started, "status": "completed",
                "engine_elo": 1500, "player_color": "white", "is_rated": mode == "normal",
                "session_mode": mode, "drill_state": None if mode == "normal" else "active",
                "opponent_decisions_expires_at": started + timedelta(days=7),
            })
            target = target_ids[index % TARGETS]
            for ply in range(per_session):
                decisions.append({
                    "decision_id": uuid.uuid4(), "session_id": session_id,
                    "request_fingerprint": uuid.uuid4().hex, "request_fen_hash": "hash",
                    "uci_history": "[]", "ply_before": ply,
                    "served_at": started + timedelta(seconds=ply),
                    "response_payload": _payload(payload_size),
                    "target_blunder_id": target if ply == 0 else None,
                    "resulting_fen": None, "reaches_drill_root": False,
                })
            facts.append({
                "session_id": session_id, "blunder_id": target,
                # Spread ages across the fact window so both sides of 30d + D exist.
                "last_served_at": now - timedelta(days=40 if expired else 1),
            })
    db.execute(insert(GameSession), sessions)
    db.execute(insert(OpponentDecision), decisions)
    db.execute(insert(OpponentTargetFact), facts)


def _hot_path_ms(factory, *, cutoff: datetime) -> dict[str, float]:
    """Median of the two reads cleanup must not slow down."""
    from app.srs_opportunity import targeted_counters_query

    with factory() as db:
        blunder_ids = db.scalars(select(Blunder.id).limit(25)).all()
        replay = db.execute(select(
            OpponentDecision.session_id, OpponentDecision.request_fingerprint,
        ).limit(1)).first()
        samples: dict[str, list[float]] = {"targeted_counters": [], "replay_lookup": []}
        for _ in range(25):
            started = time.perf_counter()
            targeted_counters_query(
                db, cutoff=cutoff, source="facts", blunder_ids=list(blunder_ids), user_id=1,
            ).all()
            samples["targeted_counters"].append((time.perf_counter() - started) * 1000)
            if replay is None:
                continue
            started = time.perf_counter()
            db.execute(select(OpponentDecision.response_payload).where(
                OpponentDecision.session_id == replay[0],
                OpponentDecision.request_fingerprint == replay[1],
            )).first()
            samples["replay_lookup"].append((time.perf_counter() - started) * 1000)
    return {
        name: round(statistics.median(values), 3)
        for name, values in samples.items() if values
    }


def run(url: str, *, reset: bool = False) -> dict[str, object]:
    engine = create_engine(url)
    if engine.dialect.name != "postgresql":
        raise SystemExit("Sizing requires PostgreSQL")
    _scratch_guard(engine)
    if reset:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine)  # noqa: E731
    now = datetime.now(timezone.utc)
    with factory() as db, db.begin():
        _seed(db, now=now)
    with factory() as db, db.begin():
        for relation in RELATIONS:
            db.execute(text(f"ANALYZE {relation}"))

    # EXPLAIN the statements the sweep runs, built by the sweep's own builders.
    # A hand-written lookalike here would record a plan for a query that does not
    # exist, which is worse than recording no plan at all.
    with factory() as db, db.begin():
        sample_session = db.scalar(
            select(OpponentDecision.session_id).order_by(OpponentDecision.session_id).limit(1)
        )
        plans = [
            _explain(db, "candidate_sessions", cleanup.candidate_sessions_query(db)),
            _explain(db, "envelope_batch", cleanup.envelope_batch_query(db, sample_session)),
            _explain(db, "fact_batch", cleanup.fact_batch_query(db)),
        ]
        before = _relation_bytes(db)
    hot_before = _hot_path_ms(factory, cutoff=now - cleanup.FACT_WINDOW)

    started = time.monotonic()
    report = cleanup.sweep(factory, apply=True, run_rows=1_000_000, run_seconds=3600)
    elapsed = time.monotonic() - started

    # Ordinary DELETE leaves reusable space behind; VACUUM is what makes the
    # "after" figures comparable. It cannot run inside a transaction block.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for relation in RELATIONS:
            conn.execute(text(f"VACUUM ANALYZE {relation}"))
    with factory() as db, db.begin():
        after = _relation_bytes(db)
    hot_after = _hot_path_ms(factory, cutoff=now - cleanup.FACT_WINDOW)

    return {
        "population": {
            "normal_sessions": NORMAL_SESSIONS, "drill_sessions": DRILL_SESSIONS,
            "normal_bytes_per_session": NORMAL_DECISIONS * NORMAL_PAYLOAD,
            "drill_bytes_per_session": DRILL_DECISIONS * DRILL_PAYLOAD,
        },
        "before": before, "after": after, "plans": plans,
        "sweep": {
            "envelopes_deleted": report.envelopes_deleted,
            "envelope_bytes_deleted": report.envelope_bytes_deleted,
            "facts_deleted": report.facts_deleted,
            "batches": report.batches, "seconds": round(elapsed, 3),
            "rows_per_second": round(report.envelopes_deleted / elapsed, 1) if elapsed else None,
            "lag_seconds": report.lag_seconds, "alerts": report.alerts,
        },
        "hot_path_median_ms": {"before": hot_before, "after": hot_after},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True, help="SCRATCH database, never production")
    parser.add_argument("--reset", action="store_true", help="Drop the scratch schema first")
    args = parser.parse_args()
    import os

    os.environ["OPPONENT_DECISION_RETENTION_ENABLED"] = "1"
    os.environ["OPPONENT_DECISION_RETENTION_SECONDS"] = "604800"
    os.environ["OPPONENT_TARGET_SOURCE"] = "facts"
    os.environ[cleanup.CLEANUP_ENABLED_ENV] = "1"
    os.environ[cleanup.CLEANUP_NOT_BEFORE_ENV] = "2000-01-01T00:00:00+00:00"
    print(json.dumps(run(args.database_url, reset=args.reset), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
