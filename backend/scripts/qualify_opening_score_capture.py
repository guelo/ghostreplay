#!/usr/bin/env python3
"""§1.3 production-shape payload capture for g-score-store-qualify.

``build_timeline`` is synthetic by its own provenance string ("deterministic
synthetic sessions; real gate/overlay/scorer; no production frequencies",
``opening_score_storage_workload.py:527``), and census COUNTS cannot supply the
row mixes and changed-row fractions the acceptance criteria demand. Real-mix
profiles are captured from a restored clone instead.

This is the ONLY step that runs the real writer and mutates evidence on
production-derived data, so it carries its own guard, its own provenance
sentinel and its own tests. Everything it writes stays in the private store;
only aggregates reach a report, ``docs/analysis`` or a bead.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from sqlalchemy import text

from scripts.qualify_opening_score_storage import (
    CAPTURE_DATABASE_ENV,
    CAPTURE_DATABASE_PATTERN,
    QualificationRefusal,
    SNAPSHOT_TEMPLATE,
    assert_cluster_identity,
    assert_resolved_engine,
    bootstrap_database_url,
    assert_private_store,
    dump_capture,
    engine_for,
    guard_capture_url,
    load_capture,
    logical_rows,
    run_migrations,
    _admin_engine,
    _child_environment,
)

SENTINEL_FRESH = "fresh"
SENTINEL_USED = "used"

# Every table that references ``game_sessions``. Enumerated explicitly rather
# than left to a join, and asserted against the live metadata below so a new
# foreign key fails loudly here instead of quietly leaving evidence behind at a
# cutoff. Revealing a session moves its dependent evidence WITH it.
# The hide/reveal closure, PARENT-FIRST. A cutoff is defined over a session set
# AND everything that references it — but ``game_sessions`` is not the only
# parent in that closure. ``blunders.source_session_id`` makes every blunder of
# a hidden session hidden too, and six tables reference ``blunders``:
#
#   * ``blunder_reviews``, ``blunder_opportunity_events`` and
#     ``blunder_opportunity_summaries`` are ON DELETE CASCADE, so deleting
#     ``blunders`` before holding them destroys rows the reveal can never
#     restore — and ``blunder_opportunity_summaries`` has no session column at
#     all, so a session-only list never held it in the first place;
#   * ``opponent_decisions.target_blunder_id``, ``opponent_target_facts
#     .blunder_id`` and ``session_moves.target_blunder_id`` are NO ACTION, so a
#     still-populated referencing row makes the DELETE itself raise.
#
# Declared as (table, ((column, parent table, parent key, nullable), …)). HIDE
# walks it in REVERSE (children first); REVEAL walks it forward and sweeps to a
# fixpoint, because a drill session's move can reference a blunder belonging to
# a session that is still hidden.
HELD_TABLES = (
    ("game_sessions", ()),
    ("rating_history", (("game_session_id", "game_sessions", "id", False),)),
    ("opening_session_replay_cache", (("session_id", "game_sessions", "id", False),)),
    ("blunders", (("source_session_id", "game_sessions", "id", True),)),
    (
        "session_moves",
        (
            ("session_id", "game_sessions", "id", False),
            ("target_blunder_id", "blunders", "id", True),
        ),
    ),
    (
        "opponent_target_facts",
        (
            ("session_id", "game_sessions", "id", False),
            ("blunder_id", "blunders", "id", False),
        ),
    ),
    (
        "opponent_decisions",
        (
            ("session_id", "game_sessions", "id", False),
            ("target_blunder_id", "blunders", "id", True),
        ),
    ),
    (
        "blunder_reviews",
        (
            ("session_id", "game_sessions", "id", False),
            ("blunder_id", "blunders", "id", False),
        ),
    ),
    (
        "blunder_opportunity_events",
        (
            ("session_id", "game_sessions", "id", False),
            ("blunder_id", "blunders", "id", False),
        ),
    ),
    (
        "blunder_opportunity_summaries",
        (("blunder_id", "blunders", "id", False),),
    ),
)

# The parents whose rows a cutoff hides. Anything referencing one of these is in
# the closure; ``assert_dependents_complete`` asserts that against live metadata.
HELD_PARENTS = ("game_sessions", "blunders")


def assert_dependents_complete() -> None:
    """Every foreign key into a HELD PARENT must be declared, or a cutoff leaks.

    Asserted against ``Base.metadata`` on every run rather than reviewed once:
    the day a new table references ``game_sessions`` or ``blunders``, this
    refuses here instead of silently leaving that table's evidence visible at a
    cutoff — which would make the scorer read evidence for a session the cutoff
    says does not exist yet.
    """
    from app.models import Base

    parents = {Base.metadata.tables[name] for name in HELD_PARENTS}
    live = {
        (table.name, fk.parent.name, fk.column.table.name)
        for table in Base.metadata.sorted_tables
        for fk in table.foreign_keys
        if fk.column.table in parents
    }
    declared = {
        (table, column, parent)
        for table, columns in HELD_TABLES
        for column, parent, _key, _nullable in columns
    }
    if live != declared:
        raise QualificationRefusal(
            "the hide/reveal closure is stale; a cutoff would leave evidence "
            f"behind: missing={sorted(live - declared)} "
            f"stale={sorted(declared - live)}"
        )
    held = {table for table, _ in HELD_TABLES}
    missing_parents = set(HELD_PARENTS) - held
    if missing_parents:
        raise QualificationRefusal(
            f"held parents {sorted(missing_parents)} are not themselves held"
        )


# --------------------------------------------------------------------------
# Clone, sentinel and guard
# --------------------------------------------------------------------------


def clone_snapshot(run_id: str) -> str:
    """Create the clone HERE and stamp it, because PostgreSQL forgets templates.

    An earlier revision said "refuse if this is not a fresh clone of
    ``gr_snap_base``", which is unimplementable: PostgreSQL records no template
    lineage for a database. So the tool creates the clone itself and stamps a
    ``COMMENT ON DATABASE`` sentinel carrying the run id, the template and a
    state. Capture never reuses a database.
    """
    name = f"gr_score_capture_{run_id}"
    if not re.fullmatch(CAPTURE_DATABASE_PATTERN, name):
        raise QualificationRefusal(f"{name!r} is not a gr_score_capture_* database")
    engine, url = _admin_engine()
    try:
        with engine.connect() as conn:
            conn.execute(
                text(f'CREATE DATABASE "{name}" TEMPLATE "{SNAPSHOT_TEMPLATE}"')
            )
            conn.execute(
                text(
                    f"COMMENT ON DATABASE \"{name}\" IS "
                    f"'{run_id}|{SNAPSHOT_TEMPLATE}|{SENTINEL_FRESH}'"
                )
            )
    finally:
        engine.dispose()
    return url.set(database=name).render_as_string(hide_password=False)


def parse_sentinel(comment: str | None) -> tuple[str, str, str]:
    """``<run id>|<template>|<state>``, or a refusal. Pure, so §2.6 can pin it."""
    if not comment:
        raise QualificationRefusal(
            "capture database carries no provenance sentinel; it was not created "
            "by this tool and may not be a fresh clone"
        )
    parts = comment.split("|")
    if len(parts) != 3:
        raise QualificationRefusal(f"malformed provenance sentinel {comment!r}")
    return parts[0], parts[1], parts[2]


def assert_sentinel(parts: tuple[str, str, str], run_id: str) -> None:
    stamped_run, template, state = parts
    if stamped_run != run_id:
        raise QualificationRefusal(
            f"capture database belongs to run {stamped_run!r}, not {run_id!r}"
        )
    if template != SNAPSHOT_TEMPLATE:
        raise QualificationRefusal(
            f"clone template was {template!r}, not {SNAPSHOT_TEMPLATE!r}"
        )
    if state != SENTINEL_FRESH:
        raise QualificationRefusal(
            f"capture database state is {state!r}; capture never reuses a database"
        )


def read_sentinel(engine) -> tuple[str, str, str]:
    with engine.connect() as conn:
        comment = conn.execute(
            text(
                "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                "WHERE datname = current_database()"
            )
        ).scalar_one_or_none()
    return parse_sentinel(comment)


def assert_capture_guard(engine, run_id: str) -> dict:
    """The §1.3 guard — a DIFFERENT shape from the measurement guard's.

    The capture database is a POPULATED clone of the production snapshot, so it
    violates the measurement guard's empty-on-entry rule by construction; its
    name pattern differs for exactly that reason.
    """
    with engine.connect() as conn:
        identity = assert_cluster_identity(
            conn, expected_database_pattern=CAPTURE_DATABASE_PATTERN
        )
    parts = read_sentinel(engine)
    assert_sentinel(parts, run_id)
    return {**identity, "sentinel": "|".join(parts)}


def mark_used(engine, run_id: str) -> None:
    """Flip the sentinel as soon as the first cutoff publishes, not at the end."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        name = conn.execute(text("SELECT current_database()")).scalar_one()
        conn.execute(
            text(
                f"COMMENT ON DATABASE \"{name}\" IS "
                f"'{run_id}|{SNAPSHOT_TEMPLATE}|{SENTINEL_USED}'"
            )
        )


# --------------------------------------------------------------------------
# Cutoff construction — hide, then reveal, with dependents moving together
# --------------------------------------------------------------------------

HOLD_PREFIX = "gr_capture_hold_"


def select_cutoff_sessions(db, user_id: int, color: str, cutoffs: int) -> list:
    """The newest ``cutoffs`` sessions, oldest first: cutoff k reveals the k-th.

    The pinned clock is the session's END, falling back to its start. §1.3 says
    "that cutoff session's real timestamp", and the point of the rule is that
    the scoring clock is at or after the evidence being scored — a session's
    moves happen between ``started_at`` and ``ended_at``, so pinning to the
    start puts the clock marginally BEFORE its own last moves. The calculator
    clamps ``days_since_last_touch`` at zero (``opening_rootcalc.py:1271``), so
    the numeric error is nil either way, but the invariant should hold as
    stated rather than by a clamp.
    """
    rows = db.execute(
        text(
            "SELECT id, COALESCE(ended_at, started_at), started_at "
            "FROM game_sessions "
            "WHERE user_id = :user_id AND player_color = :color "
            "ORDER BY started_at DESC, id DESC LIMIT :limit"
        ),
        {"user_id": user_id, "color": color, "limit": cutoffs},
    ).all()
    return [(row[0], row[1]) for row in reversed(rows)]


def _hidden_blunders_sql() -> str:
    return "SELECT id FROM blunders WHERE source_session_id IN :ids"


def _session_ids_bindparam():
    """``:ids`` typed FROM THE MODEL, and expanding rather than ``= ANY``.

    Two reasons, one of them load-bearing on PostgreSQL too. ``text()`` carries
    no type information, so a ``uuid.UUID`` reached the driver raw; and
    ``= ANY(:ids)`` is PostgreSQL-only, which left the one step that mutates
    production-derived data with no executable test anywhere. An expanding
    ``IN`` renders the same plan on PostgreSQL and also runs on SQLite, so the
    hide/reveal closure can be exercised end to end before it is pointed at a
    restored snapshot.
    """
    from sqlalchemy import bindparam

    from app.models import Base

    return bindparam(
        "ids", expanding=True, type_=Base.metadata.tables["game_sessions"].c.id.type
    )


def _session_id_bindparam():
    from sqlalchemy import bindparam

    from app.models import Base

    return bindparam("id", type_=Base.metadata.tables["game_sessions"].c.id.type)


def _hide_predicate(columns) -> str:
    """A row is hidden when ANY of its foreign keys points at a hidden parent."""
    clauses = []
    for column, parent, _key, _nullable in columns:
        if parent == "game_sessions":
            clauses.append(f"{column} IN :ids")
        else:
            clauses.append(f"{column} IN ({_hidden_blunders_sql()})")
    return " OR ".join(clauses)


def hide_sessions(engine, session_ids: list[uuid.UUID]) -> None:
    """Move the sessions and their WHOLE dependent closure into holding tables.

    Children first, so no CASCADE destroys a row before its holding table exists
    and no NO ACTION reference makes the DELETE raise. The hidden-blunder
    subquery is evaluated while ``blunders`` is still populated, which the
    children-first order guarantees.
    """
    assert_dependents_complete()
    ids = _session_ids_bindparam()
    with engine.begin() as conn:
        for table, columns in reversed(HELD_TABLES):
            hold = f"{HOLD_PREFIX}{table}"
            where = _hide_predicate(columns) if columns else "id IN :ids"
            conn.execute(
                text(
                    f"CREATE TABLE {hold} AS SELECT * FROM {table} WHERE {where}"
                ).bindparams(ids),
                {"ids": session_ids},
            )
            conn.execute(
                text(f"DELETE FROM {table} WHERE {where}").bindparams(ids),
                {"ids": session_ids},
            )


def _restorable_predicate(columns, alias: str) -> str:
    """Restorable = every non-null foreign key of the row has a LIVE parent.

    This subsumes "belongs to a revealed session": a row whose session is still
    hidden has a dead parent and stays held. It is also what makes the sweep
    safe for a drill session's move that targets a blunder from an older
    session — the move waits in the hold table until that blunder is back.
    """
    if not columns:
        return "1 = 1"
    clauses = []
    for column, parent, key, nullable in columns:
        live = f"EXISTS (SELECT 1 FROM {parent} p WHERE p.{key} = {alias}.{column})"
        clauses.append(
            f"({alias}.{column} IS NULL OR {live})" if nullable else f"({live})"
        )
    return " AND ".join(clauses)


def reveal_session(engine, session_id: uuid.UUID) -> dict:
    """Restore one session, then SWEEP the hold tables to a fixpoint.

    A single parent-first pass is not enough. Restoring session X can make rows
    of an ALREADY-revealed session restorable — a move of an earlier session
    that targets a blunder of X, for instance — so the sweep repeats until a
    pass moves nothing. It terminates: every pass either moves at least one row
    out of a finite hold set or ends the loop.
    """
    moved_total: dict[str, int] = {}
    one = _session_id_bindparam()
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO game_sessions SELECT * FROM "
                f"{HOLD_PREFIX}game_sessions WHERE id = :id"
            ).bindparams(one),
            {"id": session_id},
        )
        conn.execute(
            text(f"DELETE FROM {HOLD_PREFIX}game_sessions WHERE id = :id").bindparams(
                one
            ),
            {"id": session_id},
        )
        moved_total["game_sessions"] = 1
        while True:
            moved = 0
            for table, columns in HELD_TABLES:
                if table == "game_sessions":
                    continue
                hold = f"{HOLD_PREFIX}{table}"
                # INSERT then DELETE under the SAME predicate, rather than a
                # data-modifying CTE. It is equivalent HERE because a hold
                # table's restorable predicate reads only its PARENT tables and
                # never the table being inserted into, so step one cannot make
                # a row restorable that step two would then delete unmoved —
                # and the equality of the two counts asserts exactly that,
                # instead of leaving it as an argument.
                selected = conn.execute(
                    text(f"SELECT count(*) FROM {hold} WHERE {_restorable_predicate(columns, hold)}")
                ).scalar_one()
                if not selected:
                    continue
                conn.execute(
                    text(
                        f"INSERT INTO {table} SELECT * FROM {hold} "
                        f"WHERE {_restorable_predicate(columns, hold)}"
                    )
                )
                deleted = conn.execute(
                    text(
                        f"DELETE FROM {hold} "
                        f"WHERE {_restorable_predicate(columns, hold)}"
                    )
                ).rowcount
                if deleted != selected:
                    raise QualificationRefusal(
                        f"{hold}: {selected} rows were restorable but {deleted} "
                        "were removed from the hold table; the reveal sweep is "
                        "not self-consistent and evidence would be lost"
                    )
                moved += deleted
                moved_total[table] = moved_total.get(table, 0) + deleted
            if not moved:
                break
    return moved_total


def assert_holding_tables_empty(engine) -> None:
    """After the LAST cutoff every held row must be back, or evidence was lost."""
    remaining = {}
    with engine.connect() as conn:
        for table, _ in HELD_TABLES:
            count = conn.execute(
                text(f"SELECT count(*) FROM {HOLD_PREFIX}{table}")
            ).scalar_one()
            if count:
                remaining[table] = count
    if remaining:
        raise QualificationRefusal(
            "the final cutoff did not restore every held row, so the capture's "
            f"last payload is not the full evidence set: {remaining}"
        )


def drop_holding_tables(engine) -> None:
    with engine.begin() as conn:
        for table, _ in HELD_TABLES:
            conn.execute(text(f"DROP TABLE IF EXISTS {HOLD_PREFIX}{table}"))


# --------------------------------------------------------------------------
# The capture itself
# --------------------------------------------------------------------------


def capture_pair(engine, user_id: int, color: str, cutoffs: int, run_id: str) -> dict:
    """Score each cutoff AT THAT CUTOFF SESSION'S REAL TIMESTAMP.

    The scorer takes ``computed_at`` as its scoring time (``_build_cached_scores
    (color, graph, overlay, roots, computed_at, routing_snapshot, …)``,
    ``opening_cache.py:308-334``), so confidence and decay are FIXED AT CAPTURE
    TIME and replay cannot change them. Capturing N cutoffs back to back on the
    wall clock would score them all within minutes of each other, understating
    both the changed-row fraction and B50's exact-diff WAL, and thereby making
    the ≤ 0.5 × A gate easier to pass. Pinning the clock per cutoff also
    reproduces the real gaps between sessions instead of a compressed run.
    """
    from unittest.mock import patch

    from sqlalchemy.orm import Session

    from app import opening_cache as oc
    from app.opening_evidence import reset_session_evidence_cache
    from scripts.opening_score_storage_workload import score_payload

    clock = {"now": None}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"] if tz else clock["now"].replace(tzinfo=None)

    measured: dict = {}

    def record_build(color_, graph, overlay, roots, computed_at, routing_snapshot, **kw):
        scores, positions = real_build(
            color_, graph, overlay, roots, computed_at, routing_snapshot, **kw
        )
        measured["objects"] = (scores, positions, overlay)
        return scores, positions

    real_build = oc._build_cached_scores
    candidates, provenance = [], []
    with Session(engine) as db:
        revealed = select_cutoff_sessions(db, user_id, color, cutoffs)
    if len(revealed) < 2:
        raise QualificationRefusal(
            f"pair has {len(revealed)} cutoff sessions; at least two are required"
        )
    hidden = [session_id for session_id, _ in revealed[1:]]
    hide_sessions(engine, hidden)
    marked = False
    restored: list[dict] = []
    try:
        for index, (session_id, started_at) in enumerate(revealed):
            if index:
                restored.append(reveal_session(engine, session_id))
            clock["now"] = started_at
            with (
                Session(engine) as db,
                patch.object(oc, "datetime", Clock),
                patch.object(oc, "_utcnow", side_effect=lambda: clock["now"]),
                patch.object(oc, "_build_cached_scores", side_effect=record_build),
            ):
                # Exactly the reset build_timeline performs. Without it the
                # second cutoff scores stale evidence.
                oc.bump_evidence_seq(db, user_id, color)
                db.commit()
                reset_session_evidence_cache()
                batch = oc.recompute_opening_scores(
                    db, user_id, color, computed_at=started_at
                )
                freshness = oc.FreshnessSnapshot(
                    batch.inputs_fingerprint,
                    batch.evidence_seq,
                    batch.cache_epoch,
                    *_scope_fens(db, batch.id),
                    batch.scoped_shared_digest,
                )
                payload = score_payload(*measured["objects"], freshness)
                candidates.append(
                    _candidate(payload, started_at, freshness, index, len(revealed))
                )
                db.rollback()
            if not marked:
                mark_used(engine, run_id)
                marked = True
            provenance.append(
                {
                    "cutoff": index,
                    "scored_at": started_at,
                    "logical_rows": logical_rows(payload),
                    "positions": len(payload.positions),
                    "roots": len(payload.roots),
                    "edges": len(payload.edges),
                    "scope": len(payload.scope),
                }
            )
        # The last cutoff restores the whole set, which is what makes the
        # closure check a comparison against an untouched clone at all.
        assert_holding_tables_empty(engine)
    finally:
        oc._build_cached_scores = real_build
        drop_holding_tables(engine)
    return {
        "candidates": candidates,
        "cutoff_provenance": provenance,
        "rows_restored_per_cutoff": restored,
    }


def _scope_fens(db, batch_id: int) -> tuple[tuple, tuple]:
    rows = db.execute(
        text(
            "SELECT kind, fen FROM opening_score_batch_shared_scope "
            "WHERE batch_id = :batch ORDER BY kind, fen"
        ),
        {"batch": batch_id},
    ).all()
    return (
        tuple(fen for kind, fen in rows if kind == "raw"),
        tuple(fen for kind, fen in rows if kind == "norm"),
    )


def _candidate(payload, computed_at, freshness, index: int, total: int):
    from scripts.opening_score_storage_workload import Candidate

    return Candidate(
        payload,
        computed_at,
        freshness,
        reason="captured_cutoff",
        cause=f"session_reveal_{index + 1}_of_{total}",
        period="production_shape",
    )


# --------------------------------------------------------------------------
# Closure check, shared evidence and the composite-T request set
# --------------------------------------------------------------------------


def closure_check(admin_run_id: str, final_payload, output: Path) -> dict:
    """Prove the reveal mechanism did not perturb evidence — IN A CHILD.

    The last cutoff restores the whole session set, so its captured payload must
    equal a plain recompute on an UNTOUCHED clone at the SAME pinned clock. If it
    does not, hiding and revealing changed what the scorer saw and the capture is
    invalid — not "close enough".

    It CANNOT run in this process. ``run_migrations`` refuses unless
    ``DATABASE_URL`` equals the target it is migrating (that refusal is the
    §2.1 MIGRATIONS rule), and this process bootstrapped ``DATABASE_URL`` to the
    CAPTURE clone long ago; ``app.db`` is bound to it too (app/db.py:49). So the
    control clone gets its own process with a constructed environment, exactly
    as a cell does, and hands its payload back through the private store.
    """
    control_url = clone_snapshot(f"{admin_run_id}_control")
    control_path = assert_private_store(output).with_name(
        f"{Path(output).stem}.control.pickle"
    )
    request_path = _pair_request_path(output)
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.qualify_opening_score_capture",
            "control-recompute",
            "--run-id",
            f"{admin_run_id}_control",
            "--user-id-from-capture",
            str(request_path),
            "--output",
            str(control_path),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=_child_environment(control_url, env_name=CAPTURE_DATABASE_ENV),
        capture_output=True,
        text=True,
        check=False,
    )
    if child.returncode:
        return {
            "ran": False,
            "equal": None,
            "recorded_gap": True,
            "reason": child.stderr.strip()[-2000:],
            "note": (
                "the control recompute failed; the capture itself is already "
                "written and the closure check is recorded as a GAP, never as a "
                "pass"
            ),
        }
    control = load_capture(control_path)
    equal = control["payload"] == final_payload["payload"]
    result = {
        "ran": True,
        "control_database": control["database"],
        "equal": equal,
        "logical_rows": logical_rows(control["payload"]),
        "pinned_clock_matches": control["computed_at"] == final_payload["computed_at"],
    }
    if not equal:
        raise QualificationRefusal(
            "closure check failed: the final cutoff's payload differs from a "
            "plain recompute on an untouched clone at the same pinned clock, so "
            "the reveal mechanism perturbed evidence and the capture is invalid"
        )
    return result


def control_recompute(args) -> int:
    """The child half of ``closure_check``: its OWN clone, its OWN bootstrap."""
    url = bootstrap_database_url(env_name=CAPTURE_DATABASE_ENV, guard=guard_capture_url)
    assert_resolved_engine(url)
    engine = engine_for(url)
    request = json.loads(Path(args.user_id_from_capture).read_text())
    user_id, color = request["user_id"], request["color"]
    computed_at = datetime.fromisoformat(request["computed_at"])
    try:
        assert_capture_guard(engine, args.run_id)
        run_migrations(url, expected_database_pattern=CAPTURE_DATABASE_PATTERN)
        mark_used(engine, args.run_id)
        payload = _plain_recompute(engine, user_id, color, computed_at)
    finally:
        engine.dispose()
    dump_capture(
        Path(args.output),
        {
            "payload": payload,
            "computed_at": computed_at,
            "database": url.database,
            "candidates": [],
            "color": color,
        },
    )
    print(json.dumps({"output": str(args.output), "database": url.database}))
    return 0


def _plain_recompute(engine, user_id: int, color: str, computed_at):
    """One recompute at the pinned clock, with nothing hidden and nothing held."""
    from unittest.mock import patch

    from sqlalchemy.orm import Session

    from app import opening_cache as oc
    from app.opening_evidence import reset_session_evidence_cache
    from scripts.opening_score_storage_workload import score_payload

    clock = {"now": computed_at}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"] if tz else clock["now"].replace(tzinfo=None)

    measured: dict = {}
    real_build = oc._build_cached_scores

    def record_build(color_, graph, overlay, roots, at, routing, **kw):
        scores, positions = real_build(color_, graph, overlay, roots, at, routing, **kw)
        measured["objects"] = (scores, positions, overlay)
        return scores, positions

    with (
        Session(engine) as db,
        patch.object(oc, "datetime", Clock),
        patch.object(oc, "_utcnow", side_effect=lambda: clock["now"]),
        patch.object(oc, "_build_cached_scores", side_effect=record_build),
    ):
        oc.bump_evidence_seq(db, user_id, color)
        db.commit()
        reset_session_evidence_cache()
        batch = oc.recompute_opening_scores(db, user_id, color, computed_at=computed_at)
        freshness = oc.FreshnessSnapshot(
            batch.inputs_fingerprint,
            batch.evidence_seq,
            batch.cache_epoch,
            *_scope_fens(db, batch.id),
            batch.scoped_shared_digest,
        )
        payload = score_payload(*measured["objects"], freshness)
        db.rollback()
    return payload


def capture_shared_evidence(engine) -> tuple[list[dict], list[dict]]:
    """BOTH global tables, IN FULL — there is no per-pair subset to take.

    ``shared_evidence_scope_versions`` is keyed ``(kind, fen)`` and
    ``shared_evidence_scope_invalidations`` by ``(kind)``, neither with an owner
    column (``models.py:1300-1334``). Copying only the FENs a pair's scope
    references would make every probe a hit against an index far smaller than
    production's — the opposite of what the read ceiling measures.
    """
    with engine.connect() as conn:
        versions = [
            dict(row._mapping)
            for row in conn.execute(text("SELECT * FROM shared_evidence_scope_versions"))
        ]
        invalidations = [
            dict(row._mapping)
            for row in conn.execute(
                text("SELECT * FROM shared_evidence_scope_invalidations")
            )
        ]
    return versions, invalidations


def reconstruct_tree_requests(payload, limit=24, max_depth=12) -> list[list]:
    """Walk the captured edges from the start position into legal lines.

    The composite-T request set is PINNED and identical for both layouts.
    Replica rows are unreachable from any legal line, so every T read addresses
    copy-0 rows whatever the scale factor.
    """
    import chess

    from app.fen import normalize_fen
    from scripts.opening_score_storage_workload import FIELDS

    parent_index = FIELDS["edges"].index("parent_fen")
    child_index = FIELDS["edges"].index("child_fen")
    uci_index = FIELDS["edges"].index("uci")
    traversal_index = FIELDS["edges"].index("traversal_count")
    children: dict[str, list[tuple[int, str, str]]] = {}
    for row in payload.edges:
        children.setdefault(row[parent_index], []).append(
            (row[traversal_index] or 0, row[uci_index], row[child_index])
        )
    for edges in children.values():
        edges.sort(reverse=True)

    requests: list[list] = []
    stack = [(chess.Board(), [])]
    while stack and len(requests) < limit:
        board, moves = stack.pop()
        options = children.get(normalize_fen(board.fen()), [])
        if moves:
            requests.append([list(moves), None])
        if len(moves) >= max_depth:
            continue
        for _count, uci, _child in options[:2]:
            try:
                move = chess.Move.from_uci(uci)
            except ValueError:
                continue
            if move not in board.legal_moves:
                continue
            next_board = board.copy()
            next_board.push(move)
            stack.append((next_board, moves + [uci]))
    if not requests:
        raise QualificationRefusal(
            "no legal line could be reconstructed from the captured edges"
        )
    return requests[:limit]


def read_inputs_for(payload, fens=32, parents=16) -> dict:
    """Bounded copy-0 keys for the direct composite, matching the shipped waves."""
    from scripts.opening_score_storage_workload import FIELDS

    position_key = FIELDS["positions"].index("normalized_fen")
    parent_key = FIELDS["edges"].index("parent_fen")
    return {
        "fens": [row[position_key] for row in payload.positions[:fens]],
        "parents": list(
            dict.fromkeys(row[parent_key] for row in payload.edges)
        )[:parents],
    }


# --------------------------------------------------------------------------
# Pair resolution — the production user id never reaches a command line
# --------------------------------------------------------------------------

PAIR_SHAPE_TOLERANCE = 0.20


def _census_pairs(engine) -> list[dict]:
    """Re-derive the census's anonymised pair ordering against this clone.

    Reproduces ``census.py`` exactly: ``DISTINCT ON (user_id, player_color)``
    ordered by ``user_id, player_color, generation DESC``, then a STABLE sort by
    descending logical rows. Index k here is therefore census ``pair_index`` k.
    """
    latest = text(
        "SELECT DISTINCT ON (user_id, player_color) "
        "id, user_id, player_color, generation, storage_format, computed_at "
        "FROM opening_score_batches "
        "ORDER BY user_id, player_color, generation DESC"
    )
    legacy = text(
        "SELECT (SELECT count(*) FROM user_opening_scores WHERE batch_id=:b),"
        "(SELECT count(*) FROM opening_position_scores WHERE batch_id=:b),"
        "(SELECT count(*) FROM opening_position_edges WHERE batch_id=:b),"
        "(SELECT count(*) FROM opening_score_batch_shared_scope WHERE batch_id=:b)"
    )
    current = text(
        "SELECT (SELECT count(*) FROM opening_current_roots WHERE user_id=:u AND player_color=:p),"
        "(SELECT count(*) FROM opening_current_positions WHERE user_id=:u AND player_color=:p),"
        "(SELECT count(*) FROM opening_current_edges WHERE user_id=:u AND player_color=:p),"
        "(SELECT count(*) FROM opening_current_scope WHERE user_id=:u AND player_color=:p)"
    )
    pairs = []
    with engine.connect() as conn:
        for batch_id, user_id, color, generation, fmt, at in conn.execute(latest).all():
            counts = conn.execute(
                legacy if fmt == "legacy" else current,
                {"b": batch_id} if fmt == "legacy" else {"u": user_id, "p": color},
            ).one()
            entry = dict(
                zip(("roots", "positions", "edges", "scope"), counts, strict=True)
            )
            entry["sessions"] = conn.execute(
                text("SELECT count(*) FROM game_sessions WHERE user_id = :u"),
                {"u": user_id},
            ).scalar_one()
            entry.update(
                user_id=user_id,
                player_color=color,
                generation=generation,
                storage_format=fmt,
                computed_at=str(at),
                logical_rows=sum(counts),
            )
            pairs.append(entry)
    pairs.sort(key=lambda d: -d["logical_rows"])
    return pairs


# Every key §1.3 and §0.4 read out of the §1.1 census. The census is produced
# by a separate run against production and consumed here and in
# ``_stated_differences``; until this existed, the agreement between the two
# was pinned only by hand-built test fixtures, so a census written with, say,
# ``colour`` or ``rows`` would have failed deep inside a capture instead of at
# its first line.
CENSUS_PAIR_FIELDS = (
    "pair_index",
    "player_color",
    "roots",
    "positions",
    "edges",
    "scope",
    "logical_rows",
    "sessions",
)
CENSUS_TOP_LEVEL_FIELDS = ("pairs", "settings", "database", "host")


def assert_census_shape(census: dict) -> None:
    """Refuse a census this bead cannot read, naming what is missing."""
    missing = [name for name in CENSUS_TOP_LEVEL_FIELDS if name not in census]
    if missing:
        raise QualificationRefusal(f"the census is missing {missing}")
    if not census["pairs"]:
        raise QualificationRefusal("the census holds no pairs")
    for position, entry in enumerate(census["pairs"]):
        absent = [name for name in CENSUS_PAIR_FIELDS if name not in entry]
        if absent:
            raise QualificationRefusal(
                f"census pair at position {position} is missing {absent}"
            )
    for name, value in census["settings"].items():
        if not isinstance(value, dict) or "setting" not in value:
            raise QualificationRefusal(
                f"census setting {name!r} is not a {{'setting': ...}} mapping, so "
                "the stated-differences list cannot be built from it"
            )


def resolve_pair(engine, census: dict, pair_index: int) -> dict:
    """Resolve the census index to a real ``(user_id, player_color)`` IN HERE.

    The user id is a CLI argument nowhere. Passing it on the command line puts
    a production identifier into shell history, ``ps`` output and every
    transcript of the run, and the anonymised census index exists precisely so
    it does not have to be.

    The resolved pair is CHECKED against the census entry, so a stale census or
    a snapshot taken long after it cannot silently select a different pair:
    colour must match exactly and the shape must be within
    ``PAIR_SHAPE_TOLERANCE``. Drift within tolerance is RECORDED, not corrected —
    a snapshot is taken after the census and the pair keeps playing.
    """
    assert_census_shape(census)
    declared = {entry["pair_index"]: entry for entry in census["pairs"]}
    if pair_index not in declared:
        raise QualificationRefusal(
            f"census has no pair_index {pair_index}; it holds "
            f"{min(declared)}..{max(declared)}"
        )
    expected = declared[pair_index]
    pairs = _census_pairs(engine)
    if pair_index >= len(pairs):
        raise QualificationRefusal(
            f"the clone holds {len(pairs)} pairs, so pair_index {pair_index} "
            "does not exist in it; the census and the snapshot disagree"
        )
    resolved = pairs[pair_index]
    if resolved["player_color"] != expected["player_color"]:
        raise QualificationRefusal(
            f"pair {pair_index} is {resolved['player_color']} in the snapshot but "
            f"{expected['player_color']} in the census; the ordering has moved "
            "and the size profiles must be re-decided"
        )
    drift = {
        field: {"census": expected[field], "snapshot": resolved[field]}
        for field in ("roots", "positions", "edges", "scope", "logical_rows", "sessions")
        if expected[field] != resolved[field]
    }
    base = expected["logical_rows"] or 1
    relative = abs(resolved["logical_rows"] - expected["logical_rows"]) / base
    if relative > PAIR_SHAPE_TOLERANCE:
        raise QualificationRefusal(
            f"pair {pair_index} moved {relative:.0%} between the census "
            f"({expected['logical_rows']} logical rows) and the snapshot "
            f"({resolved['logical_rows']}); re-run the census before capturing"
        )
    return {
        "user_id": resolved["user_id"],
        "color": resolved["player_color"],
        "pair_index": pair_index,
        "census_shape": {
            key: expected[key]
            for key in ("roots", "positions", "edges", "scope", "logical_rows", "sessions")
        },
        "snapshot_drift": drift,
        "relative_size_drift": relative,
    }


# --------------------------------------------------------------------------
# §4.1/§4.2 — the SF fixture capture, for the QC-SPIKE tie-back
# --------------------------------------------------------------------------


def create_fixture_database(run_id: str) -> str:
    """A plain database on QC-SPIKE: SF carries no production rows at all.

    It is NOT cloned from ``gr_snap_base`` — that template exists only on
    QC-PROD, and the fixture's rows are ``build_timeline``'s deterministic
    synthetic sessions. The sentinel is still stamped, so the guard, the
    reuse refusal and their tests are the same ones the real capture uses.
    """
    name = f"gr_score_capture_fx_{run_id}"
    if not re.fullmatch(CAPTURE_DATABASE_PATTERN, name):
        raise QualificationRefusal(f"{name!r} is not a gr_score_capture_* database")
    engine, url = _admin_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
            conn.execute(
                text(
                    f"COMMENT ON DATABASE \"{name}\" IS "
                    f"'{run_id}|{SNAPSHOT_TEMPLATE}|{SENTINEL_FRESH}'"
                )
            )
    finally:
        engine.dispose()
    return url.set(database=name).render_as_string(hide_password=False)


def assert_spike_schema(engine) -> None:
    """SF's schema is ``create_all``'s, never alembic's — see ``capture_fixture``.

    Checked before the timeline runs AND after it, because the second call is
    the one that proves ``build_timeline``'s own ``create_all`` did not install
    what a migration would have. Both conditions are named separately: an
    ``alembic_version`` table says someone migrated this database, and an epoch
    trigger says the hand-maintained counter now has a second author.
    """
    with engine.connect() as conn:
        migrated = conn.execute(
            text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        ).scalar_one()
        triggers = conn.execute(
            text(
                "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal "
                "AND tgname LIKE 'trg_%%_evidence_epoch%%'"
            )
        ).scalar_one()
    if migrated or triggers:
        raise QualificationRefusal(
            "the SF fixture database is not the spike's: "
            f"alembic_version={'present' if migrated else 'absent'}, "
            f"evidence-epoch triggers={triggers}. ``build_timeline`` maintains "
            "``evidence_epoch`` by hand, so a migrated fixture double-counts "
            "every evidence write and its timeline cannot tie back to the "
            "approved spike report"
        )


def capture_fixture(args) -> int:
    """Run the spike's own timeline and emit BOTH artifacts it feeds.

    One run produces the SF capture the cells replay and the REGENERATED
    TIMELINE the §2 preamble's tie-back validity check compares against the
    approved report. They must come from the same run or the tie-back compares
    a timeline to a different timeline's payloads.
    """
    from scripts.opening_score_storage_workload import (
        COLOR,
        build_timeline,
        summarize_timeline,
    )

    fixture_url = create_fixture_database(args.run_id)
    os.environ[CAPTURE_DATABASE_ENV] = fixture_url
    url = bootstrap_database_url(env_name=CAPTURE_DATABASE_ENV, guard=guard_capture_url)
    assert_resolved_engine(url)
    engine = engine_for(url)
    try:
        identity = assert_capture_guard(engine, args.run_id)
        # NO MIGRATIONS HERE, and the omission is the whole of SF's validity.
        # ``build_timeline`` calls ``Base.metadata.create_all`` itself
        # (``opening_score_storage_workload.py:306``) and then maintains the
        # evidence epoch BY HAND — it seeds the singleton row (``:339``) and
        # bumps it once per iteration to stage an "unrelated evidence changed"
        # cause (``:453``) — because the spike's fixture database has no
        # triggers. Migration ``20260708_01`` BOTH seeds that row AND installs
        # ``trg_*_evidence_epoch`` statement triggers on the shared evidence
        # tables, so migrating this database would make the seed raise
        # ``duplicate key ... evidence_epoch_pkey`` (which is how this was
        # found) and, had it not raised, would bump the epoch on EVERY evidence
        # write — turning the one controlled ``unrelated_epoch`` cause into a
        # bump on every request and regenerating a timeline the approved report
        # never produced. SF is a REGRESSION TIE-BACK to the approved spike
        # ceilings; its schema is the spike's, by construction.
        assert_spike_schema(engine)
        mark_used(engine, args.run_id)
        candidates, bucketed, events = build_timeline(engine)
        assert_spike_schema(engine)
        timeline = summarize_timeline(candidates, bucketed, events)
        versions, invalidations = capture_shared_evidence(engine)
    finally:
        engine.dispose()
    final = candidates[-1]
    artifact = {
        "run_id": args.run_id,
        "pair_index": None,
        "color": COLOR,
        "cutoffs": len(candidates),
        "candidates": candidates,
        "shared_versions": versions,
        "shared_invalidations": invalidations,
        "read_inputs": read_inputs_for(final.payload),
        "tree_requests": reconstruct_tree_requests(final.payload),
        "provenance": {
            "source": "regenerated spike fixture timeline",
            "cluster": identity,
            "synthetic_only": True,
            "timeline_provenance": timeline["provenance"],
            "seed": timeline["seed"],
            "shared_evidence_rows": {
                "versions": len(versions),
                "invalidations": len(invalidations),
            },
            "note": (
                "SF is a REGRESSION TIE-BACK, never a fit point: its row mix is "
                "not production's, and one slope through it and S1 would "
                "confound mix with size"
            ),
        },
    }
    # Synthetic: no private-store rule applies, and the cells need to read it
    # from wherever the run keeps its artifacts.
    dump_capture(args.output, artifact, production_derived=False)
    args.timeline_output.write_text(json.dumps(timeline, indent=2, default=str) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "timeline": str(args.timeline_output),
                "cutoffs": len(candidates),
                "logical_rows": logical_rows(final.payload),
            },
            indent=2,
        )
    )
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _pair_request_path(output: Path) -> Path:
    return assert_private_store(output).with_name(f"{Path(output).stem}.pair.json")


def capture(args) -> int:
    """The §1.3 production capture: clone, guard, capture, DUMP, then check."""
    census = json.loads(assert_private_store(args.census).read_text())
    assert_census_shape(census)

    # Create and stamp the clone BEFORE the first app import, then bootstrap
    # DATABASE_URL from the opt-in variable so app.db binds to the guarded
    # target and nothing else (app/db.py:49).
    capture_url = clone_snapshot(args.run_id)
    os.environ[CAPTURE_DATABASE_ENV] = capture_url
    url = bootstrap_database_url(env_name=CAPTURE_DATABASE_ENV, guard=guard_capture_url)
    assert_resolved_engine(url)
    engine = engine_for(url)
    try:
        identity = assert_capture_guard(engine, args.run_id)
        # Only now — after the guard passed — and never as a bare shell command.
        run_migrations(url, expected_database_pattern=CAPTURE_DATABASE_PATTERN)
        pair = resolve_pair(engine, census, args.pair_index)
        captured = capture_pair(
            engine, pair["user_id"], pair["color"], args.cutoffs, args.run_id
        )
        versions, invalidations = capture_shared_evidence(engine)
    finally:
        engine.dispose()
    final = captured["candidates"][-1]
    artifact = {
        "run_id": args.run_id,
        "pair_index": args.pair_index,
        "color": pair["color"],
        "cutoffs": len(captured["candidates"]),
        "candidates": captured["candidates"],
        "shared_versions": versions,
        "shared_invalidations": invalidations,
        "read_inputs": read_inputs_for(final.payload),
        "tree_requests": reconstruct_tree_requests(final.payload),
        "provenance": {
            "source": "restored production snapshot clone",
            "cluster": identity,
            "cutoff_provenance": captured["cutoff_provenance"],
            "rows_restored_per_cutoff": captured["rows_restored_per_cutoff"],
            "pair_resolution": {
                key: value for key, value in pair.items() if key != "user_id"
            },
            "shared_evidence_rows": {
                "versions": len(versions),
                "invalidations": len(invalidations),
            },
            "traffic_representative": False,
            "note": (
                "active_users_30d is 1, so production shape means "
                "representative SIZE and ROW MIX, never representative "
                "traffic; g-score-store-observe inherits the limitation."
            ),
        },
    }
    artifact["census"] = census

    # DUMP FIRST. The closure check spawns a second clone and can fail for
    # reasons that have nothing to do with this capture; discarding hours of
    # captured cutoffs because a control run could not start is not a trade
    # anyone would make. A failed check is recorded ON the artifact below.
    dump_capture(args.output, artifact)

    if args.skip_closure_check:
        closure = {"ran": False, "skipped": True, "recorded_gap": True}
    else:
        _pair_request_path(args.output).write_text(
            json.dumps(
                {
                    "user_id": pair["user_id"],
                    "color": pair["color"],
                    "computed_at": final.computed_at.isoformat(),
                }
            )
        )
        os.chmod(_pair_request_path(args.output), 0o600)
        failure = None
        try:
            closure = closure_check(
                args.run_id,
                {"computed_at": final.computed_at, "payload": final.payload},
                args.output,
            )
        except QualificationRefusal as exc:
            # The capture is invalid, and saying so ON the artifact is part of
            # saying so: a pickle on disk with no closure record would read as
            # an unchecked capture rather than a failed one.
            closure = {"ran": True, "equal": False, "refusal": str(exc)}
            failure = exc
        finally:
            _pair_request_path(args.output).unlink(missing_ok=True)
        if failure is not None:
            artifact["closure_check"] = closure
            dump_capture(args.output, artifact)
            raise failure
    artifact["closure_check"] = closure
    dump_capture(args.output, artifact)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "pair_index": args.pair_index,
                "color": pair["color"],
                "cutoffs": artifact["cutoffs"],
                "logical_rows": logical_rows(final.payload),
                "tree_requests": len(artifact["tree_requests"]),
                "shared_evidence_rows": artifact["provenance"]["shared_evidence_rows"],
                "pair_resolution": artifact["provenance"]["pair_resolution"],
                "closure_check": closure,
            },
            indent=2,
            default=str,
        )
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("capture", help="§1.3 production-shape capture")
    run.add_argument("--run-id", required=True, help="lowercase [a-z0-9_] run id")
    # NO --user-id. The anonymised census index resolves to the real pair
    # inside the tool, so no production identifier reaches argv, ps output,
    # shell history or a transcript.
    run.add_argument("--pair-index", type=int, required=True)
    run.add_argument("--census", type=Path, required=True)
    run.add_argument("--cutoffs", type=int, default=100)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument(
        "--skip-closure-check",
        action="store_true",
        help="recorded as a gap in the artifact; never the default",
    )
    run.set_defaults(func=capture)

    control = sub.add_parser(
        "control-recompute", help="child half of the closure check"
    )
    control.add_argument("--run-id", required=True)
    control.add_argument("--user-id-from-capture", required=True)
    control.add_argument("--output", type=Path, required=True)
    control.set_defaults(func=control_recompute)

    fixture = sub.add_parser(
        "fixture-capture", help="§4.2's SF tie-back capture and regenerated timeline"
    )
    fixture.add_argument("--run-id", required=True)
    fixture.add_argument("--output", type=Path, required=True)
    fixture.add_argument("--timeline-output", type=Path, required=True)
    fixture.set_defaults(func=capture_fixture)

    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
