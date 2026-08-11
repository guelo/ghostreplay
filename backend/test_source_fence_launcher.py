"""Behavior contracts for the capture source-fence handoff."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.calibrate_opening_scores_v2 as cal
import scripts.source_fence_launcher as launcher


REPO_ROOT = Path(cal.__file__).resolve().parents[2]


def _tree(tmp_path: Path, manifest: str) -> Path:
    tree = tmp_path / "tree"
    (tree / "backend/scripts").mkdir(parents=True)
    (tree / launcher.CALIBRATE_REL).write_text(manifest)
    return tree


def test_parses_the_scorers_literal_manifest_without_importing_it():
    assert launcher.read_manifest(REPO_ROOT) == cal.SCORER_SOURCE_FILES
    assert launcher.manifest_digest(REPO_ROOT) == cal.scorer_source_digest()


def test_digest_moves_when_a_manifest_byte_moves(tmp_path):
    tree = _tree(
        tmp_path,
        f'SCORER_SOURCE_FILES = ("backend/app/fen.py", "{launcher.CALIBRATE_REL}")\n',
    )
    target = tree / "backend/app/fen.py"
    target.parent.mkdir()
    target.write_text("A = 1\n")
    before = launcher.manifest_digest(tree)
    target.write_text("A = 2\n")
    assert launcher.manifest_digest(tree) != before


@pytest.mark.parametrize("entry", ["/etc/passwd", "backend/../../outside.py"])
def test_manifest_rejects_escaping_paths(tmp_path, entry):
    with pytest.raises(launcher.LauncherError, match="repo-relative"):
        launcher.manifest_path(tmp_path, entry)


def test_manifest_rejects_missing_nonliteral_and_outside_symlink_entries(tmp_path):
    nonliteral = _tree(tmp_path / "nonliteral", "SCORER_SOURCE_FILES = tuple(discover())\n")
    with pytest.raises(launcher.LauncherError, match="not a literal"):
        launcher.read_manifest(nonliteral)

    missing = _tree(
        tmp_path / "missing",
        f'SCORER_SOURCE_FILES = ("backend/app/fen.py", "{launcher.CALIBRATE_REL}")\n',
    )
    with pytest.raises(launcher.LauncherError, match="unreadable"):
        launcher.manifest_digest(missing)

    outside = tmp_path / "outside.py"
    outside.write_text("outside\n")
    linked = _tree(
        tmp_path / "linked",
        f'SCORER_SOURCE_FILES = ("backend/app/fen.py", "{launcher.CALIBRATE_REL}")\n',
    )
    (linked / "backend/app").mkdir()
    (linked / "backend/app/fen.py").symlink_to(outside)
    with pytest.raises(launcher.LauncherError, match="outside the source tree"):
        launcher.manifest_digest(linked)


@pytest.mark.parametrize("flags", [SimpleNamespace(isolated=0, no_site=0),
                                    SimpleNamespace(isolated=1, no_site=0),
                                    SimpleNamespace(isolated=0, no_site=1)])
def test_launcher_requires_both_isolation_flags(flags):
    with pytest.raises(launcher.LauncherError, match="-I -S"):
        launcher.require_isolated_launcher(flags)
    launcher.require_isolated_launcher(SimpleNamespace(isolated=1, no_site=1))


def test_child_environment_scrubs_python_inputs_and_retains_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/hostile")
    monkeypatch.setenv("PYTHONWARNINGS", "default::hostile.Warning")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    env = launcher.child_env(tmp_path, "d" * 64, tmp_path / "pycache")
    assert env[launcher.SCORER_SOURCE_DIGEST_ENV] == "d" * 64
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTHONPYCACHEPREFIX"] == str(tmp_path / "pycache")
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["DATABASE_URL"] == "postgresql://example.invalid/db"
    assert "PYTHONWARNINGS" not in env
    assert "/hostile" not in env["PYTHONPATH"].split(os.pathsep)
    assert all(Path(path).is_dir() for path in env["PYTHONPATH"].split(os.pathsep))


def test_child_command_targets_requested_tree_under_no_site(tmp_path):
    command = launcher.child_command(tmp_path, ["capture-cohort", "--output", "/private/c.json"])
    assert command[1] == "-S"
    assert command[2] == str(tmp_path / launcher.CALIBRATE_REL)
    assert command[3:] == ["capture-cohort", "--output", "/private/c.json"]


def test_launch_refuses_any_prepopulated_cache(tmp_path):
    tree = _tree(
        tmp_path,
        f'SCORER_SOURCE_FILES = ("backend/app/fen.py", "{launcher.CALIBRATE_REL}")\n',
    )
    target = tree / "backend/app/fen.py"
    target.parent.mkdir()
    target.write_text("A = 1\n")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "stale.txt").write_text("stale")
    with pytest.raises(launcher.LauncherError, match="not empty"):
        launcher.launch(tree, [], pycache_dir=cache)
