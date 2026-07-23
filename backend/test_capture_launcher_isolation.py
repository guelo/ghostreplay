"""REAL two-process launch tests for the capture entrypoint (g-p4ih-capture).

Everything here runs `backend/scripts/capture_cohort.sh` as an actual subprocess. That is
the point: the startup-hook vectors this entrypoint exists to close are INVISIBLE from
inside pytest. pytest's own interpreter already ran `site.py`, already imported
`sitecustomize`, already executed every `.pth` in the venv, and already installed whatever
`PYTHONWARNINGS` asked for — an in-process test of "the hook did not run" is vacuous,
because the hook ran before the test module was imported.

Each hostile-vector test comes in two halves, and BOTH halves are load-bearing:

  * a POSITIVE CONTROL that fires the vector against an ordinary interpreter and observes
    its side effect, proving the plant is live and the assertion is not vacuous;
  * the NEGATIVE assertion that the same plant, with the same environment, produces no side
    effect when the run goes through `capture_cohort.sh`.

WHAT A DIRTY WORKING TREE STILL PROVES. Capture refuses a dirty derivation tree, so on a
tree with uncommitted scorer edits a real run stops at the clean-tree gate. That gate is the
LAST step of the source fence, so reaching it proves everything before it succeeded:

  * the launcher started under `-I -S` and computed the manifest digest pre-exec;
  * the child imported `scripts.calibrate_opening_scores_v2` in full under `-S` — which
    means sqlalchemy, chess, and `app.*` all resolved from the PYTHONPATH the launcher
    handed it (DEPENDENCY DELIVERY: under `-S` nothing is importable except what the child
    was given);
  * `_require_capture_isolation` passed, so the child had `no_site`, bytecode writing off,
    and the inherited pre-exec digest;
  * the child's own read of the manifest AGREED with that pre-exec digest (PRE-EXEC
    VERIFICATION), and the bytecode, import-origin, and chess-distribution checks passed.

So the tests assert a STAGE, not an exit code: the run must get at least as far as the
clean-tree gate, and must not fail at any earlier one. That assertion holds on a clean tree
too, where the run proceeds past the fence to the evidence DB.

These tests never touch a real evidence database: DATABASE_URL is overridden to an
unreachable local address, and the source fence is reached before the first connection.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "backend" / "scripts" / "capture_cohort.sh"
LAUNCHER = REPO_ROOT / "backend" / "scripts" / "capture_cohort_launcher.py"
VENV_PYTHON = REPO_ROOT / "backend" / ".venv" / "bin" / "python"

# An address nothing listens on. The fence is reached before any connection is attempted,
# so this is belt-and-braces: no test in this file can reach a real evidence DB even if a
# clean tree lets the run continue past the source fence.
UNREACHABLE_DB = "postgresql://127.0.0.1:1/nonexistent"

# Refusals that happen BEFORE the source fence's clean-tree gate. If a hostile plant were
# taking effect, this is where the run would visibly derail.
PRE_FENCE_REFUSALS = (
    "CaptureIsolationError",
    "CaptureWorktreeError",
    "CaptureGovernanceError",
    "CaptureDialectError",
    "CaptureLockError",
    "CapturePublicationError",
)

pytestmark = pytest.mark.skipif(
    not WRAPPER.exists() or not VENV_PYTHON.exists(),
    reason="needs the repo venv and the capture wrapper on disk",
)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def workdir():
    """A scratch directory OUTSIDE the repo working tree (TMPDIR may point inside it, and
    a repo-interior --output is a governance refusal that would mask the real assertion)."""
    path = Path(tempfile.mkdtemp(prefix="ghostreplay-capture-launch-", dir="/private/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _base_env(workdir: Path) -> dict[str, str]:
    """A minimal environment built from scratch, NOT inherited. The developer shell's real
    DATABASE_URL must never reach a capture subprocess."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(workdir)),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "DATABASE_URL": UNREACHABLE_DB,
        "GHOSTREPLAY_RELEASE_GUARD_USER": "14",
    }


def _run_wrapper(workdir: Path, env_extra: dict[str, str] | None = None):
    env = _base_env(workdir)
    env.update(env_extra or {})
    return subprocess.run(
        [str(WRAPPER), "--output", str(workdir / "cohort.json")],
        env=env, capture_output=True, text=True, timeout=180,
    )


def _assert_reached_the_source_fence(res) -> None:
    """The run got at least as far as the clean-tree gate: the launcher hashed the tree,
    the child imported and passed every earlier gate. See the module docstring."""
    assert "[launcher] tree=" in res.stderr, res.stderr
    assert "digest=" in res.stderr, res.stderr
    for marker in PRE_FENCE_REFUSALS:
        assert marker not in res.stderr, f"derailed at {marker}:\n{res.stderr}"
    # The only source-fence refusal allowed here is the clean-tree one. Any OTHER
    # CaptureSourceError (digest disagreement, bytecode, import origin, chess origin) means
    # a plant DID reach the child.
    if "CaptureSourceError" in res.stderr:
        assert "DIRTY derivation tree" in res.stderr, res.stderr


def _plant(directory: Path, name: str, marker: Path) -> None:
    """Write a hostile module that records the fact that it was imported. Fail-safe: it can
    never raise, so a stray copy cannot break an unrelated interpreter."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        "import os\n"
        "try:\n"
        f"    open({str(marker)!r}, 'w').write('1')\n"
        "except Exception:\n"
        "    pass\n"
        "class Boom(Warning):\n"
        "    pass\n"
    )


# ---------------------------------------------------------------------------
# The launcher cannot be started wrong
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flags", [[], ["-S"], ["-I"]])
def test_launcher_refuses_without_both_isolation_flags(workdir, flags):
    """`require_isolated_launcher` is checked in a real process, not simulated. A launcher
    missing either flag has ALREADY been contaminated by the time it could re-exec itself,
    so the only honest move is exit 2 — and the wrapper is what supplies the flags."""
    res = subprocess.run(
        [str(VENV_PYTHON), *flags, str(LAUNCHER), "--output", str(workdir / "c.json")],
        env=_base_env(workdir), capture_output=True, text=True, timeout=120,
    )
    assert res.returncode == 2, res.stderr
    assert "refusing to run" in res.stderr
    # It refused BEFORE doing any work: no digest was computed, nothing was vouched for.
    assert "[launcher] tree=" not in res.stderr


def test_wrapper_supplies_the_flags_and_reaches_the_source_fence(workdir):
    """The baseline every other test in this file is measured against.

    This is the DEPENDENCY-DELIVERY and PRE-EXEC-VERIFICATION proof: the child cannot print
    a capture error at all unless `scripts.calibrate_opening_scores_v2` imported completely
    under `-S`, which requires sqlalchemy + chess + `app.*` to resolve from the PYTHONPATH
    the launcher derived — and unless its own manifest read matched the digest the launcher
    computed before the child interpreter existed."""
    res = _run_wrapper(workdir)
    _assert_reached_the_source_fence(res)


# ---------------------------------------------------------------------------
# Hostile startup vectors
# ---------------------------------------------------------------------------


def test_pythonpath_sitecustomize_never_runs(workdir):
    """`sitecustomize` is imported by `site.py` before the first line of the target script.
    A hook there can rebind hashlib in the process that computes the digest."""
    plant = workdir / "plant"
    marker = workdir / "sitecustomize-ran"
    _plant(plant, "sitecustomize.py", marker)
    env = {"PYTHONPATH": str(plant)}

    # POSITIVE CONTROL: an ordinary interpreter imports it before running anything.
    control_env = _base_env(workdir) | env
    subprocess.run([str(VENV_PYTHON), "-c", "pass"], env=control_env, timeout=60, check=True)
    assert marker.exists(), "the plant is not live — the negative assertion would be vacuous"
    marker.unlink()

    res = _run_wrapper(workdir, env)
    assert not marker.exists(), (
        "sitecustomize executed inside a capture process; -I on the launcher (which implies "
        "-E) and the child's PYTHON*-stripped environment are both supposed to prevent this"
    )
    _assert_reached_the_source_fence(res)


def test_pythonwarnings_import_hook_never_runs(workdir):
    """The vector `-S` does NOT close, and the reason the child gets an ALLOWLISTED
    environment instead of a denylisted one.

    A warnings filter names its category as ``module.Class``, and the interpreter IMPORTS
    that module while installing the filter — before the script body, and before `site` is
    even relevant. The positive control below fires it under `-S` on purpose: the only
    thing that stops it is not passing the variable on."""
    plant = workdir / "warnplant"
    marker = workdir / "warnhook-ran"
    _plant(plant, "hostile_probe_xyz.py", marker)
    env = {
        "PYTHONPATH": str(plant),
        "PYTHONWARNINGS": "default::hostile_probe_xyz.Boom",
    }

    # POSITIVE CONTROL, deliberately under -S: the hook still runs. -S is not the mitigation.
    control_env = _base_env(workdir) | env
    subprocess.run([str(VENV_PYTHON), "-S", "-c", "pass"], env=control_env, timeout=60, check=True)
    assert marker.exists(), "the plant is not live — the negative assertion would be vacuous"
    marker.unlink()

    res = _run_wrapper(workdir, env)
    assert not marker.exists(), "a PYTHONWARNINGS import hook ran inside a capture process"
    # Not merely unimportable in the child — never passed on at all. If PYTHONWARNINGS had
    # been inherited with the plant stripped from PYTHONPATH, CPython would have complained.
    assert "Invalid -W option" not in res.stderr, res.stderr
    _assert_reached_the_source_fence(res)


def test_pythonpath_cannot_shadow_the_childs_dependencies(workdir):
    """PYTHONPATH is REPLACED for the child, not extended: a shadow copy of a dependency on
    the operator's PYTHONPATH must not become the module the producer runs against."""
    plant = workdir / "shadow"
    marker = workdir / "shadow-chess-imported"
    _plant(plant, "chess.py", marker)
    env = {"PYTHONPATH": str(plant)}

    # POSITIVE CONTROL: with that PYTHONPATH, an ordinary `import chess` resolves the shadow.
    control_env = _base_env(workdir) | env
    control = subprocess.run(
        [str(VENV_PYTHON), "-S", "-c", "import chess, sys; sys.stdout.write(chess.__file__)"],
        env=control_env, capture_output=True, text=True, timeout=60,
    )
    assert control.stdout.startswith(str(plant)), control.stdout
    marker.unlink(missing_ok=True)

    res = _run_wrapper(workdir, env)
    assert not marker.exists(), "the shadow chess module was imported by a capture process"
    # A shadowed chess would have tripped the distribution-RECORD origin check, not the
    # clean-tree gate.
    _assert_reached_the_source_fence(res)


def test_pth_in_site_packages_executes_at_startup_and_S_is_what_stops_it(workdir):
    """The `.pth` half of the same claim, demonstrated against a THROWAWAY venv.

    A `.pth` line beginning with `import` is exec'd by `site.py` at startup, and anything
    pip ever installed can drop one into site-packages — this is the vector `-S` exists for.
    The demonstration deliberately uses a disposable venv rather than planting a `.pth` in
    the repo venv: a stray `.pth` in a real environment executes on EVERY interpreter start.
    That the capture processes themselves run under `-S` is proved by the wrapper tests
    above (the child refuses to proceed unless `sys.flags.no_site` is set)."""
    venv = workdir / "throwaway"
    subprocess.run([str(VENV_PYTHON), "-m", "venv", "--without-pip", str(venv)],
                   check=True, timeout=180, capture_output=True)
    site_packages = next(iter((venv / "lib").glob("python*/site-packages")))
    marker = workdir / "pth-ran"
    body = (
        "try:\n"
        f"    open({str(marker)!r}, 'w').write('1')\n"
        "except Exception:\n"
        "    pass\n"
    )
    (site_packages / "zz_hostile.pth").write_text("import os, sys; exec(%r)\n" % body)
    py = venv / "bin" / "python"

    subprocess.run([str(py), "-c", "pass"], check=True, timeout=60, capture_output=True)
    assert marker.exists(), "the .pth plant is not live"
    marker.unlink()

    subprocess.run([str(py), "-S", "-c", "pass"], check=True, timeout=60, capture_output=True)
    assert not marker.exists(), "-S did not suppress .pth execution"


def test_inherited_bytecode_settings_cannot_be_turned_back_on(workdir):
    """The digest binds .py SOURCE bytes while the interpreter runs .pyc, so the child
    refuses to certify unless bytecode writing is OFF. An operator environment that switches
    it back on must not reach the child."""
    res = _run_wrapper(workdir, {
        "PYTHONDONTWRITEBYTECODE": "",
        "PYTHONPYCACHEPREFIX": str(workdir / "attacker-cache"),
    })
    # The child's isolation check would have refused outright had either survived.
    _assert_reached_the_source_fence(res)
    assert not (workdir / "attacker-cache").exists()


def test_the_wrapper_never_falls_back_to_path(workdir):
    """A missing interpreter is exit 2 with an explanation, never a silent `python3` from
    PATH — that would capture production evidence against whatever versions the system
    happens to carry."""
    env = _base_env(workdir)
    env["GHOSTREPLAY_PYTHON"] = str(workdir / "no-such-python")
    res = subprocess.run(
        [str(WRAPPER), "--output", str(workdir / "cohort.json")],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 2
    assert "no usable interpreter" in res.stderr
    assert "[launcher] tree=" not in res.stderr


# ---------------------------------------------------------------------------
# Argument handling at the real boundary
# ---------------------------------------------------------------------------


def test_relative_output_is_refused_not_resolved_against_the_childs_cwd(workdir):
    """The launcher execs the child with cwd=<tree>/backend, so a relative --output would
    silently mean a directory the operator never named."""
    env = _base_env(workdir)
    res = subprocess.run(
        [str(WRAPPER), "--output", "cohort.json"],
        cwd=str(workdir), env=env, capture_output=True, text=True, timeout=180,
    )
    assert res.returncode == 1
    assert "CaptureGovernanceError" in res.stderr
    assert "ABSOLUTE" in res.stderr
    # Nothing was written to EITHER candidate directory.
    assert not (workdir / "cohort.json").exists()
    assert not (REPO_ROOT / "backend" / "cohort.json").exists()


def test_non_positive_max_attempts_is_a_usage_error_not_a_traceback(workdir):
    env = _base_env(workdir)
    bad = subprocess.run(
        [str(WRAPPER), "--output", str(workdir / "cohort.json"), "--max-attempts", "0"],
        env=env, capture_output=True, text=True, timeout=180,
    )
    assert bad.returncode == 2
    assert "Traceback" not in bad.stderr
    assert "max-attempts" in bad.stderr


def test_missing_release_guard_user_refuses_before_any_work(workdir):
    """The release-guard user is environment-only (never a CLI argument: a production user
    id must not enter shell history or every process listing)."""
    env = _base_env(workdir)
    del env["GHOSTREPLAY_RELEASE_GUARD_USER"]
    res = subprocess.run(
        [str(WRAPPER), "--output", str(workdir / "cohort.json")],
        env=env, capture_output=True, text=True, timeout=180,
    )
    assert res.returncode == 2
    assert "GHOSTREPLAY_RELEASE_GUARD_USER" in res.stderr
    assert "Traceback" not in res.stderr


def test_no_pycache_is_written_into_the_repo_by_a_capture_run(workdir):
    """The child runs with bytecode writing off and a fresh empty cache prefix, so a run
    must not leave compiled artifacts in the derivation tree it just certified."""
    scripts_dir = REPO_ROOT / "backend" / "scripts"
    before = {p for p in scripts_dir.rglob("__pycache__/*.pyc")}
    res = _run_wrapper(workdir)
    _assert_reached_the_source_fence(res)
    after = {p for p in scripts_dir.rglob("__pycache__/*.pyc")}
    assert after == before, f"capture wrote bytecode into the tree: {sorted(after - before)}"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
