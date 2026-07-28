"""Target fence for the Phase 3 helpers, and the semantic fingerprint they record.

Two unrelated jobs, together here only because all three Phase 3 scripts need both
and none of them should own either.

TARGET FENCE. `size_accuracy_backfill.py` prints `current_database()` before doing
anything and refuses to run without `--confirm-mutates`, because it rewrites the
rows it measures. The Phase 3 helpers are strictly *more* destructive — one drops
and recreates a database outright, one rewrites both populations, one installs a
trigger and cancels backends — so they inherit the same fence rather than a weaker
one. Three checks, and the naming rule is the one that does the real work: a typo
in a hand-typed database name is the realistic failure, not a deliberate misfire at
production, and `--confirm-mutates` alone would be typed straight past it.

SEMANTIC FINGERPRINT. A qualification run is only evidence about the code it ran
against, and the runbook says so — "any later edit to the runner or the constants
invalidates all five". A raw content digest cannot carry that claim: it moves when a
docstring is reflowed, so enforcing it would demand a re-run for edits that cannot
affect a single measurement, and an enforcement everyone learns to override is worse
than none. What has to move is *behaviour*. So the fingerprint is taken over the
parsed AST with docstrings stripped — comments never reach the AST at all, and
`ast.dump` without attributes drops line and column numbers — leaving a hash that is
stable under reformatting and prose, and that changes on any edit to a statement, a
literal or an expression. Both are recorded: `sha256` says which bytes produced the
numbers, `fingerprint` is what the gate compares.
"""

from __future__ import annotations

import ast
import hashlib
import re
import sys
from pathlib import Path

#: Disposable-copy naming for Phase 3. Narrow on purpose: this is the fence that
#: catches a typo, and a pattern loose enough to admit the application database
#: would catch nothing. Anchored, lowercase, and `\w`-free so the name is safe to
#: interpolate into DDL that cannot take a bind parameter.
#:
#: `\Z` AND `fullmatch`, never `$` with `match`. Python's `$` also matches
#: immediately BEFORE a final newline, so `^gr_p3[a-z0-9_]*$` accepts
#: `"gr_p3x\n"` — which is not injection, but it is a name carrying whitespace
#: through a fence that promises none, and the damage is asymmetric: the target is
#: dropped by the first statement and a newline-suffixed TEMPLATE then fails the
#: second, leaving no database at all. Either anchor alone is sufficient; both are
#: here because the failure is silent and the cost is nothing.
DISPOSABLE_RE = re.compile(r"\Agr_p3[a-z0-9_]*\Z")

#: Names that match the pattern but are fixtures other runs depend on. A template
#: is read by every `CREATE DATABASE ... TEMPLATE` and is expensive to rebuild.
PROTECTED_SUFFIXES = ("_base", "_template")

#: Templates get their OWN rule, and it is the exact inverse of the target's: a
#: template MUST carry a fixture suffix, where a target must not. Both halves of
#: `CREATE DATABASE <target> TEMPLATE <template>` are interpolated identifiers, so
#: validating only the target leaves the other half unfenced — a template is just
#: as capable of carrying a quote into DDL, and one that names a live database is a
#: `CREATE DATABASE` against something in use. Anchored and `\w`-free for the same
#: reason the target pattern is.
TEMPLATE_RE = re.compile(r"\Agr_[a-z0-9_]*(_base|_template)\Z")


def check_disposable_name(name: str, *, template: str | None = None) -> None:
    """Refuse anything that is not an obviously disposable Phase 3 copy."""
    if not DISPOSABLE_RE.fullmatch(name):
        raise SystemExit(
            f"refusing to touch {name!r}: Phase 3 copies must match "
            f"{DISPOSABLE_RE.pattern!r} (e.g. gr_p3c_batch). This script DROPS and "
            "rewrites its target."
        )
    if name.endswith(PROTECTED_SUFFIXES):
        raise SystemExit(
            f"refusing to touch {name!r}: names ending in {PROTECTED_SUFFIXES} are "
            "fixtures other runs are restored from, not disposable copies."
        )
    if template is not None and name == template:
        raise SystemExit(
            f"refusing to touch {name!r}: it is the template being restored from. "
            "A target that is its own template destroys the fixture."
        )


def check_template_name(name: str) -> None:
    """Refuse a template that is not a safe identifier naming a fixture.

    Called BEFORE the target is dropped, because the drop and the create are two
    statements and a template rejected after the first one leaves the operator with
    no database at all.
    """
    if not TEMPLATE_RE.fullmatch(name):
        raise SystemExit(
            f"refusing to clone from {name!r}: a template must match "
            f"{TEMPLATE_RE.pattern!r} — a fixture name ending in {PROTECTED_SUFFIXES} "
            "(e.g. gr_p3_base). Cloning from a working database is a CREATE DATABASE "
            "against something that may be in use, and an unchecked name reaches DDL "
            "that cannot take a bind parameter."
        )


def check_pair(target: str, template: str) -> None:
    """Both identifiers in `CREATE DATABASE <target> TEMPLATE <template>`."""
    check_disposable_name(target, template=template)
    check_template_name(template)


def confirm_mutates(conn, *, confirmed: bool, what: str, expect: str | None = None) -> str:
    """Print the database actually connected to, then require explicit consent.

    Reads `current_database()` rather than trusting the URL, because the fence has
    to be in front of what the connection *resolved* to — a URL with no database
    path falls back to the OS user's name, and that is exactly the case where the
    operator most needs to be told where they are.

    Returns that resolved name, and callers RECORD what is returned rather than
    what they were passed. `--url` may point at a database other than the positional
    argument; both can be disposable and safe, so nothing here refuses it, and an
    artifact that then names the positional identifies a database it never measured.
    Pass `expect` to also refuse the divergence outright, which is what every Phase 3
    helper does: the positional is what the runbook command line and every message
    names, so a URL resolving elsewhere is a mistake, not a use case.
    """
    from sqlalchemy import text

    dbname = str(conn.execute(text("SELECT current_database()")).scalar())
    server = conn.execute(text("SELECT version()")).scalar()
    print(f"database: {dbname}\nserver:   {server}", file=sys.stderr)
    check_disposable_name(dbname)
    if expect is not None and dbname != expect:
        raise SystemExit(
            f"refusing to run: the connection resolved to {dbname!r} but the target "
            f"named on the command line is {expect!r}. Whichever is right, the record "
            "of this run would name the other."
        )
    if not confirmed:
        raise SystemExit(
            f"refusing to run against {dbname!r} without --confirm-mutates: {what}"
        )
    return dbname


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return tree


def semantic_fingerprint(path: Path) -> str:
    """A hash of what the file *does*, blind to comments, docstrings and layout."""
    tree = _strip_docstrings(ast.parse(path.read_text()))
    return hashlib.sha256(ast.dump(tree).encode()).hexdigest()


def content_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def symbol_fingerprint(path: Path, names: tuple[str, ...]) -> dict[str, str]:
    """`semantic_fingerprint` for NAMED top-level definitions instead of a file.

    For binding one behaviour out of a module that does many things. The sizing
    harness is ~2,500 lines of Phase 1 and Phase 2 machinery of which only the
    synthesis functions decide what a Phase 3 fixture *is*; fingerprinting the whole
    file would expire every qualification run whenever `derive` changed, and a gate
    that fires on unrelated edits is a gate people learn to override.

    RAISES on a name that is not there. A fingerprint gate that silently covers
    nothing is worse than no gate: renaming the function would turn the binding off
    and leave the artifact still carrying a `frozen_symbols` block that looks like
    coverage.
    """
    tree = ast.parse(path.read_text())
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        else:
            continue
        if name in names:
            found[name] = hashlib.sha256(ast.dump(_strip_docstrings(node)).encode()).hexdigest()
    missing = sorted(set(names) - set(found))
    if missing:
        raise SystemExit(
            f"{path.name}: cannot fingerprint {missing} — not defined at module level. "
            "Renaming or moving a bound symbol turns its binding off silently, so this "
            "refuses rather than recording partial coverage."
        )
    return found


#: WHAT THE FIXTURE IS, read off the database rather than assumed from the script
#: that built it. Counts and relation sizes do not identify a fixture: the harness
#: documents that taking repair candidates by `ORDER BY md5(id)` instead of
#: `ORDER BY id` changes every scan measurement while still producing exactly 1,000
#: candidates and deleting exactly 1,000 plies — same counts, same dimensions,
#: different rows, different plans. Digesting the accuracy-bearing columns observes
#: WHICH rows, so the selection is evidence in the artifact instead of a property of
#: whatever the seeding script happened to do that day.
#:
#: The column lists are the migration's EXACT inputs — the two `SELECT` lists it
#: loads rows with, plus the columns its population predicates test and the two it
#: writes — and nothing else. Both directions of that matter and both were wrong
#: once here:
#:
#: TOO NARROW is a digest that agrees while the algorithm's input differs.
#: `player_color` decides which side's plies are scored and `eval_cp` / `eval_mate`
#: ARE the scores; a fixture with different eval density or flipped colours computes
#: different accuracies at a different cost, and a digest omitting them calls it the
#: same fixture.
#:
#: TOO BROAD is a digest that moves when nothing the migration reads has changed.
#: `move_san` was in this list and is read by nothing in the revision; a `SELECT *`
#: digest is the same mistake at full size, moving whenever an unrelated migration
#: adds a column to `game_sessions`. `test_the_fixture_digest_binds_the_migrations_
#: own_input_columns` checks these lists against the revision's own statement text,
#: so the binding tracks the migration rather than someone's reading of it.
SESSION_INPUT_COLUMNS = (
    # Loaded per batch: SELECT id, player_color, pgn.
    "player_color",
    "pgn",
    # Tested by POPULATION_PREDICATE_SQL / VISIBLE_ENDED_SQL, and written by the run.
    "status",
    "session_mode",
    "drill_state",
    "player_accuracy",
    "player_accuracy_algo_version",
)

#: Loaded per batch: SELECT session_id, move_number, color, eval_cp, eval_mate.
#: `move_number` and `color` are also the whole of the ply-coordinate detector.
MOVE_INPUT_COLUMNS = ("move_number", "color", "eval_cp", "eval_mate")

#: Digested through `md5()` rather than inline: whole PGN text, hundreds of bytes a
#: row, and only its content matters here.
_HASHED_COLUMNS = ("pgn",)


def _digest_sql(table: str, key: str, columns: tuple[str, ...]) -> str:
    """Row count and an order-independent content digest over `key` + `columns`."""
    parts = [f"{key}::text"] + [
        f"md5(coalesce({c}::text, ''))" if c in _HASHED_COLUMNS else f"coalesce({c}::text, '')"
        for c in columns
    ]
    expr = " || '|' || ".join(parts)
    return (
        "SELECT count(*) AS n, md5(coalesce(string_agg(t, ',' ORDER BY t), '')) AS digest "
        f"FROM (SELECT {expr} AS t FROM {table}) s"
    )


_SESSIONS_DIGEST_SQL = _digest_sql("game_sessions", "id", SESSION_INPUT_COLUMNS)
_MOVES_DIGEST_SQL = _digest_sql("session_moves", "session_id", MOVE_INPUT_COLUMNS)

#: One row, written by `phase3_prepare.py` onto each copy it makes. Names the
#: template and carries the template's OWN content digests, so every artifact taken
#: on that copy can say which base data it measured — a claim no post-seed read can
#: make, because synthesis deletes plies and rewrites accuracy columns.
PROVENANCE_TABLE = "_ghostreplay_phase3_provenance"


def fixture_digest(conn) -> dict[str, object]:
    """Row counts and content digests of the two accuracy-bearing relations."""
    from sqlalchemy import text

    sessions = conn.execute(text(_SESSIONS_DIGEST_SQL)).mappings().one()
    moves = conn.execute(text(_MOVES_DIGEST_SQL)).mappings().one()
    return {
        "sessions_rows": int(sessions["n"]),
        "sessions_digest": sessions["digest"],
        "moves_rows": int(moves["n"]),
        "moves_digest": moves["digest"],
    }


def stamp_provenance(conn, *, template: str, template_conn) -> dict[str, object]:
    """Record what this copy was cloned from, ON the copy, before anything seeds it.

    VERIFIES the label rather than trusting it. `template` arrives as a string from
    whoever prepared the copy, and a stamp that only repeats it is a claim about the
    clone, not a measurement of it: a preparer that cloned a different base while
    passing `gr_p3_base` would produce artifacts that agree with each other on a
    template name none of them was actually taken from. So the template is read and
    digested too, and a mismatch refuses.
    """
    from sqlalchemy import text

    base = fixture_digest(conn)
    source = fixture_digest(template_conn)
    if base != source:
        raise SystemExit(
            f"refusing to stamp: this copy does not match {template!r}.\n"
            f"  copy:     {base}\n  template: {source}\n"
            "A fresh clone reproduces its template exactly, so a difference on the "
            "columns the migration reads — this digest is that projection, not the "
            "whole row — means the copy was made from something else, and every "
            "artifact taken on it would name a base it was not taken from."
        )
    conn.execute(text(f"DROP TABLE IF EXISTS {PROVENANCE_TABLE}"))
    conn.execute(
        text(
            f"""CREATE TABLE {PROVENANCE_TABLE} (
                   template text NOT NULL,
                   sessions_rows bigint NOT NULL, sessions_digest text NOT NULL,
                   moves_rows bigint NOT NULL, moves_digest text NOT NULL,
                   prepared_at timestamptz NOT NULL DEFAULT now())"""
        )
    )
    conn.execute(
        text(
            f"INSERT INTO {PROVENANCE_TABLE} "
            "(template, sessions_rows, sessions_digest, moves_rows, moves_digest) "
            "VALUES (:template, :sessions_rows, :sessions_digest, :moves_rows, :moves_digest)"
        ),
        {"template": template, **base},
    )
    return {"template": template, **base}


def read_provenance(conn) -> dict[str, object] | None:
    """What `phase3_prepare.py` stamped, or None on a copy it never touched."""
    from sqlalchemy import text

    present = conn.execute(
        text("SELECT to_regclass(:t)"), {"t": PROVENANCE_TABLE}
    ).scalar()
    if not present:
        return None
    row = conn.execute(
        text(
            f"SELECT template, sessions_rows, sessions_digest, moves_rows, moves_digest, "
            f"prepared_at FROM {PROVENANCE_TABLE}"
        )
    ).mappings().one()
    return {k: (str(v) if k == "prepared_at" else v) for k, v in row.items()}


if __name__ == "__main__":
    # `python -m scripts.phase3_fixture_guard check-name <target> <template>`, for
    # anything that wants the fence without importing it. BOTH identifiers, since
    # both are interpolated into `CREATE DATABASE <target> TEMPLATE <template>`.
    if len(sys.argv) != 4 or sys.argv[1] != "check-name":
        raise SystemExit("usage: python -m scripts.phase3_fixture_guard check-name <db> <template>")
    check_pair(sys.argv[2], sys.argv[3])
