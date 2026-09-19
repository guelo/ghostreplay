"""Copy the current checkout's changes into disposable Git test clones."""

import os
import shutil
import subprocess
from pathlib import Path


def overlay_worktree(source: Path, checkout: Path) -> None:
    # Beads state is managed exclusively by its CLI, not by test fixtures.
    paths = ["--", ".", ":(exclude).beads", ":(exclude).beads.gate.lock",
             # An installed venv can be a symlink in validation checkouts;
             # Git's directory-only .venv/ ignore rule does not match that form.
             ":(exclude,glob)**/.venv"]
    changes = set()
    for args in (
        ["ls-files", "--modified", "--others", "--exclude-standard", "-z"],
        # ls-files --modified compares to the index; include staged-only edits,
        # additions and deletions too. Disable rename folding to remove old paths.
        ["diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD"],
    ):
        output = subprocess.check_output(["git", *args, *paths], cwd=source)
        changes.update(os.fsdecode(name) for name in output.split(b"\0") if name)
    for rel in sorted(changes):
        src, dst = source / rel, checkout / rel
        # Replacing a file with a directory (or a symlink) must not leave the
        # clone's old entry behind, nor follow it while copying the new bytes.
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        elif dst.is_dir():
            shutil.rmtree(dst)
        if src.is_symlink():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(os.readlink(src))
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
