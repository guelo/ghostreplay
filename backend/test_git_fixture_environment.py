"""Real pytest/Git entrypoint and working-tree overlay regressions."""

import os
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from git_test_support import overlay_worktree

BACKEND = Path(__file__).resolve().parent


@pytest.fixture
def git_env(tmp_path):
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'probe.sqlite3'}",
        "PYTHONPATH": str(BACKEND),
    }


def git(cwd, env, *args):
    return subprocess.check_output(
        ["git", *map(str, args)], cwd=cwd, env=env, text=True, stderr=subprocess.PIPE,
    ).strip()


def init_repo(root, env):
    root.mkdir()
    git(root, env, "init", "-q", "--initial-branch=main")
    git(root, env, "config", "user.name", "Parent Author")
    git(root, env, "config", "user.email", "parent@example.invalid")
    git(root, env, "config", "commit.gpgsign", "false")


@pytest.mark.parametrize("entrypoint", ["pytest", "bisect", "rebase"])
def test_pytest_isolates_git_fixtures_from_linked_worktree(tmp_path, git_env, entrypoint):
    main, linked = tmp_path / "main", tmp_path / "linked"
    init_repo(main, git_env)
    for revision in range(3):
        (main / "seed").write_text(str(revision))
        git(main, git_env, "add", "seed")
        git(main, git_env, "commit", "-qm", f"seed {revision}")
    git(main, git_env, "worktree", "add", "-q", "--detach", linked)
    admin = Path(git(linked, git_env, "rev-parse", "--absolute-git-dir"))

    # Load the real backend conftest in a separate pytest process. The prefix
    # records the environment BEFORE its cleanup, proving each Git entrypoint
    # actually supplies the absolute GIT_DIR that caused the original corruption.
    runner = tmp_path / "runner"
    runner.mkdir()
    record = tmp_path / "inherited-git-dir"
    (runner / "conftest.py").write_text(textwrap.dedent("""\
        import os as _probe_os
        from pathlib import Path as _ProbePath
        _ProbePath(_probe_os.environ["INHERITED_GIT_RECORD"]).write_text(
            _probe_os.environ.get("GIT_DIR", ""))
        """) + (BACKEND / "conftest.py").read_text())
    probe = runner / "test_probe.py"
    probe.write_text(textwrap.dedent("""\
        import os
        import subprocess
        from pathlib import Path

        local_vars = subprocess.check_output(
            ["git", "rev-parse", "--local-env-vars"], text=True).splitlines()
        assert not any(name in os.environ for name in local_vars)

        from test_calibrate_opening_scores import _git_repo_with_commit
        from test_capture_end_to_end import capture_clone, _git

        main = Path(os.environ["PARENT_MAIN"])
        linked_admin = Path(os.environ["PARENT_LINKED_ADMIN"])
        def snapshot():
            files = [main / ".git" / name for name in ("HEAD", "index", "config")]
            files += [linked_admin / name for name in ("HEAD", "index")]
            return ([p.read_bytes() for p in files], _git(main, "show-ref"))

        before = snapshot()
        def test_fixture_repositories(capture_clone, tmp_path):
            fixture = _git_repo_with_commit(tmp_path / "calibration")
            assert _git(fixture, "rev-parse", "--show-toplevel") == str(fixture.resolve())
            assert _git(fixture, "log", "-1", "--format=%s") == "seed"
            assert (capture_clone / ".git").is_dir()
            assert _git(capture_clone, "rev-parse", "--show-toplevel") == str(capture_clone)
            assert _git(capture_clone, "log", "-1", "--format=%s") == "e2e: working-tree scorer"
            assert snapshot() == before
        """))
    child_env = {
        **git_env,
        "INHERITED_GIT_RECORD": str(record),
        "PARENT_MAIN": str(main),
        "PARENT_LINKED_ADMIN": str(admin),
    }
    command = [sys.executable, "-W", "error", "-m", "pytest", "-q", str(probe),
               "--confcutdir", str(runner), "-c", str(BACKEND / "pytest.ini")]
    if entrypoint == "bisect":
        git(linked, git_env, "bisect", "start", "HEAD", "HEAD~2")
        command = ["git", "bisect", "run", *command]
    elif entrypoint == "rebase":
        command = ["git", "rebase", "--force-rebase", "--exec", shlex.join(command), "HEAD~1"]
    else:
        child_env.update({
            "GIT_DIR": str(admin),
            "GIT_WORK_TREE": str(linked),
            "GIT_COMMON_DIR": str(main / ".git"),
            "GIT_INDEX_FILE": str(admin / "index"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "Inherited Author",
        })
    result = subprocess.run(
        command, cwd=linked, env=child_env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout, result.stdout + result.stderr
    assert record.read_text() == str(admin)


def test_clone_overlay_preserves_current_files_and_deletions(tmp_path, git_env):
    source, clone = tmp_path / "source", tmp_path / "clone"
    init_repo(source, git_env)
    (source / ".gitignore").write_text("ignored.json\n")
    names = ("runtime.json", "staged.json", "deleted.json", "removed.json", "renamed.json",
             "file_slot", "directory_slot/input.json")
    for name in names:
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text("old")
    (source / "alias").symlink_to("runtime.json")
    git(source, git_env, "add", ".")
    git(source, git_env, "commit", "-qm", "seed")
    git(tmp_path, git_env, "clone", "-q", "--no-hardlinks", source, clone)

    (source / "runtime.json").write_text("modified runtime data")
    (source / "runtime.json").chmod(0o755)
    (source / "staged.json").write_text("staged data")
    (source / "added.json").write_text("staged new input")
    git(source, git_env, "add", "staged.json", "added.json")
    (source / "deleted.json").unlink()
    git(source, git_env, "rm", "-q", "removed.json")
    git(source, git_env, "mv", "renamed.json", "new name.json")
    (source / "new\ninput.json").write_text("untracked input")
    (source / "ignored.json").write_text("local state")
    (source / "alias").unlink()
    (source / "alias").symlink_to("new name.json")
    (source / "backend").mkdir()
    (source / "backend/.venv").symlink_to(tmp_path, target_is_directory=True)
    (source / "file_slot").unlink()
    (source / "file_slot").mkdir()
    (source / "file_slot/new.json").write_text("directory replaces file")
    (source / "directory_slot/input.json").unlink()
    (source / "directory_slot").rmdir()
    (source / "directory_slot").write_text("file replaces directory")

    overlay_worktree(source, clone)

    for name in ("runtime.json", "staged.json", "added.json", "new name.json", "new\ninput.json"):
        assert (clone / name).read_bytes() == (source / name).read_bytes()
    assert (clone / "runtime.json").stat().st_mode == (source / "runtime.json").stat().st_mode
    assert (clone / "alias").is_symlink()
    assert os.readlink(clone / "alias") == "new name.json"
    assert (clone / "file_slot/new.json").read_text() == "directory replaces file"
    assert (clone / "directory_slot").read_text() == "file replaces directory"
    assert not (clone / "backend/.venv").exists()
    for name in ("deleted.json", "removed.json", "renamed.json", "ignored.json"):
        assert not (clone / name).exists()
