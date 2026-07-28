"""Seed one or both Release B populations on a prepared Phase 3 copy, through the
HARNESS's own synthesis — never a hand-written UPDATE.

`size_accuracy_backfill.synthesize_stale` / `synthesize_repair` ARE the definitions
of "stale" and "repair" that every artifact under `docs/sizing/` was measured
against, and `analyze_after_synthesis` is mandatory rather than tidy (runbook §1,
trap #1: stale planner statistics turn `REPAIR_POPULATE_SQL` from 155 ms into
7 minutes by picking a nested loop). Seeding by hand here would measure a different
population and a different plan than the constants were frozen from.

This REWRITES rows, so it carries the same fence as `size_accuracy_backfill.py`:
`--confirm-mutates` is required and the target must look like a disposable Phase 3
copy (`scripts/phase3_fixture_guard.py`).

    python scripts/phase3_seed_populations.py DB --repair 1000 --confirm-mutates
    python scripts/phase3_seed_populations.py DB --repair 1000 --no-stale --confirm-mutates
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine  # noqa: E402

from scripts import size_accuracy_backfill as harness  # noqa: E402
from scripts.phase3_fixture_guard import confirm_mutates  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("database")
    ap.add_argument("--repair", type=int, default=0, metavar="K")
    ap.add_argument("--no-stale", action="store_true")
    ap.add_argument("--url", default=None)
    ap.add_argument("--confirm-mutates", action="store_true")
    args = ap.parse_args()

    url = args.url or f"postgresql+psycopg://localhost:5432/{args.database}"
    engine = create_engine(url)
    mod = harness.mod

    with engine.connect() as conn:
        # The resolved name, not the typed one: with `--url` the two can differ
        # while both pass the fence, and the record would then name a database this
        # never seeded. `expect=` refuses the divergence rather than papering it.
        database = confirm_mutates(
            conn,
            confirmed=args.confirm_mutates,
            expect=args.database,
            what="this script REWRITES both accuracy populations on the target",
        )

    out: dict = {"database": database}
    with engine.begin() as conn:
        if not args.no_stale:
            out["stale_rows_stamped"] = harness.synthesize_stale(conn)
        if args.repair:
            out["repair_synthesis"] = harness.synthesize_repair(conn, args.repair)
        harness.analyze_after_synthesis(conn)

    # Read BOTH populations back through the revision's own convergence scans.
    # `remaining_scan`, not `.scalar()`: these statements return (id, remaining)
    # rows, so `.scalar()` silently yields the first id — a UUID coerced to a
    # 36-digit integer that looks like a population count and is not one.
    with engine.connect() as conn:
        out["n_stale"] = mod.remaining_scan(conn, mod.BACKFILL_REMAINING_SQL)[0]
        out["n_repair"] = mod.remaining_scan(conn, mod.REPAIR_POPULATION_COUNT_SQL)[0]
        out["dimensions"] = harness.read_dimensions(conn)
    engine.dispose()

    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
