"""Trace actual SQL once, separately from the storage spike's timed cells.

SQLAlchemy's after_cursor_execute statement can be the unexpanded template for
insertmanyvalues. The before hook is the authority for transmitted SQL text;
neither hook's parameters nor the text itself are written to the report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.models import OpeningScoreBatch
from app.opening_cache import FreshnessSnapshot
from scripts.bench_opening_score_storage import (
    DATABASE_ENV,
    engine_for,
    guard_database,
    guard_url,
    make_adapter,
)
from scripts.opening_score_storage_workload import Candidate, capture


class ActualStatementTrace:
    def __init__(self, engine):
        self.engine = engine
        self.statements = {}
        event.listen(engine, "before_cursor_execute", self.before)

    def before(self, conn, cursor, statement, parameters, context, many):
        verb = statement.split()[0].upper()
        if verb not in {"INSERT", "UPDATE", "DELETE", "SELECT"}:
            return
        relation = re.search(
            r"\b(?:INTO|UPDATE|FROM)\s+([a-z_]+)", statement, re.IGNORECASE
        )
        normalized = re.sub(r"__\d+", "__N", statement)
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        shape = self.statements.setdefault(
            digest,
            {
                "verb": verb,
                "relation": relation.group(1) if relation else None,
                "sql_bytes": len(statement.encode()),
                "executemany": bool(many),
                "returning": "RETURNING" in statement.upper(),
                "calls": 0,
            },
        )
        shape["calls"] += 1

    def close(self):
        event.remove(self.engine, "before_cursor_execute", self.before)


def load_candidates(engine):
    result = []
    with Session(engine) as db:
        batches = db.scalars(
            select(OpeningScoreBatch)
            .order_by(OpeningScoreBatch.generation.desc())
            .limit(2)
        ).all()
        if len(batches) != 2:
            raise ValueError(
                "completed A reference must retain two synthetic candidates"
            )
        for batch in reversed(batches):
            payload = capture(db, batch)
            scope = payload.rows("scope")
            freshness = FreshnessSnapshot(
                batch.inputs_fingerprint,
                batch.evidence_seq,
                batch.cache_epoch,
                tuple(r["fen"] for r in scope if r["kind"] == "raw"),
                tuple(r["fen"] for r in scope if r["kind"] == "norm"),
                batch.scoped_shared_digest,
            )
            result.append(
                Candidate(
                    payload,
                    batch.computed_at,
                    freshness,
                    "decay_staleness",
                    "untimed_statement_probe",
                    "forced_control",
                )
            )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    benchmark = json.loads(args.benchmark.read_text())
    run_id = benchmark["run_id"]
    if (
        benchmark.get("synthetic_only") is not True
        or not re.fullmatch(r"[0-9a-f]{10}", run_id)
        or benchmark.get("status")
        not in {"awaiting_user_review", "qualification_failed"}
    ):
        raise ValueError("requires a completed synthetic benchmark report")
    winner = benchmark["cells"][benchmark["selected"]]
    if winner["layout"] not in {"B", "D"}:
        raise ValueError("selected layout must be B or D")
    url = guard_url(os.environ.get(DATABASE_ENV))
    admin = engine_for(url)
    try:
        guard_database(admin)
    finally:
        admin.dispose()
    source = engine_for(url, f"ss_{run_id}_a")
    try:
        candidates = load_candidates(source)
    finally:
        source.dispose()
    result = {
        "source_benchmark_run_id": run_id,
        "synthetic_only": True,
        "method": "before_cursor_execute; actual expanded SQL; untimed independent replay",
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cells": {},
    }
    probe_id = uuid.uuid4().hex[:10]
    for name, layout, fillfactor in [
        ("a", "A", 100),
        ("selected", winner["layout"], winner["fillfactor"]),
    ]:
        adapter = make_adapter(
            url,
            probe_id,
            "sql_" + name,
            layout,
            fillfactor,
            winner["cached"] if name == "selected" else False,
        )
        trace = ActualStatementTrace(adapter.engine)
        try:
            for candidate in candidates:
                adapter.publish(candidate)
                adapter.verify(candidate)
            result["cells"][name] = list(trace.statements.values())
        finally:
            trace.close()
            adapter.engine.dispose()
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(args.output), "exact_parity": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
