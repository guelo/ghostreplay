"""PostgreSQL proofs for Release B's correctness core (g-b-backfill-core).

Three gated suites, not one. Each proves something the SQLite migration suite is
structurally blind to:

1. **CHECK transition** — Release A created ``ck_game_sessions_player_accuracy``
   ``NOT VALID``; B's Step 0 validates it. SQLite has no such state.
2. **Population parity matrix** — the migration necessarily FREEZES a SQL copy of
   the visibility predicate that application code centralizes in
   ``app/session_contracts.py``. One hidden drill in a fixture does not prevent
   drift, so this walks 8 session shapes x 3 seeded versions and asserts the
   frozen SQL, the coverage assertion, and the live ORM predicate agree in every
   cell.
3. **Detector parity matrix** — ``PLY_DETECTOR_SQL``, ``PLY_DETECTOR_ONE_PG``,
   and ``app.accuracy_rows_v1.ply_coordinates_intact`` must agree on every row
   set. Running this only on SQLite would leave the PRODUCTION forms untested,
   and they are not the tested forms: ``PLY_DETECTOR_ONE_PG`` differs from its
   SQLite twin by ``CAST(:sid AS uuid)`` — exactly the construct that fails when
   the bind arrives as text — ``PLY_DETECTOR_SQL`` runs against uuid session IDs
   with PostgreSQL's ``row_number()`` and collation rather than SQLite's, and the
   ``i / 2 + 1`` identity rests on integer division flooring identically on both
   engines, which is asserted here rather than assumed. These are production
   safety mechanisms: the session-scoped form is what the repair consults
   immediately before nulling a served value, and the set-wide form IS the
   fail-closed soundness assertion. A drift between them and the validator on
   PostgreSQL is a wrong value served or a correct value destroyed, and neither
   is observable from the SQLite suite.

Deviation from the plan, stated rather than hidden: the population matrix seeds
all 24 cells into ONE disposable database and asserts cell by cell, instead of
parametrizing 24 tests that would each replay the migration history from base.
Same coverage, 24x less setup. The detector matrix IS parametrized — its five
cases are pinned individually in ``REQUIRED_PG_GATE_PARAM_CASES``, so a case that
stops being collected fails the manifest check instead of silently reducing
coverage to whatever still runs.
"""

from __future__ import annotations

import pathlib
import uuid
from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, text

import pg_gate_plugin
from app.accuracy_rows_v1 import ply_color, ply_coordinates_intact
from app.accuracy_v1 import AccuracyMove, compute_game_accuracy, expected_total_moves_from_pgn
from test_release_b_migrations import (
    BROKEN_PLIES,
    BROKEN_UNGUARDED_ACCURACY,
    INTACT_ACCURACY,
    INTACT_PLIES,
    PGN,
    PREVIOUS_HEAD,
    REVISION,
    _Row,
)

_BACKEND_DIR = pathlib.Path(__file__).resolve().parent


def _alembic_config() -> Config:
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    return cfg


mod = ScriptDirectory.from_config(_alembic_config()).get_revision(REVISION).module


def _seed_pg_session(
    conn,
    sid,
    *,
    status,
    mode,
    drill_state,
    version,
    accuracy=None,
    plies=INTACT_PLIES,
    user_id=910001,
):
    """Insert one session satisfying the live drill/rating boundary CHECKs.

    ``ck_game_sessions_drill_rating_boundary`` demands is_rated=true plus
    normal_started_at / converted_at / rated_start_ply for a converted drill, and
    is_rated=false with a NULL rated_start_ply for every other drill state.
    """
    converted = drill_state == "converted"
    is_drill = mode == "drill"
    # Timezone-aware datetimes, NOT strings. psycopg sends an untyped text() bind
    # as VARCHAR, and PostgreSQL has no implicit VARCHAR -> TIMESTAMPTZ
    # assignment cast, so a string here fails the INSERT outright.
    started = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    converted_ts = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
    ended = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    conn.execute(
        text(
            "INSERT INTO game_sessions (id, user_id, started_at, ended_at, status, engine_elo, "
            "is_rated, player_color, pgn, session_mode, drill_state, normal_started_at, "
            "converted_at, rated_start_ply, player_accuracy, player_accuracy_algo_version) "
            "VALUES (CAST(:id AS uuid), :uid, :started_at, :ended_at, :status, 1500, :is_rated, "
            "'white', :pgn, :mode, :drill_state, :normal_started_at, :converted_at, "
            ":rated_start_ply, :accuracy, :version)"
        ).bindparams(
            id=str(sid),
            uid=user_id,
            started_at=started,
            ended_at=ended if status == "ended" else None,
            status=status,
            is_rated=(converted or not is_drill),
            pgn=PGN,
            mode=mode,
            drill_state=drill_state,
            normal_started_at=converted_ts if converted else None,
            converted_at=converted_ts if converted else None,
            rated_start_ply=0 if converted else None,
            accuracy=accuracy,
            version=version,
        )
    )
    for move_number, color, eval_cp in plies:
        conn.execute(
            text(
                "INSERT INTO session_moves (session_id, move_number, color, move_san, fen_after, "
                "eval_cp) VALUES (CAST(:sid AS uuid), :mn, :c, 'e4', 'fen', :cp)"
            ).bindparams(sid=str(sid), mn=move_number, c=color, cp=eval_cp)
        )


# ---------------------------------------------------------------------------
# 1. CHECK validation transition.
# ---------------------------------------------------------------------------


@pg_gate_plugin.pg_gate
def test_pg_release_b_validates_the_not_valid_check(pg_migration_db, monkeypatch):
    """Never against ``head`` or the shared database: an explicit A -> B walk.

    VALIDATE takes SHARE UPDATE EXCLUSIVE and scans the whole table. That lock
    does not conflict with ROW EXCLUSIVE or ROW SHARE, so it does not block the
    /moves hook's writes or its FOR NO KEY UPDATE row locks — which is why
    validation enters the health-window budget but not the writer-stall budget.
    """
    url = pg_migration_db
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _alembic_config()

    command.upgrade(cfg, "20260709_01")
    eng = create_engine(url)
    with eng.connect() as conn:
        assert conn.execute(
            text(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conname = 'ck_game_sessions_player_accuracy'"
            )
        ).scalar() is False

    command.upgrade(cfg, REVISION)
    with eng.connect() as conn:
        assert conn.execute(
            text(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conname = 'ck_game_sessions_player_accuracy'"
            )
        ).scalar() is True
        # Both fail-closed assertions ran and passed inside that upgrade — the
        # revision could not have completed otherwise — and the population is
        # empty afterwards.
        assert conn.execute(text(mod.SQL_PG.backfill_remaining)).scalar() == 0
    eng.dispose()

    # NOTE: the revision re-arms lock_timeout immediately after VALIDATE, because
    # set_config(..., true) is SET LOCAL — TRANSACTION-scoped, not
    # statement-scoped — and alembic/env.py opens exactly one transaction around
    # the whole run, so without the re-arm every later row lock would silently
    # inherit VALIDATE_LOCK_TIMEOUT ('10s'): a value chosen for a DDL lock wait,
    # never reviewed as a row-lock wait. Asserting the armed value requires
    # observing the MIGRATION connection mid-transaction, which belongs to
    # g-b-runtime-envelope along with the named ATOMIC_LOCK_WAIT_MS constant.


# ---------------------------------------------------------------------------
# 2. Population parity matrix: 8 shapes x 3 versions.
# ---------------------------------------------------------------------------

# (label, session_mode, status, drill_state)
SHAPES = [
    ("normal_active", "normal", "active", None),
    ("normal_ended", "normal", "ended", None),
    ("drill_active", "drill", "active", "active"),
    ("drill_root_reached", "drill", "active", "root_reached"),
    ("drill_ended_failed", "drill", "ended", "failed"),
    ("drill_ended_abandoned", "drill", "ended", "abandoned"),
    # Converted mid-game, still playing: VISIBLE but not yet ended.
    ("drill_active_converted", "drill", "active", "converted"),
    ("drill_ended_converted", "drill", "ended", "converted"),
]
SEEDED_VERSIONS = [None, 0, 1]

# The expected population is shapes 2 and 8 at versions NULL and 0. Every other
# cell is out of population.
IN_POPULATION = {("normal_ended", None), ("normal_ended", 0),
                 ("drill_ended_converted", None), ("drill_ended_converted", 0)}
ENDED_VISIBLE_SHAPES = {"normal_ended", "drill_ended_converted"}

# Value seeded on the already-version-1 cells: an in-population shape at version
# 1 must keep this untouched, and it must differ from INTACT_ACCURACY or the
# assertion could not tell "left alone" from "recomputed".
PRESEEDED_V1_ACCURACY = 13


@pg_gate_plugin.pg_gate
def test_pg_population_parity_matrix(pg_migration_db, monkeypatch):
    assert PRESEEDED_V1_ACCURACY != INTACT_ACCURACY
    url = pg_migration_db
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _alembic_config()
    command.upgrade(cfg, PREVIOUS_HEAD)

    eng = create_engine(url)
    cells: dict[tuple[str, int | None], uuid.UUID] = {}
    with eng.begin() as conn:
        for label, mode, status, drill_state in SHAPES:
            for version in SEEDED_VERSIONS:
                sid = uuid.uuid4()
                cells[(label, version)] = sid
                _seed_pg_session(
                    conn, sid, status=status, mode=mode, drill_state=drill_state,
                    version=version,
                    accuracy=PRESEEDED_V1_ACCURACY if version == 1 else None,
                )
    by_id = {sid: cell for cell, sid in cells.items()}

    # --- selection: the frozen SQL predicate picks exactly the expected cells ---
    with eng.connect() as conn:
        selected = {
            by_id[r[0]]
            for r in conn.execute(
                text(f"SELECT id FROM game_sessions WHERE {mod.POPULATION_PREDICATE_SQL}")
            )
        }
    assert selected == IN_POPULATION

    command.upgrade(cfg, REVISION)

    # --- after the run: only in-population cells changed ---
    with eng.connect() as conn:
        state = {
            by_id[r[0]]: (r[1], r[2])
            for r in conn.execute(
                text(
                    "SELECT id, player_accuracy, player_accuracy_algo_version FROM game_sessions"
                )
            )
        }
    for cell, (accuracy, version) in state.items():
        label, seeded_version = cell
        if cell in IN_POPULATION:
            assert (accuracy, version) == (INTACT_ACCURACY, 1), cell
        elif label in ENDED_VISIBLE_SHAPES:
            # Ended-visible but already at version 1: value untouched.
            assert (accuracy, version) == (PRESEEDED_V1_ACCURACY, 1), cell
        else:
            # Out of population keeps the EXACT version it was seeded with — a
            # hidden version-0 drill stays version 0 — so a widened predicate is
            # caught rather than masked by a NULL default.
            assert version == seeded_version, cell
            assert accuracy == (PRESEEDED_V1_ACCURACY if seeded_version == 1 else None), cell

    # --- final assertion: counts exactly the ended-visible rows ---
    with eng.connect() as conn:
        assert conn.execute(text(mod.SQL_PG.coverage_assert)).scalar() == 0
        assert conn.execute(text(mod.SQL_PG.soundness_assert)).scalar() == 0
    # It passes with hidden ended failed/abandoned drills present at NULL version
    # (they are in the fixture above), and FAILS if an in-population row is left
    # unstamped.
    victim = cells[("normal_ended", None)]
    with eng.begin() as conn:
        conn.execute(
            text(
                "UPDATE game_sessions SET player_accuracy_algo_version = NULL "
                "WHERE id = CAST(:i AS uuid)"
            ).bindparams(i=str(victim))
        )
    with eng.connect() as conn:
        assert conn.execute(text(mod.SQL_PG.coverage_assert)).scalar() == 1
    with eng.begin() as conn:
        conn.execute(
            text(
                "UPDATE game_sessions SET player_accuracy_algo_version = 1 "
                "WHERE id = CAST(:i AS uuid)"
            ).bindparams(i=str(victim))
        )

    # --- application visibility: the ORM predicate agrees row by row ---
    from sqlalchemy.orm import sessionmaker

    from app.models import GameSession
    from app.session_contracts import is_visible_game_session, visible_session_filter

    with eng.connect() as conn:
        frozen_ended_visible = {
            r[0]
            for r in conn.execute(
                text(f"SELECT id FROM game_sessions WHERE {mod.VISIBLE_ENDED_SQL}")
            )
        }
    db = sessionmaker(bind=eng)()
    try:
        orm_ended_visible = {
            s.id
            for s in db.query(GameSession)
            .filter(GameSession.status == "ended")
            .filter(visible_session_filter())
            .all()
        }
        assert orm_ended_visible == frozen_ended_visible
        for session in db.query(GameSession).all():
            expected = session.status == "ended" and is_visible_game_session(session)
            assert (session.id in frozen_ended_visible) is expected, by_id[session.id]
    finally:
        db.close()
    eng.dispose()


# ---------------------------------------------------------------------------
# 3. The guarded update's typed arrays — the batch shapes SQLite cannot express.
# ---------------------------------------------------------------------------

# Coordinates intact, every eval stripped: frozen v1 returns None because a
# player transition is missing an eval. Distinct from a grid rejection, and the
# distinction is the point — an all-NULL batch must be reachable BOTH ways.
NO_EVAL_PLIES = [(move_number, color, None) for move_number, color, _ in INTACT_PLIES]

# (kind, plies, expected stored accuracy)
_STRIPPED = ("stripped", NO_EVAL_PLIES, None)
_BROKEN = ("broken", BROKEN_PLIES, None)
_INTACT = ("intact", INTACT_PLIES, INTACT_ACCURACY)

BATCH_ROUNDS = [
    # The shape that a `FROM (VALUES ...)` guarded update would fail on, and ONLY
    # on: every accuracy is NULL, so with untyped binds PostgreSQL resolves the
    # column by the UNION/CASE rules to `text` and the assignment into an integer
    # column errors. Reached two different ways here so it is pinned as a SHAPE
    # rather than as an accident of one fixture's evals.
    ("all_null", [_STRIPPED, _STRIPPED, _BROKEN, _BROKEN]),
    ("mixed", [_INTACT, _INTACT, _STRIPPED, _BROKEN]),
    ("all_scored", [_INTACT, _INTACT, _INTACT]),
]


@pg_gate_plugin.pg_gate
def test_pg_guarded_update_typed_arrays_all_null_mixed_and_all_scored(
    pg_migration_db, monkeypatch
):
    """Drive the REAL runner over an all-NULL, a mixed, and an all-scored batch.

    Three properties, none of which SQLite can express — its guarded update is a
    per-row statement with no arrays at all:

    1. **Type safety is a property of the STATEMENT, not of the page.** All three
       batch shapes must compile and execute identically. See the
       ``UPDATE_SQL_PG`` comment for why the obvious ``FROM (VALUES ...)`` form
       is a latent, data-dependent type error.
    2. **The two-arm obligation on the broken subset.** Unguarded
       ``compute_game_accuracy`` over the rows AS STORED must return a pinned
       non-NULL integer, and the runner must nonetheless store NULL. Without arm
       1 the broken rows would be indistinguishable from the eval-stripped rows
       beside them, and the batch would demonstrate type safety while proving
       nothing about the guard.
    3. **One server statement per batch.** ``statement_timeout`` applies PER
       STATEMENT, so a driver executemany would leave the runtime envelope's
       armed batch deadline bounding nothing. Asserted via the marker comment,
       which is what makes an execution identifiable in the first place.
    """
    url = pg_migration_db
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _alembic_config()
    command.upgrade(cfg, PREVIOUS_HEAD)
    eng = create_engine(url)

    executed: list[tuple[str, bool]] = []

    @event.listens_for(eng, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        executed.append((statement, executemany))

    try:
        for round_name, spec in BATCH_ROUNDS:
            seeded = []
            with eng.begin() as conn:
                for kind, plies, expected in spec:
                    sid = uuid.uuid4()
                    seeded.append((sid, kind, expected))
                    _seed_pg_session(
                        conn, sid, status="ended", mode="normal", drill_state=None,
                        version=None, plies=plies, user_id=910003,
                    )

            # --- arm 1, over the rows AS STORED IN POSTGRESQL ---
            for sid, kind, _ in seeded:
                if kind != "broken":
                    continue
                with eng.connect() as conn:
                    rows = conn.execute(
                        text(mod.SQL_PG.load_moves).bindparams(ids=[str(sid)])
                    ).all()
                assert ply_coordinates_intact(rows) is False, round_name
                assert compute_game_accuracy(
                    [
                        AccuracyMove(color=ply_color(r), eval_cp=r.eval_cp, eval_mate=r.eval_mate)
                        for r in rows
                    ],
                    player_color="white",
                    expected_total_moves=expected_total_moves_from_pgn(PGN),
                ) == BROKEN_UNGUARDED_ACCURACY, round_name

            # --- run the real runner over exactly this batch ---
            executed.clear()
            with eng.begin() as conn:
                mod._run_backfill(conn, mod.SQL_PG, 500)

            updates = [
                (sql, many)
                for sql, many in executed
                if "/* ghostreplay:guarded_update */" in sql
            ]
            assert len(updates) == 1, f"{round_name}: {len(updates)} guarded updates, want 1"
            assert updates[0][1] is False, f"{round_name}: guarded update went out as executemany"

            # --- arm 2 ---
            with eng.connect() as conn:
                for sid, kind, expected in seeded:
                    stored = conn.execute(
                        text(
                            "SELECT player_accuracy, player_accuracy_algo_version "
                            "FROM game_sessions WHERE id = CAST(:i AS uuid)"
                        ).bindparams(i=str(sid))
                    ).one()
                    assert stored == (expected, 1), (round_name, kind, sid)
                assert conn.execute(text(mod.SQL_PG.backfill_remaining)).scalar() == 0
    finally:
        event.remove(eng, "before_cursor_execute", _record)
        eng.dispose()


@pg_gate_plugin.pg_gate
def test_pg_nil_uuid_session_is_backfilled(pg_migration_db, monkeypatch):
    """The nil UUID is a schema-valid session ID.

    A keyset sweep that starts from a sentinel "minimum ID" (``id > '000…0'``)
    never selects this row, while the remaining-count query still counts it — so
    the backfill exhausts its passes and raises with the row unstamped. The
    first-page statement has no cursor predicate at all, which is why this
    passes.
    """
    url = pg_migration_db
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _alembic_config()
    command.upgrade(cfg, PREVIOUS_HEAD)
    eng = create_engine(url)
    nil = uuid.UUID(int=0)
    with eng.begin() as conn:
        _seed_pg_session(
            conn, nil, status="ended", mode="normal", drill_state=None,
            version=None, user_id=910004,
        )

    command.upgrade(cfg, REVISION)

    with eng.connect() as conn:
        assert conn.execute(
            text(
                "SELECT player_accuracy, player_accuracy_algo_version FROM game_sessions "
                "WHERE id = CAST(:i AS uuid)"
            ).bindparams(i=str(nil))
        ).one() == (INTACT_ACCURACY, 1)
    eng.dispose()


# ---------------------------------------------------------------------------
# 4. Detector parity matrix.
# ---------------------------------------------------------------------------

PG_DETECTOR_CASES = [
    ("well_formed", INTACT_PLIES, True),
    ("gap", [(1, "white", 1), (1, "black", 2), (3, "white", 3), (3, "black", 4)], False),
    ("white_white_adjacency", BROKEN_PLIES, False),
    # The grid simply CONTINUES past the PGN's last ply: coordinate-contiguous
    # surplus validates as intact.
    ("contiguous_surplus", INTACT_PLIES + [(4, "white", 5), (4, "black", 6)], True),
    # The case most likely to diverge — count(*) over an empty CTE versus the
    # validator's early return on [] — and the case a set-wide-only PostgreSQL
    # test never reaches.
    ("empty", [], True),
]


@pg_gate_plugin.pg_gate
@pytest.mark.parametrize(
    "name,plies,intact", PG_DETECTOR_CASES, ids=[c[0] for c in PG_DETECTOR_CASES]
)
def test_pg_detector_parity(pg_engine, name, plies, intact):
    sid = uuid.uuid4()
    try:
        with pg_engine.begin() as conn:
            _seed_pg_session(
                conn, sid, status="ended", mode="normal", drill_state=None,
                version=None, plies=plies, user_id=910002,
            )
        with pg_engine.connect() as conn:
            set_wide = {
                r[0]
                for r in conn.execute(
                    text(f"SELECT session_id FROM ({mod.PLY_DETECTOR_SQL}) d")
                )
            }
            scoped = conn.execute(
                text(mod.SQL_PG.ply_detector_one).bindparams(sid=str(sid))
            ).scalar()
    finally:
        with pg_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM game_sessions WHERE id = CAST(:i AS uuid)").bindparams(
                    i=str(sid)
                )
            )

    assert ply_coordinates_intact([_Row(*p) for p in plies]) is intact
    assert (sid not in set_wide) is intact
    assert (scoped == 0) is intact


@pg_gate_plugin.pg_gate
def test_pg_integer_division_floors_like_the_validator(pg_engine):
    """The whole ``i / 2 + 1`` identity rests on this, so assert it rather than
    assuming it from a note under the detector definitions."""
    with pg_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT i, i / 2 + 1 FROM generate_series(0, 11) AS g(i)"
            )
        ).all()
    assert [(i, expr) for i, expr in rows] == [(i, i // 2 + 1) for i in range(12)]
