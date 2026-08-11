"""The real end-to-end capture: `capture_cohort.sh` -> launcher -> child -> PostgreSQL ->
published artifact + reviewable provenance diff (g-p4ih-capture).

WHY THIS RUNS FROM A CLONE. Capture refuses a dirty derivation tree, so a full success path
cannot be exercised from a working tree with uncommitted scorer edits — and demanding that
the developer commit first would make the producer's most important test the one nobody can
run. So the fixture makes a throwaway `git clone` of this repository, copies in whatever
SCORER_SOURCE_FILES differ from the clone's HEAD, and commits THERE. The clone is a real
main worktree with a real clean tree and a real HEAD revision; nothing is written to the
developer's repository. As a bonus this is also the multi-checkout case: the clone's git
common dir is its own, which is exactly the condition the output-keyed lock exists for.

WHAT THIS PROVES THAT test_capture_launcher_isolation.py CANNOT. Those tests stop at the
clean-tree gate. These go past it: the fence runs against a real PostgreSQL snapshot, the
artifact is frozen and scored for real by the pre-publication self-check, both files are
renamed into place, and the provenance record lands in the CLONE's working tree as an
uncommitted diff a human could review and commit. The attestation the child stamps
(`capture_source_revision`, `capture_scorer_source_digest`) is the digest the launcher
computed before the child interpreter existed.

Skipped without GHOSTREPLAY_TEST_PG_URL (@pg_required). Real scoring across the arm grid is
minutes, not seconds.

INTERPRETER. Capture derives the child's dependency paths from the interpreter it is handed
(GHOSTREPLAY_PYTHON), so that interpreter's environment must carry the scorer's deps. Locally
that is the repo venv (backend/.venv). On CI there is no repo venv — dependencies are
installed into the interpreter running pytest — so we fall back to sys.executable, which is
exactly that interpreter. The source-fence launcher supports a non-venv interpreter: the
base install is the selected environment in that case.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from conftest import pg_required

import scripts.calibrate_opening_scores_v2 as cal
from test_capture_cohort_pg import _reset, _seed_scorable_cohort

REPO_ROOT = Path(__file__).resolve().parents[1]
# Prefer the repo venv when present (the local developer setup); otherwise use the
# interpreter running pytest, which on CI is the environment the deps were installed into.
_REPO_VENV_PYTHON = REPO_ROOT / "backend" / ".venv" / "bin" / "python"
VENV_PYTHON = _REPO_VENV_PYTHON if _REPO_VENV_PYTHON.exists() else Path(sys.executable)
GUARD_USER = 14

# Files the clone needs at working-tree state, not HEAD state: everything the source digest
# covers, plus capture's own two entrypoints (which are not part of SCORER_SOURCE_FILES —
# the digest binds what the SCORER is derived from, and the launcher is upstream of it).
_EXTRA_SYNC = (
    "backend/scripts/capture_cohort.sh",
    "backend/scripts/capture_cohort_launcher.py",
    "backend/scripts/source_fence_launcher.py",
    # Canonical profile manifests are data files (not in the .py import closure of
    # SCORER_SOURCE_FILES) that analysis_profiles.py reads at import time. Their
    # ``dominates`` sets must stay consistent with evidence_policy.EDGES, so sync
    # the working-tree copies into the clone (g-reuse-d21-search added
    # browser-analysis-multipv-v2 to both).
    "backend/app/canonical_profiles/canonical-sf18-depth24-v1.json",
    "backend/app/canonical_profiles/canonical-sf18-depth24-linux-v1.json",
)

pytestmark = pytest.mark.skipif(
    not os.access(VENV_PYTHON, os.X_OK),
    reason=f"no usable python interpreter at {VENV_PYTHON}",
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture(scope="module")
def capture_clone():
    """A throwaway clone of this repo with the working-tree scorer committed onto it."""
    root = Path(tempfile.mkdtemp(prefix="ghostreplay-capture-e2e-", dir=os.path.realpath("/tmp")))
    clone = root / "checkout"
    try:
        subprocess.run(
            ["git", "clone", "--quiet", "--local", "--no-hardlinks",
             str(REPO_ROOT), str(clone)],
            check=True, capture_output=True, timeout=300,
        )
        for rel in (*cal.SCORER_SOURCE_FILES, *_EXTRA_SYNC):
            src, dst = REPO_ROOT / rel, clone / rel
            if not src.exists():
                continue
            if not dst.exists() or dst.read_bytes() != src.read_bytes():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        _git(clone, "add", "-A", "--", "backend")
        # -c: do not depend on (or read) the developer's git identity.
        subprocess.run(
            ["git", "-c", "user.email=capture-e2e@invalid", "-c", "user.name=capture e2e",
             "commit", "--quiet", "--allow-empty", "-m", "e2e: working-tree scorer"],
            cwd=str(clone), check=True, capture_output=True, timeout=120,
        )
        # The premise every test below rests on: a CLEAN main worktree.
        assert _git(clone, "status", "--porcelain") == ""
        yield clone
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def outdir():
    path = Path(tempfile.mkdtemp(prefix="ghostreplay-capture-e2e-out-", dir=os.path.realpath("/tmp")))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _run_capture(clone: Path, outdir: Path, database_url: str, *extra: str):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(outdir)),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "GHOSTREPLAY_PYTHON": str(VENV_PYTHON),
        "GHOSTREPLAY_RELEASE_GUARD_USER": str(GUARD_USER),
        "DATABASE_URL": database_url,
    }
    return subprocess.run(
        [str(clone / "backend" / "scripts" / "capture_cohort.sh"),
         "--output", str(outdir / "frozen-cohort.json"), *extra],
        env=env, capture_output=True, text=True, timeout=1800,
    )


@pytest.fixture
def seeded_db(pg_engine, pg_session_factory):
    _reset(pg_engine)
    _seed_scorable_cohort(pg_session_factory)
    return str(pg_engine.url.render_as_string(hide_password=False))


# ---------------------------------------------------------------------------
# The success path
# ---------------------------------------------------------------------------


@pg_required
def test_full_capture_publishes_artifact_and_reviewable_provenance_diff(
    capture_clone, outdir, seeded_db
):
    res = _run_capture(capture_clone, outdir, seeded_db)
    assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr}"

    artifact = outdir / "frozen-cohort.json"
    provenance = capture_clone / "backend" / "scripts" / "fixtures" / "cohort_provenance.json"
    assert artifact.exists()
    assert provenance.exists()

    # --- the two files describe each other ---
    record = json.loads(provenance.read_bytes())
    assert record["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()

    # --- the artifact is private at rest, and no temp survived either rename ---
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert list(outdir.glob("*.tmp-*")) == []
    assert list(provenance.parent.glob("*.tmp-*")) == []

    # --- the record binds the run to the SOURCE the launcher hashed pre-exec ---
    head = _git(capture_clone, "rev-parse", "HEAD")
    assert record["capture_source_revision"] == head
    launcher_digest = res.stderr.split("digest=")[1].split()[0]
    assert record["capture_scorer_source_digest"].startswith(launcher_digest)
    assert record["capture_python_version"].startswith("CPython")

    # --- the provenance record is the REVIEWABLE, COMMITTABLE half ---
    # It lands in the clone's working tree as an uncommitted change a human can read; the
    # artifact itself is outside the repo entirely.
    # -uall: porcelain collapses a wholly-untracked directory to the directory name, and on
    # a first capture backend/scripts/fixtures/ is exactly that.
    dirty = _git(capture_clone, "status", "--porcelain", "-uall")
    assert "backend/scripts/fixtures/cohort_provenance.json" in dirty
    assert str(outdir) not in dirty
    # Exactly one path changed: capture published nothing else into the tree.
    assert len([ln for ln in dirty.splitlines() if ln.strip()]) == 1

    # --- the human-facing summary is redacted to a basename ---
    assert "frozen-cohort.json" in res.stderr
    assert str(outdir) not in res.stderr

    # --- the record is canonical bytes plus exactly one newline ---
    raw = provenance.read_bytes()
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    assert raw == cal._canonical_dumps(record) + b"\n"
    # ...and the artifact carries NO trailing newline.
    assert not artifact.read_bytes().endswith(b"\n")


@pg_required
def test_a_crash_between_the_two_renames_fails_closed_and_reruns_clean(
    capture_clone, outdir, seeded_db
):
    """The one on-disk state atomic publication cannot rule out: artifact renamed, record
    not. This is a REAL process stopping between the two renames, not a simulation — the
    second os.replace is made to fail by putting a non-empty DIRECTORY where the record
    belongs. Everything before it succeeds normally: both temps are written, the self-check
    scores the candidate, and the artifact rename lands."""
    provenance = capture_clone / "backend" / "scripts" / "fixtures" / "cohort_provenance.json"
    artifact = outdir / "frozen-cohort.json"
    # backend/scripts/fixtures/ is untracked, so a fresh clone does not have it: the
    # directory only exists once some capture has published into it. Create it here
    # rather than depending on an earlier test in this module having run first —
    # capture creates it itself, so this is exactly the state a real first run sees.
    provenance.parent.mkdir(parents=True, exist_ok=True)
    if provenance.exists():
        provenance.unlink()
    # os.replace(file, non-empty-dir) fails; creating the temp beside it still works.
    provenance.mkdir()
    (provenance / "occupied").write_text("x")
    try:
        crashed = _run_capture(capture_clone, outdir, seeded_db)
    finally:
        shutil.rmtree(provenance)

    assert crashed.returncode == 1
    assert "CaptureInterRenameError" in crashed.stderr
    # The artifact half DID land: the failure is genuinely between the renames.
    assert artifact.exists()
    # Neither temp survived, and the private path is not in the message.
    assert list(outdir.glob("*.tmp-*")) == []
    assert list(provenance.parent.glob("*.tmp-*")) == []
    assert str(outdir) not in crashed.stderr
    # The operator is told to rerun, not to hand-repair.
    assert "Rerun capture-cohort" in crashed.stderr

    # A rerun republishes a matched pair — the documented recovery, executed for real.
    second = _run_capture(capture_clone, outdir, seeded_db)
    assert second.returncode == 0, second.stderr
    good_record = provenance.read_bytes()
    artifact_bytes = artifact.read_bytes()
    assert json.loads(good_record)["sha256"] == hashlib.sha256(artifact_bytes).hexdigest()

    # Finally, the property the recovery rests on: the consumer REFUSES a mismatched pair
    # rather than adopting it. The artifact bytes are never touched — corrupting them would
    # trip the canonical-bytes phase and prove nothing about the PAIRING.
    stale = json.loads(good_record)
    stale["sha256"] = "0" * 64
    stale_record = cal._canonical_dumps(stale) + b"\n"

    binding = cal._current_runtime_binding(cal.get_opening_graph(), cal.get_opening_roots())
    with pytest.raises(cal.ArtifactIntegrityError):
        cal.load_frozen_artifact(artifact_bytes, stale_record, binding)
    # ...and the SAME guard accepts the matched pair, so the refusal is the mismatch and
    # not some unrelated defect in the published bytes.
    cal.load_frozen_artifact(artifact_bytes, good_record, binding)


# ---------------------------------------------------------------------------
# Multi-checkout mutual exclusion, against real processes
# ---------------------------------------------------------------------------


@pg_required
def test_a_second_capture_to_the_same_output_is_refused(capture_clone, outdir, seeded_db):
    """Two checkouts publishing to one private destination share no git common dir, which
    is why the second lock is keyed on the RESOLVED OUTPUT PATH. Held here by this pytest
    process — a different process, a different checkout, the same destination."""
    output = (outdir / "frozen-cohort.json").resolve()
    common = outdir / "unrelated-common"
    common.mkdir()
    with cal._capture_locks(str(common), output):
        res = _run_capture(capture_clone, outdir, seeded_db)
    assert res.returncode == 1
    assert "CaptureLockError" in res.stderr
    assert "output" in res.stderr
    # Refused BEFORE reading any evidence and before publishing anything.
    assert not output.exists()


def test_capture_refuses_to_run_from_a_linked_worktree(capture_clone, outdir):
    """`git worktree add` produces a checkout whose git dir is a subdirectory of the common
    dir. Capture refuses it: the provenance record is a working-tree diff meant to be
    reviewed and committed from the main checkout, and a linked worktree (the shape the
    RELEASE launcher creates, and destroys on exit) is not where that can land."""
    linked = outdir / "linked"
    subprocess.run(
        ["git", "worktree", "add", "--quiet", "--detach", str(linked), "HEAD"],
        cwd=str(capture_clone), check=True, capture_output=True, timeout=120,
    )
    try:
        res = _run_capture(linked, outdir, "postgresql://127.0.0.1:1/nonexistent")
        assert res.returncode == 1
        assert "CaptureWorktreeError" in res.stderr
        # Precedence: refused before the source fence ever read a byte of the tree.
        assert "CaptureSourceError" not in res.stderr
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(linked)],
                       cwd=str(capture_clone), capture_output=True, timeout=120)


def test_output_inside_the_checkout_is_refused(capture_clone, outdir):
    """Governance, at the real boundary: a private artifact must not become committable."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(outdir)),
        "GHOSTREPLAY_PYTHON": str(VENV_PYTHON),
        "GHOSTREPLAY_RELEASE_GUARD_USER": str(GUARD_USER),
        "DATABASE_URL": "postgresql://127.0.0.1:1/nonexistent",
    }
    inside = capture_clone / "backend" / "captured.json"
    res = subprocess.run(
        [str(capture_clone / "backend" / "scripts" / "capture_cohort.sh"),
         "--output", str(inside)],
        env=env, capture_output=True, text=True, timeout=600,
    )
    assert res.returncode == 1
    assert "CaptureGovernanceError" in res.stderr
    assert not inside.exists()
    # Nothing landed in the checkout. (A provenance record from an earlier test in this
    # module may already be here; the artifact must not be.)
    assert "captured.json" not in _git(capture_clone, "status", "--porcelain", "-uall")
