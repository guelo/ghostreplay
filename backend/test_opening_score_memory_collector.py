import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from pathlib import Path
import subprocess

import pytest

from scripts import collect_opening_score_memory as collector
from scripts import bench_opening_score_cutover as runner
from scripts import qualify_opening_score_storage as q


@pytest.fixture
def collection(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    seed = tmp_path / "C3.json"
    seed.write_text(json.dumps({
        "cell": "C3", "profile": "S1", "copies": 1, "complete": True,
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "capture_sha256": "capture", "source_manifest": {},
        "profile_identity": {"settings": {"max_wal_size": "128"}},
        "traffic_representative": False,
    }))
    args = SimpleNamespace(manifest=manifest, seed_report=seed,
        capture_slice=seed.with_suffix(".slice.pickle"), output=tmp_path / "clean.json",
        database="gr_score_qual_cutover_s1_c3")
    monkeypatch.setattr(q, "assert_private_store", lambda p: p)
    monkeypatch.setattr(runner, "assert_runtime", lambda m: None)
    monkeypatch.setattr(runner, "bootstrap", lambda *a: "guarded-url")
    monkeypatch.setattr(runner, "assert_server", lambda *a: None)
    monkeypatch.setattr(q, "engine_for", lambda url: MagicMock())
    monkeypatch.setattr(q, "profile_identity", lambda engine: {"settings": {"max_wal_size": "128"}})

    def no_capture(*args, **kwargs):
        raise AssertionError("the collector parent must never load a capture")

    monkeypatch.setattr(q, "load_capture", no_capture)
    monkeypatch.setenv("DATABASE_URL", "must-not-be-inherited")
    calls = []

    def worker(command, *, env, check):
        assert "DATABASE_URL" not in env
        assert command[command.index("--capture") + 1] == str(args.capture_slice)
        calls.append(command)
        output = Path(command[command.index("--output") + 1])
        layout = command[command.index("--layout") + 1]
        output.write_text(json.dumps({"layout": layout, "logical_rows": 100,
            "untraced_worker_rss_highwater_bytes": 1000,
            "publication_allocation_peak_bytes": 100}))

    monkeypatch.setattr(collector.subprocess, "run", worker)
    return args, calls, worker


def test_clean_parent_collects_five_independent_workers_without_loading_capture(collection):
    args, calls, _ = collection
    original = args.seed_report.read_bytes()
    collector.collect(args)
    report = json.loads(args.output.read_text())
    assert report["complete"] is True
    assert report["result"]["memory_launch_mode"] == "clean_parent"
    assert {key: len(rows) for key, rows in report["result"]["children"].items()} == {"A": 5, "B50": 5}
    assert len(calls) == len(report["child_reports_sha256"]) == 10
    assert args.seed_report.read_bytes() == original


def test_child_failure_preserves_partial_evidence(collection, monkeypatch):
    args, calls, worker = collection

    def fail_third(command, **kwargs):
        if len(calls) == 2:
            raise subprocess.CalledProcessError(1, command)
        return worker(command, **kwargs)

    monkeypatch.setattr(collector.subprocess, "run", fail_third)
    with pytest.raises(subprocess.CalledProcessError):
        collector.collect(args)
    report = json.loads(args.output.read_text())
    assert report["complete"] is False
    assert len(report["child_reports_sha256"]) == 2
