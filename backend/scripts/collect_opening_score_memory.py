"""Repeat sealed C3 workers from a parent that never loads the cohort.

Linux getrusage accounting survives exec. The capture-loaded C3 orchestrator can
therefore contaminate each child's peak with its own large resident set. This
collector reads only JSON metadata; each worker loads only the two-candidate slice.
The original C3 report and every child report remain immutable evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def worker_command(args, layout, output):
    return [sys.executable, "-m", "scripts.bench_opening_score_cutover",
            "--manifest", str(args.manifest), "--database", args.database,
            "--capture", str(args.capture_slice), "--output", str(output),
            "--cell", "C3", "--layout", layout]


def collect(args):
    previous_umask = os.umask(0o077)
    try:
        return _collect(args)
    finally:
        os.umask(previous_umask)


def _collect(args):
    from scripts import bench_opening_score_cutover as runner
    from scripts import qualify_opening_score_storage as q

    for path in (args.manifest, args.seed_report, args.capture_slice, args.output):
        q.assert_private_store(path)
    if args.output.exists():
        raise ValueError("refusing to overwrite the clean-parent memory report")
    manifest = json.loads(args.manifest.read_text())
    seed = json.loads(args.seed_report.read_text())
    if seed["cell"] != "C3" or not seed["complete"]:
        raise ValueError("a completed setup C3 report is required")
    if seed["manifest_sha256"] != hashlib.sha256(args.manifest.read_bytes()).hexdigest():
        raise ValueError("source/runtime manifest differs from the setup run")
    runner.assert_runtime(manifest)
    url = runner.bootstrap(manifest, args.database)
    engine = q.engine_for(url)
    try:
        with engine.connect() as conn:
            runner.assert_server(conn, manifest, args.database)
        identity = q.profile_identity(engine)
        if identity["settings"] != seed["profile_identity"]["settings"]:
            raise ValueError("restore the setup run's settings before memory sampling")
    finally:
        engine.dispose()
    # No full capture, candidates or shared-evidence rows are loaded here.
    child_env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    children = {"A": [], "B50": []}
    report = {key: seed[key] for key in (
        "cell", "profile", "copies", "manifest_sha256", "capture_sha256",
        "source_manifest", "profile_identity", "traffic_representative")}
    report.update(complete=False, result={"children": children, "memory_launch_mode": "clean_parent"},
                  setup_report_sha256=hashlib.sha256(args.seed_report.read_bytes()).hexdigest(),
                  collector_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  child_reports_sha256={})
    try:
        for repeat in range(5):
            for layout in (("A", "B50") if repeat % 2 == 0 else ("B50", "A")):
                output = args.output.with_suffix(f".{layout}.{repeat}.json")
                subprocess.run(worker_command(args, layout, output), env=child_env, check=True)
                child = json.loads(output.read_text())
                if child["layout"] != layout:
                    raise ValueError("memory child returned the wrong layout")
                children[layout].append(child)
                report["child_reports_sha256"][output.name] = hashlib.sha256(output.read_bytes()).hexdigest()
        report["complete"] = True
    finally:
        args.output.write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--seed-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.capture_slice = args.seed_report.with_suffix(".slice.pickle")
    collect(args)


if __name__ == "__main__":
    main()
