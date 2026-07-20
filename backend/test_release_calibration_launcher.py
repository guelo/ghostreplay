"""The pre-exec source digest + exclusive checkout (g-p4ih-srcfence).

The launcher's whole job is to know something the run cannot know about itself: that the
bytes named by ``scorer_source_digest`` are the bytes the interpreter compiled. It buys that
with ORDER (hash before the interpreter exists) and EXCLUSIVITY (a checkout nothing else can
write to). Both are tested here against a real ``git worktree`` and a real child process —
the in-process half is tested in test_calibrate_opening_scores.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.calibrate_opening_scores_v2 as cal
import scripts.release_calibration_launcher as launcher

_REPO_ROOT = Path(cal.__file__).resolve().parents[2]
_BACKEND_ROOT = _REPO_ROOT / "backend"
_LAUNCHER_PATH = Path(launcher.__file__).resolve()


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
_E2E_OVERLAY = (*cal.SCORER_SOURCE_FILES, "backend/test_calibrate_opening_scores.py")


def _overlay_commit(repo_root: Path, rel_paths: Sequence[str], index_path: Path) -> str:
    """A commit of HEAD with ``rel_paths`` replaced by their current working-tree bytes.

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

    So: seed a TEMPORARY index from HEAD and stage only _E2E_OVERLAY into it. Everything else
    in the checkout is HEAD's. The temp index matters as much as the selection — GIT_INDEX_FILE
    keeps this out of the shared index entirely, so nothing here locks or mutates state another
    agent is using, and `git stash create` is not an option for the same reason it looked
    attractive: it snapshots everyone's work, not ours.

    The launcher itself is untracked and deliberately absent from the overlay. It costs
    nothing: the launcher runs from the ORIGIN and only the checkout is exec'd.
    """
    return _overlay_commit(_REPO_ROOT, _E2E_OVERLAY,
                           tmp_path_factory.mktemp("git-index") / "index")


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
        stale = [rel for rel in _E2E_OVERLAY
                 if checked_out[rel] != (_REPO_ROOT / rel).read_bytes()]
        assert not stale, f"checkout carries committed, not working-tree, bytes for: {stale}"

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

    def test_a_run_under_the_launcher_stamps_verified(self, tmp_path, monkeypatch, worktree_rev):
        with launcher.exclusive_checkout(_REPO_ROOT, worktree_rev) as tree:
            assert _run_probe(monkeypatch, tree, tmp_path, tmp_path / "pyc") == {"preexec": True}

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
        args = launcher._parse_args(["--rev", "abc123", "--", "--select-release", "--json"])
        assert args.rev == "abc123"
        assert args.script_args == ["--select-release", "--json"]

    def test_defaults_to_head(self):
        assert launcher._parse_args([]).rev == "HEAD"
