#!/usr/bin/env python3
"""Integrated release qualification harness for g-score-store-qualify.

Drives the SHIPPED implementation (``opening_cache.recompute_opening_scores``,
``opening_score_storage`` readers, the ``/tree`` builder's own loop body) against
production-shaped payloads captured by ``qualify_opening_score_capture``. It is
deliberately NOT the spike's raw-DDL ``CurrentAdapter``: the spike selected a
design, this bead qualifies the code that ships.

Nothing here edits ``bench_opening_score_storage``,
``opening_score_storage_adapters`` or ``opening_score_storage_workload``; their
instrumentation is imported so the fixture tie-back stays driven by the same
code the approved budget run used.

Run one cell per process (see ``run-cell``) with ``backend/.venv`` active and
``GHOSTREPLAY_STORAGE_QUAL_DATABASE_URL`` naming a fresh ``gr_score_qual_*``
database on the disposable QC-PROD cluster. See BENCH_OPENING_SCORE_STORAGE.md.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, DBAPIError

QUAL_DATABASE_ENV = "GHOSTREPLAY_STORAGE_QUAL_DATABASE_URL"
QUAL_ADMIN_ENV = "GHOSTREPLAY_STORAGE_QUAL_ADMIN_URL"
CAPTURE_DATABASE_ENV = "GHOSTREPLAY_STORAGE_CAPTURE_DATABASE_URL"
CLUSTER_NAME = "ghostreplay-score-storage-qual"
# §4.1/§4.2: the SF tie-back runs on the SECOND disposable cluster. The expected
# name is an explicit opt-in restricted to these two literals, so a cell can
# never be pointed at a cluster that merely happens to answer.
SPIKE_CLUSTER_NAME = "ghostreplay-score-storage-spike"
QUAL_CLUSTER_ENV = "GHOSTREPLAY_STORAGE_QUAL_CLUSTER"
KNOWN_CLUSTERS = frozenset({CLUSTER_NAME, SPIKE_CLUSTER_NAME})
APPLICATION_NAME = "ghostreplay-storage-qualification"
SNAPSHOT_TEMPLATE = "gr_snap_base"

CELL_DATABASE_PATTERN = r"gr_score_qual_[a-z0-9_]+"
CAPTURE_DATABASE_PATTERN = r"gr_score_capture_[a-z0-9_]+"
# §4.9's C7 clone. It must NOT be gr_snap_base, which the delta-lane benchmark
# lists in ``_PROTECTED_DATABASES``, and it is a POPULATED clone, so it matches
# neither the measurement pattern nor its empty-database rule.
LANE_DATABASE_PATTERN = r"gr_delta_lane_[a-z0-9_]+"
ADMIN_DATABASES = frozenset({"postgres"})
# pg_walinspect reports a shared catalog's block references with database OID 0.
SHARED_CATALOG_DATABASE_OID = 0
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

# §1.2's refusal list is the FULL application fall-through, both spellings of
# every variable (app/database_url.py:25-29), plus EVERY alternate URL name
# `resolve_database_url` tries before them (app/database_url.py:51-67). A list
# that omits one alias is the whole defect, so §2.6 takes one case per name.
#
# `DATABASE_URL` is the FIRST name in that fall-through and was missing from
# this list until a real shell exposed it: with a production `DATABASE_URL`
# exported, `refuse_inherited_connection_environment` passed, and that value is
# the one `resolve_database_url` reaches before any other. §1.2's own wording
# had the same hole ("any of those ten names, or DATABASE_PRIVATE_URL or
# DATABASE_PUBLIC_URL"), and §2.6's per-name cases were generated from this
# tuple, so the coverage rule could not catch its own omission.
#
# It is checked like the others and, unlike the others, is also SET on purpose —
# by `bootstrap_database_url` and by `_child_environment`. So "must be provably
# loopback when INHERITED" and "must never appear in a CONSTRUCTED child" are no
# longer the same tuple; conflating them is what made the omission look
# deliberate.
BOOTSTRAPPED_URL_ENV = "DATABASE_URL"
INHERITED_URL_ENV_NAMES = (
    BOOTSTRAPPED_URL_ENV,
    "DATABASE_PRIVATE_URL",
    "DATABASE_PUBLIC_URL",
)
INHERITED_HOST_ENV_NAMES = ("PGHOST", "POSTGRES_HOST")
INHERITED_PG_ENV_NAMES = (
    "PGDATABASE",
    "POSTGRES_DB",
    "PGUSER",
    "POSTGRES_USER",
    "PGPASSWORD",
    "POSTGRES_PASSWORD",
    "PGPORT",
    "POSTGRES_PORT",
)
# What a constructed child environment must never carry. `DATABASE_URL` is
# excluded BECAUSE `_child_environment` sets it to the guarded target.
REFUSED_ENV_NAMES = (
    tuple(name for name in INHERITED_URL_ENV_NAMES if name != BOOTSTRAPPED_URL_ENV)
    + INHERITED_HOST_ENV_NAMES
    + INHERITED_PG_ENV_NAMES
)


class QualificationRefusal(RuntimeError):
    """A guard refused. Never downgraded to a warning and never caught inside."""


# --------------------------------------------------------------------------
# §1.2 / §2.1 — environment refusal and the DATABASE_URL bootstrap
# --------------------------------------------------------------------------


def _loopback_host(host: str | None) -> bool:
    return (host or "") in LOOPBACK_HOSTS


def expected_cluster_name(environ=None) -> str:
    """QC-PROD unless the SF opt-in names QC-SPIKE; never anything else."""
    environ = os.environ if environ is None else environ
    name = environ.get(QUAL_CLUSTER_ENV) or CLUSTER_NAME
    if name not in KNOWN_CLUSTERS:
        raise QualificationRefusal(
            f"{QUAL_CLUSTER_ENV}={name!r} is not one of {sorted(KNOWN_CLUSTERS)}"
        )
    return name


def refuse_inherited_connection_environment(environ=None) -> None:
    """Refuse any inherited variable that could resolve an engine off loopback.

    Stricter than "resolves to a non-loopback host", deliberately. A bare
    ``PGUSER`` or ``PGPASSWORD`` cannot be PROVEN loopback on its own, and
    ``_database_url_from_pg_env`` needs only host/database/user/password to build
    a URL (app/database_url.py:31), so an inherited half-set plus one more
    variable from a later shell is enough to resolve production. Only a host or
    URL whose value is explicitly loopback is allowed through.
    """
    environ = os.environ if environ is None else environ
    for name in INHERITED_URL_ENV_NAMES:
        raw = environ.get(name)
        if not raw:
            continue
        try:
            host = make_url(raw).host
        except ArgumentError as exc:
            # An unparseable value cannot be PROVEN loopback either, and the
            # rule is that only a provably loopback value passes.
            raise QualificationRefusal(
                f"{name} is set to a value that is not a parseable URL, so it "
                "cannot be proven loopback; unset it before qualifying"
            ) from exc
        if not _loopback_host(host):
            raise QualificationRefusal(
                f"{name} names a non-loopback database; unset it before qualifying"
            )
    for name in INHERITED_HOST_ENV_NAMES:
        raw = environ.get(name)
        if raw and not _loopback_host(raw):
            raise QualificationRefusal(
                f"{name}={raw!r} is not loopback; unset it before qualifying"
            )
    for name in INHERITED_PG_ENV_NAMES:
        if environ.get(name):
            raise QualificationRefusal(
                f"{name} is inherited and cannot be proven loopback; unset it"
            )


def _guard_url(raw: str | None, env_name: str, pattern: str) -> object:
    if not raw:
        raise QualificationRefusal(f"{env_name} must explicitly name the target")
    url = make_url(raw)
    if not url.drivername.startswith("postgresql"):
        raise QualificationRefusal(f"{env_name} must use PostgreSQL")
    if not _loopback_host(url.host):
        raise QualificationRefusal(
            f"{env_name} must resolve to loopback TCP, not {url.host!r}"
        )
    if url.query:
        raise QualificationRefusal(f"{env_name} must carry no URL query overrides")
    if not re.fullmatch(pattern, url.database or ""):
        raise QualificationRefusal(
            f"{env_name} database {url.database!r} does not match {pattern}"
        )
    return url.set(drivername="postgresql+psycopg")


def guard_measurement_url(raw: str | None):
    """§2.2 measurement guard, URL half: loopback TCP, fresh gr_score_qual_*."""
    return _guard_url(raw, QUAL_DATABASE_ENV, CELL_DATABASE_PATTERN)


def guard_capture_url(raw: str | None):
    """§1.3 capture guard, URL half.

    A DIFFERENT name pattern from the measurement guard's on purpose: the capture
    database is a populated clone of the production snapshot and therefore
    violates the measurement guard's empty-on-entry rule.
    """
    return _guard_url(raw, CAPTURE_DATABASE_ENV, CAPTURE_DATABASE_PATTERN)


def guard_lane_url(raw: str | None):
    """§4.9 lane guard, URL half.

    A THIRD pattern, and it is load-bearing: ``migrate_lane`` runs in a child
    that bootstraps its own URL, and bootstrapping under the MEASUREMENT guard
    refused every lane migration outright — ``gr_delta_lane_qual`` does not
    match ``gr_score_qual_*``, so the clone was created and then left behind
    unmigrated. The lane database is a populated clone of the snapshot, so it
    fails the measurement guard's empty-database rule as well.
    """
    return _guard_url(raw, QUAL_DATABASE_ENV, LANE_DATABASE_PATTERN)


def guard_admin_url(raw: str | None):
    if not raw:
        raise QualificationRefusal(f"{QUAL_ADMIN_ENV} must name the QC-PROD cluster")
    url = make_url(raw)
    if not url.drivername.startswith("postgresql"):
        raise QualificationRefusal(f"{QUAL_ADMIN_ENV} must use PostgreSQL")
    if not _loopback_host(url.host):
        raise QualificationRefusal(f"{QUAL_ADMIN_ENV} must resolve to loopback TCP")
    if (url.database or "") not in ADMIN_DATABASES:
        raise QualificationRefusal(
            f"{QUAL_ADMIN_ENV} must name a maintenance database, not "
            f"{url.database!r}"
        )
    return url.set(drivername="postgresql+psycopg")


def bootstrap_database_url(environ=None, *, env_name=QUAL_DATABASE_ENV, guard=None):
    """Set ``DATABASE_URL`` from the opt-in variable BEFORE the first app import.

    ``app.db`` binds its engine at import (app/db.py:49) and every spike module
    this harness imports pulls in ``app`` at module top, so the engine is fixed
    before any harness code could rebind it. This is an explicit reversal of the
    earlier "never ``DATABASE_URL``" wording: that rule was about never
    INHERITING an ambient value, and ``DATABASE_URL`` is first in
    ``resolve_database_url``'s order, so overwriting it is what makes the
    resolved engine deterministic. The inherited refusal runs first.
    """
    environ = os.environ if environ is None else environ
    refuse_inherited_connection_environment(environ)
    url = (guard or guard_measurement_url)(environ.get(env_name))
    environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    # libpq reads PGAPPNAME at connect time, so setting it here names every
    # connection this process opens — including one from an engine built
    # elsewhere. ``assert_no_foreign_activity`` identifies our own backends by
    # that name, and an unnamed backend of our own would refuse the cell after
    # it had already run.
    environ["PGAPPNAME"] = APPLICATION_NAME
    # The CAPTURE parent runs the real writer in THIS process, so a
    # child-only setting missed the one path that actually publishes against
    # production-derived data. Set where every entry point passes.
    environ["POSTHOG_DISABLED"] = "true"
    return url


def assert_resolved_engine(url) -> None:
    """Assert the RESOLVED engine, not the variable (§2.1).

    ``resolve_database_url`` falls through five names and then a local default,
    so an assertion on ``DATABASE_URL`` alone proves nothing about what
    ``app.db.engine`` actually bound.
    """
    from app import db as app_db

    resolved = app_db.engine.url
    expected = (url.host, url.port, url.database, url.username)
    actual = (resolved.host, resolved.port, resolved.database, resolved.username)
    if actual != expected:
        raise QualificationRefusal(
            f"app.db.engine resolved to {actual!r}, not the harness target {expected!r}"
        )


# --------------------------------------------------------------------------
# §2.1 / §2.2 — connections, cluster identity and the MIGRATIONS rule
# --------------------------------------------------------------------------


def engine_for(url, *, pool_size=5):
    """Pinned ``search_path`` and a named application, asserted per cell."""
    return create_engine(
        url,
        connect_args={
            "application_name": APPLICATION_NAME,
            "options": "-csearch_path=public",
        },
        pool_size=pool_size,
        max_overflow=0,
    )


def session_factory_for(engine):
    """Every session the cells use is bound to the HARNESS engine, never app.db's.

    ``app.db`` builds its own engine at import with no ``application_name``
    (app/db.py:49) and its own pool. Publishing through ``SessionLocal`` would
    therefore (a) open backends that ``assert_no_foreign_activity`` cannot tell
    from a co-tenant's, and (b) put the writes on an engine ``Trace`` is not
    listening to, so C6 would measure zero DataRow bytes and zero round trips.
    One engine per cell fixes both, and ``assert_resolved_engine`` still proves
    ``app.db`` bound the same target — that assertion is about the bootstrap,
    not about which engine does the work.

    Session options match ``app.db.SessionLocal`` exactly, because the unit of
    work being timed is the shipped one.
    """
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )


def assert_cluster_identity(
    conn, *, expected_database_pattern: str, expected_cluster: str | None = None
) -> dict:
    """The same connection proves cluster AND database, never two connections."""
    expected = expected_cluster or expected_cluster_name()
    cluster = conn.execute(text("SHOW cluster_name")).scalar_one()
    if cluster != expected:
        raise QualificationRefusal(
            f"cluster identifies as {cluster!r}, not {expected!r}"
        )
    database = conn.execute(text("SELECT current_database()")).scalar_one()
    if not re.fullmatch(expected_database_pattern, database):
        raise QualificationRefusal(
            f"connected to {database!r}, which does not match "
            f"{expected_database_pattern}"
        )
    search_path = conn.execute(text("SHOW search_path")).scalar_one()
    if search_path.replace('"', "").strip() != "public":
        raise QualificationRefusal(f"search_path is {search_path!r}, not public")
    return {
        "cluster_name": cluster,
        "database": database,
        "server_version": conn.execute(text("SHOW server_version")).scalar_one(),
        "search_path": search_path,
    }


def run_migrations(url, *, expected_database_pattern: str) -> None:
    """Migrate the tool's OWN target, in-process, only after its guard passed.

    ``alembic/env.py:32`` sets ``sqlalchemy.url`` from ``resolve_database_url()``
    unconditionally — no opt-in variable, no guard, no override — so a bare
    ``alembic upgrade head`` at a shell prompt migrates whatever the application
    fall-through returns, which with nothing set is the developer database on the
    shared 5432 cluster (app/database_url.py:67). That is the one DDL write path
    outside every guard in this bead, and it is refused everywhere it would
    otherwise appear. Alembic re-executes ``env.py`` on every run, so the value
    this bootstrap put in ``os.environ`` is the one it resolves.
    """
    from alembic import command
    from alembic.config import Config

    if os.environ.get("DATABASE_URL") != url.render_as_string(hide_password=False):
        raise QualificationRefusal(
            "migrations require DATABASE_URL bootstrapped to the guarded target"
        )
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    # Read the identity back on a connection of our own: a migration that
    # resolved somewhere else would otherwise leave no trace here.
    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            assert_cluster_identity(
                conn, expected_database_pattern=expected_database_pattern
            )
    finally:
        engine.dispose()


def create_schema(engine) -> tuple[str, ...]:
    """``create_all`` plus the infrastructure it does not create (§2.1).

    The PostgreSQL precedent is ``scripts/seed_e2e_data.py:300-308``:
    ``Base.metadata.create_all`` builds tables — including the current-format
    fillfactor DDL (``models.py:2064``) — but neither the evidence-epoch triggers
    nor the singleton epoch and invalidation rows, without which no batch can
    ever be proven fresh. ``ensure_retention_policy_row`` (``seed_e2e_data.py:313``)
    is deliberately NOT copied: that singleton gates SRS target publication,
    which no path this harness times ever reaches.
    """
    from app.models import Base, ensure_evidence_epoch_infrastructure

    Base.metadata.create_all(engine)
    ensure_evidence_epoch_infrastructure(engine, assume_new_schema=True)
    # The counter rule needs the FULL created set, not the measured subset.
    # ``ensure_evidence_epoch_infrastructure`` inserts the ``evidence_epoch``
    # singleton (models.py:1765), so a created set of MEASURED_RELATIONS alone
    # makes every cell refuse at the end of its first block with "writes to
    # relations the harness did not create: ['evidence_epoch']".
    return tuple(sorted(table.name for table in Base.metadata.sorted_tables))


def assert_current_format_reloptions(engine) -> dict:
    """Assert the ACTUAL storage parameters, never trust the DDL that set them."""
    from app.models import CurrentOpeningPosition, CurrentOpeningRoot

    # fillfactor=50 is installed on the two UPDATE-heavy score tables only
    # (models.py:2064); edges and scope carry the default and asserting 50 on
    # them would fail against correct DDL.
    names = [
        model.__tablename__
        for model in (CurrentOpeningRoot, CurrentOpeningPosition)
    ]
    with engine.connect() as conn:
        options = dict(
            conn.execute(
                text(
                    "SELECT relname, array_to_string(reloptions, ',') FROM pg_class "
                    "WHERE relname = ANY(:names)"
                ),
                {"names": names},
            ).all()
        )
    missing = [name for name in names if "fillfactor=50" not in (options.get(name) or "")]
    if missing:
        raise QualificationRefusal(
            f"current-format relations missing fillfactor=50: {missing}"
        )
    return options


# The relations a cell measures: both formats' payload tables, the marker, the
# generation cursor, and the two GLOBAL shared-evidence tables the scope proofs
# probe. Autovacuum is disabled per relation on all of them, heap AND TOAST.
MEASURED_RELATIONS = (
    "opening_score_batches",
    "opening_score_cursors",
    "user_opening_scores",
    "opening_position_scores",
    "opening_position_edges",
    "opening_score_batch_shared_scope",
    "opening_current_roots",
    "opening_current_positions",
    "opening_current_edges",
    "opening_current_scope",
    "shared_evidence_scope_versions",
    "shared_evidence_scope_invalidations",
)


# --------------------------------------------------------------------------
# §2.2 — catalog upkeep: the harness's own churn, settled between measurements
# --------------------------------------------------------------------------

# Every window's ``VACUUM (ANALYZE)`` over the measured relations rewrites
# roughly a hundred ``pg_statistic`` rows and touches ``pg_class``; the cell's
# DDL churns a dozen catalogs harder than that. These are vacuumed EXPLICITLY,
# so the cluster-level ``autovacuum`` setting can stay census-matched instead of
# becoming a second settings deviation.
#
# ``pg_statistic`` IS LAST, AND THAT ORDER IS THE WHOLE POINT. Analyzing any
# relation writes that relation's statistics INTO ``pg_statistic``, so a list
# with ``pg_statistic`` first cleaned the table and immediately dirtied it
# again. Measured on a throwaway 18.4 cluster with twelve stand-in tables:
# first, 158-277 dead ``pg_statistic`` tuples survived each window against a
# threshold of 157 and an autovacuum followed three of five windows; last, the
# residue was 69-115 and none did. (That measurement read ``autovacuum_count``
# alone; everything the harness counts reads ``autoanalyze_count`` beside it.)
# With the flush ordering below in place the same arrangement measured 0 dead
# after every window and still 0 twelve seconds into the next block, against a
# threshold of 160, and no autovacuum event in any window or block.
CATALOG_STATISTIC_RELATION = "pg_catalog.pg_statistic"

# How many times ``maintain_catalog`` settles and rechecks before it gives up
# and lets ``assert_catalog_settled`` refuse. Each round's own ``VACUUM``s write
# ``pg_class`` and each round's own ``ANALYZE``s write ``pg_statistic``, so the
# residue shrinks rather than reaching zero; the loop therefore stops on the
# only condition that matters — nothing is left that would REFUSE the cell.
# One round is the measured case (0.02-0.06 s); a second means something moved
# after the flush, and a third that it is still moving.
CATALOG_SETTLE_ROUNDS = 3

# The thresholds autovacuum itself decides on. Recorded per cell, because a
# cluster that raised them would make every "settled" claim below weaker
# without changing a line of this file.
AUTOVACUUM_SETTINGS = (
    "autovacuum_vacuum_threshold",
    "autovacuum_vacuum_scale_factor",
    "autovacuum_vacuum_insert_threshold",
    "autovacuum_vacuum_insert_scale_factor",
    "autovacuum_analyze_threshold",
    "autovacuum_analyze_scale_factor",
)


def flush_backend_stats(conn) -> None:
    """Make THIS backend's pending statistics visible before anything reads them.

    ``pgstat_report_stat`` runs between commands and returns without flushing
    when less than a second has passed since the last one, so the counts a
    statement has just produced are normally still private to its own backend.
    ``pg_stat_force_next_flush`` waives that interval for the NEXT flush, which
    happens once the current statement finishes — hence the second statement,
    whose only job is to make the flush have already happened when this returns.
    """
    conn.execute(text("SELECT pg_stat_force_next_flush()"))
    conn.execute(text("SELECT 1"))


def flush_pooled_backend_stats(engine) -> None:
    """Every OTHER backend's pending statistics, which no statement here reaches.

    ``pg_stat_force_next_flush`` is per backend. The schema DDL, the reloption
    ALTERs and a window's ``VACUUM (ANALYZE)`` run on whichever pooled
    connection was free, and a pooled connection goes idle the moment it is
    done. An idle backend does flush eventually — measured at 10.14 s, which is
    ``PGSTAT_IDLE_INTERVAL`` — but "eventually" is the problem, not the
    reassurance: ten seconds after a cell's schema DDL is inside block 0.
    Disposing the pool disconnects those backends instead, and a backend flushes
    what it holds on the way out, so the counts are there before the discovery
    query below asks.

    That delay is the mechanism behind nine catalog autovacuum events landing in
    block 0. ``create_schema`` inserts thousands of catalog rows;
    ``maintain_catalog`` ran a second later on the same backend against counters
    that did not yet include them, reset those counters, and left the launcher
    to pick up the real churn one naptime later — inside a measured block.

    The ordering this relies on — a disconnecting backend's flush being visible
    to the next connection's read — is not a documented PostgreSQL guarantee.
    It held in 180 of 180 trials on a throwaway 18.4 cluster against 20 of 20
    stale reads without the dispose, and ``assert_catalog_settled`` re-reads the
    thresholds afterwards, so a miss costs a settle round rather than a block.
    """
    engine.dispose()


def order_catalog_relations(names) -> tuple[str, ...]:
    """``pg_statistic`` last, and present whenever anything is maintained at all.

    Maintaining ANY relation analyzes it, and analyzing anything writes that
    relation's statistics into ``pg_statistic``. So ``pg_statistic`` is not
    merely ordered last when it happens to be pending: it is appended whenever
    the round has any other work, because that work is what dirties it.
    """
    wanted = {str(name) for name in names}
    others = sorted(wanted - {CATALOG_STATISTIC_RELATION})
    if not others and CATALOG_STATISTIC_RELATION not in wanted:
        return ()
    return tuple([*others, CATALOG_STATISTIC_RELATION])


def pending_catalog_relations(conn) -> tuple[str, ...]:
    """Every catalog with churn waiting on it, discovered rather than listed.

    The fixed list this replaces named four catalogs. Replaying a cell's own
    start-of-cell sequence on a throwaway 18.4 cluster left EIGHT with pending
    churn — ``pg_attrdef``, ``pg_constraint``, ``pg_depend`` and ``pg_trigger``
    among them — and the four that were missing are precisely what the
    launcher's first pass picked up: seven autoanalyzes and a ``pg_depend``
    vacuum in one pass, then a ``pg_statistic`` vacuum a naptime later, nine
    events inside block 0.

    A catalog's TOAST relation is maintained THROUGH ITS PARENT, which is what
    ``VACUUM`` on the parent does; a measured table's TOAST relation has a
    parent in ``public`` and is therefore not matched here.
    """
    rows = (
        conn.execute(
            text(
                "SELECT DISTINCT COALESCE(pn.nspname || '.' || p.relname, "
                "                         s.schemaname || '.' || s.relname) "
                "FROM pg_stat_all_tables s "
                "JOIN pg_class c ON c.oid = s.relid "
                "LEFT JOIN pg_class p ON p.reltoastrelid = c.oid "
                "LEFT JOIN pg_namespace pn ON pn.oid = p.relnamespace "
                "WHERE s.n_dead_tup + s.n_mod_since_analyze + "
                "      s.n_ins_since_vacuum > 0 "
                "AND (s.schemaname = 'pg_catalog' "
                "     OR (s.schemaname = 'pg_toast' AND pn.nspname = 'pg_catalog'))"
            )
        )
        .scalars()
        .all()
    )
    return order_catalog_relations(rows)


def catalog_modified_counts(conn, names) -> dict[str, int]:
    """``n_mod_since_analyze`` for the named relations, as pgstat has it NOW.

    Read on both sides of a round's ANALYZEs, which is the only way to tell an
    ANALYZE that cleared a counter from one PostgreSQL declined to act on. The
    caller flushes first; this only looks.
    """
    if not names:
        return {}
    conn.execute(text("SELECT pg_stat_clear_snapshot()"))
    rows = conn.execute(
        text(
            "SELECT schemaname || '.' || relname, n_mod_since_analyze "
            "FROM pg_stat_all_tables "
            "WHERE schemaname || '.' || relname = ANY(:names)"
        ),
        {"names": list(names)},
    ).all()
    return {str(name): int(count) for name, count in rows}


def _settle_catalog_once(engine) -> dict:
    """One maintenance pass: flush, discover, maintain, ``pg_statistic`` twice.

    Returns what it maintained and, of that, which relations came out of their
    own ANALYZE with ``n_mod_since_analyze`` no lower than it went in. That
    second list is the ONLY thing ``assert_catalog_settled`` will excuse, and it
    is an observation of this round rather than a name in this file.
    """
    flush_pooled_backend_stats(engine)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        flush_backend_stats(conn)
        conn.execute(text("SELECT pg_stat_clear_snapshot()"))
        targets = pending_catalog_relations(conn)
        before = catalog_modified_counts(conn, targets)
        for name in targets:
            if name == CATALOG_STATISTIC_RELATION:
                # The ANALYZEs above wrote into pg_statistic ON THIS BACKEND and
                # those counts are still pending. Vacuuming before the flush
                # removes the rows, reports zero dead, and then lets the pending
                # counts land on a table that no longer holds them — which is
                # how an S0 window, whose measured span is tens of milliseconds,
                # handed the following block a pg_statistic one to three rows
                # under its threshold. Measured: 141-159 dead against 160.
                flush_backend_stats(conn)
            conn.execute(text(f"VACUUM (ANALYZE) {name}"))
        if targets:
            # Its own ANALYZE rewrote its own rows. Flush those, then take them
            # out with a plain VACUUM: a second ANALYZE would only write more.
            flush_backend_stats(conn)
            conn.execute(text(f"VACUUM {CATALOG_STATISTIC_RELATION}"))
            flush_backend_stats(conn)
        after = catalog_modified_counts(conn, targets)
    return {
        "maintained": targets,
        "analyzed_without_effect": tuple(
            name
            for name in targets
            if before.get(name, 0) > 0 and after.get(name, 0) >= before.get(name, 0)
        ),
    }


def read_autovacuum_state(engine) -> tuple[list[dict], dict]:
    """What every relation in the database has waiting, and the live thresholds."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("SELECT pg_stat_clear_snapshot()"))
        rows = [
            {
                "name": str(name),
                "dead_tuples": int(dead),
                "modified_since_analyze": int(modified),
                "inserted_since_vacuum": int(inserted),
                "reltuples": float(reltuples),
                "reloptions": list(reloptions or ()),
                "relkind": str(relkind),
            }
            for name, dead, modified, inserted, reltuples, reloptions, relkind in (
                conn.execute(
                    text(
                        "SELECT s.schemaname || '.' || s.relname, s.n_dead_tup, "
                        "s.n_mod_since_analyze, s.n_ins_since_vacuum, c.reltuples, "
                        "c.reloptions, c.relkind FROM pg_stat_all_tables s "
                        "JOIN pg_class c ON c.oid = s.relid"
                    )
                ).all()
            )
        ]
        settings = {
            str(name): float(setting)
            for name, setting in conn.execute(
                text("SELECT name, setting FROM pg_settings WHERE name = ANY(:names)"),
                {"names": list(AUTOVACUUM_SETTINGS)},
            ).all()
        }
    return rows, settings


def relation_autovacuum_option(reloptions) -> bool | None:
    """``autovacuum_enabled`` as the relation's own storage parameters state it."""
    for option in reloptions or ():
        key, _, value = str(option).partition("=")
        if key.strip() == "autovacuum_enabled":
            return value.strip().lower() in ("true", "on", "1", "yes")
    return None


def eligible_relations(rows, settings) -> list[dict]:
    """Every relation autovacuum could pick up RIGHT NOW, and on which rule.

    Pure, so §2.6 pins the arithmetic without a cluster. TOAST relations are
    never eligible on the ANALYZE rule (see below). Relations carrying
    ``autovacuum_enabled=false`` are skipped — the measured tables all do, heap
    and TOAST alike (``disable_relation_autovacuum``), and they sit far over
    every threshold by design, which is the whole reason their vacuum windows
    are explicit and equal.
    """
    eligible = []
    for row in rows:
        if relation_autovacuum_option(row.get("reloptions")) is False:
            continue
        # A negative ``reltuples`` means "never yet vacuumed or analyzed"
        # (PG 14+); autovacuum reads that as zero, not as a negative threshold.
        reltuples = max(float(row.get("reltuples") or 0.0), 0.0)
        vacuum_threshold = (
            settings["autovacuum_vacuum_threshold"]
            + settings["autovacuum_vacuum_scale_factor"] * reltuples
        )
        analyze_threshold = (
            settings["autovacuum_analyze_threshold"]
            + settings["autovacuum_analyze_scale_factor"] * reltuples
        )
        reasons = []
        if row["dead_tuples"] > vacuum_threshold:
            reasons.append("vacuum")
        insert_threshold = settings.get("autovacuum_vacuum_insert_threshold", -1.0)
        if insert_threshold >= 0:
            insert_threshold += (
                settings.get("autovacuum_vacuum_insert_scale_factor", 0.0) * reltuples
            )
            if row["inserted_since_vacuum"] > insert_threshold:
                reasons.append("insert_vacuum")
        # AUTOVACUUM NEVER ANALYZES A TOAST RELATION, so an analyze threshold
        # crossed on one is not something the launcher can pick up. PostgreSQL
        # sets ``doanalyze = false`` for ``RELKIND_TOASTVALUE`` in
        # ``do_autovacuum``, and nothing can clear the counter either: ANALYZE
        # on a TOAST relation is skipped outright — measured on 18.4,
        # ``WARNING: skipping "pg_toast_2619" --- cannot analyze non-tables or
        # special system tables``, with ``n_mod_since_analyze`` unchanged and
        # ``last_analyze`` still null afterwards. Counting it made the refusal
        # PERMANENT: ``pg_toast_2619`` — ``pg_statistic``'s own TOAST relation,
        # which every vacuum window dirties by analyzing the measured tables —
        # crossed 52 during C1/S0 and refused the cell at its first vacuum
        # window, and no number of maintenance rounds could ever bring it back.
        # Corroborated on a live 18.4 cluster: of 113 TOAST relations across
        # two databases, one had been autovacuumed and NONE had ever been
        # autoanalyzed, against 43 autoanalyzed heaps.
        #
        # The VACUUM side is untouched: autovacuum does vacuum TOAST relations,
        # and a TOAST relation over its vacuum threshold is still eligible.
        is_toast = row.get("relkind") == "t"
        if not is_toast and row["modified_since_analyze"] > analyze_threshold:
            reasons.append("analyze")
        if reasons:
            eligible.append(
                {
                    "relation": row["name"],
                    "reasons": reasons,
                    "dead_tuples": row["dead_tuples"],
                    "modified_since_analyze": row["modified_since_analyze"],
                    "inserted_since_vacuum": row["inserted_since_vacuum"],
                    "vacuum_threshold": vacuum_threshold,
                    "analyze_threshold": analyze_threshold,
                }
            )
    return sorted(eligible, key=lambda entry: entry["relation"])


def maintain_catalog(engine, *, rounds=CATALOG_SETTLE_ROUNDS) -> dict:
    """Settle the catalogs until nothing left over would refuse the cell.

    ``ANALYZE`` as well as ``VACUUM``: ``VACUUM`` clears ``n_dead_tup`` but only
    ``ANALYZE`` clears ``n_mod_since_analyze``, and autoanalyze is triggered by
    the second.

    Run AFTER a measured span, never before, so the maintenance WAL is charged
    to neither layout and each following block starts from a settled counter.
    Running it first cleaned up churn the span was about to recreate.

    The loop is what makes the claim checkable rather than asserted: each round
    maintains what is pending and then asks ``eligible_relations`` whether
    anything in the WHOLE database — catalogs and ``public`` alike — is still
    over an autovacuum threshold, and ``partition_catalog_residue`` whether any
    of that is refusable. It returns what it did and what is left;
    ``assert_catalog_settled`` reaches the same verdict on the record alone.
    """
    performed: list[list[str]] = []
    eligible: list[dict] = []
    unmoved: tuple[str, ...] = ()
    for _ in range(max(1, int(rounds))):
        settled = _settle_catalog_once(engine)
        performed.append(list(settled["maintained"]))
        unmoved = tuple(settled["analyzed_without_effect"])
        rows, settings = read_autovacuum_state(engine)
        eligible = eligible_relations(rows, settings)
        # The exit condition is what ``assert_catalog_settled`` would REFUSE,
        # not what is merely eligible: ``pg_statistic`` is permanently over its
        # analyze threshold on a real cluster, so testing eligibility here ran
        # all three rounds every time and made ``catalog_settle_rounds`` a
        # constant instead of a measurement.
        if not partition_catalog_residue(eligible, unmoved)[1]:
            break
    return {
        "catalog_settle_rounds": len(performed),
        "catalog_maintained": performed,
        "catalog_analyzed_without_effect": list(unmoved),
        "autovacuum_eligible_after_maintenance": eligible,
    }


def partition_catalog_residue(eligible, analyzed_without_effect):
    """Split what is left over into what PostgreSQL will not clear and the rest.

    Pure, and shared by the settle loop and the refusal, so the condition the
    loop stops on and the condition the cell is judged by cannot drift apart.
    """
    excused = set(analyzed_without_effect or ())
    unclearable, refused = [], []
    for entry in eligible:
        if (
            list(entry["reasons"]) == ["analyze"]
            and entry["relation"].startswith("pg_catalog.")
            and entry["relation"] in excused
        ):
            unclearable.append(entry)
        else:
            refused.append(entry)
    return unclearable, refused


def assert_catalog_settled(summary: dict) -> dict:
    """Refuse unless nothing in the database is autovacuum-eligible any more.

    "We listed the right catalogs" was an assumption; this is the checked
    condition that replaces it. A relation over its threshold is one the
    launcher's next pass can pick up, every pass that picks anything up lands
    inside a measured block, and every such landing discards the pair it lands
    in — which C2 could not afford even once while it ran four blocks against a
    forty-publication floor.

    ONE residue is recorded instead of refused: a ``pg_catalog`` relation over
    its ANALYZE threshold ALONE whose ``n_mod_since_analyze`` the last round's
    own ANALYZE did not bring down — read on both sides of that ANALYZE, not
    inferred from the fact that it was attempted. ``maintain_catalog``
    analyzing it and the launcher autoanalyzing it are the same operation, so a
    counter our ANALYZE cannot move is one autoanalyze cannot move either;
    refusing on it would make every cell unpassable without making any cell
    cleaner. ``pg_statistic`` is the observed instance — its
    ``n_mod_since_analyze`` was measured climbing to 930 across five windows on
    a throwaway 18.4 cluster while no autoanalyze of it ever ran. The carve-out
    is EARNED per run rather than hard-coded: a relation the maintenance never
    touched, or one whose counter DID fall and then rose again, is refused,
    which is what catches a catalog the discovery query missed.
    """
    unclearable, refused = partition_catalog_residue(
        summary.get("autovacuum_eligible_after_maintenance") or (),
        summary.get("catalog_analyzed_without_effect") or (),
    )
    if refused:
        raise QualificationRefusal(
            "relations are still over an autovacuum threshold after "
            f"{summary.get('catalog_settle_rounds')} maintenance round(s), so "
            "the launcher's next pass would land inside a measured block: "
            + "; ".join(
                f"{entry['relation']} ({'/'.join(entry['reasons'])}: "
                f"{entry['dead_tuples']} dead over {entry['vacuum_threshold']:.0f}, "
                f"{entry['modified_since_analyze']} modified over "
                f"{entry['analyze_threshold']:.0f})"
                for entry in refused
            )
        )
    return {
        "catalog_settle_rounds": summary.get("catalog_settle_rounds"),
        "catalog_maintained": summary.get("catalog_maintained"),
        "catalog_unclearable": unclearable,
        "catalog_unclearable_note": (
            "over its ANALYZE threshold alone, and its n_mod_since_analyze did "
            "not fall across this call's own ANALYZE; autoanalyze is the same "
            "operation, so it cannot clear it either"
        ),
    }


def measured_relation_names(engine, relations=MEASURED_RELATIONS) -> frozenset[str]:
    """Every measured relation AND its TOAST relation, as ``pg_stat_all_tables``
    names them.

    A measured table's TOAST relation is reported as ``pg_toast.pg_toast_<oid>``,
    which matches no entry in ``MEASURED_RELATIONS``. Autovacuum there was
    therefore classified as CATALOG churn and merely discarded a block pair,
    although ``disable_relation_autovacuum`` sets ``toast.autovacuum_enabled =
    false`` precisely so that it cannot happen — and a TOAST autovacuum on a
    measured relation breaks the equal-explicit-windows premise exactly as a
    heap one does. The names are resolved ONCE per cell, after the schema
    exists.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT n.nspname || '.' || c.relname, tn.nspname || '.' || t.relname "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "LEFT JOIN pg_class t ON t.oid = c.reltoastrelid "
                "LEFT JOIN pg_namespace tn ON tn.oid = t.relnamespace "
                "WHERE c.relname IN :names AND c.relkind IN ('r', 'p')"
            ).bindparams(bindparam("names", expanding=True)),
            {"names": list(relations)},
        ).all()
    names = set(relations)
    for qualified, toast in rows:
        names.add(str(qualified))
        if toast:
            names.add(str(toast))
    return frozenset(names)


def disable_relation_autovacuum(engine, relations=MEASURED_RELATIONS) -> None:
    """Both storage parameters, because TOAST has its OWN (§0.2).

    Cluster-level ``autovacuum`` stays census-matched — turning it off would be a
    settings deviation from the shape being qualified — so equal EXPLICIT vacuum
    windows are bought per relation instead. Setting only the heap option leaves
    the TOAST relation eligible, and a worker that starts and finishes between
    two ``pg_stat_activity`` checks is invisible to the process check.
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for name in relations:
            conn.execute(
                text(
                    f"ALTER TABLE {name} SET (autovacuum_enabled=false, "
                    "toast.autovacuum_enabled=false)"
                )
            )


# --------------------------------------------------------------------------
# §2.2 — cell invalidation: foreign activity, autovacuum, checkpoints, WAL
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CellCounters:
    """Everything a cell diffs across its own span."""

    autovacuum: int
    autoanalyze: int
    # (qualified_relname, autovacuum_count, autoanalyze_count) for every
    # relation IN THE WHOLE DATABASE with a non-zero count, catalogs included.
    # The totals alone said only "something moved", and the most likely
    # something is ``pg_statistic``: each vacuum window's ``VACUUM (ANALYZE)``
    # over the measured relations rewrites roughly a hundred of its rows, so a
    # few windows in, its own autovacuum threshold is reached. A refusal that
    # cannot name the relation reads as foreign contamination.
    counts_by_relation: tuple[tuple[str, int, int], ...]
    checkpoints_timed: int
    checkpoints_requested: int
    created_relations: tuple[str, ...]
    written_relations: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "autovacuum_count": self.autovacuum,
            "autoanalyze_count": self.autoanalyze,
            "counts_by_relation": [list(row) for row in self.counts_by_relation],
            "checkpoints_num_timed": self.checkpoints_timed,
            "checkpoints_num_requested": self.checkpoints_requested,
            "written_relations": list(self.written_relations),
        }


def assert_no_foreign_activity(engine) -> None:
    """No other client backend ANYWHERE in the cluster, and no autovacuum worker.

    An autovacuum worker is not a ``client backend``, so the backend check alone
    misses it. Checked at cell start AND cell end; the counter diff below covers
    a worker that starts and finishes between the two checks.

    ONE benign refusal is possible here. ``pg_statistic`` stays over its analyze
    threshold permanently (see ``assert_catalog_settled``), so every launcher
    visit starts a worker that looks at it and returns having moved no counter
    and logged nothing: about sixty such visits in an hour of measurement. Its
    only trace is an ``autovacuum worker`` row alive for roughly ten
    milliseconds, so this check can catch one at about 10 ms in 60 s. A refusal
    naming ``autovacuum workerx1`` with no matching counter movement anywhere is
    that, and the cell is worth rerunning rather than investigating.
    """
    with engine.connect() as conn:
        foreign = conn.execute(
            text(
                "SELECT backend_type, count(*) FROM pg_stat_activity "
                "WHERE pid <> pg_backend_pid() "
                "AND (backend_type = 'autovacuum worker' "
                "OR (backend_type = 'client backend' AND application_name <> :app)) "
                "GROUP BY backend_type"
            ),
            {"app": APPLICATION_NAME},
        ).all()
    assert_no_foreign_backends(foreign)


def assert_no_foreign_backends(rows) -> None:
    """The decision, split from the query so §2.6 can pin it without a cluster."""
    if rows:
        raise QualificationRefusal(
            "foreign cluster activity invalidates an isolated measurement: "
            + ", ".join(f"{kind}x{count}" for kind, count in rows)
        )


def read_counters(engine, created_relations) -> CellCounters:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("SELECT pg_stat_force_next_flush()"))
        conn.execute(text("SELECT pg_stat_clear_snapshot()"))
        rows = conn.execute(
            text(
                "SELECT schemaname || '.' || relname, autovacuum_count, "
                "autoanalyze_count FROM pg_stat_all_tables "
                "WHERE autovacuum_count + autoanalyze_count > 0 ORDER BY 1"
            )
        ).all()
        timed, requested = conn.execute(
            text("SELECT num_timed, num_requested FROM pg_stat_checkpointer")
        ).one()
        written = conn.execute(
            text(
                "SELECT relname FROM pg_stat_user_tables "
                "WHERE n_tup_ins + n_tup_upd + n_tup_del > 0 ORDER BY relname"
            )
        ).scalars().all()
    by_relation = tuple(
        (str(name), int(vacuums), int(analyzes)) for name, vacuums, analyzes in rows
    )
    return CellCounters(
        autovacuum=sum(row[1] for row in by_relation),
        autoanalyze=sum(row[2] for row in by_relation),
        counts_by_relation=by_relation,
        checkpoints_timed=int(timed),
        checkpoints_requested=int(requested),
        created_relations=tuple(created_relations),
        written_relations=tuple(written),
    )


def moved_counters(before: CellCounters, after: CellCounters) -> dict[str, int]:
    """Per relation, how many autovacuum/autoanalyze events landed in the span."""
    start = {name: (vacuums, analyzes) for name, vacuums, analyzes in before.counts_by_relation}
    moved = {}
    for name, vacuums, analyzes in after.counts_by_relation:
        was_vacuums, was_analyzes = start.get(name, (0, 0))
        delta = (vacuums - was_vacuums) + (analyzes - was_analyzes)
        if delta:
            moved[name] = delta
    return moved


def assert_counters_clean(
    before: CellCounters, after: CellCounters, *, measured=MEASURED_RELATIONS
) -> dict:
    """Empty on entry, no writes outside the created relations, and no
    autovacuum ON A MEASURED RELATION.

    The measured relations carry ``autovacuum_enabled=false`` on heap and TOAST
    alike, so a counter moving there means the setting did not hold and the
    equal explicit vacuum windows the paired comparison rests on are gone: that
    is a cell refusal, and it NAMES the relation. ``measured`` is expected to be
    ``measured_relation_names``' resolved set, which includes each measured
    table's ``pg_toast.pg_toast_<oid>`` relation; matching bare table names
    alone let a TOAST autovacuum pass as catalog churn.

    A CATALOG relation is different in kind. ``pg_statistic`` is rewritten by
    the harness's own ``VACUUM (ANALYZE)`` windows, so its autovacuum is a
    consequence of the measurement rather than foreign traffic; what it costs
    is some WAL and some I/O inside whichever block it lands in. That is the
    same hazard a mid-block checkpoint poses, and it gets the same treatment —
    the count is recorded per block and the PAIR is discarded when the two
    sides differ (``discard_mismatched_blocks``), rather than the whole cell
    being thrown away. ``maintain_catalog`` keeps it rare in the first place.
    """
    moved = moved_counters(before, after)
    # ``measured`` may arrive bare (the default) or already schema-qualified and
    # TOAST-resolved (what ``run_cell`` passes); accept both spellings.
    measured_names = set(measured) | {
        f"public.{name}" for name in measured if "." not in name
    }
    on_measured = sorted(name for name in moved if name in measured_names)
    if on_measured:
        raise QualificationRefusal(
            "autovacuum or autoanalyze touched measured relation(s) "
            f"{on_measured} inside the cell although the harness set "
            "autovacuum_enabled=false on their heap and TOAST; equal explicit "
            "vacuum windows no longer hold"
        )
    foreign = sorted(set(after.written_relations) - set(before.created_relations))
    if foreign:
        raise QualificationRefusal(f"writes to relations the harness did not create: {foreign}")
    return {
        "checkpoints_num_timed": after.checkpoints_timed - before.checkpoints_timed,
        "checkpoints_num_requested": after.checkpoints_requested
        - before.checkpoints_requested,
        "catalog_autovacuum_events": sum(moved.values()),
        "catalog_autovacuum_relations": sorted(moved),
        "written_relations": list(after.written_relations),
    }


def assert_database_empty(engine) -> list[str]:
    """Empty on entry — the database is fresh per cell, so this is total."""
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
    assert_no_carried_over_relations(relations)
    return list(relations)


def assert_no_carried_over_relations(relations) -> None:
    """A second cell may not run in a database that already holds relations.

    One fresh database per cell is what gives the footprint and plateau cells
    relations with no carried-over bloat, and what makes "empty on entry" a
    total statement rather than a per-schema one.
    """
    if relations:
        raise QualificationRefusal(
            f"cell database is not empty on entry ({len(relations)} relations); "
            "each cell requires its own fresh database"
        )


_BLOCK_REF = re.compile(r"rel (\d+)/(\d+)/(\d+)")


def classify_wal(engine, start: str, end: str) -> dict:
    """Attribute WAL by DATABASE OID first, then relfilenode (§2.2).

    The sealed classifier's regex (``bench_opening_score_storage.py:270``)
    discards the tablespace and database OIDs and keeps only the relfilenode, so
    it cannot tell a co-tenant database's write from an unattributed one of our
    own. This one keeps all three:

    * a foreign database OID is CONTAMINATION and invalidates the cell;
    * our own database with an unattributed relfilenode is own-catalog
      OVERHEAD, reported separately and never treated as contamination.

    That replaces the arbitrary percentage threshold an earlier revision
    proposed, which replayed over the approved spike runs would have invalidated
    the harness's own vacuum windows at the small sizes this bead measures.
    """
    with engine.connect() as conn:
        own_oid = conn.execute(
            text("SELECT oid FROM pg_database WHERE datname = current_database()")
        ).scalar_one()
        total = int(
            conn.execute(
                text("SELECT pg_wal_lsn_diff(:end, :start)"),
                {"start": start, "end": end},
            ).scalar_one()
        )
        locators = dict(
            conn.execute(
                text(
                    "SELECT c.relfilenode::text, COALESCE(parent.relname, c.relname) "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "LEFT JOIN pg_index i ON i.indexrelid = c.oid "
                    "LEFT JOIN pg_class parent ON parent.oid = i.indrelid "
                    "WHERE n.nspname = current_schema() AND c.relfilenode <> 0"
                )
            ).all()
        )
        try:
            records = conn.execute(
                text(
                    "SELECT record_length, fpi_length, block_ref "
                    "FROM pg_get_wal_records_info(:start, :end)"
                ),
                {"start": start, "end": end},
            ).all()
        except DBAPIError as exc:
            # At S3 one layout-A publication is ~160 MB against a 128 MB
            # max_wal_size, so a checkpoint can complete mid-publication and
            # recycle the segments holding start_lsn before this reads them.
            # QC-PROD sets wal_keep_size for exactly this reason (§0.2, recorded
            # as a stated difference: it retains segments and changes neither
            # checkpoint timing nor WAL volume).
            if "has already been removed" not in str(exc):
                raise
            raise QualificationRefusal(
                "the WAL segment holding this publication's start LSN was "
                "recycled before it could be classified; raise wal_keep_size on "
                "the qualification cluster and re-run the cell"
            ) from exc

    classified = classify_wal_records(records, own_oid, locators)
    return {
        "total_bytes": total,
        "alignment_and_page_headers": total - classified["record_bytes"],
        **classified,
    }


def classify_wal_records(records, own_oid: int, locators: dict) -> dict:
    """The classification itself, split out so §2.6 can pin it without a cluster."""
    by_relation, fpi = Counter(), Counter()
    record_total = foreign_bytes = own_overhead = shared_catalog = 0
    foreign_databases: Counter = Counter()
    for length, image_length, refs in records:
        record_total += length
        databases = {int(db) for _, db, _ in _BLOCK_REF.findall(refs or "")}
        # Database OID 0 is a SHARED catalog (pg_database, pg_authid, …), not
        # another tenant: VACUUM alone updates pg_database in place, so treating
        # 0 as foreign would invalidate the harness's own vacuum windows.
        if databases and databases <= {SHARED_CATALOG_DATABASE_OID}:
            shared_catalog += length
            own_overhead += length
            by_relation["shared_catalog"] += length
            fpi["shared_catalog"] += image_length
            continue
        foreign = databases - {own_oid, SHARED_CATALOG_DATABASE_OID}
        if foreign:
            foreign_bytes += length
            for oid in foreign:
                foreign_databases[oid] += length
            continue
        names = {
            locators.get(node, "unattributed")
            for _, _, node in _BLOCK_REF.findall(refs or "")
        }
        if len(names) == 1:
            name = next(iter(names))
        else:
            name = "mixed_or_metadata"
        if name in {"unattributed", "mixed_or_metadata"}:
            own_overhead += length
        by_relation[name] += length
        fpi[name] += image_length
    return {
        "record_bytes": record_total,
        "record_bytes_by_relation": dict(by_relation),
        "fpi_bytes_by_relation": dict(fpi),
        "own_catalog_overhead_bytes": own_overhead,
        "shared_catalog_bytes": shared_catalog,
        "foreign_database_bytes": foreign_bytes,
        "foreign_database_oids": {str(k): v for k, v in foreign_databases.items()},
    }


def assert_wal_uncontaminated(classified: dict) -> None:
    if classified["foreign_database_bytes"]:
        raise QualificationRefusal(
            "WAL from a foreign database appeared inside the cell "
            f"({classified['foreign_database_oids']}); the measurement is invalid"
        )


def discard_mismatched_blocks(
    reference: list[dict], selected: list[dict], *, compare_requested: bool = True
) -> dict:
    """Drop any paired ten-publication block whose checkpoint counts differ.

    A checkpoint landing mid-block converts the writes that follow it into
    full-page writes, so two blocks that saw different checkpoints are not
    comparable however well the publications match. A CATALOG autovacuum inside
    one side of a pair is the same hazard by a different route — unequal WAL and
    unequal I/O across the two sides — so the per-block catalog autovacuum count
    joins the comparability tuple. ``num_timed`` and the catalog count are
    ALWAYS compared: both arrive from outside the layouts.

    ``num_requested`` IS DIFFERENT, AND IN A CELL THAT CHECKPOINTS BEFORE EVERY
    PUBLICATION IT IS RECORDED RATHER THAN COMPARED (§2.2, §9.5, amended rev 10).
    A requested checkpoint is WAL-volume-triggered, so in a paired cell it is
    caused BY THE TREATMENT: at the census ``max_wal_size = 128 MB``, PostgreSQL
    requests one after roughly 48-64 MB of WAL, an S3 layout-A publication is
    about 75-80 MB and the matching B50 publication about 6 MB. Comparing the
    count would therefore discard EVERY C2 pair at S3 and most at S2 — and it
    would discard exactly the pairs where A is worst, biasing the retained
    evidence toward A. C2 runs at three sizes and its ceiling fit needs three
    points, so the rule made the cell structurally unpassable at census settings
    while ``settings_homogeneity`` correctly refuses any C2 settings deviation.

    What makes recording safe in C2 specifically is C2's own construction: every
    publication is preceded by an explicit CHECKPOINT, so both sides already
    measure the full-page-write regime, and a mid-publication requested
    checkpoint restarts that regime rather than importing a different one. C2's
    absolute ceiling is fitted from B50 alone, and B50's ~6 MB publications sit
    far below any requested-checkpoint threshold. The asymmetry is counted and
    reported so that a reader sees it; it is not silently dropped.
    """
    if len(reference) != len(selected):
        raise QualificationRefusal("paired blocks must be equal in number")
    kept_reference, kept_selected, discarded = [], [], []
    asymmetric_requested = []
    for index, (a_block, b_block) in enumerate(zip(reference, selected, strict=True)):
        counts = (
            a_block["checkpoints_num_timed"],
            a_block["checkpoints_num_requested"],
            a_block.get("catalog_autovacuum_events", 0),
            b_block["checkpoints_num_timed"],
            b_block["checkpoints_num_requested"],
            b_block.get("catalog_autovacuum_events", 0),
        )
        if counts[1] != counts[4]:
            asymmetric_requested.append(
                {
                    "block": index,
                    "reference_num_requested": counts[1],
                    "selected_num_requested": counts[4],
                }
            )
        comparable = [0, 2] if not compare_requested else [0, 1, 2]
        if all(counts[i] == counts[i + 3] for i in comparable):
            kept_reference.append(a_block)
            kept_selected.append(b_block)
        else:
            discarded.append(
                {
                    "block": index,
                    "reference_num_timed": counts[0],
                    "reference_num_requested": counts[1],
                    "reference_catalog_autovacuum": counts[2],
                    "selected_num_timed": counts[3],
                    "selected_num_requested": counts[4],
                    "selected_catalog_autovacuum": counts[5],
                }
            )
    return {
        "reference": kept_reference,
        "selected": kept_selected,
        "discarded": discarded,
        "complete_paired_blocks": len(kept_reference),
        "requested_checkpoints_compared": compare_requested,
        "asymmetric_requested_blocks": asymmetric_requested,
        "requested_checkpoint_asymmetry": len(asymmetric_requested),
        "requested_checkpoint_note": (
            "compared: an unequal num_requested discarded the pair"
            if compare_requested
            else "recorded, not compared: every publication in this cell starts "
            "from an explicit CHECKPOINT, and a WAL-volume-triggered checkpoint "
            "is caused by the layout under test rather than by foreign traffic "
            "(§2.2 as amended)"
        ),
    }


# --------------------------------------------------------------------------
# §1.3 / §4.3 — replay sequence construction
# --------------------------------------------------------------------------


def ping_pong_sequence(cutoffs: int, length: int) -> list[int]:
    """1…N, N−1…2, 1…N, … — period 2(N−1), every step an ADJACENT-cutoff diff.

    Not cycling, and not "1…N, N−1…1" either. Cycling wraps from cutoff N back to
    cutoff 1, and ``fixed_membership`` (``remeasure_opening_score_budgets.py:61``)
    unions MEMBERSHIP but leaves each candidate's ROW VALUES as captured, so that
    wrap is a jump across the whole capture window: B50's exact diff rewrites
    nearly every row, and at N = 10 that lands one publication in ten, above the
    5% tail, so B50's WAL and publication p95 would BE the wrap step rather than
    steady state. The "N−1…1" spelling has the opposite flaw — it publishes
    cutoff 1 twice in a row, a ZERO-diff step where B50 writes almost nothing
    while A rewrites every row regardless, flattering B50 in exactly the
    direction the main WAL gate measures.

    However long the sequence runs it contains only N−1 DISTINCT adjacent steps,
    so callers record that count beside every p95 derived from it: at N = 10, 100
    publications carry nine independent diffs and must not be read as 100.
    """
    if cutoffs < 2:
        raise QualificationRefusal("ping-pong requires at least two cutoffs")
    period = 2 * (cutoffs - 1)
    indices = []
    for step in range(length):
        position = step % period
        indices.append(position if position < cutoffs else period - position)
    return indices


def sequence_provenance(indices: list[int], cutoffs: int) -> dict:
    """Forward and backward changed-row fractions stay separable (§1.3)."""
    directions = [
        "forward" if b > a else "backward"
        for a, b in zip(indices, indices[1:], strict=False)
    ]
    repeats = [i for i, (a, b) in enumerate(zip(indices, indices[1:], strict=False)) if a == b]
    if repeats:
        raise QualificationRefusal(
            f"two consecutive publications share a cutoff at steps {repeats}"
        )
    return {
        "publications": len(indices),
        "capture_cutoffs": cutoffs,
        "distinct_adjacent_steps": cutoffs - 1,
        "period": 2 * (cutoffs - 1),
        "step_directions": directions,
        "forward_steps": directions.count("forward"),
        "backward_steps": directions.count("backward"),
        "caveat": (
            "ping-pong repeats steps rather than adding new ones; every p95 "
            "derived from this sequence rests on distinct_adjacent_steps "
            "independent diffs, not on publications"
        ),
    }


def qualification_scale(candidate, copies: int):
    """``workload.scale`` with copy 0 restored to LEGAL keys.

    ``scale`` rewrites every key field to ``<fen>|replica:NNNN`` for ALL copies
    INCLUDING copy 0 (``opening_score_storage_workload.py:240``), and the shipped
    reader normalizes every incoming FEN through ``chess.Board``
    (``_normalize_lookup_fen`` → ``app/fen.py:14``), which raises
    ``ValueError: invalid en passant part`` on such a key. Copy 0 is therefore
    stripped back to its captured keys and re-sorted, so it is a legal-key
    dataset the real reader can address; copies 1..n-1 keep their suffixes and
    add only bulk. Every measured read addresses copy-0 keys, which are also the
    only ones reachable from a legal line.
    """
    from dataclasses import replace as _replace

    from scripts.opening_score_storage_workload import (
        FIELDS,
        MODELS,
        Payload,
        scale,
        sorted_rows,
    )

    if copies == 1:
        return candidate
    scaled = scale(candidate, copies)
    suffix = "|replica:0000"
    groups = {}
    for name in MODELS:
        rows = []
        for row in scaled.payload.rows(name):
            for field, value in list(row.items()):
                if isinstance(value, str) and value.endswith(suffix):
                    row[field] = value[: -len(suffix)]
            rows.append(tuple(row[field] for field in FIELDS[name]))
        groups[name] = sorted_rows(name, rows)
    payload = Payload(**groups)
    scope = payload.rows("scope")
    return _replace(
        scaled,
        payload=payload,
        freshness=_replace(
            scaled.freshness,
            shared_raw_fens=tuple(r["fen"] for r in scope if r["kind"] == "raw"),
            shared_norm_fens=tuple(r["fen"] for r in scope if r["kind"] == "norm"),
        ),
    )


def logical_rows(payload) -> int:
    """Positions, roots, edges and scope counted ONCE.

    The marker, every index and every MVCC version are excluded, because the
    denominator has to mean the same thing in a ceiling as it does in a census.
    """
    from scripts.opening_score_storage_workload import MODELS

    return sum(len(getattr(payload, name)) for name in MODELS)


# --------------------------------------------------------------------------
# §2.1 — scheduler isolation, publication, and the two read composites
# --------------------------------------------------------------------------


def scheduler_isolation():
    """Stub the scheduler AT CLASS LEVEL and record what a timed path requested.

    ``_scheduler`` is a module singleton created at import
    (``opening_score_scheduler.py:1085``) whose ``session_factory`` defaults to
    ``app.db.SessionLocal`` with ``auto_start=True``, so an enqueue from a timed
    read would start a real worker against the harness database. Patching the
    CLASS (never the instance) reaches that singleton through ordinary attribute
    lookup, which is why the rule is what it is. Requests are RECORDED rather
    than refused, because the optional route smoke run expects exactly one
    ``TREE_READER_WARM`` per call; the cells assert the list is empty.
    """
    from contextlib import ExitStack
    from unittest.mock import patch

    from app.opening_score_scheduler import OpeningScoreScheduler

    requests: list[dict] = []

    def _record(_self, user_id, player_color, *, source):
        requests.append(
            {
                "user_id": user_id,
                "player_color": player_color,
                "source": getattr(source, "value", str(source)),
            }
        )

    def _refuse(_self, *_args, **_kwargs):
        raise QualificationRefusal(
            "a timed path attempted a scheduler dispatch; reads must not enqueue"
        )

    stack = ExitStack()
    stack.enter_context(patch.object(OpeningScoreScheduler, "request_recompute", _record))
    for name in ("refresh_now", "run_due", "flush_pending", "start"):
        stack.enter_context(patch.object(OpeningScoreScheduler, name, _refuse))
    stack.enter_context(patch.dict(os.environ, {"POSTHOG_DISABLED": "true"}))
    return stack, requests


def publish(session_factory, owner: int, color: str, candidate, storage_format):
    """One publication through the SHIPPED writer, format passed EXPLICITLY.

    Never through ``default_storage_format`` patching: §4.6's memory children are
    SPAWNED and re-import unpatched modules, so a patch-based knob would silently
    publish legacy there while the parent believed otherwise. The kwarg has
    exactly one resolution point (``opening_cache.py:1474``), so passing it
    explicitly fully determines the format — and the emitted marker is asserted
    after EVERY publication, not once per cell.

    ``PublicationSuperseded`` propagates: timing a discarded candidate as a
    successful write would invalidate the sample.
    """
    import time
    from unittest.mock import patch

    from app import opening_cache as oc
    from scripts.opening_score_storage_workload import replay_objects

    roots, positions, overlay = replay_objects(candidate)
    started = time.perf_counter()
    with (
        session_factory() as db,
        patch.object(oc, "_build_cached_scores", return_value=(roots, positions)),
    ):
        batch = oc.recompute_opening_scores(
            db,
            owner,
            color,
            storage_format=storage_format,
            overlay=overlay,
            freshness=candidate.freshness,
            computed_at=candidate.computed_at,
        )
        emitted, marker, generation = (
            batch.storage_format,
            batch.id,
            batch.generation,
        )
    elapsed_ms = (time.perf_counter() - started) * 1000
    if emitted != storage_format.value:
        raise QualificationRefusal(
            f"publication emitted {emitted!r}, not the requested "
            f"{storage_format.value!r}"
        )
    return {
        "publish_ms": elapsed_ms,
        "batch_id": marker,
        "generation": generation,
        "storage_format": emitted,
    }


def composite_d(db, owner: int, color: str, fens, parents) -> dict:
    """Direct reads, timed as ONE unit; per-component times are diagnostics.

    Rev 2's "800 reads over five entry points" pooled unlike workloads at 160
    each, below the 500-per-comparable-window rule. These five statements are one
    comparable sample, exactly as the spike timed its three queries together.

    ``load_cached_rows_nonblocking`` is deliberately absent: its warm path calls
    ``request_recompute`` on every read (``opening_cache.py:947``).
    ``list_cached_opening_scores`` (``:825``) is the same one-statement reader
    shape A in both formats and enqueues nothing.

    The epoch passed to both proofs is THE BATCH'S OWN, so ``EXISTS`` finds no
    change and the statement probes every scope row instead of short-circuiting
    on the first hit — which is the plan shape the ceiling is about.
    """
    import time

    from app import opening_cache as oc
    from app.opening_score_delta import (
        _marker_rooted_change,
        _shared_invalidation_statement,
        _shared_scope_change_statement,
    )

    components: dict[str, float] = {}
    started = time.perf_counter()

    stage = time.perf_counter()
    view, root_rows = oc.list_cached_opening_scores(db, owner, color)
    components["roots_ms"] = (time.perf_counter() - stage) * 1000
    if view is None:
        raise QualificationRefusal("composite D ran against a pair with no marker")
    handle = view.handle
    if view.cache_epoch is None:
        raise QualificationRefusal("composite D requires the batch's own epoch")

    stage = time.perf_counter()
    positions = oc.lookup_position_scores_for_batch(db, handle, fens)
    components["position_rows_ms"] = (time.perf_counter() - stage) * 1000

    stage = time.perf_counter()
    edges = oc.lookup_observed_edges_for_parents(db, handle, parents)
    components["observed_edges_ms"] = (time.perf_counter() - stage) * 1000

    stage = time.perf_counter()
    scope_changed = _marker_rooted_change(
        db, handle, _shared_scope_change_statement(db, handle, view.cache_epoch)
    )
    components["scope_proof_ms"] = (time.perf_counter() - stage) * 1000

    stage = time.perf_counter()
    kind_invalidated = _marker_rooted_change(
        db, handle, _shared_invalidation_statement(db, handle, view.cache_epoch)
    )
    components["invalidation_proof_ms"] = (time.perf_counter() - stage) * 1000

    return {
        "composite_ms": (time.perf_counter() - started) * 1000,
        "components_ms": components,
        "root_rows": len(root_rows),
        "position_rows": len(positions),
        "edge_parents": len(edges),
        "scope_changed": scope_changed,
        "kind_invalidated": kind_invalidated,
        "storage_format": view.storage_format,
    }


def composite_t(db, graph, roots, routing, owner: int, color: str, moves, opening) -> dict:
    """The ``/tree`` route's own loop body, driven directly — NOT the route.

    The route calls ``ensure_tree_cache`` (``api/openings.py:1642``) before the
    wave, and its warm-fresh branch fires ``request_recompute(...,
    TREE_READER_WARM)`` (``opening_cache.py:1135-1138``), so driving 600 T reads
    through it would make 600 scheduler requests against a cell that asserts
    zero. The route span also adds the registry fingerprint pass, ``routing_view``,
    the structural columns, JSON serialization and the ASGI threadpool — none
    format-dependent, all of which dilute the A/B ratio so a real regression
    could hide under 1.1. Entering ``TestClient`` as a context manager would
    additionally run the app lifespan, starting four schedulers plus prewarm
    against ``SessionLocal`` (``app/main.py:41-96``).

    ``graph``, ``roots`` and ``routing`` are built ONCE per cell, outside every
    timed span. The gate is on the FORMAT-DEPENDENT stages only —
    ``observed_prefetch_ms + position_rows_ms``; the other stages either never
    touch the database or read the analysis cache rather than score storage
    (``api/openings.py:1353-1362``). Builder total is retained as a diagnostic.
    """
    import time

    from app.api.openings import _OpeningTreeBuilder
    from app.opening_score_storage import handle_is_live, latest_batch_view

    timings: dict = {}
    started = time.perf_counter()
    view = latest_batch_view(db, owner, color)
    if view is None:
        raise QualificationRefusal("composite T ran against a pair with no marker")
    builder = _OpeningTreeBuilder(
        db, graph, roots, view.handle, color, owner, routing=routing
    )
    builder.build(moves, opening, timings=timings)
    live = handle_is_live(db, view.handle)
    total_ms = (time.perf_counter() - started) * 1000
    stragglers = builder._observed_straggler_count
    if stragglers:
        # A prefetch miss falls back to a single point query issued inside the
        # UNGATED structural_columns stage (api/openings.py:884-887), which moves
        # storage work out of the gated window entirely and silently shrinks the
        # measured ratio. Fail the read rather than record a flattered one.
        raise QualificationRefusal(
            f"composite T saw {stragglers} straggler reads; the gated window is "
            "no longer the whole storage cost"
        )
    if not live:
        raise QualificationRefusal("composite T's marker retired mid-read")
    format_stage_ms = float(timings.get("observed_prefetch_ms", 0.0)) + float(
        timings.get("position_rows_ms", 0.0)
    )
    return {
        "composite_ms": total_ms,
        "format_stage_ms": format_stage_ms,
        "builder_total_ms": float(timings.get("total_ms", total_ms)),
        "observed_prefetch_ms": float(timings.get("observed_prefetch_ms", 0.0)),
        "position_rows_ms": float(timings.get("position_rows_ms", 0.0)),
        "observed_edge_query_count": builder._observed_edge_query_count,
        "observed_straggler_count": stragglers,
        "storage_format": view.storage_format,
    }


# --------------------------------------------------------------------------
# §1.3 / §2.1 — shared evidence, footprint snapshots, profile identity
# --------------------------------------------------------------------------

# Both layouts publish into ONE cell database under distinct synthetic owners.
# The two formats write DISJOINT relation groups, so a shared database gives
# each layout a separately measurable footprint while holding the checkpoint and
# vacuum schedule, the cluster and the wall clock identical between them — which
# is what makes a paired block a paired block. Real production user ids never
# reach an artifact.
OWNER_BY_LAYOUT = {"A": 1, "B50": 2}
LEGACY_RELATIONS = (
    "user_opening_scores",
    "opening_position_scores",
    "opening_position_edges",
    "opening_score_batch_shared_scope",
)
CURRENT_RELATIONS = (
    "opening_current_roots",
    "opening_current_positions",
    "opening_current_edges",
    "opening_current_scope",
)
SHARED_RELATIONS = ("opening_score_batches", "opening_score_cursors")
RELATIONS_BY_LAYOUT = {"A": LEGACY_RELATIONS, "B50": CURRENT_RELATIONS}


def copy_shared_evidence(engine, versions: list[dict], invalidations: list[dict]) -> dict:
    """Copy BOTH global shared-evidence tables in WHOLE, then ANALYZE.

    ``shared_evidence_scope_versions`` is keyed ``(kind, fen)`` and
    ``shared_evidence_scope_invalidations`` by ``(kind)``, neither with an owner
    column (``models.py:1300-1334``), so there is no per-pair subset to take.
    Copying only the FENs a pair's scope references would make every probe a hit
    against an index far smaller than production's — the opposite of what the
    ceiling is meant to measure — and on an empty table the per-scope-row index
    probe and the collation plan ``_shared_probe`` protects
    (``opening_score_delta.py``) would never execute at all.
    """
    from sqlalchemy.orm import Session

    from app.models import SharedEvidenceScopeInvalidation, SharedEvidenceScopeVersion

    with Session(engine) as db:
        existing = {
            (row.kind, row.fen)
            for row in db.query(
                SharedEvidenceScopeVersion.kind, SharedEvidenceScopeVersion.fen
            )
        }
        db.bulk_insert_mappings(
            SharedEvidenceScopeVersion,
            [row for row in versions if (row["kind"], row["fen"]) not in existing],
        )
        present = {row.kind for row in db.query(SharedEvidenceScopeInvalidation.kind)}
        db.bulk_insert_mappings(
            SharedEvidenceScopeInvalidation,
            [row for row in invalidations if row["kind"] not in present],
        )
        db.commit()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for name in ("shared_evidence_scope_versions", "shared_evidence_scope_invalidations"):
            conn.execute(text(f"ANALYZE {name}"))
        counts = {
            name: conn.execute(text(f"SELECT count(*) FROM {name}")).scalar_one()
            for name in (
                "shared_evidence_scope_versions",
                "shared_evidence_scope_invalidations",
            )
        }
    return counts


def footprint(engine, *, vacuum=False) -> dict:
    """Reuse the sealed snapshot instrumentation through a minimal shim.

    ``bench_opening_score_storage.snapshot`` wants only ``.engine`` and
    ``.tables``; handing it those keeps the footprint and dead-tuple accounting
    byte-identical to the approved run's rather than reimplementing it here.
    """
    from types import SimpleNamespace

    from scripts.bench_opening_score_storage import snapshot

    shim = SimpleNamespace(engine=engine, tables=list(MEASURED_RELATIONS))
    result = snapshot(shim, vacuum=vacuum)
    for layout, relations in RELATIONS_BY_LAYOUT.items():
        result[f"{layout}_total_bytes"] = sum(
            result["relations"][name]["total_bytes"] for name in relations
        )
        result[f"{layout}_live_tuples"] = sum(
            result["relations"][name]["live_tuples"] for name in relations
        )
        result[f"{layout}_dead_tuples"] = sum(
            result["relations"][name]["dead_tuples"] for name in relations
        )
    result["shared_marker_bytes"] = sum(
        result["relations"][name]["total_bytes"] for name in SHARED_RELATIONS
    )
    return result


def profile_identity(engine, census: dict | None = None) -> dict:
    """§0.4 — the FULL stated-differences list, not a version string.

    The PostgreSQL build and its absolute binary prefix are part of host
    identity: the default ``PATH`` here is PostgreSQL 15.18 (Homebrew), so a bare
    ``initdb``/``pg_dump`` is the wrong build entirely, and production runs a
    different MINOR release from either local 18.4 build. That minor difference
    is immaterial to WAL and footprint — a minor release is binary compatible and
    changes neither WAL record formats nor on-disk layout — and it is RECORDED
    rather than corrected, because the build is part of what was measured.
    """
    with engine.connect() as conn:
        settings = {
            name: {"setting": setting, "unit": unit, "source": source}
            for name, setting, unit, source in conn.execute(
                text(
                    "SELECT name, setting, unit, source FROM pg_settings WHERE name = ANY(:names)"
                ),
                {
                    "names": [
                        "server_version",
                        "shared_buffers",
                        "checkpoint_timeout",
                        "checkpoint_completion_target",
                        "max_wal_size",
                        "min_wal_size",
                        # §0.2 sets wal_keep_size so pg_walinspect cannot lose a
                        # recycled segment mid-cell. A setting the harness asks
                        # for and does not RECORD cannot appear in the stated
                        # differences, and the evaluator diffs this mapping.
                        "wal_keep_size",
                        # fsync is not something this bead changes, and that is
                        # exactly why it is recorded: every WAL and footprint
                        # figure here assumes it is on, so a cell measured with
                        # it off must be visible as a settings deviation rather
                        # than silently pooled.
                        "fsync",
                        "full_page_writes",
                        "wal_compression",
                        "wal_level",
                        "synchronous_commit",
                        "autovacuum",
                        "autovacuum_naptime",
                        # Every threshold ``eligible_relations`` decides on. A
                        # cluster that raised any of them would make the cell's
                        # "nothing is eligible" claim weaker without changing a
                        # line of this file, so all six are recorded and the
                        # evaluator diffs them like any other setting.
                        "autovacuum_vacuum_threshold",
                        "autovacuum_vacuum_scale_factor",
                        "autovacuum_vacuum_insert_threshold",
                        "autovacuum_vacuum_insert_scale_factor",
                        "autovacuum_analyze_threshold",
                        "autovacuum_analyze_scale_factor",
                        "autovacuum_vacuum_cost_delay",
                        "work_mem",
                        "maintenance_work_mem",
                        "default_toast_compression",
                        "data_checksums",
                        "block_size",
                        "cluster_name",
                        "max_connections",
                        "random_page_cost",
                        "effective_cache_size",
                    ]
                },
            ).all()
        }
        database = dict(
            zip(
                ("datcollate", "datctype", "datlocprovider", "encoding"),
                conn.execute(
                    text(
                        "SELECT datcollate, datctype, datlocprovider::text, "
                        "pg_encoding_to_char(encoding) FROM pg_database "
                        "WHERE datname = current_database()"
                    )
                ).one(),
                strict=True,
            )
        )
        version = conn.execute(text("SELECT version()")).scalar_one()
    identity = {
        "postgres_version": version,
        "postgres_binary_prefix": os.environ.get(
            "GHOSTREPLAY_STORAGE_QUAL_PG_PREFIX", "unrecorded"
        ),
        "settings": settings,
        "database": database,
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python": platform.python_version(),
        },
        "tested_revision": _git_revision(),
    }
    if census is not None:
        identity["stated_differences"] = _stated_differences(identity, census)
    return identity


def _git_revision() -> str:
    """The revision — and whether the tree it ran from actually MATCHED it.

    A bare ``rev-parse HEAD`` stamps a clean sha onto a run whose working tree
    was modified, and that is the one identity claim a later reader cannot
    falsify: the commit exists, the sha resolves, nothing looks wrong. The
    suffix costs one more subprocess and makes the claim checkable.

    Untracked files are ignored deliberately — this repository is edited by
    several agents at once and unrelated untracked paths are expected (AGENTS.md)
    — but a MODIFIED TRACKED file is exactly the case that matters: it means the
    run came from the shared working tree rather than from §2.7's pinned clone.
    §5's homogeneity check compares revisions for equality, so a dirty cell can
    no longer pass as a clean one.
    """
    root = str(Path(__file__).resolve().parents[2])
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        modified = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - thin
        return "unknown"
    return f"{head}-dirty" if modified else head


def _stated_differences(identity: dict, census: dict) -> list[dict]:
    """Every census value this host could not match, named rather than implied."""
    differences = []
    local = identity["settings"]
    remote = census.get("settings", {})
    for name, value in remote.items():
        mine = local.get(name, {}).get("setting")
        if mine is not None and mine != value.get("setting"):
            differences.append(
                {"kind": "setting", "name": name, "production": value.get("setting"), "local": mine}
            )
    for field, value in census.get("database", {}).items():
        mine = identity["database"].get(field)
        if mine is not None and str(mine) != str(value):
            differences.append(
                {"kind": "database", "name": field, "production": value, "local": mine}
            )
    differences.append(
        {
            "kind": "host",
            "name": "cpu_storage_rss_collation_network",
            "production": census.get("host", {}),
            "local": identity["host"],
            "note": (
                "A-relative ratios transfer between hosts; absolute "
                "host-dependent ceilings do not. Apple Silicon vs Railway vCPUs, "
                "macOS vs Linux fsync and RSS accounting, libc collation matched "
                "by NAME and PROVIDER only, and no matched "
                "application-to-database network path exists locally."
            ),
        }
    )
    return differences


# --------------------------------------------------------------------------
# §4.3 / §4.4 — C1 (warm, fixed membership) and C2 (post-checkpoint, growing)
# --------------------------------------------------------------------------


def _confidence_change_fraction(previous, current) -> dict:
    """A STATED EXPECTATION, compared — never an assertion that can only pass.

    An earlier revision claimed confidence writes are "dense by construction"
    because each publication advances ``computed_at``. That is false, and it
    biased the main WAL gate toward passing: in replay ``_build_cached_scores``
    is patched to return the CAPTURED rows, so the replay's ``computed_at``
    stamps the marker and changes no payload row. Density is fixed at CAPTURE
    time, which is why the capture pins the scoring clock to each cutoff's real
    session timestamp. A materially lower fraction than the fixture's means the
    capture's clock spacing is unrepresentative and the WAL results are
    optimistic — a finding against the profile, not a silent pass.
    """
    from scripts.opening_score_storage_workload import changed

    groups = changed(previous, current)["groups"]
    positions = groups["positions"]
    common = positions["common"] or 1
    return {
        "confidence_changed_fraction": positions["confidence_changed"] / common,
        "stable_changed_fraction": positions["stable_changed"] / common,
        "inserted": positions["inserted"],
        "deleted": positions["deleted"],
        "common": positions["common"],
    }


def run_paired_cell(
    engine,
    session_factory,
    candidates,
    indices,
    *,
    cell: str,
    color: str,
    created_relations,
    reads_per_publication=(0, 0),
    checkpoint_before_each=False,
    vacuum_every=10,
    read_inputs=None,
    tree_requests=None,
    partial=None,
    measured_relations=MEASURED_RELATIONS,
) -> dict:
    """Both layouts, ten-publication blocks, alternating AB/BA.

    Both layouts replay the IDENTICAL sequence, under the same cluster, the same
    checkpoint schedule and the same explicit vacuum windows, so the A-relative
    comparison the acceptance gates rest on is a paired one. Every individual
    sample is retained; nothing is summarised away here.

    ``partial`` is a dict OWNED BY THE CALLER, wired to the live sample lists
    before the first publication. ``run_cell`` writes its report from a
    ``finally``, and without this the retained samples were locals here and died
    with the exception — so a cell that refused in its last minute wrote a
    report claiming "every sample collected before this point is retained" and
    carrying none of them.
    """
    import time

    from app import opening_cache as oc
    from app.opening_densify import routing_view
    from app.opening_score_storage import StorageFormat
    from sqlalchemy.orm import Session

    d_reads, t_reads = reads_per_publication
    formats = {"A": StorageFormat.LEGACY, "B50": StorageFormat.CURRENT}
    graph = oc.get_opening_graph()
    roots_index = oc.get_opening_roots()
    routing = routing_view(graph)  # built ONCE, outside every timed span

    records: dict[str, list] = {"A": [], "B50": []}
    blocks: dict[str, list] = {"A": [], "B50": []}
    windows: list[dict] = []
    stack, scheduler_requests = scheduler_isolation()
    result = {} if partial is None else partial
    result.update(
        {
            "cell": cell,
            "records": records,
            "blocks": blocks,
            "paired_blocks": {
                "reference": [],
                "selected": [],
                "discarded": [],
                "complete_paired_blocks": 0,
                "pairing": "not reached: the cell did not finish its blocks",
            },
            "vacuum_windows": windows,
            "scheduler_requests": scheduler_requests,
            "reads_per_publication": {"composite_d": d_reads, "composite_t": t_reads},
            "complete": False,
        }
    )
    with stack:
        for block_index in range(0, len(indices), 10):
            step_indices = indices[block_index : block_index + 10]
            order = ("A", "B50") if (block_index // 10) % 2 == 0 else ("B50", "A")
            for layout in order:
                owner = OWNER_BY_LAYOUT[layout]
                before = read_counters(engine, created_relations)
                block_started = time.perf_counter()
                for offset, cutoff in enumerate(step_indices):
                    candidate = candidates[cutoff]
                    if checkpoint_before_each:
                        with engine.connect().execution_options(
                            isolation_level="AUTOCOMMIT"
                        ) as conn:
                            conn.execute(text("CHECKPOINT"))
                    start_lsn = _lsn(engine)
                    # NOT ``result``: that name is the caller-owned ``partial``
                    # dict, and rebinding it here left every later write —
                    # ``paired_blocks``, ``complete`` — on the last publication's
                    # return value instead of on the cell result. The report then
                    # carried "pairing: not reached" and zero kept blocks for a
                    # cell that had run to the end, and the evaluator marked every
                    # gate of every paired cell ``insufficient_evidence``.
                    published = publish(
                        session_factory, owner, color, candidate, formats[layout]
                    )
                    end_lsn = _lsn(engine)
                    wal = classify_wal(engine, start_lsn, end_lsn)
                    assert_wal_uncontaminated(wal)
                    step = block_index + offset
                    previous = candidates[indices[step - 1]] if step else None
                    record = {
                        "layout": layout,
                        "cell": cell,
                        "block": block_index // 10,
                        "step": step,
                        "cutoff": cutoff,
                        "direction": (
                            None
                            if step == 0
                            else ("forward" if cutoff > indices[step - 1] else "backward")
                        ),
                        "post_checkpoint": checkpoint_before_each,
                        "publish_ms": published["publish_ms"],
                        "storage_format": published["storage_format"],
                        "wal": wal,
                        "logical_rows": logical_rows(candidate.payload),
                    }
                    if previous is not None:
                        record["confidence"] = _confidence_change_fraction(
                            previous.payload, candidate.payload
                        )
                    if d_reads or t_reads:
                        with Session(engine) as db:
                            record["composite_d"] = [
                                composite_d(
                                    db,
                                    owner,
                                    color,
                                    read_inputs["fens"],
                                    read_inputs["parents"],
                                )
                                for _ in range(d_reads)
                            ]
                            record["composite_t"] = [
                                composite_t(
                                    db,
                                    graph,
                                    roots_index,
                                    routing,
                                    owner,
                                    color,
                                    *tree_requests[i % len(tree_requests)],
                                )
                                for i in range(t_reads)
                            ]
                    records[layout].append(record)
                after = read_counters(engine, created_relations)
                blocks[layout].append(
                    {
                        "block": block_index // 10,
                        "layout": layout,
                        "order_position": order.index(layout),
                        "elapsed_s": time.perf_counter() - block_started,
                        **assert_counters_clean(
                            before, after, measured=measured_relations
                        ),
                    }
                )
            if vacuum_every and (block_index + 10) % vacuum_every == 0:
                windows.append(
                    _vacuum_window(
                        engine,
                        block_index // 10,
                        created_relations=created_relations,
                        measured_relations=measured_relations,
                    )
                )
                windows[-1]["after_block"] = block_index // 10
        assert_no_foreign_activity(engine)
    if scheduler_requests:
        raise QualificationRefusal(
            f"timed paths made {len(scheduler_requests)} scheduler requests; "
            "the measured reads must enqueue nothing"
        )
    result["paired_blocks"] = discard_mismatched_blocks(
        blocks["A"], blocks["B50"], compare_requested=not checkpoint_before_each
    )
    result["complete"] = True
    return result


def _lsn(engine) -> str:
    from scripts.bench_opening_score_storage import lsn

    return lsn(engine)


def _vacuum_window(
    engine,
    index: int,
    *,
    created_relations=(),
    measured_relations=MEASURED_RELATIONS,
) -> dict:
    """One EXPLICIT vacuum window, equal for both layouts, WAL measured and SPLIT.

    One window vacuums every measured relation, so its WAL total covers BOTH
    layouts. Adding that total to each side of the combined-WAL gate adds a
    common constant to numerator and denominator and drags the ratio toward 1 —
    with A at 28 MB of vacuum WAL against B50's 0.24 MB, an A-relative ratio of
    0.29 is reported as 0.53 and the ≤ 0.5 gate fails on arithmetic rather than
    on the design. Worse in the other direction: fitting B50's absolute vacuum
    ceiling from the whole window is fitting it largely from A's vacuum, which
    is exactly the "a slower A must not loosen an absolute ceiling" rule.
    ``record_bytes_by_relation`` already attributes per relation, so the split
    is measured, not apportioned.
    """
    counters_before = read_counters(engine, created_relations)
    before = footprint(engine)
    start_lsn = _lsn(engine)
    after = footprint(engine, vacuum=True)
    end_lsn = _lsn(engine)
    wal = classify_wal(engine, start_lsn, end_lsn)
    assert_wal_uncontaminated(wal)
    # AFTER the measured span. The maintenance is upkeep for the harness's own
    # vacuum, not part of either layout's vacuum cost, so it stays outside the
    # LSN range; putting it here rather than before the span is also what leaves
    # the FOLLOWING block starting from a settled ``pg_statistic``, which is
    # where an autovacuum would otherwise have landed and cost a pair. What it
    # maintains is DISCOVERED, and the result is asserted: a window that cannot
    # bring the database back under every autovacuum threshold refuses the cell
    # here rather than letting the next block absorb the launcher's pass.
    catalog = assert_catalog_settled(maintain_catalog(engine))
    counters_after = read_counters(engine, created_relations)
    return {
        "window": index,
        "before_vacuum": before,
        "after_vacuum": after,
        "vacuum_wal": wal,
        **catalog,
        **split_vacuum_wal(wal),
        # The counter diff used to span BLOCKS only, so the vacuum windows — and
        # the whole of C5, which is nothing but windows — were the one part of a
        # cell where an autovacuum on a measured relation could pass unobserved.
        **assert_counters_clean(counters_before, counters_after, measured=measured_relations),
    }


def split_vacuum_wal(wal: dict) -> dict:
    """Attribute a vacuum window's WAL per layout, from measured block refs."""
    by_relation = wal["record_bytes_by_relation"]
    attributed = {
        layout: sum(by_relation.get(name, 0) for name in relations)
        for layout, relations in RELATIONS_BY_LAYOUT.items()
    }
    return {
        "vacuum_wal_by_layout": attributed,
        "vacuum_wal_shared_and_unattributed_bytes": (
            wal["record_bytes"] - sum(attributed.values())
        ),
        "attribution_note": (
            "per-layout bytes are pg_walinspect block references resolved "
            "through pg_class, never the window total apportioned; the marker, "
            "the cursor and own-catalog records stay in the shared remainder "
            "and belong to neither layout"
        ),
    }


# --------------------------------------------------------------------------
# §4.8 — C5: plateau, MVCC reclamation, orphans
# --------------------------------------------------------------------------

# The approved spike cell emits one window per ten publications and reads the
# LAST FIVE of them, so five is the floor below which the statistic has no
# meaning at all.
PUBLICATIONS_PER_PLATEAU_WINDOW = 10
MINIMUM_PLATEAU_WINDOWS = 5


def run_plateau_cell(
    engine,
    session_factory,
    candidates,
    indices,
    color: str,
    partial=None,
    *,
    created_relations=(),
    measured_relations=MEASURED_RELATIONS,
) -> dict:
    """Fixed-working-set plateau, reader-held dead tuples, and orphan proofs.

    Genuine evidence growth is reported SEPARATELY from dead tuples and leaked
    data: a fixed working set that grows is a leak, while a growing working set
    that grows is the workload, and one number cannot say which happened.

    TEN WINDOWS, AND THE APPROVED FORMULA (corrected rev 16). The window count
    was a hard-coded ``range(6)`` that nothing decided: every "sixty" in the
    plan belongs to C2 (§4.4), and the approved shape is the spike's fixed
    cell, which ran 100 publications and emits one window per ten — so its last
    five were windows five to nine, where layout A measured 1.5%, while six
    windows put the last five at the steepest part of A's settling. The count
    is now derived from the cell's own publication spec, so the two cannot
    drift apart again. The growth statistic is the spike's
    ``(max - min) / min`` over the last five rather than
    ``(last - first) / first``: the two agree exactly on a monotone series and
    the spike's is STRICTER when the series oscillates, which is the case the
    looser one was least able to see — layout A at the fixture size alternates
    between two footprints rather than converging, and reads 0.0664 by the old
    formula against 0.0741 by this one.
    """
    from app.opening_score_storage import StorageFormat

    formats = {"A": StorageFormat.LEGACY, "B50": StorageFormat.CURRENT}
    windows: dict[str, list] = {"A": [], "B50": []}
    # BEFORE ``scheduler_isolation``, and that ordering is load-bearing: it
    # enters its patches EAGERLY, at the call, while only ``with stack:``
    # unwinds them. Anything that raises in between leaves the scheduler class
    # patched for the rest of the process — in-process callers (the §2.6 tests)
    # then see every later dispatch refused by a cell that already failed.
    # Nothing used to raise there, so the hazard had never fired.
    # One window per ten publications, as the approved spike cell does it.
    window_count = len(indices) // PUBLICATIONS_PER_PLATEAU_WINDOW
    if window_count < MINIMUM_PLATEAU_WINDOWS:
        raise QualificationRefusal(
            f"a plateau cell of {len(indices)} publications yields "
            f"{window_count} windows; the growth statistic reads the LAST FIVE, "
            f"so it needs at least {MINIMUM_PLATEAU_WINDOWS}"
        )
    stack, scheduler_requests = scheduler_isolation()
    result = {} if partial is None else partial
    result.update(
        {"cell": "C5", "windows": windows, "window_count": window_count,
         "complete": False}
    )
    with stack:
        for layout, storage_format in formats.items():
            owner = OWNER_BY_LAYOUT[layout]
            for window in range(window_count):
                # The counter diff used to start at the window, so C5's ten
                # publications between windows were the one span of a cell where
                # an autovacuum on a measured relation could pass unobserved.
                # C5 is nothing but publications and windows, so "the windows are
                # diffed" left most of the cell undiffed.
                counters_before = read_counters(engine, created_relations)
                span = slice(
                    window * PUBLICATIONS_PER_PLATEAU_WINDOW,
                    (window + 1) * PUBLICATIONS_PER_PLATEAU_WINDOW,
                )
                for cutoff in indices[span]:
                    publish(
                        session_factory, owner, color, candidates[cutoff], storage_format
                    )
                counters_after = read_counters(engine, created_relations)
                entry = _vacuum_window(
                    engine,
                    window,
                    created_relations=created_relations,
                    measured_relations=measured_relations,
                )
                entry["publication_span"] = assert_counters_clean(
                    counters_before, counters_after, measured=measured_relations
                )
                windows[layout].append(entry)

        # One slow reader holding a REPEATABLE READ snapshot ACROSS publications:
        # dead tuples must be unreclaimable while it is open and return to zero
        # once it finishes. That is the MVCC reclamation proof, not a guess from
        # a single post-vacuum number.
        held_by_layout, released_by_layout = {}, {}
        # The reader-held phase runs four more vacuum spans with no upkeep
        # between them and ends in ``assert_no_foreign_activity``, so without
        # its own diff and its own settling the cell either missed a worker or
        # tripped over one it had earned itself.
        reader_counters_before = read_counters(engine, created_relations)
        for layout, storage_format in formats.items():
            owner = OWNER_BY_LAYOUT[layout]
            connection = engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            )
            transaction = connection.begin()
            try:
                connection.execute(
                    text(f"SELECT count(*) FROM {RELATIONS_BY_LAYOUT[layout][1]}")
                ).scalar_one()
                for cutoff in indices[:3]:
                    publish(
                        session_factory, owner, color, candidates[cutoff], storage_format
                    )
                held_by_layout[layout] = footprint(engine, vacuum=True)
            finally:
                transaction.rollback()
                connection.close()
            released_by_layout[layout] = footprint(engine, vacuum=True)
        reader_phase = assert_counters_clean(
            reader_counters_before,
            read_counters(engine, created_relations),
            measured=measured_relations,
        )
        reader_phase.update(assert_catalog_settled(maintain_catalog(engine)))
        assert_no_foreign_activity(engine)
    if scheduler_requests:
        raise QualificationRefusal("plateau cell enqueued a recompute")

    plateau = {}
    for layout, layout_windows in windows.items():
        totals = [w["after_vacuum"][f"{layout}_total_bytes"] for w in layout_windows]
        live = [w["after_vacuum"][f"{layout}_live_tuples"] for w in layout_windows]
        last_five = totals[-5:]
        plateau[layout] = {
            "window_total_bytes": totals,
            "window_live_tuples": live,
            "fixed_live_counts": len(set(live[-5:])) == 1,
            # The APPROVED statistic (corrected rev 16): max minus min over the
            # last five, not last minus first. Identical on a monotone series,
            # and strictly larger on one that oscillates — which is what a
            # two-generation retention pattern on a small relation looks like,
            # and exactly what last-minus-first cannot see.
            "last_five_growth_fraction": (
                (max(last_five) - min(last_five)) / min(last_five)
                if min(last_five)
                else 0.0
            ),
            "growth_statistic": "(max - min) / min over the last five windows",
            "dead_tuples_held": held_by_layout[layout][f"{layout}_dead_tuples"],
            "dead_tuples_released": released_by_layout[layout][f"{layout}_dead_tuples"],
            "reclaimed_after_reader_finished": (
                released_by_layout[layout][f"{layout}_dead_tuples"] == 0
            ),
            # Zero held means the open snapshot blocked nothing, so "returned to
            # zero" proves only that nothing was ever there. The proof needs
            # BOTH halves, and the evaluator gates on this field, not on
            # ``reclaimed_after_reader_finished`` alone.
            **reclamation_verdict(
                held_by_layout[layout][f"{layout}_dead_tuples"],
                released_by_layout[layout][f"{layout}_dead_tuples"],
            ),
        }
    result.update(
        {
            "plateau": plateau,
            "reader_phase": reader_phase,
            "orphans": check_orphans(session_factory, color),
            "complete": True,
        }
    )
    return result


def reclamation_verdict(held: int, released: int) -> dict:
    """Both halves, because either alone proves nothing.

    Zero held means the open REPEATABLE READ snapshot blocked no cleanup, so
    "returned to zero" only says nothing was ever there.
    """
    return {
        "reader_held_dead_tuples": held > 0,
        "reclamation_proved": held > 0 and released == 0,
    }


def check_orphans(session_factory, color: str) -> dict:
    """Zero cross-format payload, zero unreferenced rows, bounded markers.

    All three of the docstring's promises are now COMPUTED AND GATED. The
    earlier version derived ``clean`` from the cross-format counts alone: the
    unreferenced rows were never calculated at all, and ``markers_per_owner``
    was reported beside a verdict that did not read it, so C5 could report
    ``clean`` with a pair's whole previous generation still resident.

    "Unreferenced" means a different thing per layout, because the two are keyed
    differently. The current-format tables carry ``(user_id, player_color)`` and
    no ``batch_id``, so a row is reachable only through a live CURRENT marker and
    one without such a marker is unreachable evidence. The legacy tables carry
    ``batch_id``, so a row is unreferenced when its batch is gone.

    The marker bound is TWO for a pair whose live marker is legacy and ONE for a
    pair whose live marker is current, and that asymmetry is the shipped
    writer's: ``_retire`` (``app/opening_score_storage.py:380``) passes
    ``keep_legacy=True`` on a legacy-over-legacy publication and offsets the
    stale set by one, so legacy readers still see the prior snapshot. A flat
    "one marker per pair" would fail every legacy cell on correct behaviour.
    """
    from sqlalchemy import func, select

    from app.models import (
        CurrentOpeningEdge,
        CurrentOpeningPosition,
        CurrentOpeningRoot,
        CurrentOpeningScope,
        OpeningPositionEdge,
        OpeningPositionScore,
        OpeningScoreBatch,
        OpeningScoreBatchSharedScope,
        UserOpeningScore,
    )
    from app.opening_score_storage import StorageFormat

    current_models = (
        CurrentOpeningRoot,
        CurrentOpeningPosition,
        CurrentOpeningEdge,
        CurrentOpeningScope,
    )
    legacy_models = (
        UserOpeningScore,
        OpeningPositionScore,
        OpeningPositionEdge,
        OpeningScoreBatchSharedScope,
    )
    legacy_owner = OWNER_BY_LAYOUT["A"]
    current_owner = OWNER_BY_LAYOUT["B50"]
    with session_factory() as db:
        current_for_legacy_pair = sum(
            db.scalar(
                select(func.count())
                .select_from(model)
                .where(model.user_id == legacy_owner, model.player_color == color)
            )
            for model in current_models
        )
        legacy_for_current_pair = sum(
            db.scalar(
                select(func.count())
                .select_from(model)
                .join(
                    OpeningScoreBatch,
                    OpeningScoreBatch.id == model.batch_id,
                )
                .where(
                    OpeningScoreBatch.user_id == current_owner,
                    OpeningScoreBatch.player_color == color,
                )
            )
            for model in legacy_models
        )
        # A current-format row whose pair carries no live CURRENT marker is
        # unreachable: nothing keys to it and nothing retires it.
        current_without_marker = sum(
            db.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    ~select(1)
                    .select_from(OpeningScoreBatch)
                    .where(
                        OpeningScoreBatch.user_id == model.user_id,
                        OpeningScoreBatch.player_color == model.player_color,
                        OpeningScoreBatch.storage_format
                        == StorageFormat.CURRENT.value,
                    )
                    .exists()
                )
            )
            for model in current_models
        )
        legacy_without_batch = sum(
            db.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    ~select(1)
                    .select_from(OpeningScoreBatch)
                    .where(OpeningScoreBatch.id == model.batch_id)
                    .exists()
                )
            )
            for model in legacy_models
        )
        marker_rows = db.execute(
            select(
                OpeningScoreBatch.user_id,
                OpeningScoreBatch.player_color,
                OpeningScoreBatch.storage_format,
                OpeningScoreBatch.generation,
            )
        ).all()
    markers_per_pair: dict[tuple, int] = {}
    formats_per_pair: dict[tuple, set] = {}
    newest_per_pair: dict[tuple, tuple] = {}
    for user_id, player_color, storage_format, generation in marker_rows:
        pair = (user_id, player_color)
        markers_per_pair[pair] = markers_per_pair.get(pair, 0) + 1
        formats_per_pair.setdefault(pair, set()).add(storage_format)
        if pair not in newest_per_pair or generation > newest_per_pair[pair][0]:
            newest_per_pair[pair] = (generation, storage_format)

    # KEYED BY FORMAT AS WELL AS BY PAIR. Grouping on the format and then keying
    # the dict by the pair alone kept whichever format the database returned
    # last, so a pair holding BOTH formats — the leak this cell exists to catch —
    # was reported as holding one.
    allowance = {StorageFormat.LEGACY.value: 2, StorageFormat.CURRENT.value: 1}
    over_limit = {
        f"{pair[0]}:{pair[1]}": {
            "markers": count,
            "live_format": newest_per_pair[pair][1],
            "permitted": allowance.get(newest_per_pair[pair][1], 1),
        }
        for pair, count in markers_per_pair.items()
        if count > allowance.get(newest_per_pair[pair][1], 1)
    }
    both_formats = sorted(
        f"{pair[0]}:{pair[1]}"
        for pair, formats in formats_per_pair.items()
        if len(formats) > 1
    )
    return {
        "current_rows_for_legacy_pair": current_for_legacy_pair,
        "legacy_rows_for_current_pair": legacy_for_current_pair,
        "current_rows_without_live_marker": current_without_marker,
        "legacy_rows_without_batch": legacy_without_batch,
        "markers_per_owner": {
            f"{pair[0]}:{pair[1]}": count for pair, count in markers_per_pair.items()
        },
        "markers_over_limit": over_limit,
        "pairs_holding_both_formats": both_formats,
        "live_marker_formats": {
            f"{pair[0]}:{pair[1]}:{fmt}": True
            for pair, formats in formats_per_pair.items()
            for fmt in sorted(formats)
        },
        "marker_allowance_note": (
            "two markers are correct for a pair whose live marker is legacy: "
            "_retire keeps the prior legacy snapshot for legacy readers "
            "(app/opening_score_storage.py:380). One is correct for current."
        ),
        "clean": (
            current_for_legacy_pair == 0
            and legacy_for_current_pair == 0
            and current_without_marker == 0
            and legacy_without_batch == 0
            and not over_limit
            and not both_formats
        ),
    }


# --------------------------------------------------------------------------
# §4.10 — C6: the loopback network characterisation
# --------------------------------------------------------------------------


class RowCounter:
    """Accumulate result-row counts alongside ``Trace``'s byte counts.

    ``Trace`` (bench_opening_score_storage.py:187) totals DataRow bytes but not
    rows, and the sealed file may not be edited. This listens on the SAME engine
    and adds only the row count, so the modelled throughput probe can use the
    MEASURED mean DataRow width instead of an invented divisor.
    """

    def __init__(self, engine):
        from sqlalchemy import event

        self._event = event
        self.engine = engine
        self.read_rows = 0
        event.listen(engine, "after_cursor_execute", self.after)

    def reset(self) -> None:
        self.read_rows = 0

    def after(self, conn, cursor, statement, parameters, context, executemany):
        result = getattr(cursor, "pgresult", None)
        if result is not None and result.ntuples:
            self.read_rows += result.ntuples

    def close(self) -> None:
        self._event.remove(self.engine, "after_cursor_execute", self.after)


def modelled_row_width(layout_measurements: dict) -> int:
    """The MEASURED mean DataRow width of a publication, for the probe.

    The earlier ``publication_datarow_bytes_max // 20_000`` divided a
    publication's whole byte count by the probe's own row count, which is not a
    width of anything; the throughput sample it shaped therefore modelled rows
    of an invented size.
    """
    return max(1, round(layout_measurements["mean_datarow_width_bytes"]))


def run_network_cell(
    engine, session_factory, candidates, color: str, read_inputs, tree_requests
) -> dict:
    """DataRow bytes and MEASURED round trips, then a modelled network term.

    No matched application-to-database path exists locally, so the network term
    is published as MODELLED with all three operands and their provenance, and it
    is an INPUT to the deferred cutover gate's expectation — never a production
    ceiling.
    """
    import time

    from app import opening_cache as oc
    from app.opening_densify import routing_view
    from app.opening_score_storage import StorageFormat
    from scripts.bench_opening_score_storage import Trace
    from scripts.opening_score_storage_adapters import wire_bytes
    from scripts.opening_score_storage_workload import FIELDS, MODELS
    from sqlalchemy.orm import Session

    formats = {"A": StorageFormat.LEGACY, "B50": StorageFormat.CURRENT}
    graph = oc.get_opening_graph()
    roots_index = oc.get_opening_roots()
    routing = routing_view(graph)
    measured: dict[str, dict] = {}
    trace = Trace(engine)
    rows_seen = RowCounter(engine)
    stack, scheduler_requests = scheduler_isolation()
    try:
        with stack:
            for layout, storage_format in formats.items():
                owner = OWNER_BY_LAYOUT[layout]
                publish(session_factory, owner, color, candidates[0], storage_format)
                samples = []
                for candidate in candidates[1:4]:
                    trace.reset()
                    rows_seen.reset()
                    before_calls = sum(s["calls"] for s in trace.shapes.values())
                    publish(session_factory, owner, color, candidate, storage_format)
                    samples.append(
                        {
                            "datarow_bytes": trace.read_bytes,
                            "result_rows": rows_seen.read_rows,
                            "result_queries": trace.read_queries,
                            "round_trips": sum(s["calls"] for s in trace.shapes.values())
                            - before_calls,
                        }
                    )
                trace.reset()
                read_before = sum(s["calls"] for s in trace.shapes.values())
                with Session(engine) as db:
                    composite_d(db, owner, color, read_inputs["fens"], read_inputs["parents"])
                    d_bytes, d_calls = trace.read_bytes, sum(
                        s["calls"] for s in trace.shapes.values()
                    ) - read_before
                    trace.reset()
                    t_before = sum(s["calls"] for s in trace.shapes.values())
                    composite_t(
                        db, graph, roots_index, routing, owner, color, *tree_requests[0]
                    )
                    t_bytes, t_calls = trace.read_bytes, sum(
                        s["calls"] for s in trace.shapes.values()
                    ) - t_before
                measured[layout] = {
                    "publications": samples,
                    "publication_datarow_bytes_max": max(
                        s["datarow_bytes"] for s in samples
                    ),
                    "publication_result_rows_max": max(
                        s["result_rows"] for s in samples
                    ),
                    "mean_datarow_width_bytes": (
                        sum(s["datarow_bytes"] for s in samples)
                        / max(1, sum(s["result_rows"] for s in samples))
                    ),
                    "publication_round_trips_max": max(s["round_trips"] for s in samples),
                    "composite_d_datarow_bytes": d_bytes,
                    "composite_d_round_trips": d_calls,
                    "composite_t_datarow_bytes": t_bytes,
                    "composite_t_round_trips": t_calls,
                    "reconstructed_payload_bytes": sum(
                        wire_bytes(
                            [
                                tuple(row[f] for f in FIELDS[name])
                                for row in candidates[1].payload.rows(name)
                            ]
                        )
                        for name in MODELS
                    ),
                }
    finally:
        trace.close()
        rows_seen.close()
    if scheduler_requests:
        raise QualificationRefusal("network cell enqueued a recompute")

    width = modelled_row_width(measured["B50"])
    with engine.connect() as conn:
        rtt = []
        for _ in range(200):
            started = time.perf_counter()
            conn.execute(text("SELECT 1")).scalar_one()
            rtt.append((time.perf_counter() - started) * 1000)
        # generate_series rows of the MEASURED B50 DataRow width, never one
        # large value: a single wide DataRow does not reproduce the per-row
        # framing cost of ~11 MB of small ones, which is the cost being modelled.
        rows = 20_000
        started = time.perf_counter()
        fetched = conn.execute(
            text("SELECT repeat('x', :width) FROM generate_series(1, :rows)"),
            {"width": width, "rows": rows},
        ).all()
        throughput_s = time.perf_counter() - started
    sample_bytes = wire_bytes([(value,) for (value,) in fetched])
    throughput_bytes_per_s = sample_bytes / throughput_s if throughput_s else 0.0
    rtt.sort()
    rtt_ms = rtt[max(0, (len(rtt) * 50) // 100 - 1)]

    def network_term(byte_count, round_trips):
        return {
            "modelled_ms": (
                (byte_count / throughput_bytes_per_s * 1000)
                if throughput_bytes_per_s
                else 0.0
            )
            + round_trips * rtt_ms,
            "bytes": byte_count,
            "round_trips": round_trips,
            "throughput_bytes_per_s": throughput_bytes_per_s,
            "rtt_ms_median": rtt_ms,
            "provenance": "MODELLED from loopback-measured throughput and RTT; "
            "no matched application-to-database path exists on this host",
        }

    return {
        "cell": "C6",
        "measured": measured,
        "probe": {
            "datarow_width_bytes": width,
            "sample_rows": rows,
            "sample_bytes": sample_bytes,
            "elapsed_s": throughput_s,
            "rtt_samples_ms": rtt,
        },
        "network_terms": {
            layout: {
                "publication": network_term(
                    values["publication_datarow_bytes_max"],
                    values["publication_round_trips_max"],
                ),
                "composite_d": network_term(
                    values["composite_d_datarow_bytes"], values["composite_d_round_trips"]
                ),
                "composite_t": network_term(
                    values["composite_t_datarow_bytes"], values["composite_t_round_trips"]
                ),
            }
            for layout, values in measured.items()
        },
    }


# --------------------------------------------------------------------------
# §4.6 — C3: integrated worker memory, in FRESH SPAWNED processes
# --------------------------------------------------------------------------


def memory_child(capture_path: Path, layout: str, color: str) -> dict:
    """One fresh process that imports the application the way the service does.

    This is the INTEGRATED worker the bead asks for, not the spike's raw-adapter
    memory worker: it includes the scorer and app imports the earlier anchor
    excluded. ``storage_format`` arrives as an ARGUMENT because module patches do
    not cross ``spawn``. Only the last two candidates are loaded, so the replay
    library never inflates the figure.
    """
    import tracemalloc

    from app.opening_score_storage import StorageFormat
    from scripts.bench_opening_score_storage import rss_bytes

    url = bootstrap_database_url()
    assert_resolved_engine(url)
    engine = engine_for(url)
    session_factory = session_factory_for(engine)

    storage_format = {"A": StorageFormat.LEGACY, "B50": StorageFormat.CURRENT}[layout]
    owner = OWNER_BY_LAYOUT[layout]
    # A PRE-SCALED two-candidate slice, written by the parent. Loading the whole
    # capture and slicing to two would put a hundred cutoffs through this
    # process's RSS high-water first (see ``load_capture``).
    sliced = load_capture(capture_path)
    candidates = sliced["candidates"]
    if len(candidates) != 2:
        raise QualificationRefusal(
            f"a memory child takes a two-candidate slice, not {len(candidates)}"
        )
    stack, _requests = scheduler_isolation()
    with stack:
        publish(session_factory, owner, color, candidates[0], storage_format)
        publish(session_factory, owner, color, candidates[1], storage_format)
        untraced_rss = rss_bytes()
        publish(session_factory, owner, color, candidates[0], storage_format)
        tracemalloc.start()
        try:
            publish(session_factory, owner, color, candidates[1], storage_format)
            _, allocation_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        traced_rss = rss_bytes()
    engine.dispose()
    return {
        "layout": layout,
        "logical_rows": sliced["logical_rows"],
        "copies": sliced["copies"],
        "untraced_worker_rss_highwater_bytes": untraced_rss,
        "traced_rss_highwater_bytes": traced_rss,
        "publication_allocation_peak_bytes": allocation_peak,
        "caveat": (
            "Fresh spawned process importing the application as the service "
            "does, including scorer and app imports. Traced RSS carries profiler "
            "overhead. RSS accounting is macOS, not the deployed Linux container: "
            "this figure is local_host_only and deferred to g-score-store-cutover."
        ),
    }


def run_memory_cell(
    database_url: str, capture_path: Path, color: str, *, copies: int, repeats=5
) -> dict:
    """Five fresh children per layout; the REPEATED MAXIMUM is the figure."""
    source = Path(capture_path)
    slice_path = source.with_name(f"{source.stem}.c3slice.pickle")
    final = write_capture_slice(source, slice_path, copies=copies, count=2)
    capture_path = slice_path
    results: dict[str, list] = {"A": [], "B50": []}
    for layout in ("A", "B50"):
        for _ in range(repeats):
            child = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.qualify_opening_score_storage",
                    "memory-child",
                    "--capture",
                    str(capture_path),
                    "--layout",
                    layout,
                    "--color",
                    color,
                ],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=_child_environment(database_url),
                capture_output=True,
                text=True,
                check=False,
            )
            if child.returncode:
                raise QualificationRefusal(
                    f"memory child for {layout} failed: {child.stderr.strip()[-2000:]}"
                )
            results[layout].append(json.loads(child.stdout.splitlines()[-1]))
    return {
        "cell": "C3",
        "logical_rows": logical_rows(final.payload),
        "copies": copies,
        "children": results,
        "repeated_maximum": {
            layout: {
                "untraced_worker_rss_highwater_bytes": max(
                    r["untraced_worker_rss_highwater_bytes"] for r in runs
                ),
                "publication_allocation_peak_bytes": max(
                    r["publication_allocation_peak_bytes"] for r in runs
                ),
                "repeats": len(runs),
            }
            for layout, runs in results.items()
        },
    }


def _child_environment(database_url: str, *, env_name: str = QUAL_DATABASE_ENV) -> dict:
    """A CONSTRUCTED environment, never an inherited one.

    The URL travels by environment rather than as a spawn argument, so no
    connection string — and no password — ever reaches a process listing.
    """
    keep = (
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "GHOSTREPLAY_STORAGE_QUAL_PG_PREFIX",
        QUAL_CLUSTER_ENV,
        # §0.2 puts the QC-PROD password in a passfile and nowhere else, so a
        # child that does not inherit this cannot authenticate at all. It is
        # NOT one of the refused names: on its own it resolves no engine —
        # ``_database_url_from_pg_env`` needs host, database, user AND password
        # (app/database_url.py:31) — and the URL it applies to is the guarded
        # one this environment carries explicitly.
        "PGPASSFILE",
    )
    environment = {name: os.environ[name] for name in keep if name in os.environ}
    environment.update(
        {
            env_name: database_url,
            "DATABASE_URL": database_url,
            "PGAPPNAME": APPLICATION_NAME,
            # The capture child runs the REAL writer; without this it would
            # reach PostHog from a process holding production-derived rows.
            "POSTHOG_DISABLED": "true",
        }
    )
    return environment


# --------------------------------------------------------------------------
# Capture artifact — written by qualify_opening_score_capture, read here
# --------------------------------------------------------------------------

CAPTURE_VERSION = 1


PRIVATE_STORE = Path.home() / ".ghostreplay-private"


def under_private_store(path) -> bool:
    root = PRIVATE_STORE.expanduser().resolve()
    return Path(path).expanduser().resolve().is_relative_to(root)


def assert_private_store(path: Path) -> Path:
    """RESOLVE, then compare. A prefix test on the unresolved string passes for
    ``~/.ghostreplay-private/../../tmp/leak.pickle`` and for a symlink pointing
    anywhere, which is the whole failure this guard exists to prevent."""
    resolved = Path(path).expanduser().resolve()
    root = PRIVATE_STORE.expanduser().resolve()
    if not resolved.is_relative_to(root):
        raise QualificationRefusal(
            f"production-derived captures may only be written under {root}, "
            f"not {resolved}"
        )
    return resolved


def dump_capture(path: Path, payload: dict, *, production_derived: bool = True) -> None:
    """Pickle, not JSON, and — when production-derived — only inside the store.

    The payloads carry exact float and timezone-aware datetime values that a
    JSON round trip would not preserve byte for byte, and at S1 they run to a
    hundred cutoffs of ~25k rows, where JSON is neither small nor faster. The
    handling rule is what keeps this safe: captured payloads stay under
    ``~/.ghostreplay-private/score-store-qualify/`` (mode 700) and only
    AGGREGATES — counts, mixes, changed fractions — reach ``docs/analysis``, a
    bead or a report.

    ``production_derived=False`` is for the SF fixture capture ONLY, whose rows
    are ``build_timeline``'s deterministic synthetic sessions. It still writes
    mode 600.
    """
    import pickle

    if production_derived:
        path = assert_private_store(path)
    temp = path.with_suffix(".partial")
    with open(temp, "wb") as handle:
        pickle.dump({"version": CAPTURE_VERSION, **payload}, handle, protocol=5)
    os.chmod(temp, 0o600)
    temp.replace(path)


def load_capture(path: Path, *, tail: int | None = None) -> dict:
    """``tail`` SLICES AFTER UNPICKLING, so it does not bound peak memory.

    C3 measures a worker's RSS high-water. A child that unpickles a hundred
    cutoffs and then keeps two has already paid the high-water for a hundred —
    ``ru_maxrss`` never comes back down — so the figure would be the capture
    library in both layouts, identically, and would measure nothing about
    either. C3 therefore does NOT use ``tail``: the parent writes a two-candidate
    slice with ``write_capture_slice`` and the child loads only that.
    """
    import pickle

    with open(path, "rb") as handle:
        payload = pickle.load(handle)
    if payload.get("version") != CAPTURE_VERSION:
        raise QualificationRefusal(
            f"capture version {payload.get('version')!r} is not {CAPTURE_VERSION}"
        )
    if tail is not None:
        payload = dict(payload, candidates=payload["candidates"][-tail:])
    return payload


def write_capture_slice(source: Path, destination: Path, *, copies: int, count: int):
    """Pre-SCALED last ``count`` candidates, written for a memory child to load.

    Scaling happens HERE, in the parent, for two reasons: the child must publish
    S2/S3-sized payloads rather than S1-sized ones (a child that receives no
    ``copies`` measures the same size at every profile), and the intermediate
    allocations of ``qualification_scale`` are the parent's, not part of the
    worker's high-water.
    """
    capture = load_capture(source)
    candidates = [
        qualification_scale(candidate, copies)
        for candidate in capture["candidates"][-count:]
    ]
    dump_capture(
        destination,
        {
            "color": capture["color"],
            "candidates": candidates,
            "sliced_from": str(source),
            "copies": copies,
            "logical_rows": logical_rows(candidates[-1].payload),
        },
        # A slice is exactly as sensitive as its source: production-derived for
        # a real capture, synthetic for the SF fixture.
        production_derived=under_private_store(source),
    )
    return candidates[-1]


def prepare_candidates(capture: dict, *, copies: int, membership: str, length: int):
    """Scale, fix membership where the cell asks for it, and order the replay."""
    from scripts.remeasure_opening_score_budgets import fixed_membership

    candidates = [qualification_scale(c, copies) for c in capture["candidates"]]
    if membership == "fixed":
        candidates = fixed_membership(candidates)
        indices = ping_pong_sequence(len(candidates), length)
    elif membership == "growing":
        if len(candidates) < length:
            raise QualificationRefusal(
                f"growing membership needs {length} ordered cutoffs, not "
                f"{len(candidates)}; a growing sequence cannot be ping-ponged or "
                "cycled, because each publication must add to the previous one"
            )
        indices = list(range(length))
    else:
        raise QualificationRefusal(f"unknown membership {membership!r}")
    return candidates, indices


# --------------------------------------------------------------------------
# Cell orchestration and CLI
# --------------------------------------------------------------------------

CELL_SPECS = {
    # THIRTEEN reads of each composite per publication, not eight and six.
    # The read floor is 500 per layout per composite and it is counted over KEPT
    # blocks only, so k reads per publication survive ceil(500 / (10 k)) kept
    # blocks: at six, composite T needed nine of ten blocks and could not lose
    # more than ONE. A timed checkpoint every 300 s lands in one side of a pair
    # and discards it, and raising ``max_wal_size`` does not change that, so a
    # C1 running longer than about ten minutes — which S2 and S3 will — lost its
    # T gate by arithmetic. Thirteen reaches 500 at FOUR kept blocks, six
    # discards' worth of tolerance, while keeping the two floors within one
    # block of each other rather than making reads the binding constraint.
    "C1": {"membership": "fixed", "publications": 100, "reads": (13, 13), "checkpoint": False},
    # SIXTY, not forty. Forty publications against a forty-publication floor is
    # zero slack: any launcher pass that picks anything up lands inside a block,
    # the pair it lands in is discarded, and the size is lost — and the ceiling
    # needs all three sizes. Six blocks absorb TWO discarded pairs and still
    # clear the floor. C2 runs only on S1/S2/S3, all built from pair 0's 5576
    # sessions, so the ordered cutoffs a growing sequence needs are there;
    # §4.1's S0 eligibility rule moves from forty sessions to sixty with it.
    "C2": {"membership": "growing", "publications": 60, "reads": (0, 0), "checkpoint": True},
    "C3": {"membership": "fixed", "publications": 0, "reads": (0, 0), "checkpoint": False},
    # 100, not 60 (corrected rev 16): ten windows is the APPROVED shape, taken
    # from the spike's fixed cell. The 60 here was never decided — every
    # "sixty" in the plan is C2's (§4.4) — and it put C5's last five windows at
    # the steepest part of layout A's settling.
    "C5": {"membership": "fixed", "publications": 100, "reads": (0, 0), "checkpoint": False},
    "C6": {"membership": "fixed", "publications": 0, "reads": (0, 0), "checkpoint": False},
}


def assert_capture_closure(capture: dict) -> None:
    """§1.3's closure check decides whether this capture may be replayed at all.

    The capture tool runs it and writes the outcome onto the artifact, but
    nothing downstream looked: a capture whose final payload did NOT equal a
    plain recompute on an untouched clone means the reveal mechanism perturbed
    evidence, and every cell replaying it would then measure a fiction. A
    SKIPPED check is permitted — it is an explicit operator choice — and travels
    into the cell report, where the evaluator records it as a coverage gap. A
    synthetic capture has no reveal mechanism to perturb anything, so there is
    nothing to check.
    """
    provenance = capture.get("provenance") or {}
    if provenance.get("synthetic_only"):
        return
    closure = capture.get("closure_check")
    if closure is None:
        raise QualificationRefusal(
            "the capture carries no closure_check; §1.3 requires the final "
            "cutoff's payload to equal a plain recompute on an untouched clone "
            "before any cell replays it"
        )
    if closure.get("skipped"):
        return
    if closure.get("equal") is not True:
        raise QualificationRefusal(
            "the capture's closure check did not pass "
            f"({closure.get('reason') or closure.get('failure') or closure}); "
            "the reveal mechanism perturbed evidence and the capture is invalid"
        )


def sanitised_capture_provenance(capture: dict) -> dict:
    """What a cell report may carry about a production capture.

    The capture's ``cutoff_provenance`` holds each cutoff session's REAL
    timestamp. Cell reports go to an unguarded ``--output`` and on to
    ``docs/analysis``, and a real user's session times are production-derived
    data that the "only aggregates leave the private store" rule covers just as
    much as a FEN does. The SPACING is what a reader needs — it is what §4.5's
    confidence expectation rests on — so spacing is what is kept.
    """
    provenance = dict(capture.get("provenance") or {})
    cutoffs = provenance.pop("cutoff_provenance", None)
    if cutoffs:
        stamps = sorted(entry["scored_at"] for entry in cutoffs)
        gaps = [
            (b - a).total_seconds() / 86400.0
            for a, b in zip(stamps, stamps[1:], strict=False)
        ]
        provenance["cutoff_spacing_days"] = {
            "cutoffs": len(stamps),
            "span_days": (
                (stamps[-1] - stamps[0]).total_seconds() / 86400.0 if gaps else 0.0
            ),
            "min": min(gaps, default=0.0),
            "median": sorted(gaps)[len(gaps) // 2] if gaps else 0.0,
            "max": max(gaps, default=0.0),
            "note": (
                "absolute session timestamps stay in the private store; the "
                "spacing is the part a reader of the profile needs"
            ),
        }
    return {
        key: capture[key]
        for key in ("pair_index", "cutoffs", "closure_check")
        if key in capture
    } | {"provenance": provenance}


def run_cell(args) -> int:
    """One cell, in its own process, against its own fresh database.

    THE REPORT IS WRITTEN WHETHER OR NOT THE CELL SUCCEEDS (§4). A cell is hours
    of measurement; a refusal in its last minute must not throw away every
    retained sample, and the refusal itself is evidence — it is how the
    ``max_wal_size`` fallback in §9.5 gets decided on observation rather than
    pre-emptively.
    """
    url = bootstrap_database_url()
    assert_resolved_engine(url)
    engine = engine_for(url)
    session_factory = session_factory_for(engine)
    capture = load_capture(Path(args.capture))
    spec = CELL_SPECS[args.cell]
    color = capture["color"]
    report: dict = {
        "cell": args.cell,
        "profile": args.profile,
        "copies": args.copies,
        "color": color,
        "capture": sanitised_capture_provenance(capture),
        "capture_closure": capture.get("closure_check"),
        "complete": False,
    }
    try:
        assert_capture_closure(capture)
        assert_database_empty(engine)
        with engine.connect() as conn:
            identity = assert_cluster_identity(
                conn, expected_database_pattern=CELL_DATABASE_PATTERN
            )
        created = create_schema(engine)
        reloptions = assert_current_format_reloptions(engine)
        disable_relation_autovacuum(engine)
        measured = measured_relation_names(engine)
        # The schema DDL and the reloption ALTERs have churned a dozen catalogs
        # and the counts are still pending on the backends that did it, so this
        # FLUSHES first, DISCOVERS what is actually pending, and then refuses
        # the cell if anything is left over an autovacuum threshold. The first
        # spelling maintained four named catalogs against counters that did not
        # yet include the DDL, and block 0 collected the nine events that earned.
        catalog = assert_catalog_settled(maintain_catalog(engine))
        shared_counts = copy_shared_evidence(
            engine, capture["shared_versions"], capture["shared_invalidations"]
        )
        assert_no_foreign_activity(engine)
        report.update(
            {
                "cluster": identity,
                "reloptions": reloptions,
                "created_relations": list(created),
                "measured_relations": sorted(measured),
                "catalog_maintenance": catalog,
                "shared_evidence_rows": shared_counts,
                "profile_identity": profile_identity(engine, capture.get("census")),
            }
        )
        if args.cell == "C3":
            report["result"] = run_memory_cell(
                url.render_as_string(hide_password=False),
                Path(args.capture),
                color,
                copies=args.copies,
            )
        elif args.cell == "C6":
            candidates, _ = prepare_candidates(
                capture, copies=args.copies, membership="fixed", length=10
            )
            report["result"] = run_network_cell(
                engine,
                session_factory,
                candidates,
                color,
                capture["read_inputs"],
                capture["tree_requests"],
            )
        elif args.cell == "C5":
            candidates, indices = prepare_candidates(
                capture,
                copies=args.copies,
                membership="fixed",
                length=spec["publications"],
            )
            partial: dict = {}
            report["result"] = partial
            run_plateau_cell(
                engine,
                session_factory,
                candidates,
                indices,
                color,
                partial=partial,
                created_relations=created,
                measured_relations=measured,
            )
        else:
            candidates, indices = prepare_candidates(
                capture,
                copies=args.copies,
                membership=spec["membership"],
                length=spec["publications"],
            )
            report["sequence"] = sequence_provenance(indices, len(candidates))
            partial = {}
            report["result"] = partial
            run_paired_cell(
                engine,
                session_factory,
                candidates,
                indices,
                cell=args.cell,
                color=color,
                created_relations=created,
                measured_relations=measured,
                reads_per_publication=spec["reads"],
                checkpoint_before_each=spec["checkpoint"],
                read_inputs=capture["read_inputs"],
                tree_requests=capture["tree_requests"],
                partial=partial,
            )
        report["final_footprint"] = footprint(engine, vacuum=True)
        report["orphans"] = check_orphans(session_factory, color)
        report["complete"] = True
    except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
        report["failure"] = {
            "type": type(exc).__name__,
            "message": str(exc)[:4000],
            "refusal": isinstance(exc, QualificationRefusal),
            "note": (
                "partial cell: every sample collected before this point is "
                "retained. The evaluator treats an incomplete cell as a recorded "
                "gap, never as a pass."
            ),
        }
        raise
    finally:
        engine.dispose()
        from scripts.bench_opening_score_storage import write_report

        write_report(Path(args.output), report)
        print(f"wrote {args.output} (complete={report['complete']})")
    return 0


def _admin_engine():
    refuse_inherited_connection_environment()
    url = guard_admin_url(os.environ.get(QUAL_ADMIN_ENV))
    engine = create_engine(
        url,
        connect_args={"application_name": APPLICATION_NAME},
        isolation_level="AUTOCOMMIT",
        poolclass=None,
    )
    expected = expected_cluster_name()
    with engine.connect() as conn:
        cluster = conn.execute(text("SHOW cluster_name")).scalar_one()
        if cluster != expected:
            raise QualificationRefusal(
                f"admin connection reached cluster {cluster!r}, not {expected!r}"
            )
    return engine, url


def create_cell_database(name: str) -> str:
    """The harness creates and drops each cell's database itself (§2.1)."""
    if not re.fullmatch(CELL_DATABASE_PATTERN, name):
        raise QualificationRefusal(f"{name!r} is not a gr_score_qual_* database")
    engine, url = _admin_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        target = url.set(database=name)
        cell_engine = engine_for(target)
        try:
            with cell_engine.connect() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_walinspect"))
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgstattuple"))
                conn.commit()
        finally:
            cell_engine.dispose()
    finally:
        engine.dispose()
    return target.render_as_string(hide_password=False)


def create_measurement_database(name: str) -> dict:
    """Create ONE empty ``gr_score_qual_*`` database and say nothing secret.

    §2.8's C4 control runner takes its target from the environment and creates
    its own tables through ``LegacyAdapter.create``, but nothing in the harness
    could MAKE that database: ``run`` only creates one for a harness cell and
    the teardown side had no counterpart. The alternative was a hand-rolled
    ``createdb``, which is the one DDL path outside every guard here.

    The URL is NOT printed. The operator already holds the admin URL this was
    derived from, so a password on stdout would land in a transcript for
    nothing.
    """
    url = make_url(create_cell_database(name))
    return {
        "database": name,
        "url": url.render_as_string(hide_password=True),
        "note": (
            "empty database with pg_walinspect and pgstattuple; set "
            f"{QUAL_DATABASE_ENV} to this target yourself — the harness never "
            "prints a password and never puts a URL on a command line"
        ),
    }


def drop_cell_database(name: str) -> None:
    if not re.fullmatch(CELL_DATABASE_PATTERN, name):
        raise QualificationRefusal(f"refusing to drop {name!r}")
    engine, _ = _admin_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        engine.dispose()


# --------------------------------------------------------------------------
# §4.9 — C7's delta-lane clone, created and migrated under the same rules
# --------------------------------------------------------------------------


def create_lane_database(name: str, *, template: str = SNAPSHOT_TEMPLATE) -> str:
    """Clone ``gr_snap_base`` for the delta lane, then migrate it in a CHILD.

    §4.9 names this clone but nothing produced it, so the lane run would have
    reached for a bare ``alembic upgrade head`` — the one DDL path outside every
    guard in this bead (``alembic/env.py:32`` resolves its URL unconditionally).
    The migration therefore runs in a spawned child whose environment this
    process CONSTRUCTS, exactly as a cell does, because ``alembic`` imports
    ``app`` and would otherwise pin this process's engine to the clone.
    """
    if not re.fullmatch(LANE_DATABASE_PATTERN, name):
        raise QualificationRefusal(f"{name!r} is not a gr_delta_lane_* database")
    if name == template:
        raise QualificationRefusal(f"refusing to use {template!r} as the lane clone")
    engine, url = _admin_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
    finally:
        engine.dispose()
    target = url.set(database=name)
    database_url = target.render_as_string(hide_password=False)
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.qualify_opening_score_storage",
            "migrate-lane",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=_child_environment(database_url),
        capture_output=True,
        text=True,
        check=False,
    )
    if child.returncode:
        # An unmigrated clone is worse than no clone: it matches the lane
        # pattern, so a later run would accept it and measure against a schema
        # nobody migrated.
        drop_lane_database(name)
        raise QualificationRefusal(
            f"migrating {name} failed (the clone has been dropped): "
            f"{child.stderr.strip()[-2000:]}"
        )
    return database_url


def migrate_lane(_args=None) -> int:
    """The child half of ``create_lane_database``: bootstrap, guard, migrate.

    Under the DEFAULT measurement guard this refused every time — the lane
    database is ``gr_delta_lane_*`` and the measurement pattern is
    ``gr_score_qual_*`` — so the lane guard is passed explicitly.
    """
    url = bootstrap_database_url(guard=guard_lane_url)
    assert_resolved_engine(url)
    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            assert_cluster_identity(
                conn, expected_database_pattern=LANE_DATABASE_PATTERN
            )
    finally:
        engine.dispose()
    run_migrations(url, expected_database_pattern=LANE_DATABASE_PATTERN)
    print(
        json.dumps(
            {
                "database": url.database,
                "migrated": True,
                "note": (
                    "in-process alembic.command.upgrade against the bootstrapped "
                    "target; never a bare `alembic upgrade head`"
                ),
            }
        )
    )
    return 0


def drop_lane_database(name: str) -> None:
    if not re.fullmatch(LANE_DATABASE_PATTERN, name):
        raise QualificationRefusal(f"refusing to drop {name!r}")
    engine, _ = _admin_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        engine.dispose()


def run(args) -> int:
    """Create the cell's database, run the cell in a child, then drop it.

    The child's environment is CONSTRUCTED rather than inherited, and the
    orchestrator never imports ``app`` — ``app.db`` binds its engine at import,
    so a parent that imported it would be pinned to one cell's database for the
    whole run.
    """
    name = f"gr_score_qual_{args.run_id}_{args.cell.lower()}"
    database_url = create_cell_database(name)
    try:
        child = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.qualify_opening_score_storage",
                "run-cell",
                "--cell",
                args.cell,
                "--capture",
                str(args.capture),
                "--profile",
                args.profile,
                "--copies",
                str(args.copies),
                "--output",
                str(args.output),
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=_child_environment(database_url),
            check=False,
        )
    finally:
        if not args.keep_database:
            drop_cell_database(name)
    return child.returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    runner = sub.add_parser("run", help="create a fresh database, run one cell, drop it")
    runner.add_argument("--cell", choices=sorted(CELL_SPECS), required=True)
    runner.add_argument("--capture", type=Path, required=True)
    runner.add_argument("--profile", required=True)
    runner.add_argument("--copies", type=int, default=1)
    runner.add_argument("--run-id", required=True)
    runner.add_argument("--output", type=Path, required=True)
    runner.add_argument("--keep-database", action="store_true")
    runner.set_defaults(func=run)

    cell = sub.add_parser("run-cell", help="run one cell in this process")
    cell.add_argument("--cell", choices=sorted(CELL_SPECS), required=True)
    cell.add_argument("--capture", type=Path, required=True)
    cell.add_argument("--profile", required=True)
    cell.add_argument("--copies", type=int, default=1)
    cell.add_argument("--output", type=Path, required=True)
    cell.set_defaults(func=run_cell)

    child = sub.add_parser("memory-child", help="one C3 worker process")
    child.add_argument("--capture", type=Path, required=True)
    child.add_argument("--layout", choices=sorted(OWNER_BY_LAYOUT), required=True)
    child.add_argument("--color", choices=("white", "black"), required=True)
    child.set_defaults(
        func=lambda a: (
            print(json.dumps(memory_child(Path(a.capture), a.layout, a.color))) or 0
        )
    )

    lane = sub.add_parser(
        "create-lane-database", help="clone and migrate C7's delta-lane database"
    )
    lane.add_argument("--name", required=True)
    # The URL is created but NOT printed: it carries the cluster password.
    lane.set_defaults(
        func=lambda a: (
            create_lane_database(a.name)
            and print(json.dumps({"database": a.name, "migrated": True}))
        )
        or 0
    )

    creator = sub.add_parser(
        "create-database", help="one empty gr_score_qual_* database (C4's target)"
    )
    creator.add_argument("--name", required=True)
    creator.set_defaults(
        func=lambda a: print(json.dumps(create_measurement_database(a.name), indent=2))
        or 0
    )

    migrate = sub.add_parser("migrate-lane", help="child half of create-lane-database")
    migrate.set_defaults(func=migrate_lane)

    teardown = sub.add_parser("drop-database", help="standalone cell teardown")
    teardown.add_argument("--name", required=True)
    teardown.set_defaults(func=lambda a: (drop_cell_database(a.name) or 0))

    lane_teardown = sub.add_parser("drop-lane-database", help="C7 teardown")
    lane_teardown.add_argument("--name", required=True)
    lane_teardown.set_defaults(func=lambda a: (drop_lane_database(a.name) or 0))

    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
