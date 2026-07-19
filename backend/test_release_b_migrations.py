"""Migration tests for Release B's correctness core (g-b-backfill-core).

Revision ``20260719_01`` is the backfill/repair/fail-closed state machine. This
file proves it on SQLite via the Alembic command API, plus the structural
contracts that hold on every dialect:

- **lineage** — a single head, and a ``down_revision`` asserted against
  ``ScriptDirectory``'s walk rather than a hardcoded string, so a future revision
  inserted between ``20260718_01`` and B fails here instead of silently
  re-branching;
- **statement bundles** — the ``_PG`` / ``_SQLITE`` split, the marker comments,
  and the UUID-cast rules, asserted TEXTUALLY so a bundle member that acquired a
  cast (or lost one) fails at collection rather than on the one page that happens
  to trigger it;
- **environment surface** — exactly two variables, collected by parsing the
  revision with ``ast``;
- **behaviour** — backfill, repair, three-way detector parity, and both
  fail-closed assertions over a hand-built pre-B schema.

The PostgreSQL-only proofs (population-parity matrix, PG detector parity, and the
``NOT VALID`` → validated CHECK transition) live in
``test_release_b_pg_matrix.py``.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from app.accuracy_rows_v1 import ply_coordinates_intact
from app.accuracy_v1 import AccuracyMove, compute_game_accuracy, expected_total_moves_from_pgn

_BACKEND_DIR = pathlib.Path(__file__).resolve().parent
_REVISION_PATH = (
    _BACKEND_DIR / "alembic" / "versions" / "20260719_01_backfill_session_player_accuracy.py"
)

REVISION = "20260719_01"
PREVIOUS_HEAD = "20260718_01"


def _alembic_config() -> Config:
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    return cfg


# The revision loaded as a module, for the direct-call tests. NOTE: each
# ``command.upgrade`` builds its own ScriptDirectory and RE-IMPORTS the file, so
# the class objects there are not these class objects — which is why the
# alembic-driven tests below assert on ``RuntimeError`` (MigrationError's base)
# plus the message, and never on ``mod.MigrationError`` identity. For the same
# reason, monkeypatching ``mod`` would not affect a ``command.upgrade`` run, so
# no test tries to.
mod = ScriptDirectory.from_config(_alembic_config()).get_revision(REVISION).module


# ---------------------------------------------------------------------------
# Pre-B schema fixture.
#
# NOT Release A's _PRE_A_GAME_SESSIONS_DDL: that fixture is a deliberately
# reduced pre-A game_sessions (nine columns, no ended_at, no pgn, no
# session_moves at all) because A only had to add two columns and a CHECK. B
# READS ended_at (to prove it does NOT filter on it), pgn (for
# expected_total_moves_from_pgn), and the whole of session_moves — driving B
# against A's fixture fails at the first statement.
#
# NOT Base.metadata.create_all either: that would produce TODAY's model schema
# rather than the schema as of 20260718_01, so a column B depends on that no
# migration ever created would still be present and the fixture would pass while
# production failed — precisely the drift class 20260718_01 exists to close.
# ---------------------------------------------------------------------------

_PRE_B_GAME_SESSIONS_DDL = """
    CREATE TABLE game_sessions (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        started_at TIMESTAMP NOT NULL,
        ended_at TIMESTAMP,
        status VARCHAR(20) NOT NULL,
        engine_elo INTEGER NOT NULL,
        is_rated BOOLEAN NOT NULL DEFAULT 1,
        player_color VARCHAR(5) NOT NULL DEFAULT 'white',
        pgn TEXT,
        session_mode VARCHAR(10) NOT NULL DEFAULT 'normal',
        drill_state VARCHAR(12),
        player_accuracy INTEGER,
        player_accuracy_algo_version SMALLINT,
        CONSTRAINT ck_game_sessions_session_mode CHECK (session_mode IN ('normal','drill')),
        CONSTRAINT ck_game_sessions_player_accuracy CHECK (
            player_accuracy IS NULL OR (player_accuracy >= 0 AND player_accuracy <= 100)
        )
    )
"""

# Both session_moves constraints are load-bearing for B rather than decoration.
# The UNIQUE constraint is the index the session-scoped detector's affordability
# claim rests on, and the colour CHECK is what makes "a broken grid" mean
# MISORDERED rather than GARBAGE COLOUR VALUES — a fixture that could write
# color = 'w' would exercise a defect class the detector's
# `CASE WHEN color = 'white'` arm silently absorbs.
_PRE_B_SESSION_MOVES_DDL = """
    CREATE TABLE session_moves (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES game_sessions(id) ON DELETE CASCADE,
        move_number INTEGER NOT NULL,
        color VARCHAR(5) NOT NULL,
        move_san VARCHAR(10) NOT NULL,
        fen_after TEXT NOT NULL,
        eval_cp INTEGER,
        eval_mate INTEGER,
        segment VARCHAR(10) NOT NULL DEFAULT 'normal',
        CONSTRAINT ck_session_moves_color CHECK (color in ('white','black')),
        CONSTRAINT uq_session_moves_session_move_color UNIQUE (session_id, move_number, color)
    )
"""

_PRE_B_MOVES_INDEX_DDL = "CREATE INDEX idx_session_moves_session ON session_moves (session_id)"


# ---------------------------------------------------------------------------
# Ply fixtures.
#
# THE seeding constraint the whole guard story depends on: every broken-grid
# fixture must be SCORABLE BUT FOR THE GRID. Frozen v1 already returns None for
# an unknown PGN ply count, for len(moves) < expected_total_moves, for an empty
# row set, and for a missing eval on a player transition
# (accuracy_v1.py:135-152). So a broken-grid fixture that is ALSO short, empty,
# or eval-stripped yields None through both paths, and an assertion reading
# "broken grid -> NULL, version 1" passes identically whether or not
# ply_coordinates_intact was ever called. That assertion is not a test of the
# guard; it is a test of nothing.
#
# Both broken rows below therefore carry complete evaluations on every ply and a
# row count equal to the PGN's expected total, and break only the COORDINATES: a
# white-white adjacency, which keeps the count, the evals, and the PGN agreement
# intact.
# ---------------------------------------------------------------------------

PGN = '[Event "?"]\n\n1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 *'
EXPECTED_PLIES = 6

# (move_number, color, eval_cp) — the contiguous mainline coordinate grid.
INTACT_PLIES = [
    (1, "white", 20),
    (1, "black", 10),
    (2, "white", -60),
    (2, "black", 80),
    (3, "white", -120),
    (3, "black", 150),
]
INTACT_ACCURACY = 87

# Same six plies, six complete evals, coordinates broken: the black reply at move
# 1 is missing and a stray white at move 4 keeps the count. Ordered, row 1 is
# (2, white) where the grid demands (1, black) — a white-white adjacency.
BROKEN_PLIES = [
    (1, "white", 20),
    (2, "white", -30),
    (2, "black", 60),
    (3, "white", -90),
    (3, "black", 140),
    (4, "white", -200),
]
BROKEN_UNGUARDED_ACCURACY = 89

# The incomplete row: coordinates intact, but short of the PGN's ply count, so
# frozen v1 legitimately returns None. Distinct from a grid rejection.
INCOMPLETE_PLIES = INTACT_PLIES[:4]


class _Row:
    """Minimal stand-in for a query row, for the unguarded arm."""

    def __init__(self, move_number, color, eval_cp):
        self.move_number = move_number
        self.color = color
        self.eval_cp = eval_cp
        self.eval_mate = None


def _unguarded_accuracy(plies) -> int | None:
    """Frozen ``compute_game_accuracy`` with NO validator in front of it."""
    return compute_game_accuracy(
        [AccuracyMove(color=c, eval_cp=cp, eval_mate=None) for _, c, cp in plies],
        player_color="white",
        expected_total_moves=expected_total_moves_from_pgn(PGN),
    )


def _seed_session(conn, sid, *, status="ended", mode="normal", drill_state=None,
                  plies=None, pgn=PGN, ended_at="2026-01-01T01:00:00",
                  accuracy=None, version=None):
    conn.execute(
        text(
            "INSERT INTO game_sessions (id, user_id, started_at, ended_at, status, engine_elo, "
            "player_color, pgn, session_mode, drill_state, player_accuracy, "
            "player_accuracy_algo_version) "
            "VALUES (:id, 1, '2026-01-01T00:00:00', :ended_at, :status, 1500, 'white', :pgn, "
            ":mode, :drill_state, :accuracy, :version)"
        ).bindparams(
            id=sid, ended_at=ended_at, status=status, pgn=pgn, mode=mode,
            drill_state=drill_state, accuracy=accuracy, version=version,
        )
    )
    for move_number, color, eval_cp in plies or []:
        conn.execute(
            text(
                "INSERT INTO session_moves (session_id, move_number, color, move_san, fen_after, "
                "eval_cp) VALUES (:sid, :mn, :c, 'e4', 'fen', :cp)"
            ).bindparams(sid=sid, mn=move_number, c=color, cp=eval_cp)
        )


def _build_pre_b(url) -> None:
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text(_PRE_B_GAME_SESSIONS_DDL))
        conn.execute(text(_PRE_B_SESSION_MOVES_DDL))
        conn.execute(text(_PRE_B_MOVES_INDEX_DDL))
    eng.dispose()


def _cached(conn, sid):
    return conn.execute(
        text(
            "SELECT player_accuracy, player_accuracy_algo_version FROM game_sessions WHERE id = :i"
        ).bindparams(i=sid)
    ).one()


# ---------------------------------------------------------------------------
# Lineage.
# ---------------------------------------------------------------------------


def test_revision_is_the_single_head_and_descends_from_the_previous_one():
    script = ScriptDirectory.from_config(_alembic_config())
    heads = list(script.get_heads())
    assert heads == [REVISION], f"expected a single head {REVISION}, got {heads}"

    # Asserted against ScriptDirectory's walk, not a hardcoded string: a future
    # revision inserted between 20260718_01 and B must fail HERE rather than
    # silently re-branching the lineage.
    walk = [rev.revision for rev in script.walk_revisions(base="base", head=REVISION)]
    assert walk[0] == REVISION
    assert mod.down_revision == walk[1]
    assert walk[1] == PREVIOUS_HEAD


def test_revision_reads_exactly_two_environment_variables():
    """Parsed with ``ast``: a third variable fails the suite.

    Neither variable can disable an admission guard, and measurement lives in a
    separate harness, never in a deployment code path.
    """
    tree = ast.parse(_REVISION_PATH.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        target = None
        if isinstance(func, ast.Attribute) and func.attr in {"get", "getenv"}:
            value = func.value
            if isinstance(value, ast.Name) and value.id == "os":
                target = node.args[0] if node.args else None
            elif isinstance(value, ast.Attribute) and value.attr == "environ":
                target = node.args[0] if node.args else None
        if target is None:
            continue
        if isinstance(target, ast.Constant):
            names.add(target.value)
        elif isinstance(target, ast.Name):
            names.add(target.id)
    assert names == {"ENV_MODE", "ENV_BATCH"}
    assert {mod.ENV_MODE, mod.ENV_BATCH} == {
        "GHOSTREPLAY_ACCURACY_BACKFILL_MODE",
        "GHOSTREPLAY_ACCURACY_BACKFILL_BATCH",
    }


@pytest.mark.parametrize("bad", ["ATOMIC", "Batch", "atomicc", "", "  ", "batch "])
def test_mode_parsing_is_exact_and_case_sensitive(monkeypatch, bad):
    # "batch " strips to "batch" and is accepted; everything else raises. Case is
    # not folded on purpose: silently normalizing ATOMIC hides a service-config
    # value nobody reviewed.
    monkeypatch.setenv(mod.ENV_MODE, bad)
    if bad.strip() in mod.VALID_MODES:
        assert mod.resolve_mode() == bad.strip()
    else:
        with pytest.raises(mod.MigrationError):
            mod.resolve_mode()


def test_batch_size_bounds(monkeypatch):
    monkeypatch.delenv(mod.ENV_BATCH, raising=False)
    assert mod.resolve_batch_size() == mod.DEFAULT_BATCH_SIZE
    monkeypatch.setenv(mod.ENV_BATCH, "10")
    assert mod.resolve_batch_size() == 10
    for bad in ["0", "-1", str(mod.MAX_BATCH_SIZE + 1), "lots"]:
        monkeypatch.setenv(mod.ENV_BATCH, bad)
        with pytest.raises(mod.MigrationError):
            mod.resolve_batch_size()


# ---------------------------------------------------------------------------
# Statement bundles — textual contracts, cheap and collected-time.
# ---------------------------------------------------------------------------


def _bundle_statements(bundle):
    return {
        field: value
        for field, value in bundle._asdict().items()
        if field != "dialect" and value is not None
    }


def test_bundles_have_the_same_field_set_and_no_accidental_holes():
    assert mod.SQL_PG._fields == mod.SQL_SQLITE._fields
    for field in mod.SQL_PG._fields:
        if field in mod.PG_ONLY_FIELDS:
            assert getattr(mod.SQL_SQLITE, field) is None, field
            assert getattr(mod.SQL_PG, field) is not None, field
        else:
            assert getattr(mod.SQL_PG, field) is not None, field
            assert getattr(mod.SQL_SQLITE, field) is not None, field


def test_dialect_neutral_statements_are_the_same_object():
    """Identity, not two copies that can drift."""
    for field in mod.DIALECT_NEUTRAL_FIELDS:
        assert getattr(mod.SQL_PG, field) is getattr(mod.SQL_SQLITE, field), field


def test_every_statement_starts_with_a_distinct_marker_comment():
    """A marker makes a statement identifiable from ANOTHER session.

    ``pg_stat_activity.query`` reports the text a backend is currently executing,
    so a marker turns "which statement is this backend running" from a guess
    about whitespace into an exact match — which the runtime envelope's
    cancellation probe and listener tests depend on.
    """
    seen: dict[str, str] = {}
    for bundle in (mod.SQL_PG, mod.SQL_SQLITE):
        for field, sql in _bundle_statements(bundle).items():
            match = re.match(r"/\* (ghostreplay:[a-z_]+) \*/", sql)
            assert match, f"{bundle.dialect}.{field} does not begin with a marker: {sql[:60]!r}"
            marker = match.group(1)
            assert seen.setdefault(marker, field) == field, (
                f"marker {marker} is shared by {seen[marker]} and {field}"
            )


def test_sqlite_bundle_carries_no_postgresql_construct():
    """A SQLite bundle member that acquired a cast would otherwise only fail at
    the moment the repair phase ran on a fixture that reached it."""
    for field, sql in _bundle_statements(mod.SQL_SQLITE).items():
        for forbidden in ("AS uuid", "ANY(", "FOR NO KEY UPDATE", "SKIP LOCKED"):
            assert forbidden not in sql, f"SQL_SQLITE.{field} contains {forbidden!r}"


def test_no_truncate_anywhere():
    """SQLite has no TRUNCATE, and the SQLite suite is required to exercise the
    repair phase end to end — so the clear step is DELETE FROM on both dialects
    and only the candidate table's DDL branches by dialect."""
    for bundle in (mod.SQL_PG, mod.SQL_SQLITE):
        for field, sql in _bundle_statements(bundle).items():
            assert "TRUNCATE" not in sql.upper(), f"{bundle.dialect}.{field}"


def test_every_postgresql_uuid_bind_is_cast():
    """A new PostgreSQL statement with a bare bind fails at collection, rather
    than on the all-NULL page that happens to trigger it.

    An untyped bind against a uuid column on PostgreSQL is ``uuid = text``, for
    which no operator exists — the same type-resolution failure the guarded
    update's typed arrays exist to avoid.
    """
    cast_pattern = re.compile(r"CAST\(\s*(:sid|:last_id|:ids)\s+AS\s+uuid(\[\])?\s*\)")
    for field, sql in _bundle_statements(mod.SQL_PG).items():
        cast_spans = [m.span(1) for m in cast_pattern.finditer(sql)]
        for m in re.finditer(r":(sid|last_id|ids)\b", sql):
            assert any(s <= m.start() and m.end() <= e for s, e in cast_spans), (
                f"SQL_PG.{field} binds :{m.group(1)} outside a CAST(... AS uuid)"
            )


def test_bundle_selection_rejects_unsupported_dialects():
    assert mod.bundle_for("postgresql") is mod.SQL_PG
    assert mod.bundle_for("sqlite") is mod.SQL_SQLITE
    with pytest.raises(mod.MigrationError):
        mod.bundle_for("mysql")


def test_pre_b_fixture_matches_the_live_models_and_the_bundle(tmp_path):
    """Two directions: the fixture must not INVENT a column (subset of the live
    models), and must not OMIT one the bundle names."""
    from app.models import GameSession, SessionMove

    # Reflected from the built fixture, not regexed out of the DDL string, so the
    # comparison is against what SQLite actually created.
    url = f"sqlite:///{tmp_path / 'schema.db'}"
    _build_pre_b(url)
    eng = create_engine(url)
    with eng.connect() as conn:
        fixture = {
            table: {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
            for table in ("game_sessions", "session_moves")
        }
    eng.dispose()
    live = {
        "game_sessions": {c.name for c in GameSession.__table__.columns},
        "session_moves": {c.name for c in SessionMove.__table__.columns},
    }
    for table in fixture:
        assert fixture[table], f"failed to parse {table} columns out of the fixture DDL"
        assert fixture[table] <= live[table], (
            f"{table} fixture invents columns: {fixture[table] - live[table]}"
        )

    # The named column list is read out of the bundle text, not restated here.
    all_sql = "\n".join(
        sql
        for bundle in (mod.SQL_PG, mod.SQL_SQLITE)
        for sql in _bundle_statements(bundle).values()
    )
    for table, columns in live.items():
        for column in columns:
            if re.search(rf"\b{re.escape(column)}\b", all_sql):
                assert column in fixture[table] or column in fixture[
                    "game_sessions" if table == "session_moves" else "session_moves"
                ], f"bundle names {table}.{column} but the pre-B fixture omits it"


# ---------------------------------------------------------------------------
# Behaviour, on SQLite via the Alembic command API.
# ---------------------------------------------------------------------------


VISIBLE_COMPLETE = [f"s-complete-{n}" for n in range(1, 6)]
INCOMPLETE = "s-incomplete"
HIDDEN_DRILL = "s-hidden-drill"
MALFORMED = "s-malformed-no-ended-at"
BROKEN_UNSTAMPED = "s-broken-unstamped"
BROKEN_STAMPED = "s-broken-stamped"
CLEAN_STAMPED = "s-clean-stamped"
CONVERTED = "s-converted-drill"


def _seed_fixture(url) -> None:
    eng = create_engine(url)
    with eng.begin() as conn:
        for sid in VISIBLE_COMPLETE:
            _seed_session(conn, sid, plies=INTACT_PLIES)
        # Visible, ended, converted drill — in population by the same rule as a
        # normal game.
        _seed_session(conn, CONVERTED, mode="drill", drill_state="converted", plies=INTACT_PLIES)
        # Coordinates intact, short of the PGN: frozen v1 legitimately returns
        # None, and the row is still stamped.
        _seed_session(conn, INCOMPLETE, plies=INCOMPLETE_PLIES)
        # Hidden drill: must stay wholly unstamped.
        _seed_session(conn, HIDDEN_DRILL, mode="drill", drill_state="failed", plies=INTACT_PLIES)
        # ended_at IS NULL: the population predicate must never look at it.
        _seed_session(conn, MALFORMED, ended_at=None, plies=INTACT_PLIES)
        # Broken grid, never stamped — the BACKFILL must reject it.
        _seed_session(conn, BROKEN_UNSTAMPED, plies=BROKEN_PLIES)
        # Broken grid, already stamped by the unguarded Release-A hook — the
        # backfill's predicate SKIPS it, so only the REPAIR reaches it.
        _seed_session(
            conn, BROKEN_STAMPED, plies=BROKEN_PLIES,
            accuracy=BROKEN_UNGUARDED_ACCURACY, version=1,
        )
        # Clean grid, already stamped: the repair only ever nulls, so this is
        # untouched.
        _seed_session(conn, CLEAN_STAMPED, plies=INTACT_PLIES, accuracy=42, version=1)
    eng.dispose()


def test_unguarded_arm_the_broken_fixtures_are_scorable_but_for_the_grid():
    """Arm 1 of the two-arm obligation. The delta between the arms IS the guard.

    Without this arm, the broken rows would be indistinguishable from the
    eval-stripped ones beside them, and the migration test would prove nothing
    about ``ply_coordinates_intact`` having run at all.
    """
    broken_rows = [_Row(*p) for p in BROKEN_PLIES]
    assert ply_coordinates_intact(broken_rows) is False
    assert len(BROKEN_PLIES) == EXPECTED_PLIES
    assert all(cp is not None for _, _, cp in BROKEN_PLIES)
    # A specific non-NULL integer, pinned as a literal: this is what fails if
    # someone later "simplifies" the fixture into a short or eval-stripped one.
    assert _unguarded_accuracy(BROKEN_PLIES) == BROKEN_UNGUARDED_ACCURACY

    assert ply_coordinates_intact([_Row(*p) for p in INTACT_PLIES]) is True
    assert _unguarded_accuracy(INTACT_PLIES) == INTACT_ACCURACY
    assert _unguarded_accuracy(INCOMPLETE_PLIES) is None


def test_sqlite_release_b_backfill_repair_and_assertions(tmp_path, monkeypatch):
    db_path = tmp_path / "release_b.db"
    url = f"sqlite:///{db_path}"
    _build_pre_b(url)
    _seed_fixture(url)

    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _alembic_config()
    command.stamp(cfg, PREVIOUS_HEAD)

    eng = create_engine(url)
    bundle = mod.SQL_SQLITE

    # The two populations are disjoint BY CONSTRUCTION (version IS NULL OR < 1
    # versus version = 1 AND value IS NOT NULL), so the repair can never undo the
    # backfill's own work.
    with eng.connect() as conn:
        backfill_pop = {
            r[0]
            for r in conn.execute(
                text(f"SELECT id FROM game_sessions WHERE {mod.POPULATION_PREDICATE_SQL}")
            )
        }
        repair_pop = {
            r[0]
            for r in conn.execute(
                text(
                    f"SELECT id FROM game_sessions WHERE {mod.REPAIR_PREDICATE_SQL} "
                    f"AND id IN ({mod.PLY_DETECTOR_SQL})"
                )
            )
        }
    assert backfill_pop & repair_pop == set()
    assert repair_pop == {BROKEN_STAMPED}
    assert HIDDEN_DRILL not in backfill_pop
    assert {MALFORMED, BROKEN_UNSTAMPED, INCOMPLETE, CONVERTED} <= backfill_pop
    assert CLEAN_STAMPED not in backfill_pop

    command.upgrade(cfg, REVISION)

    with eng.connect() as conn:
        for sid in VISIBLE_COMPLETE:
            assert _cached(conn, sid) == (INTACT_ACCURACY, 1)
        assert _cached(conn, CONVERTED) == (INTACT_ACCURACY, 1)
        # Incomplete: value stays NULL, but the row IS stamped — that is what
        # distinguishes unavailable data from work the migration never attempted.
        assert _cached(conn, INCOMPLETE) == (None, 1)
        # ended_at IS NULL and still backfilled: the predicate never touches it.
        assert _cached(conn, MALFORMED) == (INTACT_ACCURACY, 1)
        # Arm 2 for the unstamped broken row. Together with the unguarded arm
        # above — and ONLY together — this proves the frozen validator ran INSIDE
        # the migration.
        assert _cached(conn, BROKEN_UNSTAMPED) == (None, 1)
        # Arm 2 for the already-stamped broken row: reached only by the repair,
        # whose candidate temp table, clear, populate, keyset select and
        # per-session re-read all execute here on SQLite.
        assert _cached(conn, BROKEN_STAMPED) == (None, 1)
        # The repair only ever nulls a broken row; a clean stamped row is left
        # exactly as it was.
        assert _cached(conn, CLEAN_STAMPED) == (42, 1)
        # Hidden drills stay wholly unstamped.
        assert _cached(conn, HIDDEN_DRILL) == (None, None)

        # Both fail-closed assertions pass after a clean run.
        assert conn.execute(text(bundle.coverage_assert)).scalar() == 0
        assert conn.execute(text(bundle.soundness_assert)).scalar() == 0
        # And the population is empty, so nothing is re-selected on rerun.
        assert conn.execute(text(bundle.backfill_remaining)).scalar() == 0

    # The Release-A CHECK survived (on SQLite it was created validated, so B
    # skips VALIDATE — but the constraint must still be there or the assertion
    # would be vacuous).
    with eng.connect() as conn:
        gs_sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='game_sessions'")
        ).scalar()
        assert "ck_game_sessions_player_accuracy" in gs_sql

    # Idempotent: at head, `upgrade head` is a no-op with values unchanged.
    before = _snapshot(eng)
    command.upgrade(cfg, "head")
    assert _snapshot(eng) == before

    # Idempotent across both PHASES too, not just across the Alembic no-op:
    # re-running the phases directly changes nothing.
    with eng.begin() as conn:
        mod._run_backfill(conn, bundle, 10)
        mod._run_repair(conn, bundle)
        mod._assert_fail_closed(conn, bundle)
    assert _snapshot(eng) == before

    eng.dispose()


def _snapshot(eng) -> list:
    with eng.connect() as conn:
        return conn.execute(
            text(
                "SELECT id, player_accuracy, player_accuracy_algo_version "
                "FROM game_sessions ORDER BY id"
            )
        ).all()


def test_sqlite_paging_is_keyset_and_survives_a_batch_size_of_one(tmp_path, monkeypatch):
    """Updated rows LEAVE the stale predicate, which is exactly why OFFSET would
    skip remaining rows. A batch size of 1 is the shape that exposes it."""
    db_path = tmp_path / "release_b_paging.db"
    url = f"sqlite:///{db_path}"
    _build_pre_b(url)
    _seed_fixture(url)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv(mod.ENV_BATCH, "1")
    cfg = _alembic_config()
    command.stamp(cfg, PREVIOUS_HEAD)
    command.upgrade(cfg, REVISION)

    eng = create_engine(url)
    with eng.connect() as conn:
        assert conn.execute(text(mod.SQL_SQLITE.coverage_assert)).scalar() == 0
        for sid in VISIBLE_COMPLETE:
            assert _cached(conn, sid) == (INTACT_ACCURACY, 1)
    eng.dispose()


def test_coverage_assertion_raises_on_an_unstamped_visible_row(tmp_path):
    """Dirty the coverage population and the assertion must refuse to let cache-
    only reads serve.

    The row is inserted AFTER the backfill converged — i.e. it models a writer
    that created an ended-visible session mid-run, which is the case a count
    taken before the phases could never catch.
    """
    url = f"sqlite:///{tmp_path / 'coverage_dirty.db'}"
    _build_pre_b(url)
    _seed_fixture(url)
    eng = create_engine(url)
    bundle = mod.SQL_SQLITE
    with eng.begin() as conn:
        mod._run_backfill(conn, bundle, 500)
        mod._run_repair(conn, bundle)
        mod._assert_fail_closed(conn, bundle)  # clean
        _seed_session(conn, "s-late-arrival", plies=INTACT_PLIES)
        with pytest.raises(mod.MigrationError, match="coverage"):
            mod._assert_fail_closed(conn, bundle)
    eng.dispose()


def test_soundness_assertion_raises_on_a_stamped_broken_row(tmp_path):
    """The coverage assertion passes whether or not the repair ran — a repaired
    row is still version 1. Only the soundness assertion checks the VALUE.

    Re-dirtying after a clean run models the case the assertion exists for: a
    writer produced a fresh non-mainline grid WITH a non-NULL accuracy DURING the
    migration, which the repair phase's stale candidate set cannot see. That is a
    live-guard bug, not a migration bug — and either way, do not serve.
    """
    url = f"sqlite:///{tmp_path / 'soundness_dirty.db'}"
    _build_pre_b(url)
    _seed_fixture(url)
    eng = create_engine(url)
    bundle = mod.SQL_SQLITE
    with eng.begin() as conn:
        mod._run_backfill(conn, bundle, 500)
        mod._run_repair(conn, bundle)
        mod._assert_fail_closed(conn, bundle)  # clean
        conn.execute(
            text(
                "UPDATE game_sessions SET player_accuracy = :a, "
                "player_accuracy_algo_version = 1 WHERE id = :i"
            ).bindparams(a=BROKEN_UNGUARDED_ACCURACY, i=BROKEN_STAMPED)
        )
        # Coverage still passes — that is precisely why a second assertion exists.
        assert conn.execute(text(bundle.coverage_assert)).scalar() == 0
        with pytest.raises(mod.MigrationError, match="soundness"):
            mod._assert_fail_closed(conn, bundle)
    eng.dispose()


def test_sqlite_failed_assertion_rolls_the_whole_run_back(tmp_path, monkeypatch):
    """Fail closed AND leave nothing behind.

    The extra row is stamped at a version the backfill's population predicate
    cannot reach (``version IS NULL OR version < 1`` excludes 2) but the coverage
    assertion counts (``IS DISTINCT FROM 1``). So a REAL, unpatched run reaches
    the assertion with an uncovered row, raises, and rolls back — proving both
    the fail-closed exit and that atomic mode leaves the database exactly as it
    found it.
    """
    url = f"sqlite:///{tmp_path / 'release_b_rollback.db'}"
    _build_pre_b(url)
    _seed_fixture(url)
    eng = create_engine(url)
    with eng.begin() as conn:
        _seed_session(conn, "s-future-version", plies=INTACT_PLIES, accuracy=50, version=2)

    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _alembic_config()
    command.stamp(cfg, PREVIOUS_HEAD)
    with pytest.raises(RuntimeError, match="phase=assert coverage"):
        command.upgrade(cfg, REVISION)

    with eng.connect() as conn:
        # Nothing stamped, and the version pointer did not advance, so a rerun
        # redoes the whole revision.
        assert _cached(conn, VISIBLE_COMPLETE[0]) == (None, None)
        assert _cached(conn, BROKEN_STAMPED) == (BROKEN_UNGUARDED_ACCURACY, 1)
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == (
            PREVIOUS_HEAD
        )
    eng.dispose()


# ---------------------------------------------------------------------------
# Three-way detector parity.
# ---------------------------------------------------------------------------

# (name, plies, expected_intact)
DETECTOR_CASES = [
    ("well_formed", INTACT_PLIES, True),
    ("gap", [(1, "white", 1), (1, "black", 2), (3, "white", 3), (3, "black", 4)], False),
    ("white_white_adjacency", BROKEN_PLIES, False),
    (
        # The grid simply CONTINUES past the PGN's last ply. Coordinate-contiguous
        # surplus validates as intact — frozen v1 rejects only n < expected
        # (accuracy_v1.py:152-153), and tightening that to `==` is an accuracy-v2
        # decision, not a detector one.
        "contiguous_surplus",
        INTACT_PLIES + [(4, "white", 5), (4, "black", 6)],
        True,
    ),
    ("empty", [], True),
]


@pytest.mark.parametrize("name,plies,intact", DETECTOR_CASES, ids=[c[0] for c in DETECTOR_CASES])
def test_sqlite_three_way_detector_parity(tmp_path, name, plies, intact):
    """``PLY_DETECTOR_SQL``, ``PLY_DETECTOR_ONE_SQLITE`` and
    ``ply_coordinates_intact`` must agree on every seeded row set.

    The session-scoped form is not a convenience copy: the repair's safety rests
    on it, since it is what the per-session re-read consults immediately before
    nulling a served value. A drift between it and the set-wide form would mean
    the repair selects a candidate it then declines to inspect correctly.
    """
    url = f"sqlite:///{tmp_path / f'detector_{name}.db'}"
    _build_pre_b(url)
    eng = create_engine(url)
    sid = f"s-{name}"
    with eng.begin() as conn:
        _seed_session(conn, sid, plies=plies)

    with eng.connect() as conn:
        set_wide = {
            r[0] for r in conn.execute(text(f"SELECT session_id FROM ({mod.PLY_DETECTOR_SQL})"))
        }
        scoped = conn.execute(
            text(mod.SQL_SQLITE.ply_detector_one).bindparams(sid=sid)
        ).scalar()
    eng.dispose()

    python_intact = ply_coordinates_intact([_Row(*p) for p in plies])
    assert python_intact is intact
    assert (sid not in set_wide) is intact
    assert (scoped == 0) is intact
