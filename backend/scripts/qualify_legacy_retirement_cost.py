#!/usr/bin/env python3
"""§2.8 — C4's A_old/A_new control runner for g-score-store-qualify.

The shipped LEGACY path now retires the previous snapshot atomically under the
publication lock, and that is production's CURRENT writer whether or not
activation happens. A regression there blocks the release even though it is not
the new format.

Deliberately STANDALONE — it does not import ``qualify_opening_score_storage``,
because that file does not exist at ``6699678`` and the same script file is
invoked by absolute path against BOTH worktrees. It imports only names present
at both commits: ``LegacyAdapter`` and ``replay_objects``. At ``6699678``
``recompute_opening_scores`` has no ``storage_format`` kwarg and ``ScoreHandle``,
``read_group`` and ``latest_batch_view`` do not exist, so nothing newer may be
referenced here. THE GUARDS ARE INLINED FOR THE SAME REASON: §2.1's guard
functions cannot be imported at ``6699678``, and a control runner without them
would be the one unguarded connection path in the bead.

Run with cwd set to the target worktree's ``backend`` so ``app`` and ``scripts``
resolve to THAT tree. The database URL travels by ENVIRONMENT, never as an
argument (§2.1) — a connection string in argv puts the password in ``ps``. The
target database is created by the HARNESS, under its admin guard, never by a
hand-rolled ``createdb``::

    python -m scripts.qualify_opening_score_storage create-database \\
        --name gr_score_qual_<run>_c4_new

    # The URL is never typed and never inlined in front of the command: it is
    # read inside a subshell from the mode-600 file the operator keeps in the
    # private store, exported there, and gone when the subshell exits.
    (
      set +o history
      export GHOSTREPLAY_STORAGE_QUAL_DATABASE_URL="$(
          cat ~/.ghostreplay-private/score-store-qualify/c4_new.url)"
      cd <worktree>/backend
      .venv/bin/python /abs/path/scripts/qualify_legacy_retirement_cost.py \\
          --payloads ~/.ghostreplay-private/score-store-qualify/capture.pickle \\
          --label A_new --output <report>.json
    )

ONE FRESH DATABASE PER LABEL. The schema comes from ``LegacyAdapter.create``,
not from alembic — the two commits do not share a revision head — and
``create_all`` only creates what is MISSING, so a database reused across the two
labels would hand A_old whatever columns A_new's models had already created.
The runner refuses a database that is not empty on entry.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import platform
import re
from pathlib import Path
import subprocess
import time

# NOTHING THAT IMPORTS ``app`` IS IMPORTED AT MODULE TOP. ``LegacyAdapter``
# pulls in ``app.db``, which binds its engine at import (app/db.py:49), so
# importing it here bound the control runner's application engine to whatever
# the shell happened to carry — before a single guard had run. The adapter uses
# the explicit engine this file builds, so the exposure was bounded, but it is
# the same rule §2.1 follows and the same rule is followed here: guard, set
# ``DATABASE_URL`` from the guarded value, then import. The imports live in
# ``main``.

REQUIREMENTS = ("requirements.txt", "requirements-dev.txt")
WRITER_PARENT = "6699678"

QUAL_DATABASE_ENV = "GHOSTREPLAY_STORAGE_QUAL_DATABASE_URL"
CLUSTER_NAME = "ghostreplay-score-storage-qual"
SPIKE_CLUSTER_NAME = "ghostreplay-score-storage-spike"
QUAL_CLUSTER_ENV = "GHOSTREPLAY_STORAGE_QUAL_CLUSTER"
APPLICATION_NAME = "ghostreplay-storage-qualification"
CELL_DATABASE_PATTERN = r"gr_score_qual_[a-z0-9_]+"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

# The FULL application fall-through, both spellings (app/database_url.py:25-29,
# 51-67). Copied rather than imported, because §2.1's module is absent at the
# A_old commit; the list is asserted against the harness's in §2.6 so the two
# cannot drift apart.
#
# REFUSED_ENV_NAMES is "must never be inherited". `DATABASE_URL` is NOT in it,
# because this runner sets it itself before importing the adapters (below); it
# is in INHERITED_URL_ENV_NAMES instead, which is the loopback check, and that
# is where it was missing until a real shell exposed it.
REFUSED_ENV_NAMES = (
    "DATABASE_PRIVATE_URL",
    "DATABASE_PUBLIC_URL",
    "PGHOST",
    "POSTGRES_HOST",
    "PGDATABASE",
    "POSTGRES_DB",
    "PGUSER",
    "POSTGRES_USER",
    "PGPASSWORD",
    "POSTGRES_PASSWORD",
    "PGPORT",
    "POSTGRES_PORT",
)
INHERITED_URL_ENV_NAMES = (
    "DATABASE_URL",
    "DATABASE_PRIVATE_URL",
    "DATABASE_PUBLIC_URL",
)
INHERITED_HOST_ENV_NAMES = ("PGHOST", "POSTGRES_HOST")


class ControlRefusal(RuntimeError):
    """A guard refused. Never downgraded to a warning and never caught inside."""


def _loopback_host(host: str | None) -> bool:
    return (host or "") in LOOPBACK_HOSTS


def refuse_inherited_connection_environment(environ=None) -> None:
    """Same rule as §2.1's, inlined: only an explicitly loopback value passes."""
    from sqlalchemy.engine import make_url

    environ = os.environ if environ is None else environ
    from sqlalchemy.exc import ArgumentError

    for name in INHERITED_URL_ENV_NAMES:
        raw = environ.get(name)
        if not raw:
            continue
        try:
            host = make_url(raw).host
        except ArgumentError as exc:
            raise ControlRefusal(
                f"{name} is not a parseable URL and cannot be proven loopback"
            ) from exc
        if not _loopback_host(host):
            raise ControlRefusal(f"{name} names a non-loopback database; unset it")
    for name in INHERITED_HOST_ENV_NAMES:
        raw = environ.get(name)
        if raw and not _loopback_host(raw):
            raise ControlRefusal(f"{name}={raw!r} is not loopback; unset it")
    for name in REFUSED_ENV_NAMES:
        if name in INHERITED_URL_ENV_NAMES + INHERITED_HOST_ENV_NAMES:
            continue
        if environ.get(name):
            raise ControlRefusal(f"{name} is inherited and cannot be proven loopback")


def guard_database_url(environ=None):
    """Loopback PostgreSQL, opt-in variable, ``gr_score_qual_*`` database."""
    from sqlalchemy.engine import make_url

    environ = os.environ if environ is None else environ
    refuse_inherited_connection_environment(environ)
    raw = environ.get(QUAL_DATABASE_ENV)
    if not raw:
        raise ControlRefusal(f"{QUAL_DATABASE_ENV} must explicitly name the target")
    url = make_url(raw)
    if not url.drivername.startswith("postgresql"):
        raise ControlRefusal(f"{QUAL_DATABASE_ENV} must use PostgreSQL")
    if not _loopback_host(url.host):
        raise ControlRefusal(
            f"{QUAL_DATABASE_ENV} must resolve to loopback TCP, not {url.host!r}"
        )
    if url.query:
        # The harness refuses these for a reason that applies here too: a URL
        # query can carry `host=`, which silently overrides the host the
        # loopback check just proved.
        raise ControlRefusal(f"{QUAL_DATABASE_ENV} must carry no URL query overrides")
    if not re.fullmatch(CELL_DATABASE_PATTERN, url.database or ""):
        raise ControlRefusal(
            f"{url.database!r} does not match {CELL_DATABASE_PATTERN}"
        )
    # A bare ``postgresql://`` selects psycopg2, which is not installed in
    # either worktree's venv; the harness normalises the driver and so does
    # this, or the control runner fails at connect time on a correct URL.
    return url.set(drivername="postgresql+psycopg")


def assert_database_empty(engine) -> None:
    """Empty on entry, because the schema comes from ``create_all``.

    ``create_all`` creates what is MISSING and alters nothing, so running A_old
    in a database A_new has already populated would silently measure A_old's
    writer against A_new's columns. One fresh ``create-database`` per label.
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        relations = (
            conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace WHERE c.relkind IN ('r','p') "
                    "AND n.nspname NOT IN ('pg_catalog','information_schema') "
                    "AND n.nspname NOT LIKE 'pg_toast%' ORDER BY c.relname"
                )
            )
            .scalars()
            .all()
        )
    if relations:
        raise ControlRefusal(
            f"the control database already holds {list(relations)}; each label "
            "needs its own fresh database, because LegacyAdapter.create only "
            "creates what is missing"
        )


# INLINED FROM §2.1, for the reason the module docstring gives: the harness
# does not exist at ``6699678`` and this file runs against both worktrees.
CAPTURE_VERSION = 1
PRIVATE_STORE = Path.home() / ".ghostreplay-private"


def assert_capture_admissible(path: Path, capture: dict) -> dict:
    """The same three questions §2.1 asks before any cell replays a capture.

    C4 loaded the payload with a bare ``pickle.load`` and asked none of them, so
    the one runner that measures the SHIPPED legacy writer was also the one that
    would happily replay a capture whose §1.3 closure check had failed — that
    is, a capture whose final payload did NOT equal a plain recompute on an
    untouched clone, meaning the reveal mechanism perturbed evidence and every
    number below it is a fiction. It would also load a production-derived
    pickle from anywhere on disk, which is the private-store rule.

    A SKIPPED closure check is an explicit operator choice and is permitted; it
    travels onto the report so the evaluator can record the coverage gap. A
    synthetic capture has no reveal mechanism and nothing to check.
    """
    if capture.get("version") != CAPTURE_VERSION:
        raise ControlRefusal(
            f"capture version {capture.get('version')!r} is not {CAPTURE_VERSION}"
        )
    provenance = capture.get("provenance") or {}
    synthetic = bool(provenance.get("synthetic_only"))
    if not synthetic:
        try:
            resolved = path.resolve()
            resolved.relative_to(PRIVATE_STORE.resolve())
        except (ValueError, OSError) as exc:
            raise ControlRefusal(
                f"{path} is a production-derived capture outside "
                f"{PRIVATE_STORE}; captured payloads never leave the private "
                "store"
            ) from exc
    closure = capture.get("closure_check")
    if synthetic:
        return {"closure_check": closure, "synthetic_only": True}
    if closure is None:
        raise ControlRefusal(
            "the capture carries no closure_check; §1.3 requires the final "
            "cutoff's payload to equal a plain recompute on an untouched clone "
            "before anything replays it"
        )
    if not closure.get("skipped") and closure.get("equal") is not True:
        raise ControlRefusal(
            "the capture's closure check did not pass "
            f"({closure.get('reason') or closure.get('failure') or closure}); "
            "the reveal mechanism perturbed evidence and the capture is invalid"
        )
    return {"closure_check": closure, "synthetic_only": False}


def assert_resolved_engine(url) -> None:
    """The RESOLVED engine, not the variable — §2.1's second line of defence.

    ``resolve_database_url`` falls through five names before a local default, so
    an assertion on ``DATABASE_URL`` alone proves nothing about what
    ``app.db.engine`` actually bound.
    """
    from app import db as app_db

    resolved = app_db.engine.url
    expected = (url.host, url.port, url.database, url.username)
    actual = (resolved.host, resolved.port, resolved.database, resolved.username)
    if actual != expected:
        raise ControlRefusal(
            f"app.db.engine resolved to {actual!r}, not the control target {expected!r}"
        )


def expected_cluster_name(environ=None) -> str:
    environ = os.environ if environ is None else environ
    name = environ.get(QUAL_CLUSTER_ENV) or CLUSTER_NAME
    if name not in {CLUSTER_NAME, SPIKE_CLUSTER_NAME}:
        raise ControlRefusal(f"{QUAL_CLUSTER_ENV}={name!r} is not a known cluster")
    return name


def assert_cluster_identity(engine) -> dict:
    """The same connection proves cluster AND database, never two connections."""
    from sqlalchemy import text

    expected = expected_cluster_name()
    with engine.connect() as conn:
        cluster = conn.execute(text("SHOW cluster_name")).scalar_one()
        if cluster != expected:
            raise ControlRefusal(
                f"cluster identifies as {cluster!r}, not {expected!r}"
            )
        database = conn.execute(text("SELECT current_database()")).scalar_one()
        if not re.fullmatch(CELL_DATABASE_PATTERN, database):
            raise ControlRefusal(f"connected to {database!r}, not a cell database")
        return {
            "cluster_name": cluster,
            "database": database,
            "server_version": conn.execute(text("SHOW server_version")).scalar_one(),
        }


def assert_shared_venv_still_valid(base: str = WRITER_PARENT) -> dict:
    """One venv serves both worktrees ONLY while the requirements diff is empty.

    The pathspec is TOP-LEVEL RELATIVE (``:/``). This runs with cwd set to a
    worktree's ``backend``, where the plain pathspec ``backend/requirements.txt``
    means ``backend/backend/requirements.txt`` and matches nothing — so the check
    printed an empty diff for every commit and proved nothing at all.
    """
    pathspecs = [f":/backend/{name}" for name in REQUIREMENTS]
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", base, "HEAD", "--", *pathspecs],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        # A pathspec that matches nothing at all means the check is vacuous.
        tracked = subprocess.run(
            ["git", "ls-tree", "--name-only", "HEAD", "--", *pathspecs],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ControlRefusal(f"could not verify the requirements diff: {exc}") from exc
    if not tracked:
        raise ControlRefusal(
            f"pathspecs {pathspecs} match no tracked file at HEAD, so the "
            "requirements check would pass vacuously"
        )
    if diff:
        raise ControlRefusal(
            f"requirements changed between {base} and HEAD ({diff}); one venv is "
            "no longer valid for both worktrees and C4 cannot compare them"
        )
    return {"base": base, "requirements_diff": "empty", "pathspecs": pathspecs}


def p95(values):
    return sorted(values)[max(0, math.ceil(len(values) * 0.95) - 1)]


class PublicationStages:
    """§4.7 part (ii), measured at the CONNECTION rather than from app internals.

    ``LegacyAdapter.last_metrics`` carries ``publish_ms``, ``read_ms``,
    ``diff_ms`` and ``read_bytes_estimate`` and nothing else, so the retirement
    duration and the lock hold this bead asks for are simply not there — and
    they cannot be taken from app internals either, because the names differ
    between the two commits this runner has to drive. Both are visible as
    STATEMENTS, which exist identically at both:

    * the publication lock is ``pg_advisory_xact_lock(...)``
      (``opening_score_storage.py:226``), held until the transaction ends, so
      the hold is from that statement to the ``commit`` event;
    * retirement is the DELETEs ``_retire`` issues against the payload tables
      and the marker (``opening_score_storage.py:380-398``), so the stage is the
      time spent inside DELETE statements.

    Reported as what it is — a connection-level observation, not an internal
    timer — and still ONE-SIDED where A_old emits no such structure to compare.
    """

    RETIREMENT_RELATIONS = (
        "user_opening_scores",
        "opening_position_scores",
        "opening_position_edges",
        "opening_score_batch_shared_scope",
        "opening_score_batches",
    )

    def __init__(self, engine):
        from sqlalchemy import event

        self._event = event
        self.engine = engine
        self.reset()
        event.listen(engine, "before_cursor_execute", self._before)
        event.listen(engine, "after_cursor_execute", self._after)
        event.listen(engine, "commit", self._commit)

    def reset(self) -> None:
        self.delete_ms = 0.0
        self.delete_statements = 0
        self.delete_rows = 0
        self.lock_acquired_at = None
        self.lock_hold_ms = None
        self._started = None

    def _before(self, conn, cursor, statement, parameters, context, many):
        self._started = time.perf_counter()

    def _after(self, conn, cursor, statement, parameters, context, many):
        if self._started is None:
            return
        elapsed = (time.perf_counter() - self._started) * 1000
        self._started = None
        lowered = statement.lower()
        if "pg_advisory_xact_lock" in lowered:
            self.lock_acquired_at = time.perf_counter()
        elif lowered.lstrip().startswith("delete") and any(
            name in lowered for name in self.RETIREMENT_RELATIONS
        ):
            self.delete_ms += elapsed
            self.delete_statements += 1
            self.delete_rows += max(0, cursor.rowcount or 0)

    def _commit(self, conn):
        if self.lock_acquired_at is not None:
            self.lock_hold_ms = (time.perf_counter() - self.lock_acquired_at) * 1000
            self.lock_acquired_at = None

    def sample(self) -> dict:
        return {
            "retirement_delete_ms": self.delete_ms,
            "retirement_delete_statements": self.delete_statements,
            "retirement_deleted_rows": self.delete_rows,
            "publication_lock_hold_ms": self.lock_hold_ms,
        }

    def close(self) -> None:
        for name, handler in (
            ("before_cursor_execute", self._before),
            ("after_cursor_execute", self._after),
            ("commit", self._commit),
        ):
            self._event.remove(self.engine, name, handler)


def run(adapter, candidates, *, repetitions: int, stages: PublicationStages) -> dict:
    """End-to-end legacy publication timing, plus the observed stage split.

    The retirement stage and the lock hold are emitted by A_NEW's structure; at
    A_old the same observation runs and reports whatever is there, so the report
    states which half is a comparison and which is a characterisation rather
    than presenting a missing baseline as an improvement.
    """
    adapter.publish(candidates[0])
    adapter.publish(candidates[0])
    samples, stage_samples = [], []
    for index in range(repetitions):
        candidate = candidates[1 + (index % (len(candidates) - 1))]
        stages.reset()
        started = time.perf_counter()
        adapter.publish(candidate)
        samples.append((time.perf_counter() - started) * 1000)
        metrics = dict(getattr(adapter, "last_metrics", {}) or {})
        stage_samples.append(
            {
                **{k: v for k, v in metrics.items() if isinstance(v, (int, float))},
                **stages.sample(),
            }
        )
    holds = [s["publication_lock_hold_ms"] for s in stage_samples if s["publication_lock_hold_ms"]]
    deletes = [s["retirement_delete_ms"] for s in stage_samples]
    return {
        "publication_ms": samples,
        "publication_p95_ms": p95(samples),
        "publication_median_ms": sorted(samples)[len(samples) // 2],
        "adapter_stage_metrics": stage_samples,
        "retirement_delete_p95_ms": p95(deletes) if deletes else None,
        "publication_lock_hold_p95_ms": p95(holds) if holds else None,
        "publication_lock_hold_samples": len(holds),
        "repetitions": repetitions,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payloads", type=Path, required=True)
    parser.add_argument("--label", required=True, choices=("A_old", "A_new"))
    parser.add_argument("--profile", default="S1")
    parser.add_argument("--repetitions", type=int, default=40)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    provenance = assert_shared_venv_still_valid()
    url = guard_database_url()
    # Guarded first, THEN bound, THEN imported — the §2.1 order. ``app.db``
    # binds at import, so this is the only point at which the application's own
    # engine can be pointed at the guarded target rather than at the shell's.
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    os.environ["PGAPPNAME"] = APPLICATION_NAME
    os.environ["POSTHOG_DISABLED"] = "true"

    # `LegacyAdapter` hard-codes OWNER = 1, COLOR = "black"
    # (opening_score_storage_workload.py:61-62). That is correct for a
    # publish-only control and it keeps the real production user id out of
    # every artifact. ``replay_objects`` is imported for the same reason the
    # module docstring gives: both names exist at BOTH commits.
    from scripts.opening_score_storage_adapters import LegacyAdapter
    from scripts.opening_score_storage_workload import replay_objects  # noqa: F401

    assert_resolved_engine(url)
    # Payloads are loaded BY PATH from the private store; nothing production-
    # derived is ever copied into either worktree.
    with open(args.payloads, "rb") as handle:
        capture = pickle.load(handle)
    admissibility = assert_capture_admissible(args.payloads, capture)
    candidates = capture["candidates"]
    if len(candidates) < 2:
        raise ControlRefusal("C4 needs at least two candidates to alternate")

    from sqlalchemy import create_engine

    engine = create_engine(
        url,
        connect_args={
            "application_name": APPLICATION_NAME,
            "options": "-csearch_path=public",
        },
        pool_size=2,
        max_overflow=0,
    )
    stages = None
    try:
        identity = assert_cluster_identity(engine)
        assert_database_empty(engine)
        adapter = LegacyAdapter(engine)
        adapter.create()
        stages = PublicationStages(engine)
        result = run(
            adapter, candidates, repetitions=args.repetitions, stages=stages
        )
    finally:
        if stages is not None:
            stages.close()
        engine.dispose()
    report = {
        "label": args.label,
        "profile": args.profile,
        "capture_closure": admissibility["closure_check"],
        "capture_synthetic_only": admissibility["synthetic_only"],
        "cluster": identity,
        "revision": subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
        ).stdout.strip(),
        # The evaluator compares this against the run's host and cluster exactly
        # as it does for a cell report. Without it a C4 pair measured on another
        # machine joined the verdict unexamined; the REVISION is the one field
        # where A_old is expected to differ, because A_old IS the predecessor
        # commit, and the evaluator exempts that label on that field alone.
        "host_platform": platform.platform(),
        "worktree": str(Path.cwd()),
        "venv_provenance": provenance,
        "stage_measurement": (
            "retirement duration and lock hold are CONNECTION-LEVEL "
            "observations — time inside retirement DELETEs, and advisory-lock "
            "statement to commit — not internal timing events, because the "
            "internal names differ between the two commits this runner drives"
        ),
        "one_sided_stages": (
            "A_old's writer has no atomic retirement stage to compare against; "
            "where its retirement figures are absent or zero that is a "
            "characterisation of A_new, not an improvement over a baseline"
        ),
        **result,
    }
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in {"publication_ms", "adapter_stage_metrics"}
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
