"""Phase 3 fixture prep: one disposable copy, reconstructed to the state revision
`20260719_01` would meet on a from-scratch run.

    python scripts/phase3_prepare.py gr_p3a --confirm-mutates

THE TARGET IS DROPPED. `--confirm-mutates` is required, matching
`size_accuracy_backfill.py`'s fence, and both identifiers are checked before
anything is dropped — the target must look like a disposable Phase 3 copy, the
template like a fixture (`scripts/phase3_fixture_guard.py`). Before the drop and
not between the two statements: `DROP` and `CREATE` are separate, so a template
rejected after the drop leaves no database at all.

THIS IS PYTHON, AND IT USED TO BE SHELL. It chooses the template, decides that
stamping happens after the clone and before any seeding, and reconstructs the
pre-revision state — all of which decides what every Phase 3 artifact is evidence
about. A shell script cannot be part of the fingerprint set that makes those
artifacts expire (`ast.parse` has nothing to say about it), so the behaviour lives
here, where `phase3_cancellation_probe.FROZEN_FILES` can bind it like the rest.

Reconstructs the pre-`20260719_01` state exactly as `docs/release_b_runbook.md` §8
describes it: `alembic_version` back to `20260718_01` (the revision's own
`down_revision`) and the CHECK dropped and re-added `NOT VALID` with `20260709_01`'s
condition verbatim, so the run performs the real `NOT VALID` -> validated transition
rather than a no-op against an already-validated constraint.

The template is a restore of a production dump (runbook §10) and is never written
to — this opens it read-only, to verify the clone against it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, text  # noqa: E402

from scripts.phase3_fixture_guard import check_pair, stamp_provenance  # noqa: E402

#: `20260719_01`'s `down_revision`. Asserted against the revision at run time rather
#: than trusted, so a graph change fails here instead of silently preparing a copy
#: the migration will refuse.
PREVIOUS_REVISION = "20260718_01"

#: `20260709_01`'s condition, VERBATIM. Re-added `NOT VALID` so the run performs the
#: real validation rather than a no-op.
CHECK_NAME = "ck_game_sessions_player_accuracy"
CHECK_CONDITION = "player_accuracy IS NULL OR (player_accuracy >= 0 AND player_accuracy <= 100)"

REPORT_SQL = """
SELECT 'alembic_version=' || version_num FROM alembic_version
UNION ALL SELECT 'convalidated=' || convalidated::text FROM pg_constraint
  WHERE conname = :check_name
UNION ALL SELECT 'game_sessions=' || count(*)::text FROM game_sessions
UNION ALL SELECT 'session_moves=' || count(*)::text FROM session_moves
UNION ALL SELECT 'ended_visible=' || count(*)::text FROM game_sessions
  WHERE status = 'ended' AND (session_mode = 'normal' OR drill_state = 'converted')
UNION ALL SELECT 'sessions_bytes=' || pg_total_relation_size('game_sessions')::text
UNION ALL SELECT 'moves_bytes=' || pg_total_relation_size('session_moves')::text
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("database")
    ap.add_argument("--template", default="gr_p3_base")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", default="5432")
    ap.add_argument("--confirm-mutates", action="store_true")
    args = ap.parse_args()

    target, template = args.database, args.template
    # BOTH identifiers, before anything is dropped.
    check_pair(target, template)
    # And the state we are about to reconstruct is the one the revision descends
    # from — read off the revision rather than trusted, so a graph change fails here
    # instead of leaving a copy the migration will refuse.
    from scripts.phase3_cancellation_probe import REVISION, revision_module

    down = revision_module().down_revision
    if down != PREVIOUS_REVISION:
        raise SystemExit(
            f"{REVISION} now descends from {down!r}, not {PREVIOUS_REVISION!r}: this "
            "script would prepare a state the migration does not start from."
        )
    if not args.confirm_mutates:
        raise SystemExit(
            f"refusing to run against {target!r} without --confirm-mutates: this script "
            f"DROPS the target database and recreates it from {template!r}."
        )

    def url(db: str) -> str:
        return f"postgresql+psycopg://{args.host}:{args.port}/{db}"

    print(f"target:   {target}\ntemplate: {template}", file=sys.stderr)

    # Autocommit: CREATE DATABASE and DROP DATABASE cannot run inside a transaction.
    maint = create_engine(url("postgres"), isolation_level="AUTOCOMMIT")
    with maint.connect() as conn:
        # Safe to interpolate: the guard above restricts the target to
        # \Agr_p3[a-z0-9_]*\Z and the template to \Agr_[a-z0-9_]*(_base|_template)\Z,
        # so neither holds a quote, space or semicolon — and neither can, since both
        # rules are applied with `fullmatch`. Quoted anyway, so each identifier is
        # taken literally rather than folded.
        conn.execute(text(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{target}" TEMPLATE "{template}"'))
    maint.dispose()

    engine = create_engine(url(target))
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE alembic_version SET version_num = :v"), {"v": PREVIOUS_REVISION}
        )
        conn.execute(text(f"ALTER TABLE game_sessions DROP CONSTRAINT {CHECK_NAME}"))
        conn.execute(
            text(
                f"ALTER TABLE game_sessions ADD CONSTRAINT {CHECK_NAME} "
                f"CHECK ({CHECK_CONDITION}) NOT VALID"
            )
        )

    # PROVENANCE, after the clone and before any seeding, so the digests are of the
    # template's content. The template is opened read-only for exactly as long as it
    # takes to verify the clone against it: a stamp that only repeats the name it was
    # given is a claim about the copy, not a measurement of it.
    template_engine = create_engine(url(template))
    with engine.begin() as conn, template_engine.connect() as tconn:
        base = stamp_provenance(conn, template=template, template_conn=tconn)
    template_engine.dispose()
    print(
        f"provenance: template={base['template']} "
        f"sessions={base['sessions_digest'][:12]} moves={base['moves_digest'][:12]}",
        file=sys.stderr,
    )

    with engine.connect() as conn:
        for (line,) in conn.execute(text(REPORT_SQL), {"check_name": CHECK_NAME}):
            print(line)
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
