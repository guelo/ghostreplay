#!/usr/bin/env python3
"""Isolated source-fence handoff for frozen-cohort capture.

The outer process runs under ``python -I -S`` and hashes the scorer manifest before
starting a fresh ``-S`` child. The child receives only that digest, explicitly derived
dependency paths, and a new bytecode-cache directory.
"""
from __future__ import annotations

import ast
import hashlib
import os
import subprocess
import sys
import sysconfig
from pathlib import Path, PurePosixPath
from typing import Sequence


CALIBRATE_REL = "backend/scripts/calibrate_opening_scores_v2.py"
_MANIFEST_NAME = "SCORER_SOURCE_FILES"
SCORER_SOURCE_DIGEST_ENV = "GHOSTREPLAY_SCORER_SOURCE_DIGEST"


class LauncherError(Exception):
    """The source fence cannot safely start the scorer."""


def manifest_path(tree_root: Path, rel: str) -> Path:
    """Resolve a manifest entry, refusing paths that escape ``tree_root``."""
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts:
        raise LauncherError(f"manifest path {rel!r} is not a repo-relative path")
    root = tree_root.resolve()
    resolved = (tree_root / pure).resolve()
    if resolved != root and root not in resolved.parents:
        raise LauncherError(
            f"manifest path {rel!r} resolves outside the source tree; refusing to hash "
            "bytes the capture tree does not contain"
        )
    return resolved


def read_manifest(tree_root: Path) -> tuple[str, ...]:
    """Parse the scorer's literal manifest without importing the scorer."""
    source = manifest_path(tree_root, CALIBRATE_REL)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise LauncherError(f"cannot read the scorer: {exc}") from exc
    try:
        nodes = ast.parse(text).body
    except SyntaxError as exc:
        raise LauncherError(f"cannot parse the scorer manifest: {exc}") from exc
    for node in nodes:
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        if not any(isinstance(target, ast.Name) and target.id == _MANIFEST_NAME for target in targets):
            continue
        if node.value is None:
            continue
        try:
            value = ast.literal_eval(node.value)
        except ValueError as exc:
            raise LauncherError(
                f"{_MANIFEST_NAME} is not a literal; the source fence must not execute the scorer"
            ) from exc
        if not isinstance(value, tuple) or not value or not all(isinstance(rel, str) for rel in value):
            raise LauncherError(f"{_MANIFEST_NAME} is not a non-empty tuple of str")
        if CALIBRATE_REL not in value:
            raise LauncherError(f"{_MANIFEST_NAME} must include {CALIBRATE_REL}")
        return value
    raise LauncherError(f"no module-level {_MANIFEST_NAME} found in {CALIBRATE_REL}")


def manifest_digest(tree_root: Path) -> str:
    """Return the scorer's ``path + NUL + bytes + NUL`` SHA-256 manifest digest."""
    digest = hashlib.sha256()
    for rel in read_manifest(tree_root):
        path = manifest_path(tree_root, rel)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise LauncherError(f"manifest file {rel!r} is unreadable: {exc}") from exc
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def require_isolated_launcher(flags: object = sys.flags) -> None:
    """Require ``-I -S`` before this process reads or hashes source bytes."""
    if not getattr(flags, "no_site", False) or not getattr(flags, "isolated", False):
        raise LauncherError(
            "the source-fence launcher must run under `-I -S`; site startup hooks can run "
            "before this process hashes anything. Use backend/scripts/capture_cohort.sh."
        )


def _venv_root() -> Path | None:
    root = Path(sys.executable).parent.parent
    return root if (root / "pyvenv.cfg").is_file() else None


def _audited_dep_paths() -> list[Path]:
    """Derive the selected interpreter's dependency paths for a child run under ``-S``."""
    venv = _venv_root()
    if venv is None:
        paths = sysconfig.get_paths()
    else:
        schemes = sysconfig.get_scheme_names()
        scheme = "venv" if "venv" in schemes else ("nt" if os.name == "nt" else "posix_prefix")
        paths = sysconfig.get_paths(scheme=scheme, vars={"base": str(venv), "platbase": str(venv)})
    dep_paths: list[Path] = []
    for key in ("purelib", "platlib"):
        path = Path(paths[key])
        if path not in dep_paths:
            dep_paths.append(path)
    missing = [path for path in dep_paths if not path.is_dir()]
    if missing:
        raise LauncherError(f"derived dependency paths do not exist: {missing}")
    return dep_paths


def child_env(tree_root: Path, digest: str, pycache_dir: Path) -> dict[str, str]:
    """Build the child's environment from non-Python configuration plus fenced inputs."""
    del tree_root
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    env[SCORER_SOURCE_DIGEST_ENV] = digest
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPYCACHEPREFIX"] = str(pycache_dir)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(str(path) for path in _audited_dep_paths())
    return env


def child_command(tree_root: Path, script_args: Sequence[str]) -> list[str]:
    """Run the scorer from the requested tree with site startup disabled."""
    return [sys.executable, "-S", str(tree_root / CALIBRATE_REL), *script_args]


def launch(tree_root: Path, script_args: Sequence[str], *, pycache_dir: Path) -> int:
    """Hash the source manifest, then execute a fresh fenced scorer child."""
    digest = manifest_digest(tree_root)
    pycache_dir.mkdir(parents=True, exist_ok=True)
    if any(pycache_dir.iterdir()):
        raise LauncherError("bytecode cache is not empty; capture requires a fresh cache directory")
    print(f"[source-fence] tree={tree_root} digest={digest[:12]}", file=sys.stderr)
    return subprocess.run(
        child_command(tree_root, script_args),
        env=child_env(tree_root, digest, pycache_dir),
        cwd=str(tree_root / "backend"),
        check=False,
    ).returncode
