"""The pre-exec source digest + exclusive checkout (g-p4ih-srcfence).

The launcher's whole job is to know something the run cannot know about itself: that the
bytes named by ``scorer_source_digest`` are the bytes the interpreter compiled. It buys that
with ORDER (hash before the interpreter exists) and EXCLUSIVITY (a checkout nothing else can
write to). Both are tested here against a real ``git worktree`` and a real child process —
the in-process half is tested in test_calibrate_opening_scores.py.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.opening_graph as opening_graph
import scripts.calibrate_opening_scores_v2 as cal
import scripts.release_calibration_launcher as launcher

_REPO_ROOT = Path(cal.__file__).resolve().parents[2]
_BACKEND_ROOT = _REPO_ROOT / "backend"
_LAUNCHER_PATH = Path(launcher.__file__).resolve()

# The proofs that ATTACH A REAL VOLUME. They are the acceptance cases for the OS boundary and
# they are not optional — but they are wrong to run on every push, for two reasons that are
# properties of the thing under test rather than of how the tests are written:
#
#   COST      4m41s measured, most of it the one module-scoped sealed volume: it carries
#             the interpreter, the dependency tree and the dylib closure, so it is ~900MB
#             staged, copied into an image and attached. The other 3,466 backend tests run in
#             2m57s, so these fifteen were more than half the gate.
#   SHARING   the sealed bytes are compared against the LIVE host trees (py/, deps/, dylibs/)
#             — deliberately, it is the concurrent-`pip install` detector — and this repo is
#             worked in by several agents at once. Somebody else's install, or a .pyc landing
#             mid-stage, fails the comparison for a reason that belongs to nobody's change.
#             In a pre-push gate that reads as a flake; in a release run it reads correctly,
#             as "the host moved under the seal, do it again".
#
# So they are deselected by `.githooks/pre-push` (`-m "not release_seal"`) and run on demand:
#
#     backend/.venv/bin/python -m pytest -c backend/pytest.ini backend -m release_seal
#
# which is part of approving a release — see "Release runs" in
# backend/scripts/CALIBRATE_OPENING_SCORES.md.
#
# Everything else stays in the push gate, and the line is drawn at ATTACHING, not at the
# subject matter: the ordering, staging, containment and refusal cases all fake `hdiutil` out
# and cost milliseconds, and TestLaunchEndToEnd's real checkout and real child interpreter
# come in at ~9s for the class. What is expensive is building the boundary, not reasoning
# about it, so only the building is deferred.
_RELEASE_SEAL = pytest.mark.release_seal


def _disposable_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with a minimal manifest, for the checkout tests.

    Deliberately NOT the real repo: these tests need a dirty working tree and a mutated
    manifest file, and this repo is shared with other agents. Editing a tracked file and
    restoring the bytes we captured would silently discard anything they wrote in between,
    and would make their scorer tests fail while the edit was live.
    """
    repo = tmp_path / "origin"
    (repo / "backend/scripts").mkdir(parents=True)
    (repo / "backend/app").mkdir(parents=True)
    (repo / launcher.CALIBRATE_REL).write_text(
        f'SCORER_SOURCE_FILES = ("backend/app/fen.py", "{launcher.CALIBRATE_REL}")\n'
    )
    (repo / "backend/app/fen.py").write_text("COMMITTED = 1\n")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t",
                                                 "commit", "-qm", "seed"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo


class TestManifestReading:
    """The manifest is read from the checkout being vouched for, by parsing — a copy of the
    tuple in the launcher would be a second source of truth that silently stops covering a
    file the scorer added, and an import would compile the code we are here to precede."""

    def test_reads_the_scorers_own_manifest(self):
        assert launcher.read_manifest(_REPO_ROOT) == cal.SCORER_SOURCE_FILES

    def test_reads_the_tree_it_is_given_not_its_own_repo(self, tmp_path):
        # A launcher hashing the manifest of the tree it launched FROM, rather than the tree
        # it launches INTO, would bind a release run to the wrong file set.
        fake = tmp_path / "tree"
        (fake / "backend/scripts").mkdir(parents=True)
        (fake / launcher.CALIBRATE_REL).write_text(
            "SCORER_SOURCE_FILES: tuple[str, ...] = (\n"
            '    "backend/app/fen.py",\n'
            f'    "{launcher.CALIBRATE_REL}",\n'
            ")\n"
        )
        assert launcher.read_manifest(fake) == (
            "backend/app/fen.py", launcher.CALIBRATE_REL,
        )

    def test_env_var_name_matches_the_scorer(self):
        # The two constants are declared independently (the launcher must not import the
        # scorer), so a rename on either side has to fail here rather than in production, as
        # a run that silently stamps scorer_source_verified_preexec=False.
        assert launcher.SCORER_SOURCE_DIGEST_ENV == cal.SCORER_SOURCE_DIGEST_ENV

    @pytest.mark.parametrize("body, match", [
        ("X = 1\n", "no module-level SCORER_SOURCE_FILES"),
        ("SCORER_SOURCE_FILES = ()\n", "non-empty tuple"),
        ('SCORER_SOURCE_FILES = ("a.py",)\n', "must be part of the binding"),
        ("SCORER_SOURCE_FILES = tuple(discover())\n", "not a literal"),
    ])
    def test_refuses_a_manifest_it_cannot_trust(self, tmp_path, body, match):
        fake = tmp_path / "tree"
        (fake / "backend/scripts").mkdir(parents=True)
        (fake / launcher.CALIBRATE_REL).write_text(body)
        with pytest.raises(launcher.LauncherError, match=match):
            launcher.read_manifest(fake)


class TestDigest:
    def test_matches_the_scorers_own_construction(self):
        """THE constant that makes the handoff work. The child compares the inherited digest
        against its OWN scorer_source_digest() read and fails closed on any difference, so a
        launcher whose construction drifts by one byte does not produce a weaker guarantee —
        it produces a release path that can never run at all."""
        assert launcher.manifest_digest(_REPO_ROOT) == cal.scorer_source_digest()

    def test_moves_when_any_manifest_byte_moves(self, tmp_path):
        tree = tmp_path / "tree"
        (tree / "backend/scripts").mkdir(parents=True)
        (tree / "backend/app").mkdir(parents=True)
        (tree / launcher.CALIBRATE_REL).write_text(
            f'SCORER_SOURCE_FILES = ("backend/app/fen.py", "{launcher.CALIBRATE_REL}")\n'
        )
        target = tree / "backend/app/fen.py"
        target.write_text("A = 1\n")
        before = launcher.manifest_digest(tree)
        target.write_text("A = 2\n")  # same size — the case a timestamp .pyc would hide
        assert launcher.manifest_digest(tree) != before

    def test_refuses_a_manifest_file_symlinked_out_of_the_checkout(self, tmp_path):
        # The checkout is the whole defence against change-and-revert, so a manifest file
        # that lives outside it — via a committed symlink — is hashed and imported from
        # storage the checkout does not contain and cannot make read-only.
        outside = tmp_path / "outside.py"
        outside.write_text("A = 1\n")
        tree = tmp_path / "tree"
        (tree / "backend/scripts").mkdir(parents=True)
        (tree / "backend/app").mkdir(parents=True)
        (tree / launcher.CALIBRATE_REL).write_text(
            f'SCORER_SOURCE_FILES = ("backend/app/fen.py", "{launcher.CALIBRATE_REL}")\n'
        )
        (tree / "backend/app/fen.py").symlink_to(outside)
        with pytest.raises(launcher.LauncherError, match="outside the checkout"):
            launcher.manifest_digest(tree)

    def test_allows_a_symlink_that_stays_inside_the_checkout(self, tmp_path):
        # Those bytes ARE in the checkout, so the guarantee holds and the run may proceed.
        tree = tmp_path / "tree"
        (tree / "backend/scripts").mkdir(parents=True)
        (tree / "backend/app").mkdir(parents=True)
        (tree / launcher.CALIBRATE_REL).write_text(
            f'SCORER_SOURCE_FILES = ("backend/app/fen.py", "{launcher.CALIBRATE_REL}")\n'
        )
        (tree / "backend/app/real.py").write_text("A = 1\n")
        (tree / "backend/app/fen.py").symlink_to(tree / "backend/app/real.py")
        assert launcher.manifest_digest(tree)  # does not raise

    @pytest.mark.parametrize("rel", ["/etc/passwd", "backend/../../escape.py"])
    def test_refuses_a_manifest_path_that_is_not_repo_relative(self, tmp_path, rel):
        with pytest.raises(launcher.LauncherError, match="not a repo-relative path"):
            launcher.manifest_path(tmp_path, rel)

    def test_refuses_a_tree_missing_a_manifest_file(self, tmp_path):
        tree = tmp_path / "tree"
        (tree / "backend/scripts").mkdir(parents=True)
        (tree / launcher.CALIBRATE_REL).write_text(
            f'SCORER_SOURCE_FILES = ("backend/app/fen.py", "{launcher.CALIBRATE_REL}")\n'
        )
        with pytest.raises(launcher.LauncherError, match="unreadable"):
            launcher.manifest_digest(tree)


class TestChildEnv:
    """What the child is handed. Each of these is a precondition the child independently
    re-checks and fails closed on; the launcher's job is to satisfy them, not to be trusted."""

    def test_exports_the_digest_and_the_bytecode_policy(self, tmp_path):
        env = launcher.child_env(tmp_path / "tree", "d" * 64, tmp_path / "pyc")
        assert env[cal.SCORER_SOURCE_DIGEST_ENV] == "d" * 64
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert env["PYTHONPYCACHEPREFIX"] == str(tmp_path / "pyc")

    def test_replaces_an_inherited_pythonpath_rather_than_extending_it(self, tmp_path, monkeypatch):
        # An inherited PYTHONPATH naming the shared working tree would let the child import
        # app.* from bytes the digest never hashed, under a digest describing the worktree.
        # PYTHONPATH is no longer simply dropped (the child needs its deps under -S), so the
        # bug to guard is now an APPEND: the audited entries plus the inherited hazard.
        monkeypatch.setenv("PYTHONPATH", str(_REPO_ROOT / "backend"))
        entries = launcher.child_env(tmp_path, "d" * 64, tmp_path / "pyc")["PYTHONPATH"]
        assert entries.split(os.pathsep) == [str(p) for p in launcher._audited_dep_paths()]
        assert str(_REPO_ROOT / "backend") not in entries.split(os.pathsep)

    def test_hands_over_the_interpreters_own_dep_paths(self, tmp_path):
        # Under -S nothing adds site-packages, so the child cannot import chess/sqlalchemy
        # unless these are supplied. Wrong entries here do not weaken the fence, they break
        # the run outright — which is why the E2E child actually imports its deps.
        entries = launcher.child_env(tmp_path, "d" * 64, tmp_path / "pyc")["PYTHONPATH"]
        assert entries and all(Path(p).name == "site-packages" for p in entries.split(os.pathsep))

    @pytest.mark.parametrize("var", ["PYTHONHOME", "PYTHONWARNINGS", "PYTHONSTARTUP",
                                     "PYTHONBREAKPOINT", "PYTHONVERBOSE"])
    def test_drops_every_inherited_python_variable(self, tmp_path, monkeypatch, var):
        # An ALLOWLIST, not a denylist. PYTHONHOME relocates the stdlib and PYTHONWARNINGS
        # imports code (below), but the point is that guessing the dangerous ones is the bug:
        # the child gets only what child_env chose to give it.
        monkeypatch.setenv(var, "hostile")
        env = launcher.child_env(tmp_path, "d" * 64, tmp_path / "pyc")
        assert env.get(var) != "hostile"

    def test_keeps_non_python_configuration(self, tmp_path, monkeypatch):
        # The scrub is scoped to PYTHON*: DATABASE_URL and friends ARE the run's configuration,
        # and a child that lost them would fail for reasons unrelated to the fence.
        monkeypatch.setenv("DATABASE_URL", "sqlite:///x.db")
        assert launcher.child_env(tmp_path, "d" * 64, tmp_path / "pyc")["DATABASE_URL"] == \
            "sqlite:///x.db"

    def test_pythonwarnings_cannot_import_code_before_the_scorer(self, tmp_path, monkeypatch):
        """PYTHONWARNINGS is the one that makes the allowlist necessary rather than tidy.

        A filter names its category as `module.Class`, and the interpreter IMPORTS that module
        to install the filter — before the script body, under -S, through a variable that
        reads like a logging preference. Proven live first, then proven closed, so this cannot
        pass for some unrelated reason.
        """
        hostile = tmp_path / "deps"
        hostile.mkdir()
        marker = tmp_path / "imported-by-warnings"
        (hostile / "zz_warncat.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran')\n"
            "class Boom(Warning): pass\n"
        )
        # The variable must be in the LAUNCHER's own environment: the bug under test is that
        # child_env INHERITS it. Without this the old code has nothing to pass on and the test
        # passes for the wrong reason — it did exactly that on the first attempt.
        monkeypatch.setenv("PYTHONWARNINGS", "default::zz_warncat.Boom")
        monkeypatch.setenv("PYTHONPATH", str(hostile))

        # The hazard is real: the category module executes before our code, even under -S.
        base = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        subprocess.run([sys.executable, "-S", "-c", "pass"], env=base, timeout=60)
        assert marker.exists(), "PYTHONWARNINGS did not import the category — test proves nothing"

        marker.unlink()
        # child_env is the difference: PYTHONWARNINGS never reaches the child at all.
        env = launcher.child_env(tmp_path, "d" * 64, tmp_path / "pyc")
        env["PYTHONPATH"] = str(hostile)  # keep the module importable; only the filter is gone
        subprocess.run([sys.executable, "-S", "-c", "pass"], env=env, timeout=60)
        assert not marker.exists(), "an inherited PYTHONWARNINGS imported code in the child"

    def test_disables_user_site_packages(self, tmp_path):
        # Necessary but nowhere near sufficient, and the reason -S exists: this disables the
        # USER site dir only, while the live vector is the interpreter's own site-packages.
        assert launcher.child_env(tmp_path, "d" * 64, tmp_path / "pyc")["PYTHONNOUSERSITE"] == "1"


class TestLauncherIsolation:
    """The launcher's OWN interpreter. The child's -S is one interpreter too late by itself:
    whatever starts the launcher runs first, and a .pth there executes before this file
    imports hashlib — early enough to rebind sha256 in the process that mints the digest."""

    @pytest.mark.parametrize("isolated,no_site", [(0, 0), (1, 0), (0, 1)])
    def test_refuses_an_interpreter_not_started_with_both_flags(self, isolated, no_site):
        # -S alone leaves PYTHONPATH able to shadow the stdlib the launcher itself imports;
        # -I alone still runs every .pth. Neither half is sufficient on its own.
        flags = SimpleNamespace(isolated=isolated, no_site=no_site)
        with pytest.raises(launcher.LauncherError, match="-I -S"):
            launcher.require_isolated_launcher(flags)

    def test_accepts_an_isolated_no_site_interpreter(self):
        launcher.require_isolated_launcher(SimpleNamespace(isolated=1, no_site=1))

    def test_the_entrypoint_refuses_a_bare_interpreter(self):
        """The command that shipped in the docs until now. pytest itself runs with site
        enabled, so this asserts against exactly the state that was being trusted."""
        proc = subprocess.run(
            [sys.executable, str(_LAUNCHER_PATH), "--rev", "HEAD", "--", "--limit", "1"],
            capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 2
        assert "must run under `-I -S`" in proc.stderr
        assert "digest it produces would vouch for nothing" in proc.stderr

    def test_main_refuses_before_it_touches_the_repo(self, monkeypatch):
        # Order matters: a contaminated launcher must not check out, hash, or exec anything.
        # If the refusal came after, it would be reporting on work it had already done.
        def fail(*a, **k):
            raise AssertionError("the launcher acted before verifying its own interpreter")
        monkeypatch.setattr(launcher, "exclusive_checkout", fail)
        monkeypatch.setattr(launcher, "manifest_digest", fail)
        with pytest.raises(launcher.LauncherError, match="-I -S"):
            launcher.main(["--rev", "HEAD"])  # pytest's interpreter has site enabled


@pytest.mark.skipif(sys.prefix == sys.base_prefix, reason="not a venv: base prefix is correct")
class TestAuditedDepPaths:
    def test_dep_paths_come_from_the_venv_not_the_base_prefix(self):
        """THE BUG THIS PINS is invisible from inside pytest. Under -S, site.py never reads
        pyvenv.cfg, so sys.prefix becomes the BASE installation and a bare
        sysconfig.get_paths() hands back the base interpreter's site-packages — while the
        child needs the venv's. Under pytest (site on) sys.prefix is already the venv and the
        wrong code looks right, so this has to run in a real -I -S child.

        Getting this wrong is not a small mistake: at best the child cannot import chess and
        dies loudly; at worst the base install has a different version of a dependency and
        the release quietly scores against code nobody chose.
        """
        expected = sysconfig.get_paths()["purelib"]  # trustworthy here: pytest has site on
        code = (f"import sys; sys.path.insert(0, {str(_BACKEND_ROOT)!r});"
                "import scripts.release_calibration_launcher as L;"
                "print(L._audited_dep_paths()[0])")
        proc = subprocess.run([sys.executable, "-I", "-S", "-c", code],
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == expected

    def test_venv_root_is_not_resolved_through_the_interpreter_symlink(self):
        # venv/bin/python is a symlink to the base interpreter, so .resolve() lands in the
        # base installation and the venv vanishes — taking pyvenv.cfg, and every path derived
        # from it, with it.
        assert launcher._venv_root() == Path(sys.prefix)


class TestStartupHooks:
    """A .pth or sitecustomize runs before the scorer's first byte, and can rebind a function
    on a module imported from the CORRECT tree. Nothing downstream sees it: the source bytes
    are untouched (digest green) and __file__ is untouched (origins green)."""

    def test_child_runs_without_site_initialisation(self, tmp_path):
        assert "-S" in launcher.child_command(tmp_path, [])

    def test_startup_hooks_cannot_run_before_the_scorer(self, tmp_path):
        """The vector is proven live first, then proven closed — otherwise this test would
        pass just as happily against an interpreter that never ran the hook for some other
        reason. sitecustomize stands in for the .pth: site.py imports both, and it is the
        one a test can plant without writing into the real site-packages."""
        deps = tmp_path / "deps"
        deps.mkdir()
        marker = tmp_path / "hook-ran"
        (deps / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
        )
        env = {**os.environ, "PYTHONPATH": str(deps), "PYTHONDONTWRITEBYTECODE": "1"}

        # The hazard is real: with site initialisation, the hook executes uninvited.
        subprocess.run([sys.executable, "-c", "pass"], env=env, check=True, timeout=60)
        assert marker.exists(), "sitecustomize did not run — this test proves nothing as written"

        marker.unlink()
        # -S is the difference, and it is the flag child_command actually passes.
        subprocess.run([sys.executable, "-S", "-c", "pass"], env=env, check=True, timeout=60)
        assert not marker.exists(), "a startup hook ran under -S"


class TestExclusiveCheckout:
    def test_yields_a_fresh_tree_and_removes_it(self, tmp_path):
        repo = _disposable_repo(tmp_path)
        with launcher.exclusive_checkout(repo, "HEAD") as tree:
            assert tree.resolve() != repo.resolve()  # never the shared working tree
            assert (tree / launcher.CALIBRATE_REL).exists()
            assert (tree.parent.stat().st_mode & 0o777) == 0o700  # no OTHER user, at least
            registered = subprocess.run(
                ["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True,
            ).stdout
            assert str(tree) in registered
        assert not tree.exists()
        assert not tree.parent.exists()
        after = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True,
        ).stdout
        assert str(tree) not in after  # no dangling registration into a vanished temp path

    def test_the_hashed_files_are_read_only_during_the_run(self, tmp_path):
        # Defence against ACCIDENT, not against a determined same-uid process (which can
        # chmod it back) — see exclusive_checkout. It is what stops a stray write from
        # moving bytes between the hash and the import without anyone meaning to.
        repo = _disposable_repo(tmp_path)
        with launcher.exclusive_checkout(repo, "HEAD") as tree:
            for rel in launcher.read_manifest(tree):
                assert (tree / rel).stat().st_mode & 0o222 == 0

    def test_never_chmods_a_file_outside_the_checkout(self, tmp_path):
        """The manifest is DATA READ FROM THE CHECKOUT, not something we wrote. An entry
        naming a path outside the tree must be refused BEFORE any chmod: the victim would be
        one of the operator's own files, and the launcher would be the one touching it."""
        # An ABSOLUTE entry, which is the exploitable shape: `tree / "/abs/path"` is
        # `/abs/path` under pathlib's join semantics, so a raw join walks straight out of the
        # checkout and lands on the named file. (A `..` entry is rejected by the same guard,
        # but cannot be aimed at a fixture from a mkdtemp worktree — different temp roots.)
        outside = tmp_path / "private.key"
        outside.write_text("secret\n")
        outside.chmod(0o600)
        repo = _disposable_repo(tmp_path)
        (repo / launcher.CALIBRATE_REL).write_text(
            f'SCORER_SOURCE_FILES = ("{outside}", "{launcher.CALIBRATE_REL}")\n'
        )
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qam", "escape"], check=True, capture_output=True)
        with pytest.raises(launcher.LauncherError, match="not a repo-relative path"):
            with launcher.exclusive_checkout(repo, "HEAD"):
                pass
        assert outside.stat().st_mode & 0o777 == 0o600  # untouched, and NOT widened to 0644

    def test_restores_the_original_modes_not_a_hardcoded_default(self, tmp_path):
        # Resetting to 0644 on the way out would silently widen a file that arrived 0600.
        repo = _disposable_repo(tmp_path)
        with launcher.exclusive_checkout(repo, "HEAD") as tree:
            target = tree / "backend/app/fen.py"
            target.chmod(0o444)  # simulate a checkout whose file was already restrictive
        # The tree is gone by now, so prove it on a checkout we keep: modes captured at entry
        # are what get restored, and the read-only pass never invents new bits.
        with launcher.exclusive_checkout(repo, "HEAD") as tree:
            held = tree / "backend/app/fen.py"
            held.chmod(0o600)
            with launcher._manifest_read_only(tree):
                assert held.stat().st_mode & 0o777 == 0o400  # write bit dropped, 0600 kept
            assert held.stat().st_mode & 0o777 == 0o600  # exactly what it was, not 0644

    def test_refuses_a_manifest_entry_that_is_not_a_regular_file(self, tmp_path):
        repo = _disposable_repo(tmp_path)
        (repo / "backend/app/fen.py").unlink()
        (repo / "backend/app/fen.py").mkdir()
        (repo / "backend/app/fen.py" / "keep").write_text("x\n")
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qam", "dir"], check=True, capture_output=True)
        with pytest.raises(launcher.LauncherError, match="not a regular file"):
            with launcher.exclusive_checkout(repo, "HEAD"):
                pass

    def test_the_rest_of_the_checkout_stays_writable(self, tmp_path):
        # Only the hashed bytes are the guarantee. The run legitimately writes inside the
        # checkout — app.opening_graph caches its ~30s build there — and a blanket chmod
        # would buy nothing while making every release run rebuild and log a failure.
        repo = _disposable_repo(tmp_path)
        with launcher.exclusive_checkout(repo, "HEAD") as tree:
            (tree / "backend" / ".opening_graph_cache").mkdir()
            (tree / "backend" / ".opening_graph_cache" / "graph.pkl").write_bytes(b"x")

    def test_the_hashed_files_are_writable_again_for_cleanup(self, tmp_path):
        # Read-only files that git cannot remove would leak the worktree they were meant
        # to protect, so the hardening must not outlive the run.
        repo = _disposable_repo(tmp_path)
        with launcher.exclusive_checkout(repo, "HEAD") as tree:
            held = tree
        assert not held.parent.exists()

    def test_checks_out_the_committed_rev_not_the_working_tree(self, tmp_path):
        # A release run must never score uncommitted bytes.
        repo = _disposable_repo(tmp_path)
        (repo / "backend/app/fen.py").write_text("COMMITTED = 1\n# working-tree dirt\n")
        with launcher.exclusive_checkout(repo, "HEAD") as tree:
            assert (tree / "backend/app/fen.py").read_text() == "COMMITTED = 1\n"

    def test_removes_the_tree_when_the_run_raises(self, tmp_path):
        repo = _disposable_repo(tmp_path)
        with pytest.raises(RuntimeError):
            with launcher.exclusive_checkout(repo, "HEAD") as tree:
                held = tree
                raise RuntimeError("run blew up")
        assert not held.parent.exists()  # a leaked worktree is a tree someone else can write
        after = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True,
        ).stdout
        assert str(held) not in after  # the read-only tree did not block its own cleanup

    def test_refuses_an_unknown_rev(self, tmp_path):
        repo = _disposable_repo(tmp_path)
        with pytest.raises(launcher.LauncherError, match="git .* failed"):
            with launcher.exclusive_checkout(repo, "not-a-real-rev"):
                pass


def _registered_paths(repo: Path) -> set[Path]:
    """Every worktree git currently lists, RESOLVED.

    Asserting on the raw path would reproduce the bug these tests exist to catch: git prints
    real paths, tempfile hands back /var/folders/... on macOS, and a raw comparison finds
    nothing — so `held not in listed` would hold whether or not the entry survived.
    """
    listed = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    return {Path(line[len("worktree "):]).resolve()
            for line in listed.splitlines() if line.startswith("worktree ")}


class TestWorktreeRemovalFallback:
    """The removal FAILURE path. The temp dir vanishes either way, so a registration left
    behind points the origin repo at a path that no longer exists."""

    def test_registration_is_detected_through_a_symlinked_temp_path(self, tmp_path):
        """The check must survive git and tempfile spelling one directory two ways; on macOS
        they always do (/var -> /private/var). Unresolved, _worktree_registered() answers
        "not registered" for a live worktree, _registration_gone() upgrades that to "proven
        absent", and every verification built on it silently passes."""
        repo = _disposable_repo(tmp_path)
        with launcher.exclusive_checkout(repo, "HEAD") as tree:
            assert launcher._worktree_registered(repo, tree) is True
            assert launcher._registration_gone(repo, tree) is False
        assert launcher._worktree_registered(repo, tree) is False  # and gone once removed

    def test_prune_fallback_deletes_before_pruning(self, tmp_path, monkeypatch, capsys):
        # ORDER IS THE BUG THIS PINS. `git worktree prune` only reclaims entries whose
        # directory is already gone, so pruning while the tree is still on disk reports
        # success having removed nothing. The tree must be deleted first.
        repo = _disposable_repo(tmp_path)
        real_run = subprocess.run

        def fail_remove(cmd, **kwargs):
            if "remove" in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "simulated remove failure")
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(launcher.subprocess, "run", fail_remove)
        with launcher.exclusive_checkout(repo, "HEAD") as tree:
            held = tree
        monkeypatch.undo()
        assert _registered_paths(repo).isdisjoint({held.resolve()})  # the fallback reclaimed it
        assert "prune fallback cleaned it up" in capsys.readouterr().err

    def test_reports_a_registration_that_survives_the_fallback(self, tmp_path, monkeypatch,
                                                              capsys):
        # If even the prune cannot reclaim it, say so with the command to run by hand rather
        # than leaving the operator with a silently dangling entry.
        repo = _disposable_repo(tmp_path)
        monkeypatch.setattr(launcher, "_worktree_registered", lambda *_: True)
        monkeypatch.setattr(launcher.subprocess, "run", lambda cmd, **kw:
                            subprocess.CompletedProcess(cmd, 1, "", "nope"))
        launcher._remove_worktree(repo, tmp_path / "ghost")
        err = capsys.readouterr().err
        assert "NOT VERIFIED GONE" in err
        assert "worktree prune --expire now" in err

    def test_a_failed_listing_is_not_read_as_a_clean_removal(self, tmp_path, monkeypatch,
                                                             capsys):
        """A broken `git worktree list` prints nothing, and an empty listing tested for a
        substring says "not registered". Absence of evidence must not become evidence of
        absence on the one path whose entire job is proving absence."""
        repo = _disposable_repo(tmp_path)
        monkeypatch.setattr(launcher.subprocess, "run", lambda cmd, **kw:
                            subprocess.CompletedProcess(cmd, 128, "", "fatal: not a git repo"))
        assert launcher._worktree_registered(repo, tmp_path / "ghost") is None
        assert launcher._registration_gone(repo, tmp_path / "ghost") is False

        launcher._remove_worktree(repo, tmp_path / "ghost")
        assert "NOT VERIFIED GONE" in capsys.readouterr().err

    def test_a_successful_remove_is_verified_not_assumed(self, tmp_path, monkeypatch, capsys):
        """`git worktree remove` returning 0 is not proof on its own — the registration is
        checked, and a bare exit code with an unverifiable listing must not shortcut it."""
        repo = _disposable_repo(tmp_path)
        real_run = subprocess.run

        def lie_about_removal(cmd, **kwargs):
            if "remove" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "", "")  # claims success, does nothing
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(launcher.subprocess, "run", lie_about_removal)
        with launcher.exclusive_checkout(repo, "HEAD") as tree:
            held = tree
        monkeypatch.undo()
        assert _registered_paths(repo).isdisjoint({held.resolve()})  # fallback ran despite exit 0
        assert "could not confirm" in capsys.readouterr().err


# Builds the artifact and selection inputs entirely from the CHECKOUT: the worktree's own
# test module and scorer, imported with cwd inside the worktree. Nothing from the origin
# tree is on the path, so what this reports is what a real run under the launcher reports.
_PROBE = """
import json, sys
from pathlib import Path
import test_calibrate_opening_scores as t
import scripts.calibrate_opening_scores_v2 as cal
graph, roots, ap, pp, _as_of, _prov = t._bsi_artifact(Path(sys.argv[1]))
try:
    si = cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)
    out = {"preexec": si.cohort.scorer_source_verified_preexec}
except cal.ScorerSourceUnstableError as e:
    out = {"error": type(e).__name__}
Path(sys.argv[2]).write_text(json.dumps(out))
"""


def _probe_command(scratch: Path):
    """Stands in for the scorer's own argv. The probe reports the flag the run would stamp,
    which the release CLI (g-p4ih-release-cli) does not yet exist to surface.

    Carries `-S` because child_command does: without it this seam would quietly run the child
    under a different interpreter configuration than a release does, and the E2E cases would
    stop covering the startup-hook fence — including whether the deps child_env supplies are
    enough to import chess and sqlalchemy with site.py switched off.
    """
    def build(tree_root: Path, script_args):
        return [sys.executable, "-S", "-c", _PROBE, str(scratch), str(scratch / "result.json")]
    return build


def _run_probe(monkeypatch, tree: Path, scratch: Path, pycache: Path, command=None) -> dict:
    monkeypatch.setattr(launcher, "child_command", command or _probe_command(scratch))
    assert launcher.launch(tree, [], pycache_dir=pycache) == 0
    return json.loads((scratch / "result.json").read_text())


# HEAD, overlaid with exactly this task's inputs: the bytes the digest binds, plus the module
# the probe imports to build its artifact. Nothing else — see worktree_rev.
_E2E_OVERLAY = (
    *cal.SCORER_SOURCE_FILES,
    # The launcher itself, because g-release-os-boundary made it EXECUTE FROM THE CHECKOUT:
    # the outer process re-execs release_calibration_launcher.py --inner off the sealed
    # volume. It used to be correct to leave this out — "the launcher runs from the ORIGIN
    # and only the checkout is exec'd" — and that sentence stopped being true the moment
    # there were two launcher processes.
    "backend/scripts/release_calibration_launcher.py",
    "backend/test_calibrate_opening_scores.py",
    # ...and what THAT module imports. The overlay is a closure, not a list of files anyone
    # edited: a checkout carrying the working-tree importer beside HEAD's imported module
    # fails in the CHILD, where the traceback is a subprocess's and says nothing about why.
    "backend/test_calibrate_selection.py",
    # Data files analysis_profiles.py reads at import; their ``dominates`` sets must
    # match evidence_policy.EDGES (g-reuse-d21-search added browser-analysis-multipv-v2).
    "backend/app/canonical_profiles/canonical-sf18-depth24-v1.json",
    "backend/app/canonical_profiles/canonical-sf18-depth24-linux-v1.json",
)

# Overlaid ABSENCES, kept apart from the bytes above because a deletion is a different fact and
# folding it into that list made a test that reads every overlaid path try to read a file whose
# whole point is that it is gone.
#
# Empty since 284d4b5, and the mechanism is kept rather than deleted because the entry it held
# is the shape of the next one: HEAD tracked `.antigravitycli/<uuid>.json` as a SYMLINK into a
# home directory outside the repository (swept in by a `git add -A` in af02eac), so a sealed
# checkout of HEAD carried a name whose bytes were off the volume, which the containment check
# refuses for every run. That removal is now COMMITTED, and a path git no longer has in the
# index is not an absence to overlay — `git rm --cached` exits 128 on it and takes the whole
# fixture, and every test that depends on it, down with it.
_E2E_OVERLAY_REMOVED: tuple[str, ...] = ()


def _overlay_commit(repo_root: Path, rel_paths: Sequence[str], index_path: Path, *,
                    removed: Sequence[str] = ()) -> str:
    """A commit of HEAD with ``rel_paths`` replaced by their current working-tree bytes, and
    ``removed`` dropped.

    ``removed`` is stated rather than inferred: ``git add`` on a path missing from the working
    tree would stage the deletion too, but only as a side effect of it happening to be deleted
    at the time, which is not a thing to build a fixture on. ``git rm --cached`` says it, and
    says it in the throwaway index only.

    GIT_INDEX_FILE is what makes this safe to run against a tree other agents are working in:
    the staging happens in a throwaway index, so nothing here locks or mutates the shared one,
    and no file is touched. The resulting objects are unreferenced — garbage to a later gc,
    which is exactly right for a fixture.
    """
    env = {
        **os.environ,
        "GIT_INDEX_FILE": str(index_path),
        "GIT_AUTHOR_NAME": "ghostreplay-tests", "GIT_AUTHOR_EMAIL": "tests@ghostreplay.invalid",
        "GIT_COMMITTER_NAME": "ghostreplay-tests",
        "GIT_COMMITTER_EMAIL": "tests@ghostreplay.invalid",
    }

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo_root), *args], env=env,
                              capture_output=True, text=True, check=True).stdout.strip()

    git("read-tree", "HEAD")
    git("add", "--", *rel_paths)
    if removed:
        git("rm", "--cached", "--quiet", "--", *removed)
    tree = git("write-tree")
    return git("commit-tree", tree, "-p", "HEAD", "-m", "e2e: this task's inputs over HEAD")


class TestOverlayCommit:
    """The fixture's mechanism, proven on a DISPOSABLE repo.

    It has to be tested here rather than against the real tree: the interesting half is that
    an unrelated dirty file is excluded, and the real tree is only dirty in that way when
    another agent happens to be mid-edit. A test that depends on someone else's timing proves
    nothing on the runs where it passes.
    """

    def test_overlays_working_bytes_and_excludes_everything_else(self, tmp_path):
        repo = _disposable_repo(tmp_path)
        mine = "backend/app/fen.py"          # in the overlay: this task's input
        theirs = "backend/app/other.py"      # tracked, dirty, and none of our business
        (repo / theirs).write_text("COMMITTED = 1\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "seed other"], check=True, capture_output=True)

        (repo / mine).write_text("WORKING = 2\n")            # my in-progress edit
        (repo / theirs).write_text("THEIR_WIP = broken(\n")  # a concurrent agent's, mid-edit

        sha = _overlay_commit(repo, [mine], tmp_path / "idx")
        show = lambda p: subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{p}"],
                                        capture_output=True, text=True, check=True).stdout
        assert show(mine) == "WORKING = 2\n"      # my edit is what gets tested
        assert show(theirs) == "COMMITTED = 1\n"  # theirs cannot break or join my run

    def test_a_removed_path_is_dropped_from_the_rev_and_left_in_the_working_tree(self, tmp_path):
        """The overlay carries absences as well as bytes, and a removal is confined to the
        throwaway index: a dropped file is still in the working tree afterwards, because
        deleting it there is the task's commit to make, not the fixture's."""
        repo = _disposable_repo(tmp_path)
        doomed = "backend/app/other.py"
        (repo / doomed).write_text("COMMITTED = 1\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "seed other"], check=True, capture_output=True)
        sha = _overlay_commit(repo, ["backend/app/fen.py"], tmp_path / "idx", removed=[doomed])
        listed = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", sha],
                                capture_output=True, text=True, check=True).stdout.split()
        assert doomed not in listed
        assert "backend/app/fen.py" in listed
        assert (repo / doomed).exists()

    def test_leaves_the_shared_index_and_working_tree_untouched(self, tmp_path):
        # The reason for GIT_INDEX_FILE. Staging into the real index would fight other agents
        # for the lock and stage their work as a side effect of running our tests.
        repo = _disposable_repo(tmp_path)
        (repo / "backend/app/fen.py").write_text("WORKING = 2\n")
        before = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                capture_output=True, text=True, check=True).stdout
        _overlay_commit(repo, ["backend/app/fen.py"], tmp_path / "idx")
        after = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                               capture_output=True, text=True, check=True).stdout
        assert after == before


@pytest.fixture(scope="module")
def worktree_rev(tmp_path_factory) -> str:
    """HEAD with THIS TASK'S files overlaid — not HEAD, and not the whole working tree.

    The rev decides which scorer is under test, and both obvious answers are wrong. HEAD is
    wrong because this repo reviews before commit: at the moment the fence is worth testing it
    is precisely not committed, so a checkout of HEAD validates the previous scorer and stays
    green for code nobody changed. The whole working tree is wrong for the opposite reason —
    it is SHARED. Other agents' tracked edits land in it continuously (files unrelated to this
    task changed while the review that caught this was being written), so a working-tree
    snapshot makes the fixture validate a combined state, or fail for reasons belonging to
    somebody else's task. Neither is a property of the code under review.

    So: seed a TEMPORARY index from HEAD, stage only _E2E_OVERLAY into it, and drop whatever
    _E2E_OVERLAY_REMOVED names — an absence belongs in the overlay for the same reason a byte
    does, when the task's deletion is not committed yet: a path HEAD still tracks but this
    work removes would otherwise be sealed and fail the tests on something nobody is
    reviewing. That list is EMPTY today, because the deletion it held is committed; see the
    comment on it for what it caught and why the mechanism stays. Everything else in the
    checkout is HEAD's. The temp index matters as much as the selection — GIT_INDEX_FILE
    keeps this out of the shared index entirely, so nothing here locks or mutates state another
    agent is using, and `git stash create` is not an option for the same reason it looked
    attractive: it snapshots everyone's work, not ours.

    The launcher is IN the overlay since g-release-os-boundary. It used to be deliberately
    absent, on the reasoning that it runs from the ORIGIN and only the checkout is exec'd —
    true until the sealed run gained an INNER launcher that executes from the volume. Left
    out, every sealed test here would validate HEAD's launcher against the code under review,
    and the mismatch surfaces as a refusal from a handshake check rather than as a diff.
    """
    return _overlay_commit(_REPO_ROOT, _E2E_OVERLAY,
                           tmp_path_factory.mktemp("git-index") / "index",
                           removed=_E2E_OVERLAY_REMOVED)


class TestLaunchEndToEnd:
    """The acceptance cases, end to end: a real worktree, a real pre-exec hash, a real child
    interpreter that builds real selection inputs."""

    def test_the_checkout_carries_every_overlaid_input(self, worktree_rev):
        """Pins the fixture's whole reason for existing, across EVERY path it overlays — not
        just the scorer. The digest binds all of SCORER_SOURCE_FILES, so a fixture that
        refreshed the scorer while leaving a manifest module at its committed bytes would
        test a tree that exists nowhere, and would do it silently.

        If this ever reads HEAD, every test below starts validating the last commit instead of
        the code under review — passing the entire time, which is what makes it worth a test.
        """
        with launcher.exclusive_checkout(_REPO_ROOT, worktree_rev) as tree:
            checked_out = {rel: (tree / rel).read_bytes() for rel in _E2E_OVERLAY}
            still_there = [rel for rel in _E2E_OVERLAY_REMOVED if (tree / rel).is_symlink()
                           or (tree / rel).exists()]
        stale = [rel for rel in _E2E_OVERLAY
                 if checked_out[rel] != (_REPO_ROOT / rel).read_bytes()]
        assert not stale, f"checkout carries committed, not working-tree, bytes for: {stale}"
        # The overlaid ABSENCES, which the sealed run refuses the checkout over. Vacuous while
        # _E2E_OVERLAY_REMOVED is empty, and kept for when it is not: it is the half that
        # catches an uncommitted deletion silently failing to reach the checkout.
        assert not still_there, f"checkout still carries paths this task removes: {still_there}"

    def test_the_checkout_is_head_outside_the_overlay(self, worktree_rev):
        """The other half of the fixture's contract, against the real repo. Note this can only
        FAIL when an unrelated tracked file is dirty — the discriminating version lives in
        TestOverlayCommit, which manufactures that condition instead of waiting for it."""
        # opening_densify.py is deliberately OUTSIDE the scorer manifest (its own test in
        # test_opening_densify.py pins that), so it is a stable outsider here.
        outsider = "backend/app/opening_densify.py"
        assert outsider not in _E2E_OVERLAY
        committed = subprocess.run(["git", "-C", str(_REPO_ROOT), "show", f"HEAD:{outsider}"],
                                   capture_output=True, check=True).stdout
        with launcher.exclusive_checkout(_REPO_ROOT, worktree_rev) as tree:
            assert (tree / outsider).read_bytes() == committed

    def test_the_pre_exec_digest_alone_no_longer_stamps_verified(self, tmp_path, monkeypatch,
                                                                 worktree_rev):
        """THE SEMANTIC CHANGE g-release-os-boundary made, pinned from the outside.

        This exact run — real worktree, real pre-exec hash, real child — used to stamp True,
        and that was the whole release path. It stamps False now, because the digest closes
        the compile window but never held the bytes still, and it says nothing at all about
        the interpreter or the installed dependencies that execute them. A `pip install` into
        the shared venv moves what runs without moving the digest.

        If this ever reverts to True, the boundary stopped being a precondition of the flag
        and every downstream refusal that depends on it quietly stopped working.
        """
        with launcher.exclusive_checkout(_REPO_ROOT, worktree_rev) as tree:
            assert _run_probe(monkeypatch, tree, tmp_path, tmp_path / "pyc") == {"preexec": False}

    def test_an_edit_between_the_hash_and_the_exec_fails_closed(self, tmp_path, monkeypatch,
                                                                worktree_rev):
        """THE case no in-process check can reach. The launcher hashes; the tree is then
        mutated; only then does the interpreter start and compile it. Every in-process read
        the child takes — import snapshot, open fence, close fence — agrees on the NEW bytes,
        so the run is invisible from the inside. It fails only because the inherited digest
        predates the compile and disagrees.

        The edit is planted in the child_command seam on purpose: the launcher calls it after
        hashing and before spawning, so this lands in exactly the window under test. A
        launcher that hashed later — after building the command — would stamp preexec=True
        here and this test would catch it.
        """
        with launcher.exclusive_checkout(_REPO_ROOT, worktree_rev) as tree:
            probe = _probe_command(tmp_path)

            def edit_then_command(tree_root, script_args):
                target = tree_root / "backend/app/fen.py"
                # chmod first: the read-only checkout blocks a stray write, but not a process
                # running as this user that means it — exactly the residual exclusive_checkout
                # documents. So this models the case still left open, which is precisely why
                # the digest handoff has to catch it independently.
                target.chmod(0o644)
                target.write_bytes(target.read_bytes() + b"\n# landed after the hash\n")
                return probe(tree_root, script_args)

            out = _run_probe(monkeypatch, tree, tmp_path, tmp_path / "pyc",
                             command=edit_then_command)
        assert out == {"error": "ScorerSourceUnstableError"}

    def test_a_bare_run_stamps_unverified(self, tmp_path, worktree_rev):
        """The counterpart: same tree, same code, no launcher. The flag is not a property of
        the tree — it is a property of how the run was started."""
        with launcher.exclusive_checkout(_REPO_ROOT, worktree_rev) as tree:
            env = {k: v for k, v in os.environ.items()
                   if k not in ("PYTHONPATH", cal.SCORER_SOURCE_DIGEST_ENV)}
            env["PYTHONPYCACHEPREFIX"] = str(tmp_path / "pyc")
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            result = tmp_path / "result.json"
            proc = subprocess.run(
                [sys.executable, "-c", _PROBE, str(tmp_path), str(result)],
                cwd=str(tree / "backend"), env=env,
                capture_output=True, text=True, timeout=300,
            )
            assert proc.returncode == 0, proc.stderr
        assert json.loads(result.read_text()) == {"preexec": False}


class TestLaunchPreconditions:
    def test_refuses_a_populated_bytecode_cache(self, tmp_path):
        # The child refuses this too (StaleBytecodeError), but the launcher owns the cache it
        # hands over: catching it here fails the run before a worktree is spent on it.
        pycache = tmp_path / "pyc"
        (pycache / "sub").mkdir(parents=True)
        (pycache / "sub" / "stale.pyc").write_bytes(b"\x00")
        with pytest.raises(launcher.LauncherError, match="not empty"):
            launcher.launch(_REPO_ROOT, [], pycache_dir=pycache)


class TestArgParsing:
    def test_forwards_script_args_after_the_separator(self):
        args = launcher._parse_args(["--rev", "abc123", "--", "select-release", "--artifact", "/abs/a"])
        assert args.rev == "abc123"
        assert args.script_args == ["select-release", "--artifact", "/abs/a"]

    def test_defaults_to_head(self):
        assert launcher._parse_args([]).rev == "HEAD"


# ---------------------------------------------------------------------------
# g-p4ih-release-cli: the candidate-provenance MOUNT.
# ---------------------------------------------------------------------------


def _repo_with_provenance(tmp_path: Path, committed: bytes, working: bytes | None) -> Path:
    """A disposable repo whose cohort_provenance.json is COMMITTED as ``committed`` and then
    overwritten in the working tree with ``working`` — the shape at approval time, where the
    candidate record exists only as an uncommitted diff."""
    repo = _disposable_repo(tmp_path)
    record = repo / launcher.COHORT_PROVENANCE_REL
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_bytes(committed)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "record"], check=True, capture_output=True)
    if working is not None:
        record.write_bytes(working)
    return repo


class TestCohortProvenanceMount:
    def test_the_env_constant_mirrors_the_scorers(self):
        # The launcher must never import the scorer, so the constant is duplicated — and
        # pinned equal here, exactly like SCORER_SOURCE_DIGEST_ENV.
        assert launcher.COHORT_PROVENANCE_DIGEST_ENV == cal.COHORT_PROVENANCE_DIGEST_ENV
        assert launcher.COHORT_PROVENANCE_REL == str(
            cal.COHORT_PROVENANCE_PATH.relative_to(_REPO_ROOT)
        )

    def test_the_record_is_not_in_the_scorer_manifest(self):
        # If it were, every capture would invalidate the scorer digest — and the mount would
        # perturb manifest_digest(tree), coupling two orderings that are deliberately free.
        assert launcher.COHORT_PROVENANCE_REL not in cal.SCORER_SOURCE_FILES

    def test_mounts_the_working_tree_bytes_not_the_committed_ones(self, tmp_path):
        repo = _repo_with_provenance(tmp_path, b'{"committed": true}', b'{"candidate": true}')
        with launcher.exclusive_checkout(repo, "HEAD") as tree:
            # The checkout carries the COMMITTED record...
            assert (tree / launcher.COHORT_PROVENANCE_REL).read_bytes() == b'{"committed": true}'
            digest = launcher.mount_cohort_provenance(repo, tree)
            # ...and after the mount, the origin WORKING TREE bytes, under their own digest.
            assert (tree / launcher.COHORT_PROVENANCE_REL).read_bytes() == b'{"candidate": true}'
            assert digest == hashlib.sha256(b'{"candidate": true}').hexdigest()

    def test_the_destination_goes_through_manifest_path(self, tmp_path):
        repo = _repo_with_provenance(tmp_path, b"{}", b'{"candidate": true}')
        with launcher.exclusive_checkout(repo, "HEAD") as tree:
            launcher.mount_cohort_provenance(repo, tree)
            destination = launcher.manifest_path(tree, launcher.COHORT_PROVENANCE_REL)
            assert destination.exists()
            assert tree.resolve() in destination.parents

    def test_the_mount_does_not_move_the_scorer_digest(self, tmp_path):
        repo = _repo_with_provenance(tmp_path, b"{}", b'{"candidate": true}')
        with launcher.exclusive_checkout(repo, "HEAD") as tree:
            before = launcher.manifest_digest(tree)
            launcher.mount_cohort_provenance(repo, tree)
            assert launcher.manifest_digest(tree) == before

    def test_a_missing_origin_record_refuses(self, tmp_path):
        repo = _disposable_repo(tmp_path)  # no cohort_provenance.json at all
        with launcher.exclusive_checkout(repo, "HEAD") as tree:
            with pytest.raises(launcher.LauncherError) as exc:
                launcher.mount_cohort_provenance(repo, tree)
        assert launcher.COHORT_PROVENANCE_REL in str(exc.value)

    def test_the_child_env_carries_the_digest_only_when_mounting(self, tmp_path):
        plain = launcher.child_env(tmp_path, "d" * 64, tmp_path / "pyc")
        assert launcher.COHORT_PROVENANCE_DIGEST_ENV not in plain
        mounted = launcher.child_env(tmp_path, "d" * 64, tmp_path / "pyc",
                                     provenance_digest="e" * 64)
        assert mounted[launcher.COHORT_PROVENANCE_DIGEST_ENV] == "e" * 64

    def test_an_inherited_digest_never_survives_into_an_unmounted_run(self, tmp_path, monkeypatch):
        # The PYTHON* strip does not cover a GHOSTREPLAY_* variable, so a stale inherited
        # value would otherwise hand the child a digest no launcher computed.
        monkeypatch.setenv(launcher.COHORT_PROVENANCE_DIGEST_ENV, "f" * 64)
        env = launcher.child_env(tmp_path, "d" * 64, tmp_path / "pyc")
        assert launcher.COHORT_PROVENANCE_DIGEST_ENV not in env

    def test_the_flag_is_off_by_default_and_parses(self):
        assert launcher._parse_args([]).mount_cohort_provenance is False
        args = launcher._parse_args([
            "--mount-cohort-provenance", "--",
            "select-release", "--artifact", "/abs/a.json", "--result-output", "/abs/r.json",
        ])
        assert args.mount_cohort_provenance is True
        # Forwarded VERBATIM through --: no per-option forwarding code exists or is needed.
        assert args.script_args == [
            "select-release", "--artifact", "/abs/a.json", "--result-output", "/abs/r.json",
        ]
        assert launcher.child_command(Path("/tree"), args.script_args)[-4:] == [
            "--artifact", "/abs/a.json", "--result-output", "/abs/r.json",
        ]

    def test_main_mounts_only_when_asked(self, tmp_path, monkeypatch):
        """The mount happens AFTER exclusive_checkout yields and BEFORE launch — so the
        record's bytes are hashed while no child interpreter exists.

        Pinned on the --no-boundary path, which is where exclusive_checkout still lives after
        g-release-os-boundary. The sealed path has the same ordering with a harder deadline
        (the record must be staged before the image is built, or it would not be sealed at
        all) and is pinned separately in TestSealedCheckoutOrdering.
        """
        order = []
        seen = {}

        @contextmanager
        def fake_checkout(repo_root, rev):
            order.append("checkout")
            yield tmp_path / "tree"

        def fake_mount(repo_root, tree):
            order.append("mount")
            return "a" * 64

        def fake_launch(tree, script_args, *, pycache_dir, provenance_digest=None):
            order.append("launch")
            seen["digest"] = provenance_digest
            return 0

        monkeypatch.setattr(launcher, "require_isolated_launcher", lambda *a, **k: None)
        monkeypatch.setattr(launcher, "exclusive_checkout", fake_checkout)
        monkeypatch.setattr(launcher, "mount_cohort_provenance", fake_mount)
        monkeypatch.setattr(launcher, "launch", fake_launch)

        assert launcher.main(["--no-boundary", "--", "report"]) == 0
        assert order == ["checkout", "launch"]        # OFF is the correct default
        assert seen["digest"] is None

        order.clear()
        assert launcher.main(
            ["--no-boundary", "--mount-cohort-provenance", "--", "select-release"]) == 0
        assert order == ["checkout", "mount", "launch"]
        assert seen["digest"] == "a" * 64

    def test_a_linked_worktree_launch_refuses_to_mount(self, tmp_path):
        """MAIN-DIRTY vs LINKED-COMMITTED. repo_root is wherever the launcher was started
        from, so a linked-worktree launch would mount that worktree's COMMITTED record — a
        stale record which, paired with its own artifact, passes every downstream gate."""
        main = _repo_with_provenance(tmp_path, b'{"stale": true}', b'{"candidate": true}')
        linked = tmp_path / "linked"
        subprocess.run(
            ["git", "-C", str(main), "worktree", "add", "-q", "--detach", str(linked)],
            check=True, capture_output=True,
        )
        # The linked worktree carries the STALE bytes, and nothing there says so.
        assert (linked / launcher.COHORT_PROVENANCE_REL).read_bytes() == b'{"stale": true}'
        with launcher.exclusive_checkout(main, "HEAD") as tree:
            with pytest.raises(launcher.LauncherError) as exc:
                launcher.mount_cohort_provenance(linked, tree)
            assert "LINKED" in str(exc.value)
            # And the main checkout still mounts its uncommitted candidate.
            assert launcher.mount_cohort_provenance(main, tree) == hashlib.sha256(
                b'{"candidate": true}'
            ).hexdigest()

    def test_the_main_worktree_check_fails_closed_when_git_will_not_answer(self, tmp_path, monkeypatch):
        repo = _repo_with_provenance(tmp_path, b"{}", b'{"candidate": true}')
        monkeypatch.setattr(launcher, "_git", lambda *a: "only-one-line")
        with pytest.raises(launcher.LauncherError) as exc:
            launcher._require_main_worktree(repo)
        assert "could not determine" in str(exc.value)

    def test_a_newline_bearing_worktree_path_is_still_seen_as_registered(self, tmp_path):
        # `git worktree list --porcelain` prints the path raw; splitlines() would cut this
        # one in half and report the registration as absent.
        repo = _disposable_repo(tmp_path)
        odd = tmp_path / "lin\nefeed"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(odd)],
            check=True, capture_output=True,
        )
        assert launcher._worktree_registered(repo, odd) is True
        assert launcher._registration_gone(repo, odd) is False


# ---------------------------------------------------------------------------
# The OS boundary (g-release-os-boundary)
#
# The bead asked for an OS-enforced boundary such that a same-uid process cannot write the
# hashed bytes for the duration of the run, and for a test that proves the denial comes from
# the OS rather than from mode bits. That is TestReadOnlyVolume, and it is deliberately built
# on a small synthetic image: the property under test belongs to the read-only MOUNT, not to
# the release checkout, and asserting it on a 900MB volume would cost 30s to learn the same
# thing.
#
# TestSealedCheckout pays that cost exactly once, module-scoped, for the acceptance case.
# ---------------------------------------------------------------------------

_MACOS_ONLY = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="hdiutil is macOS-only; _boundary_mechanism() returns None elsewhere and the "
           "launcher refuses a release run without --no-boundary",
)


@contextmanager
def _read_only_volume(tmp_path: Path, files: dict[str, str]):
    """A small read-only volume, built the way the launcher builds its own.

    Same mechanism, same flags, same unlink-after-attach — so what this proves about writes
    is what holds for a release run, without staging 900MB to learn it.
    """
    stage = tmp_path / "vol-stage"
    mount = tmp_path / "vol-mnt"
    image = tmp_path / "vol.dmg"
    mount.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = stage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    subprocess.run(["hdiutil", "create", "-srcfolder", str(stage), "-format", "UDRO",
                    "-volname", "boundary-test", "-quiet", str(image)], check=True)
    subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-owners", "off",
                    "-mountpoint", str(mount), str(image)], check=True, capture_output=True)
    image.unlink()
    try:
        yield mount
    finally:
        subprocess.run(["hdiutil", "detach", "-force", str(mount)], capture_output=True)


class TestBoundaryConstants:
    """The launcher and the scorer each keep their own copy of these, on purpose — the
    launcher must not import the scorer, because importing it would compile the very code the
    pre-exec digest exists to precede. Copies that can drift need a test that they have not."""

    def test_the_env_var_names_mirror_the_scorers(self):
        assert launcher.RELEASE_BOUNDARY_ENV == cal.RELEASE_BOUNDARY_ENV
        assert launcher.RUNTIME_IMAGE_DIGEST_ENV == cal.RUNTIME_IMAGE_DIGEST_ENV
        assert launcher.SEALED_REVISION_ENV == cal.SEALED_REVISION_ENV
        # Drift here is not a missing value but a governance rule quietly evaluated against
        # nothing: the child requires this variable under the boundary, so a launcher writing
        # a different name refuses every sealed run.
        assert launcher.SEALED_FORBIDDEN_ROOTS_ENV == cal.SEALED_FORBIDDEN_ROOTS_ENV

    def test_the_sealed_system_prefixes_mirror_the_scorers(self):
        # Drift here is silent and one-directional: a prefix the scorer accepts but the
        # launcher does not stage is a dylib loaded from outside the boundary.
        assert launcher.SEALED_SYSTEM_PREFIXES == cal.SEALED_SYSTEM_PREFIXES

    def test_the_graph_cache_switch_mirrors_the_module_that_reads_it(self):
        assert launcher.GRAPH_NO_DISK_CACHE_ENV == opening_graph.DISABLE_DISK_CACHE_ENV

    def test_a_mechanism_exists_exactly_where_one_is_implemented(self):
        expected = launcher.MACOS_MECHANISM if sys.platform == "darwin" else None
        assert launcher._boundary_mechanism() == expected


@_RELEASE_SEAL
@_MACOS_ONLY
class TestReadOnlyVolume:
    """CRITERION 4: the denial is the OS's, not a mode bit's."""

    def test_a_same_uid_write_is_denied_by_the_os(self, tmp_path):
        with _read_only_volume(tmp_path, {"backend/app/fen.py": "SEALED = 1\n"}) as mount:
            with pytest.raises(OSError) as exc:
                (mount / "backend/app/fen.py").write_text("TAMPERED = 1\n")
            assert exc.value.errno == errno.EROFS

    def test_chmod_does_not_buy_the_owner_a_write(self, tmp_path):
        """The precise failure of the pre-boundary design. exclusive_checkout marked the
        hashed files 0444, and its own docstring said why that was not a boundary: the owner
        reverts it. Here the chmod is allowed to APPEAR to succeed — and the write still
        fails, because the kernel is refusing on behalf of the filesystem, not the inode."""
        with _read_only_volume(tmp_path, {"f.py": "SEALED = 1\n"}) as mount:
            target = mount / "f.py"
            with contextlib.suppress(OSError):
                target.chmod(0o666)
            with pytest.raises(OSError) as exc:
                target.write_text("TAMPERED = 1\n")
            assert exc.value.errno == errno.EROFS
            assert target.read_text() == "SEALED = 1\n"

    def test_the_volume_outlives_its_backing_file(self, tmp_path):
        """Why the launcher unlinks the image. An attached .dmg stays writable by this uid —
        a second, WRITABLE path to every sealed byte — so it is removed as soon as the mount
        exists. This pins that doing so does not cost us the mount."""
        with _read_only_volume(tmp_path, {"f.py": "SEALED = 1\n"}) as mount:
            assert not (tmp_path / "vol.dmg").exists()
            assert (mount / "f.py").read_text() == "SEALED = 1\n"

    def test_the_scorer_can_measure_the_boundary_from_inside(self, tmp_path):
        """The property that let the release gate be a measurement instead of an attestation.
        The bead assumed the scorer could not detect its own sandbox — true of a sandbox
        profile, false of a read-only mount."""
        with _read_only_volume(tmp_path, {"f.py": "SEALED = 1\n"}) as mount:
            assert cal._mount_is_read_only(mount / "f.py") is True
            assert cal._mount_is_read_only(tmp_path) is False
            assert cal._mount_is_read_only(mount / "does-not-exist") is False


@_MACOS_ONLY
class TestCrossVolumeInputs:
    """Read-only is a PROPERTY. The sealed volume is an IDENTITY, and only the second one is
    what runtime_image_sha256 covers.

    ST_RDONLY is satisfied by any attached read-only image — a second DMG, a mounted
    installer, a network share. Bytes there are outside the digest entirely, so a module
    imported from one would execute inside a run whose attestation does not describe it. And
    the launcher unlinks the backing file of ITS image and nothing else's, so another attached
    image may still have a writable backing file on disk: even its read-only-ness is weaker
    than it reads.

    Only the two cases that ATTACH an image are marked: the other two need no boundary to
    make their point, and a class-wide mark would take them out of the push gate for nothing.
    """

    @_RELEASE_SEAL
    def test_a_foreign_read_only_volume_passes_the_weak_test_and_fails_the_real_one(
            self, tmp_path):
        with _read_only_volume(tmp_path, {"m.py": "x = 1\n"}) as foreign:
            intruder = foreign / "m.py"
            assert cal._mount_is_read_only(intruder) is True
            sealed_device = cal._device_of(tmp_path)
            assert cal._device_of(intruder) != sealed_device
            assert cal._on_sealed_volume(intruder, sealed_device) is False

    @_RELEASE_SEAL
    def test_the_sealed_volumes_own_files_are_accepted(self, tmp_path):
        with _read_only_volume(tmp_path, {"m.py": "x = 1\n"}) as mount:
            device = cal._device_of(mount / "m.py")
            assert cal._on_sealed_volume(mount / "m.py", device) is True

    def test_a_writable_path_on_the_sealed_device_is_still_refused(self, tmp_path):
        """Both halves are required: same device is not enough on its own."""
        target = tmp_path / "m.py"
        target.write_text("x = 1\n")
        assert cal._on_sealed_volume(target, cal._device_of(target)) is False

    def test_an_unidentifiable_volume_refuses_rather_than_verifying(self, monkeypatch):
        """'Could not tell which volume this is' is not 'sealed'."""
        monkeypatch.setattr(cal, "_RELEASE_BOUNDARY", "macos-hdiutil-udro")
        monkeypatch.setattr(cal, "_sealed_device", lambda: None)
        with pytest.raises(cal.BoundaryUnverifiedError, match="could not identify the volume"):
            cal.check_execution_boundary()


class TestBoundaryGate:
    """The flag is minted from the MEASUREMENT, not from what the environment claims."""

    def test_no_declared_boundary_is_recorded_not_raised(self, monkeypatch):
        """A dev run, a test, and a deliberate --no-boundary run all land here. Making this
        fatal would put the scorer out of reach of everything except releases."""
        monkeypatch.setattr(cal, "_RELEASE_BOUNDARY", None)
        assert cal.check_execution_boundary() is None

    def test_a_declared_boundary_that_is_not_there_raises(self, monkeypatch):
        """THE forgery case. Setting the attestation variable is exactly what an attacker —
        or a broken launcher — would do, and it buys nothing: this process is running from a
        writable checkout, and ST_RDONLY says so."""
        monkeypatch.setattr(cal, "_RELEASE_BOUNDARY", "macos-hdiutil-udro")
        with pytest.raises(cal.BoundaryUnverifiedError) as exc:
            cal.check_execution_boundary()
        assert "NOT on the sealed volume" in str(exc.value)

    def test_an_unenumerable_process_is_not_read_as_a_clean_one(self, monkeypatch):
        """Tri-state discipline, same as _worktree_registered: 'could not tell' must never
        collapse into 'nothing foreign'. If dyld will not answer, the run is not verified."""
        monkeypatch.setattr(cal, "_RELEASE_BOUNDARY", "macos-hdiutil-udro")
        # _on_sealed_volume, not _mount_is_read_only: the check is identity AND read-only-ness,
        # and this test is about dyld refusing to answer, not about either of those.
        monkeypatch.setattr(cal, "_on_sealed_volume", lambda path, device: True)
        monkeypatch.setattr(cal, "_loaded_native_images", lambda: ())
        with pytest.raises(cal.BoundaryUnverifiedError) as exc:
            cal.check_execution_boundary()
        assert "could not enumerate" in str(exc.value)

    def test_a_host_dylib_fails_an_otherwise_sealed_run(self, monkeypatch):
        """The half-sealed run. Everything is on the read-only volume except one dylib the
        launcher's static closure missed — which is exactly what happens to libpython and
        libintl if DYLD_LIBRARY_PATH is dropped. The digest, the bytecode check and the
        import origins would all still pass; only this catches it."""
        monkeypatch.setattr(cal, "_RELEASE_BOUNDARY", "macos-hdiutil-udro")
        monkeypatch.setattr(cal, "_on_sealed_volume",
                            lambda path, device: not str(path).startswith("/opt/homebrew"))
        monkeypatch.setattr(cal, "_loaded_native_images",
                            lambda: ("/usr/lib/libSystem.B.dylib",
                                     "/opt/homebrew/opt/gettext/lib/libintl.8.dylib"))
        with pytest.raises(cal.BoundaryUnverifiedError) as exc:
            cal.check_execution_boundary()
        assert "libintl" in str(exc.value)

    def test_the_signed_system_volume_is_accepted_and_recorded(self, monkeypatch):
        """/usr/lib and /System stay host-provided by decision. The honesty is os_build."""
        monkeypatch.setattr(cal, "_RELEASE_BOUNDARY", "macos-hdiutil-udro")
        monkeypatch.setattr(cal, "_on_sealed_volume", lambda path, device: True)
        monkeypatch.setattr(cal, "_loaded_native_images",
                            lambda: ("/usr/lib/libSystem.B.dylib",
                                     "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"))
        assert cal.check_execution_boundary() == "macos-hdiutil-udro"


class TestNoBoundaryOptOut:
    def test_the_flag_parses_and_is_off_by_default(self):
        assert launcher._parse_args([]).no_boundary is False
        assert launcher._parse_args(["--no-boundary"]).no_boundary is True

    def test_a_release_run_refuses_where_no_mechanism_exists(self, monkeypatch):
        """Fail closed. A platform with no boundary does not get a quietly weaker release —
        it gets no release, and an error that names the escape hatch and its consequence."""
        monkeypatch.setattr(launcher, "_boundary_mechanism", lambda: None)
        args = launcher._parse_args([])
        with pytest.raises(launcher.LauncherError) as exc:
            launcher._outer_main(args, _REPO_ROOT)
        assert "--no-boundary" in str(exc.value)
        assert "scorer_source_verified_preexec=False" in str(exc.value)


class TestChildEnvBoundary:
    """child_env is the only place the boundary variables are allowed to come from."""

    def _sealed(self, tmp_path: Path) -> launcher.SealedRun:
        return launcher.SealedRun(
            mechanism=launcher.MACOS_MECHANISM, runtime_image_sha256="a" * 64,
            revision="b" * 40, volume=tmp_path, dep_paths=(tmp_path / "deps",),
            dylibs=tmp_path / "dylibs", scratch=tmp_path / "scratch",
        )

    def test_an_unsealed_run_never_inherits_a_boundary_claim(self, tmp_path, monkeypatch):
        """Same rule, and same reason, as the cohort-provenance digest: SET or REMOVE. An
        inherited GHOSTREPLAY_RELEASE_BOUNDARY would otherwise describe a volume that was
        never mounted — and while the scorer would still refuse it (it measures), a run must
        not be able to read a value no launcher wrote."""
        for name in (launcher.RELEASE_BOUNDARY_ENV, launcher.RUNTIME_IMAGE_DIGEST_ENV,
                     launcher.SEALED_REVISION_ENV, launcher.GRAPH_NO_DISK_CACHE_ENV):
            monkeypatch.setenv(name, "inherited")
        env = launcher.child_env(tmp_path, "d" * 64, tmp_path / "pyc")
        for name in (launcher.RELEASE_BOUNDARY_ENV, launcher.RUNTIME_IMAGE_DIGEST_ENV,
                     launcher.SEALED_REVISION_ENV, launcher.GRAPH_NO_DISK_CACHE_ENV):
            assert name not in env

    def test_a_sealed_run_carries_the_attestation_and_the_redirect(self, tmp_path):
        env = launcher.child_env(tmp_path, "d" * 64, tmp_path / "pyc",
                                 sealed=self._sealed(tmp_path))
        assert env[launcher.RELEASE_BOUNDARY_ENV] == launcher.MACOS_MECHANISM
        assert env[launcher.RUNTIME_IMAGE_DIGEST_ENV] == "a" * 64
        assert env[launcher.SEALED_REVISION_ENV] == "b" * 40
        assert env["DYLD_LIBRARY_PATH"] == str(tmp_path / "dylibs")
        assert env["TMPDIR"] == str(tmp_path / "scratch")

    def test_a_sealed_run_takes_its_deps_from_the_volume_not_the_host_venv(self, tmp_path):
        """The pip-install hazard, closed at the source. Unsealed, PYTHONPATH is derived from
        the interpreter's venv — the shared, writable one. Sealed, it is the volume's copy."""
        env = launcher.child_env(tmp_path, "d" * 64, tmp_path / "pyc",
                                 sealed=self._sealed(tmp_path))
        assert env["PYTHONPATH"] == str(tmp_path / "deps")

    def test_a_sealed_run_takes_the_pickle_graph_cache_out_of_the_input_set(self, tmp_path):
        """Not a performance switch. The cache is a pickle validated by (version, mtimes) and
        nothing else — the one mutable scoring input a sealed run would otherwise keep, and
        an unpickle of a file a same-uid process can write."""
        env = launcher.child_env(tmp_path, "d" * 64, tmp_path / "pyc",
                                 sealed=self._sealed(tmp_path))
        assert env[launcher.GRAPH_NO_DISK_CACHE_ENV] == "1"

    def test_the_forbidden_root_floor_reaches_the_child(self, tmp_path):
        """The one governance input the child cannot honestly derive for itself: its own
        derivation goes through git, and git answers through a writable directory."""
        env = launcher.child_env(tmp_path, "d" * 64, tmp_path / "pyc",
                                 sealed=self._sealed(tmp_path),
                                 forbidden_roots=[(16, 42), (16, 7)])
        assert json.loads(env[launcher.SEALED_FORBIDDEN_ROOTS_ENV]) == [[16, 7], [16, 42]]

    def test_an_inherited_forbidden_root_floor_is_dropped(self, tmp_path, monkeypatch):
        """SET or REMOVE, and this one has the sharpest edge: an inherited floor is a set of
        inode numbers from another machine, another repository or another week, and every one
        of them would be a root this run's private paths are compared against instead of the
        real ones."""
        monkeypatch.setenv(launcher.SEALED_FORBIDDEN_ROOTS_ENV, "[[1,2]]")
        env = launcher.child_env(tmp_path, "d" * 64, tmp_path / "pyc")
        assert launcher.SEALED_FORBIDDEN_ROOTS_ENV not in env


class TestForbiddenRootFloorProtocol:
    """The floor is measured on the HOST and carried, because it cannot be re-derived inside.

    The scorer's own derivation asks git, and under the boundary git answers through the
    origin's administrative directory — which stays writable outside the boundary. Editing
    <admin>/commondir repoints it while the sealed .git file and the admin inode are both
    unchanged, and the origin checkout drops out of the set that keeps production-derived data
    out of it. Reproduced against the code before this existed.
    """

    def test_the_host_measurement_names_every_working_tree(self, tmp_path):
        repo = _disposable_repo(tmp_path)
        linked = tmp_path / "linked"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach",
                        str(linked)], check=True, capture_output=True)
        identities = launcher._forbidden_root_identities(repo, what="--artifact")
        for tree in (repo, linked):
            info = tree.stat()
            assert (info.st_dev, info.st_ino) in identities

    def test_an_unlistable_repository_refuses_rather_than_returning_nothing(
            self, tmp_path, monkeypatch):
        """git answered the common directory and then failed to list the worktrees. That is not
        'there are none' — the fallback would be a floor with the origin missing from it, which
        is the failure this whole mechanism exists to prevent."""
        repo = _disposable_repo(tmp_path)
        monkeypatch.setattr(launcher, "_git", lambda *args: str(repo / ".git"))
        monkeypatch.setattr(launcher.subprocess, "run",
                            lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"", stderr=b""))
        with pytest.raises(launcher.LauncherError) as exc:
            launcher._forbidden_root_identities(repo, what="--artifact")
        assert "could not list the registered git worktrees" in str(exc.value)

    @pytest.mark.parametrize("raw", [None, "", "[]", "not json", '[[1]]', '{"a": 1}'])
    def test_the_inner_refuses_a_floor_it_cannot_use(self, raw):
        """Including the empty string and None: 'the outer sent none' must not degrade into
        'nothing is forbidden', which is the state the whole finding is about."""
        with pytest.raises(launcher.LauncherError):
            launcher._parse_forbidden_roots(raw)

    def test_the_inner_reads_the_pairs_the_outer_wrote(self):
        assert launcher._parse_forbidden_roots("[[16,42],[16,7]]") == [(16, 42), (16, 7)]

    def test_the_outer_sends_the_floor_to_the_inner(self, tmp_path, monkeypatch):
        """End of the wire: whatever the host measured is on the inner's command line, so an
        inner that refuses without one (above) is never reached by a healthy run."""
        seen: list[list[str]] = []
        monkeypatch.setattr(launcher, "_boundary_mechanism", lambda: launcher.MACOS_MECHANISM)
        monkeypatch.setattr(launcher, "_forbidden_root_identities",
                            lambda repo_root, *, what: [(16, 42)])
        volume = SimpleNamespace(
            interpreter=tmp_path / "py", tree=tmp_path / "tree", revision="c" * 40,
            mechanism=launcher.MACOS_MECHANISM, pycache=tmp_path / "pyc",
            scratch=tmp_path / "scratch", mount=tmp_path / "mnt", provenance_digest=None,
            script_args=(),
        )
        monkeypatch.setattr(launcher, "sealed_checkout",
                            lambda *a, **k: contextlib.nullcontext(volume))
        monkeypatch.setattr(launcher.subprocess, "run",
                            lambda command, env=None: seen.append(command)
                            or SimpleNamespace(returncode=0))
        launcher._outer_main(launcher._parse_args([]), tmp_path / "repo")
        command = seen[0]
        assert json.loads(command[command.index("--forbidden-roots") + 1]) == [[16, 42]]


class TestArtifactStaging:
    """The frozen artifact is the release's other ground truth, and it used to be read live
    from a writable private store while the run was in flight."""

    def _staged(self, tmp_path, args):
        """Stage through the real hold, because the descriptor is the point: _stage_artifact
        no longer resolves a name and cannot be driven without one."""
        repo = _disposable_repo(tmp_path)
        stage, mount = tmp_path / "stage", tmp_path / "mnt"
        with launcher._hold_artifact(repo, args) as artifact:
            return stage, mount, launcher._stage_artifact(stage, mount, args, artifact)

    def test_the_artifact_is_copied_in_and_the_argument_repointed(self, tmp_path):
        source = tmp_path / "store" / "cohort.json"
        source.parent.mkdir(parents=True)
        source.write_bytes(b'{"frozen": true}')
        stage, mount, args = self._staged(
            tmp_path,
            ["select-release", "--artifact", str(source), "--result-output", "/o"])
        assert args == ("select-release", "--artifact", str(mount / "inputs" / "cohort.json"),
                        "--result-output", "/o")
        assert (stage / "inputs" / "cohort.json").read_bytes() == b'{"frozen": true}'

    def test_the_equals_form_is_repointed_too(self, tmp_path):
        """A release that ran against the unsealed original because of a spelling difference
        is precisely the failure this exists to prevent."""
        source = tmp_path / "cohort.json"
        source.write_bytes(b"{}")
        _, mount, args = self._staged(tmp_path, [f"--artifact={source}"])
        assert args == (f"--artifact={mount / 'inputs' / 'cohort.json'}",)

    def test_the_sealed_copy_is_named_after_the_resolved_file(self, tmp_path):
        """A `latest` alias on a sealed volume would name the bytes something other than what
        they are, in the one place whose whole job is naming bytes."""
        real = tmp_path / "store" / "2026-07-01.json"
        real.parent.mkdir(parents=True)
        real.write_bytes(b"{}")
        alias = tmp_path / "store" / "latest.json"
        alias.symlink_to(real)
        stage, mount, args = self._staged(tmp_path, ["--artifact", str(alias)])
        assert args == ("--artifact", str(mount / "inputs" / "2026-07-01.json"))
        assert (stage / "inputs" / "2026-07-01.json").is_file()

    def test_the_sealed_copy_keeps_the_source_mode(self, tmp_path):
        """_require_sealed_bytes_are_their_sources compares it, so it has to be the mode of
        the file that was judged and not whatever the umask produced."""
        source = tmp_path / "cohort.json"
        source.write_bytes(b"{}")
        source.chmod(0o640)
        stage, _, _ = self._staged(tmp_path, ["--artifact", str(source)])
        assert (stage / "inputs" / "cohort.json").stat().st_mode & 0o777 == 0o640

    def test_a_run_with_no_artifact_is_left_alone(self, tmp_path):
        repo = _disposable_repo(tmp_path)
        with launcher._hold_artifact(repo, ["report", "--full"]) as artifact:
            assert artifact is None
            assert launcher._stage_artifact(tmp_path / "s", tmp_path / "m",
                                            ["report", "--full"], artifact) == ("report", "--full")

    def test_a_relative_artifact_refuses(self, tmp_path):
        repo = _disposable_repo(tmp_path)
        with pytest.raises(launcher.LauncherError, match="ABSOLUTE"):
            with launcher._hold_artifact(repo, ["--artifact", "relative/cohort.json"]):
                pass

    def test_a_missing_artifact_refuses_before_anything_is_staged(self, tmp_path):
        repo = _disposable_repo(tmp_path)
        with pytest.raises(launcher.LauncherError, match="could not be opened"):
            with launcher._hold_artifact(repo, ["--artifact", str(tmp_path / "absent.json")]):
                pass

    def test_a_directory_artifact_refuses(self, tmp_path):
        repo = _disposable_repo(tmp_path)
        (tmp_path / "store").mkdir()
        with pytest.raises(launcher.LauncherError, match="not a regular file"):
            with launcher._hold_artifact(repo, ["--artifact", str(tmp_path / "store")]):
                pass


class TestArtifactGovernance:
    """Sealing REWRITES --artifact, so the scorer downstream never sees the operator's path.

    _refuse_repo_interior_path is the scorer's own governance rule: release inputs hold
    production-derived private data and must not sit in a checkout, one `git add .` from being
    committed. Under the boundary the child is handed <mount>/inputs/<name>, which is inside no
    working tree and passes trivially — so the launcher's convenience silently disabled the
    rule for every sealed run. These pin that the ORIGINAL is judged, before the copy — and
    that the file judged is the file copied, which is a second and separate claim.
    """

    def _judge(self, repo: Path, args: Sequence[str]) -> None:
        with launcher._hold_artifact(repo, args):
            pass

    def test_an_artifact_inside_the_repo_refuses(self, tmp_path):
        repo = _disposable_repo(tmp_path)
        artifact = repo / "cohort.json"
        artifact.write_bytes(b"{}")
        with pytest.raises(launcher.LauncherError, match="INSIDE a repository working tree"):
            self._judge(repo, ["select-release", "--artifact", str(artifact)])

    def test_an_artifact_outside_every_checkout_is_accepted(self, tmp_path):
        repo = _disposable_repo(tmp_path)
        artifact = tmp_path / "store" / "cohort.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"{}")
        self._judge(repo, ["--artifact", str(artifact)])

    def test_a_case_variant_spelling_does_not_launder_it(self, tmp_path):
        """APFS is case-insensitive by default, so /repo/x and /REPO/x are ONE file that
        compares unequal as a string. Identity is (st_dev, st_ino), not spelling."""
        repo = _disposable_repo(tmp_path)
        artifact = repo / "cohort.json"
        artifact.write_bytes(b"{}")
        swapped = Path(str(repo).swapcase()) / "cohort.json"
        if not swapped.exists():
            pytest.skip("filesystem is case-sensitive, so there is no variant to launder with")
        with pytest.raises(launcher.LauncherError, match="INSIDE a repository working tree"):
            self._judge(repo, [f"--artifact={swapped}"])

    def test_a_symlink_from_outside_does_not_launder_it(self, tmp_path):
        repo = _disposable_repo(tmp_path)
        (repo / "cohort.json").write_bytes(b"{}")
        link = tmp_path / "looks-external.json"
        link.symlink_to(repo / "cohort.json")
        with pytest.raises(launcher.LauncherError, match="INSIDE a repository working tree"):
            self._judge(repo, ["--artifact", str(link)])

    def test_retargeting_the_symlink_after_the_check_does_not_launder_it(self, tmp_path):
        """The reproduction that broke the previous version. The check resolved the name and
        _stage_artifact resolved it AGAIN to copy — with a worktree add and a provenance mount
        in between — so an external symlink could pass and then be repointed at a repository
        file, which was duly staged and sealed. Verified against that code before this landed.
        """
        repo = _disposable_repo(tmp_path)
        (repo / "private.txt").write_bytes(b"bytes that live in a checkout")
        real = tmp_path / "store" / "cohort.json"
        real.parent.mkdir(parents=True)
        real.write_bytes(b"the artifact the operator named")
        link = tmp_path / "latest.json"
        link.symlink_to(real)
        stage, mount = tmp_path / "stage", tmp_path / "mnt"
        args = ["--artifact", str(link)]
        with launcher._hold_artifact(repo, args) as artifact:
            link.unlink()
            link.symlink_to(repo / "private.txt")
            staged = launcher._stage_artifact(stage, mount, args, artifact)
        assert (stage / "inputs" / "cohort.json").read_bytes() == b"the artifact the operator named"
        assert not (stage / "inputs" / "private.txt").exists()
        assert staged == ("--artifact", str(mount / "inputs" / "cohort.json"))

    def test_a_hard_linked_artifact_refuses(self, tmp_path):
        """A second name for the same bytes can sit inside a checkout while the name given
        here sits outside it, and no check of one path can see the other.

        ELIGIBILITY, not a durable guarantee: nothing stops a link being created a moment
        after this returns, and what defends the bytes is the held descriptor. Refusing a
        multiply-named artifact costs hardlink-deduplicated stores and is worth it here on
        exactly those terms.
        """
        repo = _disposable_repo(tmp_path)
        inside = repo / "cohort.json"
        inside.write_bytes(b"{}")
        outside = tmp_path / "looks-external.json"
        os.link(inside, outside)
        with pytest.raises(launcher.LauncherError, match="hard links"):
            self._judge(repo, ["--artifact", str(outside)])

    def test_the_last_occurrence_is_the_one_judged(self, tmp_path):
        """argparse takes the last --artifact. A checker reading the first and a stager
        rewriting the last would disagree about which file the release ran against."""
        repo = _disposable_repo(tmp_path)
        outside = tmp_path / "ok.json"
        outside.write_bytes(b"{}")
        (repo / "cohort.json").write_bytes(b"{}")
        with pytest.raises(launcher.LauncherError, match="INSIDE a repository working tree"):
            self._judge(repo, ["--artifact", str(outside),
                               "--artifact", str(repo / "cohort.json")])

    def test_git_failing_refuses_rather_than_accepting(self, tmp_path, monkeypatch):
        """'git could not tell us which trees exist' is not 'none of them'."""
        repo = _disposable_repo(tmp_path)
        artifact = tmp_path / "cohort.json"
        artifact.write_bytes(b"{}")
        real = subprocess.run

        def fake(args, *rest, **kwargs):
            if "worktree" in args:
                return SimpleNamespace(returncode=128, stdout=b"", stderr=b"")
            return real(args, *rest, **kwargs)

        monkeypatch.setattr(launcher.subprocess, "run", fake)
        with pytest.raises(launcher.LauncherError, match="could not list"):
            self._judge(repo, ["--artifact", str(artifact)])

    def test_sealed_checkout_refuses_before_it_creates_anything(self, tmp_path, monkeypatch):
        """Before the temp parent, the worktree, or the stage — the whole point is that a
        refused artifact is never copied anywhere."""
        repo = _disposable_repo(tmp_path)
        artifact = repo / "cohort.json"
        artifact.write_bytes(b"{}")
        monkeypatch.setattr(launcher.tempfile, "mkdtemp",
                            lambda **kw: pytest.fail("staging started before the check"))
        with pytest.raises(launcher.LauncherError, match="INSIDE a repository working tree"):
            with launcher.sealed_checkout(repo, "HEAD", mount_provenance=False,
                                          script_args=["--artifact", str(artifact)]):
                pass


class TestRuntimeDigestFraming:
    """runtime_image_sha256 is what a winner is BOUND to, so it has to name its input.

    The first version separated variable-length fields with NUL and nothing else, which is not
    a unique encoding: a single file whose CONTENT spelled the delimiters fed the hash exactly
    the stream two separate files did. A digest two different volumes can share is a number,
    not an attestation.
    """

    def _volume(self, root: Path, entries: dict[str, bytes]) -> Path:
        for rel, payload in entries.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            target.chmod(0o644)
        return root

    def test_content_cannot_impersonate_the_delimiters(self, tmp_path):
        """The exact construction that collided: file `a` holding X\\0b\\0file:420\\0Y hashed
        the same as files `a`->X and `b`->Y, both 0644 (420 decimal)."""
        forged = self._volume(tmp_path / "forged", {"a": b"X\0b\0file:420\0Y"})
        honest = self._volume(tmp_path / "honest", {"a": b"X", "b": b"Y"})
        assert launcher.runtime_image_digest(forged) != launcher.runtime_image_digest(honest)

    def test_moving_a_boundary_between_path_and_content_changes_the_digest(self, tmp_path):
        """The general form: any two volumes that differ only in WHERE one field ends and the
        next begins must not agree."""
        left = self._volume(tmp_path / "left", {"ab": b"c"})
        right = self._volume(tmp_path / "right", {"a": b"bc"})
        assert launcher.runtime_image_digest(left) != launcher.runtime_image_digest(right)

    def test_a_symlinked_directory_is_covered(self, tmp_path):
        """os.walk lists a symlinked dir under dirnames and, with followlinks=False, never
        descends — so a files-only walk did not hash it at all and its target was invisible."""
        base = tmp_path / "base"
        (base / "real").mkdir(parents=True)
        (base / "real" / "f").write_bytes(b"x")
        (base / "link").symlink_to("real")
        before = launcher.runtime_image_digest(base)
        (base / "link").unlink()
        (base / "link").symlink_to("elsewhere")
        assert launcher.runtime_image_digest(base) != before

    def test_an_empty_directory_is_covered(self, tmp_path):
        """Not inert on an import path: an empty directory is a namespace package, so its
        presence changes what `import` resolves to."""
        base = tmp_path / "base"
        (base / "pkg").mkdir(parents=True)
        before = launcher.runtime_image_digest(base)
        (base / "pkg").rmdir()
        assert launcher.runtime_image_digest(base) != before

    def test_an_unhashable_node_type_refuses(self, tmp_path):
        """Skipping it would put bytes on the volume the digest silently does not describe;
        opening it would block this process forever."""
        base = tmp_path / "base"
        base.mkdir()
        os.mkfifo(base / "pipe")
        with pytest.raises(launcher.LauncherError, match="neither a regular file"):
            launcher.runtime_image_digest(base)


@contextlib.contextmanager
def _sealed_world(tmp_path: Path, monkeypatch, *, with_artifact: bool = True,
                  record: bytes | None = None,
                  committed_links: Mapping[str, str] | None = None):
    """A synthetic sealed volume, the live host trees it copied, and a REAL git repository
    behind its checkout.

    _require_sealed_bytes_are_their_sources only READS the mount, so an ordinary directory
    stands in for one. Building a real 900MB image per case would cost half a minute each, and
    the real thing is exercised end to end by the `sealed_volume` fixture below — which runs
    this check for real on every module that uses it.

    THE REPOSITORY IS NOT SCENERY. The sealed checkout is compared against the COMMIT, so the
    baseline these cases exercise has to be a commit that exists; `git worktree add` builds the
    mount's tree/ the same way sealed_checkout builds the stage's.
    """
    repo = tmp_path / "origin-repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "scorer.py").write_text("scored\n")
    (repo / "pkg" / "run.sh").write_text("#!/bin/sh\n")
    (repo / "pkg" / "run.sh").chmod(0o755)
    # Committed symlinks: the case where the volume matches its baseline exactly and still has
    # somewhere else to go. git records mode 120000 with the target as the blob's content.
    for name, target in (committed_links or {}).items():
        (repo / name).symlink_to(target)
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t",
                                                 "commit", "-qm", "seed"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    revision = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()

    host = tmp_path / "host"
    prefix = host / "py" / "lib"
    prefix.mkdir(parents=True)
    (prefix / "os.py").write_text("stdlib")
    deps = host / "site-packages"
    (deps / "pkg").mkdir(parents=True)
    (deps / "pkg" / "__init__.py").write_text("dep")
    lib = host / "libfoo.dylib"
    lib.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 32)
    monkeypatch.setattr(launcher.sys, "base_prefix", str(host / "py"))

    mount = tmp_path / "mnt"
    mount.mkdir()
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", "-q",
                    str(mount / "tree"), revision], check=True, capture_output=True)
    # What the sealed .git has to name, asked of git the way sealed_checkout asks it.
    admin = Path(subprocess.run(
        ["git", "-C", str(mount / "tree"), "rev-parse", "--path-format=absolute", "--git-dir"],
        check=True, capture_output=True, text=True).stdout.strip())
    shutil.copytree(host / "py", mount / "py")
    shutil.copytree(deps, mount / "deps" / "0")
    (mount / "dylibs").mkdir()
    shutil.copy2(lib, mount / "dylibs" / "libfoo.dylib")

    record_digest = None
    if record is not None:
        mounted = launcher.manifest_path(mount / "tree", launcher.COHORT_PROVENANCE_REL)
        mounted.parent.mkdir(parents=True, exist_ok=True)
        mounted.write_bytes(record)
        record_digest = hashlib.sha256(record).hexdigest()

    artifact = None
    fd = None
    source = host / "store" / "cohort.json"
    if with_artifact:
        source.parent.mkdir(parents=True)
        source.write_bytes(b'{"frozen": true}')
        (mount / "inputs").mkdir()
        shutil.copy2(source, mount / "inputs" / "cohort.json")
        fd = os.open(source, os.O_RDONLY)
        artifact = launcher._HeldArtifact(index=1, fd=fd, resolved=source,
                                          mode=source.stat().st_mode & 0o777)
    try:
        yield SimpleNamespace(
            mount=mount, tree=mount / "tree", repo=repo, revision=revision,
            prefix=host / "py", deps=deps, lib=lib, artifact_source=source,
            record_digest=record_digest, admin=admin,
            verify=lambda: launcher._require_sealed_bytes_are_their_sources(
                mount, repo_root=repo, revision=revision, dep_paths=[deps],
                dylib_sources={"libfoo.dylib": lib}, provenance_digest=record_digest,
                artifact=artifact, admin=admin),
        )
    finally:
        if fd is not None:
            os.close(fd)


def _rewrite_in_place(path: Path, payload: bytes) -> None:
    """Change the content and put the metadata back exactly: same size, same mtime, same inode.

    This is the shape that defeated the first version of the check, which compared metadata
    only. `os.utime` restores mtime_ns to the nanosecond.
    """
    before = path.stat()
    assert len(payload) == before.st_size, "the point is that the size does not move"
    path.write_bytes(payload)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


class TestStagingConsistency:
    """`cp -Rc` clones each file atomically. It still WALKS.

    A pip install landing mid-walk is copied ahead of the walk for some packages and behind it
    for others, so the volume holds a hybrid: internally consistent per file, and a combination
    that never existed. Hashing it afterwards names the hybrid precisely, which is the trap —
    the digest looks like proof while describing a runtime nobody assembled.

    Both halves of the fix are pinned here: the comparison is of CONTENT, so restoring metadata
    does not hide an edit, and it is of the MOUNTED bytes, so nothing that happens to the
    writable stage after the check can reach the volume.

    These are the LIVE-baseline cases — the host trees, compared against themselves as they are
    now. That catches the concurrent install this exists for, and it is a detector rather than a
    proof; the limit is stated on the function and pinned by
    test_a_source_moved_between_two_reads_is_not_detected below.
    """

    def test_a_volume_that_matches_its_sources_passes(self, tmp_path, monkeypatch):
        with _sealed_world(tmp_path, monkeypatch) as world:
            world.verify()

    def test_a_change_and_revert_with_the_mtime_restored_refuses(self, tmp_path, monkeypatch):
        """THE case the metadata fingerprint could not see. Two same-size edits are staged and
        then reverted with mtime_ns put back, so every byte of metadata agrees while the volume
        holds A1/B1 and the source holds A0/B0. Reproduced against the old code first."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            staged = world.mount / "deps" / "0" / "pkg" / "__init__.py"
            staged.write_text("hax")  # what a mid-staging edit would have left on the volume
            _rewrite_in_place(world.deps / "pkg" / "__init__.py", b"dep")
            with pytest.raises(launcher.LauncherError,
                               match="does not match what it is supposed to hold"):
                world.verify()

    def test_a_dependency_rewritten_during_staging_refuses(self, tmp_path, monkeypatch):
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.deps / "pkg" / "__init__.py").write_text("dep = 2222")
            with pytest.raises(launcher.LauncherError,
                               match="does not match what it is supposed to hold"):
                world.verify()

    def test_a_package_appearing_during_staging_refuses(self, tmp_path, monkeypatch):
        """The pip-install shape: nothing already staged was touched, and the volume is still
        a hybrid because it predates the new package."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.deps / "new.py").write_text("y = 2")
            with pytest.raises(launcher.LauncherError,
                               match="does not match what it is supposed to hold"):
                world.verify()

    def test_a_dependency_deleted_during_staging_refuses(self, tmp_path, monkeypatch):
        """Absence is a state. Treating a vanished path as 'nothing to compare' would make
        deletion the one mutation that passed."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.deps / "pkg" / "__init__.py").unlink()
            with pytest.raises(launcher.LauncherError,
                               match="does not match what it is supposed to hold"):
                world.verify()

    def test_an_interpreter_rewritten_during_staging_refuses(self, tmp_path, monkeypatch):
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.prefix / "lib" / "os.py").write_text("stdlib, but not really")
            with pytest.raises(launcher.LauncherError,
                               match="does not match what it is supposed to hold"):
                world.verify()

    def test_a_dylib_rewritten_during_staging_refuses(self, tmp_path, monkeypatch):
        """`brew upgrade gettext` is the same hazard as a pip install, one file wide."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            world.lib.write_bytes(b"\xcf\xfa\xed\xfe" + b"\1" * 32)
            with pytest.raises(launcher.LauncherError,
                               match="does not match what it is supposed to hold"):
                world.verify()

    def test_a_dylib_deleted_during_staging_refuses(self, tmp_path, monkeypatch):
        with _sealed_world(tmp_path, monkeypatch) as world:
            world.lib.unlink()
            with pytest.raises(launcher.LauncherError,
                               match="does not match what it is supposed to hold"):
                world.verify()

    def test_the_checkout_is_covered_too(self, tmp_path, monkeypatch):
        """It was not, the first time round: the checkout, the artifact and the mounted
        provenance record were all absent from the set being checked. What it is checked
        AGAINST has since changed too — see TestSealedCheckoutIsTheCommit."""
        with _sealed_world(tmp_path, monkeypatch, with_artifact=False) as world:
            (world.tree / "pkg" / "scorer.py").write_text("scored differently\n")
            with pytest.raises(launcher.LauncherError,
                               match="does not match what it is supposed to hold"):
                world.verify()

    def test_the_artifact_is_covered_too(self, tmp_path, monkeypatch):
        """Compared against the held DESCRIPTOR, so a rewrite of the private store while the
        image was being built cannot leave a torn artifact sealed and unnoticed."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            world.artifact_source.write_bytes(b'{"frozen": FAL}')
            with pytest.raises(launcher.LauncherError,
                               match="does not match what it is supposed to hold"):
                world.verify()

    def test_a_file_added_to_the_stage_after_the_copy_refuses(self, tmp_path, monkeypatch):
        """The gap the per-subtree comparison alone would leave: bytes landing where no source
        maps onto them would be sealed and put on the child's import path with nothing having
        ever compared them to anything."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.mount / "sitecustomize.py").write_text("import os; os.system('...')")
            with pytest.raises(launcher.LauncherError, match="UNEXPECTED"):
                world.verify()

    def test_an_extra_library_in_the_closure_refuses(self, tmp_path, monkeypatch):
        """DYLD_LIBRARY_PATH resolves by leaf name, so an unexpected file here is a library
        the child could load under a name the closure never vouched for."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.mount / "dylibs" / "libbar.dylib").write_bytes(b"\xcf\xfa\xed\xfe")
            with pytest.raises(launcher.LauncherError, match="UNEXPECTED"):
                world.verify()

    def test_a_second_file_in_inputs_refuses(self, tmp_path, monkeypatch):
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.mount / "inputs" / "other.json").write_bytes(b"{}")
            with pytest.raises(launcher.LauncherError, match="UNEXPECTED"):
                world.verify()

    def test_a_run_with_no_artifact_expects_no_inputs_directory(self, tmp_path, monkeypatch):
        with _sealed_world(tmp_path, monkeypatch, with_artifact=False) as world:
            world.verify()
            (world.mount / "inputs").mkdir()
            with pytest.raises(launcher.LauncherError, match="UNEXPECTED"):
                world.verify()

    def test_a_sealed_mode_change_refuses(self, tmp_path, monkeypatch):
        """The mode is part of what the volume IS, and the digest a winner carries names it."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.mount / "dylibs" / "libfoo.dylib").chmod(0o600)
            with pytest.raises(launcher.LauncherError,
                               match="does not match what it is supposed to hold"):
                world.verify()

    def test_a_source_moved_between_two_reads_is_not_detected(self, tmp_path, monkeypatch):
        """THE LIMIT, pinned so nobody re-derives the guarantee from the code and overstates it
        again. An earlier docstring claimed a hybrid could not survive this comparison. It can:
        the comparison WALKS, so a source that shows A1 while `a.py` is read, reverts, then
        shows B1 while `b.py` is read matches at every instant the check looks, while the volume
        holds an A1/B1 pair the source never held at once.

        Nothing in user space fixes it — a whole-tree snapshot needs privileges this launcher
        deliberately does not require, and a second pass is the same walk again. What was done
        instead is to take the three things a release stands on OFF this baseline entirely: the
        checkout is compared against the commit, the artifact against a held descriptor, the
        record against a digest of the bytes this process wrote. This test asserts the weakness
        that remains, so a future reader meets it as a decision rather than as a surprise.
        """
        with _sealed_world(tmp_path, monkeypatch, with_artifact=False) as world:
            a, b = world.deps / "a.py", world.deps / "b.py"
            a.write_text("A0")
            b.write_text("B0")
            shutil.copy2(a, world.mount / "deps" / "0" / "a.py")
            shutil.copy2(b, world.mount / "deps" / "0" / "b.py")
            (world.mount / "deps" / "0" / "a.py").write_text("A1")
            (world.mount / "deps" / "0" / "b.py").write_text("B1")

            def poke(path: Path, text: str) -> None:  # raw: write_text would re-enter the hook
                fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
                os.write(fd, text.encode())
                os.close(fd)

            real_open = Path.open

            def racing_open(self, *args, **kwargs):
                if self == a:
                    poke(a, "A1")
                if self == b:
                    poke(a, "A0")
                    poke(b, "B1")
                return real_open(self, *args, **kwargs)

            monkeypatch.setattr(Path, "open", racing_open)
            world.verify()  # accepted, and this is the honest state of the guarantee
            monkeypatch.undo()
            assert (a.read_text(), b.read_text()) == ("A0", "B1")


class TestSealedNamesAreSealedFiles:
    """A name on the volume is not a file on the volume, and the difference was exploitable.

    Every check reached the sealed side through `stat()` and `open()` on a path — both FOLLOW
    symlinks — while the entry lists guarding them compared names only. A symlink staged in
    place of a file was therefore found under the right name, followed off the volume to the
    live host file, and compared equal to it, because it WAS it. The child would then have read
    mutable off-volume bytes for the whole run while `runtime_image_sha256` recorded the link's
    target string. Reproduced against the previous version for both directories that had it.
    """

    def test_an_artifact_symlinked_to_the_live_source_refuses(self, tmp_path, monkeypatch):
        with _sealed_world(tmp_path, monkeypatch) as world:
            sealed = world.mount / "inputs" / "cohort.json"
            sealed.unlink()
            sealed.symlink_to(world.artifact_source)
            with pytest.raises(launcher.LauncherError, match="symlink where a regular file"):
                world.verify()

    def test_a_dylib_symlinked_to_the_live_source_refuses(self, tmp_path, monkeypatch):
        """The same shape one directory over, and it was not in the report. DYLD_LIBRARY_PATH
        resolves by leaf name, so this one is loaded by dyld rather than read by the scorer."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            sealed = world.mount / "dylibs" / "libfoo.dylib"
            sealed.unlink()
            sealed.symlink_to(world.lib)
            with pytest.raises(launcher.LauncherError, match="symlink where a regular file"):
                world.verify()

    def test_a_volume_root_entry_of_the_wrong_kind_refuses(self, tmp_path, monkeypatch):
        with _sealed_world(tmp_path, monkeypatch, with_artifact=False) as world:
            shutil.rmtree(world.mount / "dylibs")
            (world.mount / "dylibs").symlink_to(tmp_path / "host")
            with pytest.raises(launcher.LauncherError, match="symlink where a directory"):
                world.verify()

    def test_a_file_on_another_device_refuses(self, tmp_path):
        """What O_NOFOLLOW cannot answer on its own: whether these bytes are on the volume the
        kernel froze. Asserted against the helper because a second filesystem is not something
        a test can conjure — the device is compared, so a wrong one refuses."""
        target = tmp_path / "somewhere.json"
        target.write_bytes(b"{}")
        with pytest.raises(launcher.LauncherError, match="is on device"):
            with launcher._sealed_file(target, device=-1, what="the sealed artifact"):
                pass

    def test_a_real_file_on_the_volume_is_read(self, tmp_path):
        target = tmp_path / "here.json"
        target.write_bytes(b'{"ok": 1}')
        device = target.stat().st_dev
        with launcher._sealed_file(target, device=device, what="x") as (fd, opened):
            assert os.read(fd, 64) == b'{"ok": 1}'
            assert opened.st_dev == device


class TestSealedSymlinksStayOnTheVolume:
    """Matching bytes are not a closed boundary.

    A symlink's content IS its target string, so both comparisons that meet one do the right
    thing and neither notices anything: `_tree_digest` and `_sealed_blob_id` hash the target and
    never follow it, which means a link with the same target on both sides compares equal, and a
    committed link matches its commit exactly. The volume is then verified, read-only, and
    carries a name whose bytes are somewhere else and stay writable for the whole run.

    Reproduced both ways against the previous version — committed, and copied out of the
    interpreter prefix where nothing needs committing at all — and the second read through the
    volume returned bytes written after the seal.
    """

    def test_a_committed_symlink_pointing_off_the_volume_refuses(self, tmp_path, monkeypatch):
        outside = tmp_path / "outside" / "live.txt"
        outside.parent.mkdir(parents=True)
        outside.write_text("host bytes\n")
        with _sealed_world(tmp_path, monkeypatch,
                           committed_links={"pkg/linked.txt": str(outside)}) as world:
            assert (world.tree / "pkg" / "linked.txt").read_text() == "host bytes\n"
            outside.write_text("changed after the seal\n")
            assert (world.tree / "pkg" / "linked.txt").read_text() == "changed after the seal\n"
            with pytest.raises(launcher.LauncherError, match="symlinks that leave it"):
                world.verify()

    def test_a_symlink_in_the_interpreter_prefix_pointing_off_the_volume_refuses(
            self, tmp_path, monkeypatch):
        """The case that needs no commit and no attacker: a prefix that is not self-contained
        cannot be sealed by copying it, and the identical target on both sides is exactly why
        the content comparison agrees."""
        outside = tmp_path / "outside" / "libhost.py"
        outside.parent.mkdir(parents=True)
        outside.write_text("host module\n")
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.prefix / "lib" / "escape.py").symlink_to(outside)
            (world.mount / "py" / "lib" / "escape.py").symlink_to(outside)
            with pytest.raises(launcher.LauncherError, match="symlinks that leave it"):
                world.verify()

    def test_a_relative_symlink_that_stays_inside_passes(self, tmp_path, monkeypatch):
        """The common case, and the reason the rule is about where a link LANDS rather than
        about links: the interpreter prefix is full of these and they mean the same thing on the
        volume that they meant at the source."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.prefix / "lib" / "alias.py").symlink_to("os.py")
            (world.mount / "py" / "lib" / "alias.py").symlink_to("os.py")
            world.verify()

    def test_a_link_that_lands_inside_the_volume_on_nothing_passes(self, tmp_path, monkeypatch):
        """A dangling link INSIDE the volume is not a way out: the volume is read-only, so a
        target that is absent now stays absent."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.prefix / "lib" / "gone.py").symlink_to("not-here.py")
            (world.mount / "py" / "lib" / "gone.py").symlink_to("not-here.py")
            world.verify()

    def test_a_link_that_lands_outside_on_nothing_still_refuses(self, tmp_path, monkeypatch):
        """Absent OUTSIDE is a different fact from absent inside, and the rule does not treat
        them alike: nothing stops that path being created while the run is live, and the machine
        this refuses on is not necessarily the machine the target exists on. The stray
        `.antigravitycli/` link this check found in the repository was exactly that shape —
        dangling here, and a live file in the home directory it names."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.prefix / "lib" / "gone.py").symlink_to(tmp_path / "outside" / "never.py")
            (world.mount / "py" / "lib" / "gone.py").symlink_to(tmp_path / "outside" / "never.py")
            with pytest.raises(launcher.LauncherError, match="symlinks that leave it"):
                world.verify()

    def test_the_audit_reports_every_escape_rather_than_the_first(self, tmp_path, monkeypatch):
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.mount / "py" / "a").symlink_to(tmp_path)
            (world.mount / "deps" / "0" / "b").symlink_to("/etc")
            escaping = launcher._require_symlinks_stay_on_the_volume(
                world.mount, device=world.mount.stat().st_dev)
            assert len(escaping) == 2, escaping


class TestSealedCheckoutIsTheCommit:
    """The sealed checkout is compared against the COMMIT, not against the staging copy.

    The previous version digested the staging tree once git and the provenance mount had
    finished and compared the volume against that. The baseline came out of a WRITABLE
    directory, so an edit landing before that read was not caught — it was folded into the
    value everything downstream compared against, while `sealed_revision` went on naming the
    commit the operator asked for.

    A commit cannot be edited by an editor saving a file: it is content-addressed and already
    written. So each sealed file is hashed in git's own framing and compared to the object id
    the commit lists — which also makes several things visible that no whole-tree digest of the
    stage could ever have flagged, because the stage was its own baseline.
    """

    def test_a_checkout_matching_the_commit_passes(self, tmp_path, monkeypatch):
        with _sealed_world(tmp_path, monkeypatch) as world:
            world.verify()

    def test_an_edited_tracked_file_refuses(self, tmp_path, monkeypatch):
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.tree / "pkg" / "scorer.py").write_text("scored\n# and one more thing\n")
            with pytest.raises(launcher.LauncherError, match="does not hold the bytes"):
                world.verify()

    def test_a_file_the_commit_does_not_have_refuses(self, tmp_path, monkeypatch):
        """The shape a stage-derived baseline was blindest to: an added file is in the digest
        it is later compared against, so it agreed with itself."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.tree / "pkg" / "sitecustomize.py").write_text("import os\n")
            with pytest.raises(launcher.LauncherError, match="is not in"):
                world.verify()

    def test_a_missing_tracked_file_refuses(self, tmp_path, monkeypatch):
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.tree / "pkg" / "scorer.py").unlink()
            with pytest.raises(launcher.LauncherError, match="missing from the volume"):
                world.verify()

    def test_an_empty_directory_refuses(self, tmp_path, monkeypatch):
        """Not pedantry: an empty directory on an import path is a namespace package, so its
        presence changes what `import` resolves to without holding a single byte."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.tree / "pkg" / "app").mkdir()
            with pytest.raises(launcher.LauncherError, match="a directory .* does not have"):
                world.verify()

    def test_a_flipped_exec_bit_refuses(self, tmp_path, monkeypatch):
        """The exec bit is the whole of what git records about a mode, so it is the whole of
        what can be compared — and it is what decides whether a file can be run."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.tree / "pkg" / "scorer.py").chmod(0o755)
            with pytest.raises(launcher.LauncherError, match="is mode 100755"):
                world.verify()

    def test_a_tracked_file_replaced_by_a_symlink_refuses(self, tmp_path, monkeypatch):
        """The link points INSIDE the volume on purpose: an escaping one is refused earlier, by
        the containment audit, and this is here to pin the mode comparison itself — a file the
        commit records as 100644 is not a link, wherever the link goes."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            scorer = world.tree / "pkg" / "scorer.py"
            scorer.unlink()
            scorer.symlink_to("run.sh")
            with pytest.raises(launcher.LauncherError, match="is mode 120000"):
                world.verify()

    def test_the_git_file_is_allowed_and_has_to_name_this_runs_admin_directory(
            self, tmp_path, monkeypatch):
        """`git worktree add` leaves a .git file the commit does not contain, so it is permitted
        — and it is a POINTER, so what it points at is checked."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            assert (world.tree / ".git").read_text().strip() == f"gitdir: {world.admin}"
            world.verify()

    def test_a_git_file_redirected_to_another_repository_refuses(self, tmp_path, monkeypatch):
        """The reach this one has is not the checkout, it is GOVERNANCE. A gitfile decides which
        repository git answers as from inside the volume, and the scorer builds its private-path
        forbidden-root set from `git worktree list`. Reproduced against the previous version:
        the checkout verified clean while the listing from the sealed tree named the OTHER
        repository's worktrees and stopped naming the origin at all — so a production-derived
        result written straight into the real checkout would have passed the rule that exists to
        stop exactly that."""
        other = tmp_path / "other-repo"
        (other / "src").mkdir(parents=True)
        (other / "src" / "x.py").write_text("x\n")
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "other"]):
            subprocess.run(["git", "-C", str(other), *args], check=True, capture_output=True)
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.tree / ".git").write_text(f"gitdir: {other / '.git'}\n")
            listed = subprocess.run(
                ["git", "-C", str(world.tree), "worktree", "list", "--porcelain"],
                capture_output=True, text=True).stdout
            assert str(world.repo) not in listed, "the redirect is what makes this worth checking"
            with pytest.raises(launcher.LauncherError,
                               match="administrative directory"):
                world.verify()

    def test_a_git_file_that_names_nothing_refuses(self, tmp_path, monkeypatch):
        """Unparseable is not permission: anything this cannot read as a gitdir line is a
        mismatch, not a pass."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.tree / ".git").write_text("this is not a gitfile\n")
            with pytest.raises(launcher.LauncherError, match="administrative directory"):
                world.verify()

    def test_a_deleted_git_file_refuses(self, tmp_path, monkeypatch):
        """Permitted is not the same as optional. The absence check only ever looked at the
        commit's own paths, so removing this passed — the entry the volume is allowed to carry
        was never one the volume was required to carry."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            (world.tree / ".git").unlink()
            with pytest.raises(launcher.LauncherError, match=r"\.git has to be on the volume"):
                world.verify()

    def test_a_deleted_provenance_record_refuses(self, tmp_path, monkeypatch):
        """The same gap, on the entry that says which cohort the run is about."""
        with _sealed_world(tmp_path, monkeypatch, record=b'{"cohort": "c1"}') as world:
            launcher.manifest_path(world.tree, launcher.COHORT_PROVENANCE_REL).unlink()
            with pytest.raises(launcher.LauncherError, match="has to be on the volume"):
                world.verify()

    def test_the_mounted_record_is_compared_against_what_the_launcher_wrote(
            self, tmp_path, monkeypatch):
        """The record is not in the commit — the whole point of mounting it is that approval
        happens before it is committed. Its baseline is the digest mount_cohort_provenance
        returned, which is bytes this process read and hashed rather than anything on disk."""
        with _sealed_world(tmp_path, monkeypatch, record=b'{"cohort": "c1"}') as world:
            world.verify()
            launcher.manifest_path(world.tree, launcher.COHORT_PROVENANCE_REL).write_bytes(
                b'{"cohort": "c2"}')
            with pytest.raises(launcher.LauncherError, match="not the record the launcher"):
                world.verify()

    def test_an_unmounted_record_is_just_another_untracked_file(self, tmp_path, monkeypatch):
        """Without --mount-cohort-provenance there is no record to vouch for one, so a file
        appearing at that path is refused like any other."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            path = launcher.manifest_path(world.tree, launcher.COHORT_PROVENANCE_REL)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'{"cohort": "smuggled"}')
            with pytest.raises(launcher.LauncherError, match="is not in"):
                world.verify()

    def test_a_submodule_entry_refuses_rather_than_being_skipped(self, tmp_path, monkeypatch):
        """A gitlink is a second repository this launcher does not stage. Listing it as a blob
        would be wrong and skipping it would leave a directory the check cannot describe."""
        with _sealed_world(tmp_path, monkeypatch) as world:
            subprocess.run(["git", "-C", str(world.repo), "update-index", "--add", "--cacheinfo",
                            f"160000,{world.revision},vendor"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(world.repo), "-c", "user.email=t@t", "-c",
                            "user.name=t", "commit", "-qm", "gitlink"], check=True,
                           capture_output=True)
            head = subprocess.run(["git", "-C", str(world.repo), "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
            with pytest.raises(launcher.LauncherError, match="git commit entry rather than"):
                launcher._commit_blobs(world.repo, head)


class TestSealedCheckoutOrdering:
    """The staging order, pinned with fakes so it can be checked on any platform.

    The ordering is not a style preference here. Everything the run reads has to be in the
    stage BEFORE `hdiutil create` copies it, because after that the volume is immutable — a
    provenance record mounted one step later would have nowhere to go but a read-only
    filesystem, and an artifact staged one step later would simply not be in the image. And
    the image file has to be unlinked immediately after attach, before anything is hashed,
    because until it is gone there is a writable path to every byte the digest describes.
    """

    def _fake_out_the_world(self, monkeypatch, tmp_path, calls, recorded=None):
        recorded = [] if recorded is None else recorded

        def fake_git(*args):
            if "rev-parse" in args:
                return str(tmp_path / "admin")
            if "add" in args:
                # A real checkout contains a launcher, and sealed_checkout reads its
                # INNER_PROTOCOL_VERSION before staging anything. Write one rather than
                # stubbing the check out: that keeps the ordering assertions below honest
                # about where the handshake sits.
                tree = Path(args[args.index("--detach") + 1])
                (tree / "backend/scripts").mkdir(parents=True, exist_ok=True)
                (tree / launcher._LAUNCHER_REL).write_text(
                    f"{launcher._PROTOCOL_NAME} = {launcher.INNER_PROTOCOL_VERSION}\n")
            return ""

        def fake_run(*args, what):
            calls.append(args[1] if args[0] == "hdiutil" else args[0])
            if args[0] == "hdiutil" and args[1] == "create":
                Path(args[-1]).write_bytes(b"image")
            if args[0] == "hdiutil" and args[1] == "attach":
                mount = Path(args[args.index("-mountpoint") + 1])
                interpreter = (mount / "py" / "bin" /
                               f"python{sys.version_info.major}.{sys.version_info.minor}")
                interpreter.parent.mkdir(parents=True, exist_ok=True)
                interpreter.touch()
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        (tmp_path / "admin").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(launcher, "_resolve_revision", lambda repo, rev: "c" * 40)
        monkeypatch.setattr(launcher, "_git", fake_git)
        monkeypatch.setattr(launcher, "_run", fake_run)
        monkeypatch.setattr(launcher, "_clone_tree",
                            lambda src, dst: calls.append("clone") or dst.mkdir(parents=True))
        # Returns leaf name -> source path, which the source comparison needs in order to say
        # what each sealed library is a copy of; the fake stages nothing, so it reports nothing.
        monkeypatch.setattr(launcher, "_stage_dylib_closure",
                            lambda stage, dylibs: calls.append("dylibs") or {})

        def fake_verify(mount, **kwargs):
            calls.append("verify")
            assert not (mount.parent / "release.dmg").exists(), (
                "the sealed bytes were checked while a writable path to them still existed")
            # The two baselines that are not on disk anywhere: the revision the run resolved
            # before the checkout existed, and the digest of the record this process wrote.
            # Passing either one wrongly would leave the checkout compared against nothing.
            assert kwargs["revision"] == "c" * 40
            # And the administrative entry the sealed .git has to name: THIS run's, taken from
            # git after `worktree add` rather than assumed, so a repointed gitfile has something
            # to be wrong against.
            assert kwargs["admin"] == tmp_path / "admin"
            recorded.append(kwargs["provenance_digest"])

        monkeypatch.setattr(launcher, "_require_sealed_bytes_are_their_sources", fake_verify)
        monkeypatch.setattr(launcher, "_require_read_only", lambda path, what: 0)
        monkeypatch.setattr(launcher, "_detach_image", lambda mount: None)
        monkeypatch.setattr(launcher, "_remove_worktree", lambda repo, tree: None)
        monkeypatch.setattr(launcher, "mount_cohort_provenance",
                            lambda repo, tree: calls.append(f"provenance:{tree.name}") or "a" * 64)
        return recorded

    def test_everything_is_staged_before_the_image_is_built(self, tmp_path, monkeypatch):
        calls: list[str] = []
        self._fake_out_the_world(monkeypatch, tmp_path, calls)
        with launcher.sealed_checkout(_REPO_ROOT, "HEAD", mount_provenance=True,
                                      script_args=()) as volume:
            assert volume.provenance_digest == "a" * 64
        assert calls.index("provenance:tree") < calls.index("create")
        assert calls.index("clone") < calls.index("create")
        assert calls.index("dylibs") < calls.index("create")
        assert calls.index("create") < calls.index("attach")

    def test_the_record_the_launcher_wrote_is_what_the_volume_is_checked_against(
            self, tmp_path, monkeypatch):
        """The mounted record is in no commit, so the only thing that can vouch for it is the
        digest mount_cohort_provenance took of the bytes it wrote. Lose that on the way to the
        check and the record becomes an untracked file the checkout comparison cannot place —
        which fails closed, but only in production: no other test seals a real volume with a
        record mounted."""
        calls: list[str] = []
        recorded = self._fake_out_the_world(monkeypatch, tmp_path, calls)
        with launcher.sealed_checkout(_REPO_ROOT, "HEAD", mount_provenance=True,
                                      script_args=()):
            pass
        assert recorded == ["a" * 64]
        assert calls.index("provenance:tree") < calls.index("create")

    def test_a_run_without_a_record_says_so_rather_than_defaulting(self, tmp_path, monkeypatch):
        calls: list[str] = []
        recorded = self._fake_out_the_world(monkeypatch, tmp_path, calls)
        with launcher.sealed_checkout(_REPO_ROOT, "HEAD", mount_provenance=False,
                                      script_args=()):
            pass
        assert recorded == [None]

    def test_the_backing_image_is_unlinked_the_moment_it_is_attached(self, tmp_path, monkeypatch):
        calls: list[str] = []
        self._fake_out_the_world(monkeypatch, tmp_path, calls)
        with launcher.sealed_checkout(_REPO_ROOT, "HEAD", mount_provenance=False,
                                      script_args=()) as volume:
            assert not (volume.parent / "release.dmg").exists()

    def test_the_sealed_bytes_are_checked_against_their_sources_after_the_freeze(
            self, tmp_path, monkeypatch):
        """Order, not presence, is what was wrong before. The first version of this check
        measured the writable stage and then handed that same stage to `hdiutil create`, so
        everything it established was about a directory anything could rewrite in between. It
        has to run on the mount, after attach, and after the backing file is gone — the fake
        asserts that last part from inside."""
        calls: list[str] = []
        self._fake_out_the_world(monkeypatch, tmp_path, calls)
        with launcher.sealed_checkout(_REPO_ROOT, "HEAD", mount_provenance=False,
                                      script_args=()):
            pass
        assert calls.index("attach") < calls.index("verify")

    def test_the_writable_staging_copy_is_gone_before_the_run(self, tmp_path, monkeypatch):
        """Exactly one path to these bytes, and it is read-only. A surviving stage would be a
        writable second copy of the whole checkout for the length of the run."""
        calls: list[str] = []
        self._fake_out_the_world(monkeypatch, tmp_path, calls)
        with launcher.sealed_checkout(_REPO_ROOT, "HEAD", mount_provenance=False,
                                      script_args=()) as volume:
            assert not (volume.parent / "stage").exists()

    def test_the_worktree_registration_is_repointed_at_the_mount(self, tmp_path, monkeypatch):
        """git has to keep working from inside the volume: _private_path_forbidden_roots asks
        it which working trees exist, and REFUSES a result path when it cannot answer. Left
        pointing at the deleted stage, that gate would fail closed on every sealed run."""
        calls: list[str] = []
        self._fake_out_the_world(monkeypatch, tmp_path, calls)
        with launcher.sealed_checkout(_REPO_ROOT, "HEAD", mount_provenance=False,
                                      script_args=()) as volume:
            recorded = (tmp_path / "admin" / "gitdir").read_text().strip()
            assert recorded == str(volume.mount / "tree" / ".git")

    def test_the_writable_set_is_outside_the_volume(self, tmp_path, monkeypatch):
        """CRITERION 3. Everything the run may write is enumerated and none of it is on the
        sealed volume: the bytecode cache and the child's scratch. The graph cache is NOT in
        this list any more — a pickle nothing hashes has no business being a scoring input,
        so the sealed run rebuilds instead (GRAPH_NO_DISK_CACHE_ENV)."""
        calls: list[str] = []
        self._fake_out_the_world(monkeypatch, tmp_path, calls)
        with launcher.sealed_checkout(_REPO_ROOT, "HEAD", mount_provenance=False,
                                      script_args=()) as volume:
            for writable in (volume.pycache, volume.scratch):
                assert writable.is_dir()
                assert not writable.is_relative_to(volume.mount)
                assert writable.is_relative_to(volume.parent)


@contextlib.contextmanager
def _private_store():
    """A directory guaranteed to be outside every checkout — which `tmp_path` is not.

    AGENTS.md points TMPDIR at backend/.tmp for unrelated reasons, so pytest's temp directories
    can sit INSIDE this working tree, and the artifact governance rule refuses anything there.
    Correctly: that is the rule. So the store is placed somewhere no checkout reaches, which on
    the only platform this runs on is /private/tmp.
    """
    store = Path(tempfile.mkdtemp(prefix="ghostreplay-artifact-store-", dir="/private/tmp"))
    try:
        yield store
    finally:
        shutil.rmtree(store, ignore_errors=True)


@pytest.fixture(scope="module")
def sealed_volume(worktree_rev):
    """ONE real sealed volume for the whole module: ~30s and ~900MB to build, because it
    carries the interpreter, the dependency tree and the dylib closure as well as the
    checkout. Module-scoped so the acceptance cases below share the cost.
    """
    if sys.platform != "darwin":
        pytest.skip("no OS boundary mechanism on this platform")
    with launcher.sealed_checkout(_REPO_ROOT, worktree_rev, mount_provenance=False,
                                  script_args=()) as volume:
        yield volume


@_RELEASE_SEAL
@_MACOS_ONLY
class TestSealedCheckout:
    """The acceptance cases, against a real sealed volume.

    CRITERIA 1 and 4: a same-uid process cannot write the hashed bytes, and the denial is the
    OS's. CRITERION 2: it holds for the duration, not just at hash time — the fixture is
    module-scoped, so every test here runs against a volume that has been mounted since
    before the first digest was taken.
    """

    def test_a_same_uid_process_cannot_write_a_hashed_file(self, sealed_volume):
        target = sealed_volume.tree / "backend/app/fen.py"
        assert target.is_file()
        with contextlib.suppress(OSError):
            target.chmod(0o666)          # the revert that defeated the 0444 design
        with pytest.raises(OSError) as exc:
            with target.open("ab") as handle:
                handle.write(b"\n# landed after the hash\n")
        assert exc.value.errno == errno.EROFS

    def test_every_manifest_file_is_on_the_one_sealed_volume(self, sealed_volume):
        """Read-only AND the same volume. Read-only alone is satisfied by any other attached
        image, whose bytes runtime_image_sha256 does not cover."""
        device = cal._device_of(sealed_volume.mount)
        for rel in cal.SCORER_SOURCE_FILES:
            assert cal._on_sealed_volume(sealed_volume.tree / rel, device), rel

    def test_the_interpreter_and_the_deps_are_sealed_too(self, sealed_volume):
        """The hazard that made this bead blocking. A boundary around the tree alone leaves a
        concurrent `pip install` free to change what executes without touching the tree, the
        manifest, or the digest."""
        device = cal._device_of(sealed_volume.mount)
        assert cal._on_sealed_volume(sealed_volume.interpreter, device)
        deps = sorted((sealed_volume.mount / "deps").iterdir())
        assert deps, "no dependency directory was sealed"
        for dep in deps:
            assert cal._on_sealed_volume(dep, device)
        assert (sealed_volume.mount / "dylibs").is_dir()

    def test_libpython_and_libintl_are_staged_rather_than_left_on_the_host(self, sealed_volume):
        """The finding that shaped the design. `otool -L` shows the interpreter references
        libpython and libintl by ABSOLUTE PATH into ~/.pyenv and /opt/homebrew, so a copy of
        the prefix onto the volume still executes mutable host code until DYLD_LIBRARY_PATH
        redirects it — which it can only do if the copies are there to redirect to."""
        staged = {p.name for p in (sealed_volume.mount / "dylibs").iterdir()}
        assert any(name.startswith("libpython") for name in staged), staged
        assert any(name.startswith("libintl") for name in staged), staged

    def test_a_sealed_run_carries_the_artifact_it_was_given(self, worktree_rev):
        """The --artifact path end to end, and the only test that pins its wiring: judged from
        an alias outside every checkout, copied from the HELD DESCRIPTOR, and compared against
        that descriptor once the volume is frozen. Its own volume, because the shared fixture
        deliberately seals no artifact and this needs an `inputs/` on the image."""
        with _private_store() as store:
            artifact = store / "cohort-frozen.json"
            artifact.write_bytes(b'{"frozen": true, "rows": 12345}')
            artifact.chmod(0o640)
            alias = store / "latest.json"
            alias.symlink_to(artifact)
            with launcher.sealed_checkout(
                    _REPO_ROOT, worktree_rev, mount_provenance=False,
                    script_args=["select-release", "--artifact", str(alias)]) as volume:
                sealed = volume.mount / "inputs" / "cohort-frozen.json"
                assert volume.script_args == ("select-release", "--artifact", str(sealed))
                assert sealed.read_bytes() == artifact.read_bytes()
                assert sealed.stat().st_mode & 0o777 == 0o640
                with pytest.raises(OSError) as exc:
                    with sealed.open("ab") as handle:
                        handle.write(b"\n")
                assert exc.value.errno == errno.EROFS

    def test_a_write_into_the_stage_before_the_freeze_refuses(self, worktree_rev, monkeypatch):
        """The second half of the staging gap, against a real image. The first version of the
        consistency check measured the writable stage and then handed that same still-writable
        stage to `hdiutil create`, so anything landing in between was sealed unexamined. This
        drops a file into exactly that window and requires the run to refuse it."""
        real_run = launcher._run

        def intercept(*args, what):
            if args[0] == "hdiutil" and args[1] == "create":
                stage = Path(args[args.index("-srcfolder") + 1])
                (stage / "sitecustomize.py").write_text("# landed after every check\n")
            return real_run(*args, what=what)

        monkeypatch.setattr(launcher, "_run", intercept)
        with pytest.raises(launcher.LauncherError, match="UNEXPECTED.*sitecustomize"):
            with launcher.sealed_checkout(_REPO_ROOT, worktree_rev, mount_provenance=False,
                                          script_args=()):
                pass

    def test_git_still_works_from_inside_the_volume(self, sealed_volume):
        """Not a nicety: _private_path_forbidden_roots refuses a result path outright when
        git cannot list the worktrees, so a sealed run with broken git could not write its
        result at all."""
        toplevel = subprocess.run(
            ["git", "-C", str(sealed_volume.tree), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=15)
        assert toplevel.returncode == 0, toplevel.stderr
        assert Path(toplevel.stdout.strip()).resolve() == sealed_volume.tree.resolve()
        listed = subprocess.run(["git", "-C", str(_REPO_ROOT), "worktree", "list"],
                                capture_output=True, text=True, timeout=15)
        assert str(sealed_volume.tree) in listed.stdout

    def test_a_child_run_inside_the_volume_stamps_verified(self, sealed_volume, tmp_path):
        """THE acceptance case, end to end: a real read-only volume, a real sealed
        interpreter, the real scorer measuring its own filesystem, and the flag that the
        release gate demands coming out True — for the first time under this design, since
        the pre-exec digest alone now stamps False.
        """
        sealed = launcher.SealedRun(
            mechanism=sealed_volume.mechanism,
            runtime_image_sha256="a" * 64,   # the flag does not depend on its value
            revision=sealed_volume.revision,
            volume=sealed_volume.mount,
            dep_paths=tuple(sorted((sealed_volume.mount / "deps").iterdir())),
            dylibs=sealed_volume.mount / "dylibs",
            scratch=tmp_path / "scratch",
        )
        (tmp_path / "scratch").mkdir()
        result = tmp_path / "result.json"
        env = launcher.child_env(sealed_volume.tree, launcher.manifest_digest(sealed_volume.tree),
                                 tmp_path / "pyc", sealed=sealed)
        proc = subprocess.run(
            [str(sealed_volume.interpreter), "-S", "-c", _PROBE, str(tmp_path), str(result)],
            cwd=str(sealed_volume.tree / "backend"), env=env,
            capture_output=True, text=True, timeout=600,
        )
        assert proc.returncode == 0, proc.stderr
        assert json.loads(result.read_text()) == {"preexec": True}

    def test_the_runtime_image_digest_covers_the_deps_not_just_the_tree(self, sealed_volume,
                                                                       tmp_path):
        """What runtime_python and runtime_chess_version could never say. Two builds of
        '3.12.7' agree on every character of both; this moves when any byte on the volume
        moves, which is why the winner binds it."""
        digest = launcher.runtime_image_digest(sealed_volume.mount)
        assert len(digest) == 64 and digest == launcher.runtime_image_digest(sealed_volume.mount)
        copied = tmp_path / "copy"
        shutil_copy = subprocess.run(["cp", "-R", str(sealed_volume.mount / "deps"), str(copied)],
                                     capture_output=True)
        assert shutil_copy.returncode == 0
        target = next(p for p in copied.rglob("*.py") if p.is_file())
        target.chmod(0o644)
        target.write_bytes(target.read_bytes() + b"\n# moved\n")
        assert launcher.runtime_image_digest(copied) != launcher.runtime_image_digest(
            sealed_volume.mount / "deps")


class TestInnerProtocolHandshake:
    """The two launchers are not the same file: the outer one is whatever the operator
    invoked, the inner one is the copy AT --rev. A new outer against an old rev used to die
    with `unrecognized arguments: --inner --sealed-revision` from a program the operator did
    not knowingly run — after paying ~30s to build the volume it then could not use. Found on
    the first real end-to-end run, which is why this checks the checkout rather than trusting
    that both halves are current.
    """

    def test_this_launcher_declares_the_version_it_speaks(self):
        assert launcher.read_inner_protocol_version(_REPO_ROOT) == launcher.INNER_PROTOCOL_VERSION

    def test_a_revision_predating_the_boundary_refuses_by_name(self, tmp_path):
        tree = tmp_path / "old"
        (tree / "backend/scripts").mkdir(parents=True)
        (tree / launcher._LAUNCHER_REL).write_text("SCORER_SOURCE_FILES = ()\n")
        assert launcher.read_inner_protocol_version(tree) is None
        with pytest.raises(launcher.LauncherError) as exc:
            launcher._require_compatible_inner(tree, "abc1234")
        assert "predates the OS boundary" in str(exc.value)
        assert "--no-boundary" in str(exc.value)   # names the way forward, not just the wall

    def test_a_version_mismatch_refuses_before_the_volume_is_built(self, tmp_path):
        tree = tmp_path / "future"
        (tree / "backend/scripts").mkdir(parents=True)
        (tree / launcher._LAUNCHER_REL).write_text(
            f"{launcher._PROTOCOL_NAME}: int = {launcher.INNER_PROTOCOL_VERSION + 1}\n")
        with pytest.raises(launcher.LauncherError, match="speaks"):
            launcher._require_compatible_inner(tree, "def5678")

    def test_the_version_is_read_by_parsing_never_by_importing(self, tmp_path):
        """Importing the checked-out launcher would execute code from the revision inside the
        process that is supposed to be vouching for it — one interpreter before the boundary
        exists. So a checkout whose launcher raises on import is still readable."""
        tree = tmp_path / "explosive"
        (tree / "backend/scripts").mkdir(parents=True)
        (tree / launcher._LAUNCHER_REL).write_text(
            f"raise SystemExit('imported')\n{launcher._PROTOCOL_NAME} = 1\n")
        assert launcher.read_inner_protocol_version(tree) == 1
