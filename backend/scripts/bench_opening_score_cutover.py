"""Run qualified storage cells on an explicitly sealed, disposable Railway host.

The local qualification guards stay unchanged. This entry point requires a
separate manifest, Railway environment/service identity, exact private hostname,
PostgreSQL system identifier, cluster name and database comment before any write.
It never creates databases, restores snapshots or changes server settings.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
from pathlib import Path
import re
import subprocess
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

PRODUCTION_ENVIRONMENT = "be83d48a-d5a4-460e-95fd-fd810b4d16de"
DATABASE_PATTERN = r"gr_score_qual_cutover_[a-z0-9_]+"


def assert_runtime(manifest):
    expected = manifest["runtime"]
    actual = {"python": platform.python_version(), "packages": {
        name: importlib.metadata.version(name) for name in expected["packages"]
    }}
    if actual != expected:
        raise ValueError("runner dependencies differ from the sealed production runtime")
    return actual


def guarded_url(manifest, database, environ):
    if (
        not manifest.get("environment_id")
        or manifest["environment_id"] == PRODUCTION_ENVIRONMENT
        or manifest["environment_id"] != environ.get("RAILWAY_ENVIRONMENT_ID")
        or manifest.get("runner_service_id") != environ.get("RAILWAY_SERVICE_ID")
        or not manifest.get("runner_service_id")
        or environ.get("RAILWAY_ENVIRONMENT_NAME") != "score-store-cutover"
    ):
        raise ValueError("not the sealed non-production Railway runner")
    if not re.fullmatch(DATABASE_PATTERN, database):
        raise ValueError("not a cutover measurement database")
    # Reject ambient app/PG fallbacks. Only our explicit temporary reference is used.
    from scripts.qualify_opening_score_storage import INHERITED_URL_ENV_NAMES, REFUSED_ENV_NAMES

    if any(environ.get(key) for key in set(INHERITED_URL_ENV_NAMES + REFUSED_ENV_NAMES)):
        raise ValueError("ambient database configuration must be absent")
    url = make_url(environ.get("CUTOVER_DATABASE_URL", ""))
    if (
        url.drivername not in {"postgresql", "postgresql+psycopg"}
        or not manifest.get("database_host", "").endswith(".railway.internal")
        or url.host != manifest["database_host"]
        or url.query
        or url.port not in {None, 5432}
        or url.database != "railway"
    ):
        raise ValueError("database endpoint does not match the disposable manifest")
    return url.set(drivername="postgresql+psycopg", database=database)


def assert_server(conn, manifest, database):
    row = conn.execute(text(
        "SELECT current_database(), current_setting('cluster_name'), "
        "(SELECT system_identifier::text FROM pg_control_system()), "
        "shobj_description(oid, 'pg_database') FROM pg_database "
        "WHERE datname = current_database()"
    )).one()
    expected = (database, manifest["cluster_name"], manifest["system_identifier"], manifest["sentinel"])
    if tuple(row) != expected:
        raise ValueError("disposable server/database seal mismatch")


def bootstrap(manifest, database):
    url = guarded_url(manifest, database, os.environ)
    probe = create_engine(url)
    try:
        with probe.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            assert_server(conn, manifest, database)
    finally:
        probe.dispose()
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    os.environ["POSTHOG_DISABLED"] = "true"
    from scripts import qualify_opening_score_storage as q

    q.assert_resolved_engine(url)
    return url


def memory_sample(args, manifest, url):
    import tracemalloc
    from scripts import qualify_opening_score_storage as q
    from scripts.bench_opening_score_storage import rss_bytes
    from app.opening_score_storage import StorageFormat

    engine = q.engine_for(url)
    try:
        with engine.connect() as conn:
            assert_server(conn, manifest, args.database)
        capture = q.load_capture(args.capture)
        candidates = capture["candidates"]
        if len(candidates) != 2:
            raise ValueError("memory samples require the pre-sliced two-candidate capture")
        factory = q.session_factory_for(engine)
        fmt = {"A": StorageFormat.LEGACY, "B50": StorageFormat.CURRENT}[args.layout]
        stack, requests = q.scheduler_isolation()
        with stack:
            for candidate in candidates:
                q.publish(factory, q.OWNER_BY_LAYOUT[args.layout], capture["color"], candidate, fmt)
            rss = rss_bytes()
            q.publish(factory, q.OWNER_BY_LAYOUT[args.layout], capture["color"], candidates[0], fmt)
            tracemalloc.start()
            try:
                q.publish(factory, q.OWNER_BY_LAYOUT[args.layout], capture["color"], candidates[1], fmt)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        if requests:
            raise ValueError("memory sample enqueued work")
        return {"layout": args.layout, "logical_rows": capture["logical_rows"],
                "untraced_worker_rss_highwater_bytes": rss,
                "publication_allocation_peak_bytes": peak}
    finally:
        engine.dispose()


def run(args, manifest, url):
    from scripts import qualify_opening_score_storage as q
    engine = q.engine_for(url)
    report = {"complete": False, "cell": args.cell, "profile": args.profile,
              "copies": args.copies, "traffic_representative": False,
              "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest()}
    try:
        capture = q.load_capture(args.capture)
        q.assert_capture_closure(capture)
        q.assert_database_empty(engine)
        with engine.connect() as conn:
            assert_server(conn, manifest, args.database)
        created = q.create_schema(engine)
        report["reloptions"] = q.assert_current_format_reloptions(engine)
        q.disable_relation_autovacuum(engine)
        measured = q.measured_relation_names(engine)
        report["catalog_maintenance"] = q.assert_catalog_settled(q.maintain_catalog(engine))
        q.copy_shared_evidence(engine, capture["shared_versions"], capture["shared_invalidations"])
        q.assert_no_foreign_activity(engine)
        report["profile_identity"] = q.profile_identity(engine, capture.get("census"))
        report["profile_identity"]["runtime"] = assert_runtime(manifest)
        report["source_manifest"] = manifest["source_manifest"]
        report["capture_sha256"] = hashlib.sha256(args.capture.read_bytes()).hexdigest()
        report["result"] = {}
        if args.cell == "C3":
            sliced = args.output.with_suffix(".slice.pickle")
            q.write_capture_slice(args.capture, sliced, copies=args.copies, count=2)
            children = {"A": [], "B50": []}
            report["result"]["children"] = children
            child_env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
            for repeat in range(5):
                for layout in (("A", "B50") if repeat % 2 == 0 else ("B50", "A")):
                    output = args.output.with_suffix(f".{layout}.{repeat}.json")
                    command = [sys.executable, "-m", "scripts.bench_opening_score_cutover",
                               "--manifest", str(args.manifest), "--database", args.database,
                               "--capture", str(sliced), "--output", str(output),
                               "--cell", "C3", "--layout", layout]
                    subprocess.run(command, env=child_env, check=True)
                    children[layout].append(json.loads(output.read_text()))
        else:
            spec = q.CELL_SPECS[args.cell]
            candidates, indices = q.prepare_candidates(capture, copies=args.copies,
                membership=spec["membership"], length=spec["publications"])
            report["sequence"] = q.sequence_provenance(indices, len(candidates))
            q.run_paired_cell(engine, q.session_factory_for(engine), candidates, indices,
                cell=args.cell, color=capture["color"], created_relations=created,
                measured_relations=measured, reads_per_publication=spec["reads"],
                checkpoint_before_each=spec["checkpoint"], read_inputs=capture["read_inputs"],
                tree_requests=capture["tree_requests"], partial=report["result"])
        report["final_footprint"] = q.footprint(engine, vacuum=True)
        report["orphans"] = q.check_orphans(q.session_factory_for(engine), capture["color"])
        report["complete"] = True
    except BaseException as exc:
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        engine.dispose()
        args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cell", choices=["C1", "C2", "C3"], required=True)
    parser.add_argument("--profile", default="S1")
    parser.add_argument("--copies", type=int, default=1)
    parser.add_argument("--layout", choices=["A", "B50"])
    args = parser.parse_args()
    os.umask(0o077)
    from scripts.qualify_opening_score_storage import assert_private_store
    for path in (args.manifest, args.capture, args.output):
        assert_private_store(path)
    if args.output.exists():
        raise ValueError("refusing to overwrite a cutover report")
    manifest = json.loads(args.manifest.read_text())
    assert_runtime(manifest)
    for relative, digest in manifest["source_manifest"].items():
        if hashlib.sha256(Path(relative).read_bytes()).hexdigest() != digest:
            raise ValueError("runner source differs from the sealed manifest")
    url = bootstrap(manifest, args.database)
    if args.layout:
        result = memory_sample(args, manifest, url)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    else:
        run(args, manifest, url)


if __name__ == "__main__":
    main()
