#!/usr/bin/env python3
"""Launch a release calibration run from an exclusive checkout, under a pre-exec source
digest (g-p4ih-srcfence).

WHAT THIS CLOSES
----------------
``calibrate_opening_scores_v2.py`` stamps ``scorer_source_digest``: a SHA-256 over the
on-disk bytes of its ``SCORER_SOURCE_FILES``. Phase 3 revalidates an approved winner
against that digest before applying it, so the digest has to name the code that ACTUALLY
scored. Two gaps no code inside that process can close:

1. THE COMPILE WINDOW. CPython compiles ``calibrate_opening_scores_v2.py`` and everything
   it imports BEFORE any of its top-level statements run. An edit landing in that window
   leaves old compiled code executing while every in-process read — the import snapshot,
   the open fence, the close fence — agrees on the new bytes. By the time our code runs,
   compilation is over; only a hash taken BEFORE the interpreter existed can rule it out.
   This launcher takes that hash and hands it to the child through
   ``GHOSTREPLAY_SCORER_SOURCE_DIGEST``, which the scorer captures at the top of its
   module and compares against its own read (raising ``ScorerSourceUnstableError`` on
   disagreement, stamping ``scorer_source_verified_preexec`` on agreement).
2. CHANGE-AND-REVERT. No digest read at any moment detects bytes that changed and changed
   back. What defends against it is where the run happens: a throwaway ``git worktree`` at a
   private temp path, its hashed files marked 0444, instead of the shared working tree that
   editors, builds, and other agents write to continuously. That is what turns the digest
   from a statement about a MOMENT into a statement about a TREE.

   Read ``exclusive_checkout`` before relying on the word "exclusive": it removes the
   ambient hazard, but a process running as the same user can still reach the tree. A
   literal no-other-writer guarantee needs an OS boundary around the run (container,
   sandbox, separate uid). This launcher does not provide one, and the digest must not be
   described as if it did.
3. STARTUP-HOOK SUBSTITUTION. Both of the above are about BYTES ON DISK. Neither says
   anything about code that never touches the tree: ``site.py`` executes every ``.pth``
   import line in site-packages, and ``sitecustomize``, before the scorer's first byte. Such
   a hook can import a manifest module from the right tree and rebind a function on it — the
   digest matches, the origins check out, and the flag is minted over code it never hashed.
   ``child_command`` runs the child under ``-S`` so none of it auto-executes.

   THE SAME HAZARD APPLIES TO THIS FILE, ONE INTERPRETER EARLIER, and it is worse here: a
   hook that rebinds ``hashlib.sha256`` in the LAUNCHER makes the digest whatever it wants,
   and the digest is the entire product. So the launcher refuses to start unless it was
   itself given ``-I -S`` — see ``require_isolated_launcher``. It cannot arrange that for
   itself; by the time any of its code runs, the hook has already run.

WHAT IS STILL NOT VOUCHED FOR
-----------------------------
The interpreter, the stdlib, and every installed dependency. The digest binds
``SCORER_SOURCE_FILES`` and nothing else, and no in-process check can audit the runtime it is
already inside. This launcher narrows the accidental hazard — the release machine also running
editors, agents, and instrumentation — and is not a defence against a hostile runtime or a
hostile operator, who could simply commit the change. See ``g-release-os-boundary``.

WHY IT IS A SEPARATE PROGRAM
----------------------------
A process cannot vouch for its own compilation, so the vouching code must not be the
vouched-for code: this file imports nothing from ``app.*`` and nothing from the scorer, and
its own bytes are deliberately NOT in the manifest it hashes. It reads the manifest by
PARSING the checkout's scorer source (never importing it) so the two digest constructions
cannot drift apart.

USAGE
-----
    backend/scripts/release_calibration.sh [--rev REV] -- [SCRIPT ARGS...]

or, equivalently and explicitly::

    python -I -S backend/scripts/release_calibration_launcher.py [--rev REV] -- [SCRIPT ARGS...]

``-I -S`` IS NOT OPTIONAL and is not decoration on the command line: without it this process
is contaminated before it hashes anything, and it refuses to run (require_isolated_launcher).
Use the interpreter whose environment has the scorer's dependencies — the child inherits
``sys.executable`` from this process, and its deps are derived from that interpreter's venv.

``--rev`` (default ``HEAD``) is the commit the run executes from. Uncommitted edits in the
working tree are IRRELEVANT by construction — the worktree is checked out from ``rev``, so
a release run can never score bytes that were never committed.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

# Mirrors cal.SCORER_SOURCE_DIGEST_ENV. NOT imported from it: importing the scorer would
# compile the very code this launcher exists to vouch for. Pinned by a test instead.
SCORER_SOURCE_DIGEST_ENV = "GHOSTREPLAY_SCORER_SOURCE_DIGEST"

# The scorer, repo-relative. It is both what we exec and where the manifest is read from,
# and it must appear IN that manifest — see read_manifest.
CALIBRATE_REL = "backend/scripts/calibrate_opening_scores_v2.py"

_MANIFEST_NAME = "SCORER_SOURCE_FILES"


class LauncherError(Exception):
    """The launcher cannot honestly vouch for a run, so it does not start one."""


def manifest_path(tree_root: Path, rel: str) -> Path:
    """``tree_root / rel``, proven to stay INSIDE ``tree_root``.

    Everything the checkout buys rests on the hashed bytes living in the checkout. A
    committed symlink pointing out of the tree would be hashed and imported from storage the
    worktree does not contain — externally writable, and change-and-revert is back. So the
    path is resolved (which follows every link in it) and required to land under the tree.
    A link that stays inside the tree is fine: those bytes are still in the checkout.
    """
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts:
        raise LauncherError(f"manifest path {rel!r} is not a repo-relative path")
    root = tree_root.resolve()
    resolved = (tree_root / pure).resolve()
    if resolved != root and root not in resolved.parents:
        raise LauncherError(
            f"manifest path {rel!r} resolves to {resolved}, outside the checkout {root} — "
            "the digest would bind bytes the exclusive checkout does not contain"
        )
    return resolved


def read_manifest(tree_root: Path) -> tuple[str, ...]:
    """The ``SCORER_SOURCE_FILES`` of the scorer IN ``tree_root``, read by parsing its
    source — never by importing it.

    Two reasons it is parsed out of the checkout rather than duplicated here. Importing the
    scorer would compile it (and every ``app.*`` module) in THIS process, which is the exact
    thing a pre-exec hash exists to precede. And a copy of the tuple in this file would be a
    second source of truth: it would silently stop covering a file the scorer added, and the
    digest would then be missing bytes the child folds in — a mismatch that fails the run
    closed at best, and hashes an incomplete manifest at worst.
    """
    source = manifest_path(tree_root, CALIBRATE_REL)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise LauncherError(f"cannot read the scorer at {source}: {exc}") from exc
    for node in ast.parse(text).body:  # module level only: a nested rebind is not the manifest
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == _MANIFEST_NAME for t in targets):
            continue
        if node.value is None:
            continue
        try:
            value = ast.literal_eval(node.value)
        except ValueError as exc:
            raise LauncherError(
                f"{_MANIFEST_NAME} in {CALIBRATE_REL} is not a literal — the launcher must "
                f"read the manifest without executing the scorer: {exc}"
            ) from exc
        if not isinstance(value, tuple) or not value:
            raise LauncherError(f"{_MANIFEST_NAME} in {CALIBRATE_REL} is not a non-empty tuple")
        if not all(isinstance(rel, str) for rel in value):
            raise LauncherError(f"{_MANIFEST_NAME} in {CALIBRATE_REL} is not a tuple of str")
        if CALIBRATE_REL not in value:
            # The manifest is read FROM this file, so a manifest that does not bind this
            # file lets the bytes that define the binding change unbound.
            raise LauncherError(
                f"{_MANIFEST_NAME} does not include {CALIBRATE_REL} — the scorer's own bytes "
                "must be part of the binding it declares"
            )
        return value
    raise LauncherError(f"no module-level {_MANIFEST_NAME} found in {CALIBRATE_REL}")


def manifest_digest(tree_root: Path) -> str:
    """SHA-256 over, for each path in manifest order, ``path`` + NUL + on-disk bytes + NUL.

    Byte-identical construction to the scorer's ``scorer_source_digest()`` — that is the
    whole point, and ``test_launcher_digest_matches_the_scorers_own_construction`` pins it.
    """
    manifest = read_manifest(tree_root)
    h = hashlib.sha256()
    for rel in manifest:
        path = manifest_path(tree_root, rel)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise LauncherError(f"manifest file {rel} is unreadable in {tree_root}: {exc}") from exc
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
    return h.hexdigest()


def _git(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise LauncherError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


@contextmanager
def _manifest_read_only(tree: Path) -> Iterator[None]:
    """Drop the write bits on the HASHED files for the duration, then restore them exactly.

    Scoped to the manifest on purpose. The guarantee is about the bytes the digest binds, so
    those are what must not move; the rest of the checkout is not hashed and its writability
    changes nothing. Hardening the whole tree instead would be security theatre with a real
    cost: the run legitimately writes inside the checkout (app.opening_graph caches its
    ~30s build under backend/.opening_graph_cache), and a blanket chmod turns that into a
    logged failure and a rebuild on every release run.

    Best-effort: mode bits are a guard against ACCIDENTAL writes, never the proof. The digest
    is the proof, and it is checked whatever these bits say.

    Every entry goes through manifest_path() BEFORE it is touched. The manifest is data read
    out of the checkout, so it is not automatically ours to trust: an entry naming an absolute
    path, a `..`, or a symlinked parent would otherwise have us chmod a file OUTSIDE the
    checkout — one of the operator's own — before anything validated it.

    The modes are captured once, here, and restored to exactly what they were. Restoring a
    hardcoded 0644 instead would silently widen a file that arrived 0600, and re-reading the
    modes on the way out would just read back the 0444 we set.
    """
    original: dict[Path, int] = {}
    for rel in read_manifest(tree):
        path = manifest_path(tree, rel)  # validated + resolved: proven inside the checkout
        if not path.is_file():
            raise LauncherError(
                f"manifest entry {rel!r} is not a regular file — the digest binds file bytes"
            )
        original[path] = path.stat().st_mode & 0o777
    try:
        for path, mode in original.items():
            try:
                path.chmod(mode & ~0o222)
            except OSError:
                pass  # best-effort: the digest is the proof, not the mode bits
        yield
    finally:
        for path, mode in original.items():
            try:
                path.chmod(mode)
            except OSError:
                pass


def _worktree_registered(repo_root: Path, tree: Path) -> bool | None:
    """True / False / None, where None means GIT COULD NOT TELL US.

    Tri-state because the two failure modes are not the same fact. If `git worktree list`
    itself fails, its stdout is empty — and an empty listing read as a bool says "not
    registered", i.e. a broken git would report the cleanup verified. Absence of evidence
    would become evidence of absence, on the exact path whose job is to prove absence.

    COMPARE RESOLVED PATHS, never the raw strings. git prints the worktree's real path, while
    ours comes from tempfile — and on macOS those spell the same directory differently
    (/var/folders/... vs git's /private/var/folders/...), because /var is a symlink. A
    substring test against the unresolved path matches NOTHING here, so every caller would be
    told the registration is gone, always, and the check would be decoration. Resolution is
    non-strict on purpose: the caller that matters most asks after deleting the directory.
    """
    listed = subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    )
    if listed.returncode != 0:
        return None
    target = tree.resolve()
    return any(
        Path(line[len("worktree "):]).resolve() == target
        for line in listed.stdout.splitlines()
        if line.startswith("worktree ")
    )


def _registration_gone(repo_root: Path, tree: Path) -> bool:
    """True only when git AFFIRMED the registration is absent. Unknown is not gone."""
    return _worktree_registered(repo_root, tree) is False


def _remove_worktree(repo_root: Path, tree: Path) -> None:
    """Drop the worktree and its administrative registration, verifying the registration is
    actually gone.

    The temp directory vanishes either way, so a registration left behind points the origin
    repo at a path that no longer exists — with nothing to show for it. ORDER MATTERS on the
    fallback: `git worktree prune` only reclaims entries whose directory is ALREADY GONE, so
    pruning while the tree is still on disk succeeds while removing nothing. Delete first,
    then prune. `--expire now` because a freshly-stale entry is younger than the default
    expiry and would otherwise be kept.
    """
    removed = subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(tree)],
        capture_output=True, text=True,
    )
    if removed.returncode == 0 and _registration_gone(repo_root, tree):
        return
    detail = removed.stderr.strip() or "could not confirm the registration was dropped"
    shutil.rmtree(tree, ignore_errors=True)  # prune is a no-op until this is gone
    subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "prune", "--expire", "now"],
        capture_output=True, text=True,
    )
    if _registration_gone(repo_root, tree):
        print(
            f"[launcher] worktree remove failed ({detail}); prune fallback cleaned it up",
            file=sys.stderr,
        )
    else:
        # Reached when the entry is still listed AND when git would not answer. Both mean the
        # same thing to the operator — this is unverified — so neither gets to pass silently.
        print(
            f"[launcher] could not remove worktree {tree}: {detail}\n"
            f"[launcher] the registration is NOT VERIFIED GONE after the prune fallback — run "
            f"`git -C {repo_root} worktree prune --expire now`",
            file=sys.stderr,
        )


@contextmanager
def exclusive_checkout(repo_root: Path, rev: str) -> Iterator[Path]:
    """A detached ``git worktree`` of ``rev`` at a fresh temp path, with the hashed files
    read-only for the duration of the run, removed on exit.

    WHAT THIS IS AND IS NOT. It defeats the realistic threat, which is ACCIDENT and
    CONCURRENCY, not a determined attacker: the shared working tree this repo hands to
    editors, builds, and other agents is continuously written, and a release run must not
    score bytes that something else can move underneath it. A temp path nothing is pointed
    at, checked out from a commit with its manifest files marked 0444, gives that.

    It is NOT an airtight boundary, and the digest must not be described as if it were:

    * 0700 excludes other UNIX USERS, not other processes running as this user. Anything
      with this uid can chmod the tree back and write to it.
    * The path is not secret. ``git worktree list`` on the origin repo publishes it for as
      long as the run lasts.
    * Only the MANIFEST FILES are marked read-only, and deliberately so (see
      _manifest_read_only): the rest of the checkout stays writable because the run needs it,
      and its bytes are not what the digest binds.

    So a same-uid process that goes looking can still change-and-revert a manifest file
    between the hash and the import. Closing THAT needs a real OS boundary — a container,
    a sandbox, a separate uid — around the whole run. What is here removes the ambient
    hazard and makes any remaining write deliberate rather than incidental.
    """
    parent = Path(tempfile.mkdtemp(prefix="ghostreplay-release-"))
    parent.chmod(0o700)
    tree = parent / "tree"  # git worktree add requires a path that does not exist yet
    try:
        _git("-C", str(repo_root), "worktree", "add", "--detach", str(tree), rev)
        try:
            with _manifest_read_only(tree):
                yield tree
        finally:
            _remove_worktree(repo_root, tree)
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def child_env(tree_root: Path, digest: str, pycache_dir: Path) -> dict[str, str]:
    """The environment the verified child must run under.

    Three things, each load-bearing:

    * ``GHOSTREPLAY_SCORER_SOURCE_DIGEST`` — the hash taken before the interpreter existed.
      The child compares it against its own read and refuses to proceed on disagreement.
    * ``PYTHONDONTWRITEBYTECODE`` + a fresh, empty ``PYTHONPYCACHEPREFIX`` — the digest binds
      .py SOURCE bytes but the interpreter runs .pyc. Under CPython's default timestamp
      invalidation a cached .pyc is valid whenever the source's (mtime, size) match its
      header, so a same-size edit with the mtime preserved runs OLD bytecode while the digest
      hashes the NEW file. The child refuses to certify unless writing is off AND no manifest
      module was servable from unverified cache; an empty prefix directory is how every
      verdict comes back "absent". Writing must be off for the same reason the prefix must be
      empty: with writing on, CPython caches the scorer module before its body runs, after
      which "freshly compiled" and "loaded from a stale .pyc" are indistinguishable.
    * EVERY INHERITED ``PYTHON*`` VARIABLE IS DROPPED, and only the four above are added
      back. Not a denylist of the dangerous-looking ones — an allowlist, because the
      denylist was wrong. ``PYTHONHOME`` (relocate the stdlib) and ``PYTHONPATH`` (resolve
      ``app.*`` back to the shared working tree, unhashed, under a digest describing the
      worktree) are the obvious two, but ``PYTHONWARNINGS`` is the one that proves the point:
      a filter names its category as ``module.Class``, and the interpreter IMPORTS that module
      while installing the filter — before the script body, under ``-S``. Measured against the
      real venv: ``PYTHONWARNINGS=default::sqlalchemy.exc.SAWarning`` had SQLAlchemy imported
      before the child's first line. That is a pre-scorer hook point reached through a
      variable that looks like a logging preference, and it falsifies any claim that deps run
      only when the scorer imports them. The next such variable is not worth guessing at, so
      the child gets only what this function chose to give it.

    The scorer puts its own BACKEND_ROOT at ``sys.path[0]``, so the checkout still wins for
    ``app.*`` — the PYTHONPATH entries exist only to make third-party deps importable under
    ``-S``. Non-PYTHON* variables are inherited on purpose: DATABASE_URL and friends are the
    run's actual configuration.

    Read ``-S`` in child_command for what the startup-hook half of this buys, and
    ``_audited_dep_paths`` for what "audited" is and is not worth.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env[SCORER_SOURCE_DIGEST_ENV] = digest
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPYCACHEPREFIX"] = str(pycache_dir)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(str(p) for p in _audited_dep_paths())
    return env


def require_isolated_launcher(flags: object = sys.flags) -> None:
    """Refuse to vouch for anything unless THIS interpreter started with ``-I -S``.

    The child's ``-S`` is one interpreter too late on its own. Whatever runs the launcher runs
    FIRST: with site initialisation on, every ``.pth`` in site-packages and ``sitecustomize``
    execute before this module imports hashlib, subprocess, or sysconfig — before it reads a
    manifest, before it hashes a byte. A hook that rebinds ``hashlib.sha256`` makes the digest
    whatever it likes, and the digest is the whole product. Measured, not argued: with a .pth
    installed, manifest_digest() returned an attacker-chosen prefix for the real tree.

    So the launcher cannot fix this from the inside, and must not try. Re-exec'ing itself under
    ``-I -S`` would be theatre — the tampering has already happened by the time this function
    runs, and a hook that can patch hashlib can patch os.execv just as easily. The only honest
    move is to refuse, and to make the entrypoint responsible for starting us correctly.

    ``-S`` is the no-site half. ``-I`` adds the rest of what a vouching process needs: it
    implies ``-E`` (so PYTHONPATH cannot shadow the stdlib modules this file depends on, and
    PYTHONHOME cannot relocate the stdlib wholesale) and ``-s`` (no user site).

    This is not a proof that the runtime is honest — a tampered interpreter reports whatever
    flags it likes, and the stdlib on disk is unhashed either way. It rules out the ambient,
    routine hazard: an instrumentation .pth that some pip install dropped into a shared venv.
    """
    if not getattr(flags, "no_site", False) or not getattr(flags, "isolated", False):
        raise LauncherError(
            "the launcher must run under `-I -S`, and this interpreter did not "
            f"(isolated={getattr(flags, 'isolated', None)}, no_site={getattr(flags, 'no_site', None)}). "
            "Without them, site.py runs .pth files and sitecustomize inside THIS process before "
            "it hashes anything — including code that can replace hashlib — so the digest it "
            "produces would vouch for nothing. Re-run:\n"
            "    python -I -S backend/scripts/release_calibration_launcher.py [--rev REV] -- [ARGS]\n"
            "or use backend/scripts/release_calibration.sh, which does this for you."
        )


def _venv_root() -> Path | None:
    """The venv ``sys.executable`` lives in, or None if this is not a venv interpreter.

    NOT resolved, and that is the entire subtlety: ``venv/bin/python`` is a symlink to the base
    interpreter, so ``Path(sys.executable).resolve()`` lands in the BASE installation and the
    venv disappears. pyvenv.cfg is what distinguishes the two.
    """
    root = Path(sys.executable).parent.parent
    return root if (root / "pyvenv.cfg").is_file() else None


def _audited_dep_paths() -> list[Path]:
    """The dep directories the child gets on PYTHONPATH under ``-S``.

    DERIVED FROM THE VENV, NOT FROM sys.prefix. Under ``-S`` site.py never runs, so it never
    reads pyvenv.cfg, so ``sys.prefix`` is the BASE installation — and a bare
    ``sysconfig.get_paths()`` in an isolated launcher hands back the base interpreter's
    site-packages while the venv's is what the child needs. Measured on this machine: base
    /usr/local/lib/python3.11/site-packages instead of the venv's. That is not a small
    mistake. At best the child fails to import chess and the run dies loudly; at worst the
    base install has a DIFFERENT version of a dependency and the release quietly scores
    against code nobody chose.

    The scheme is forced for the same class of reason: macOS defaults to
    osx_framework_library, whose paths ignore the base we substitute, so it would answer with
    the framework's own site-packages no matter which root we asked about.

    "Audited" is aspirational and the name should not be read as a claim. These bytes are not
    hashed and not vouched for by anything here: the digest binds SCORER_SOURCE_FILES, and a
    dependency (or the interpreter, or the stdlib) that lies is outside what this launcher can
    detect — a concurrent `pip install` into a shared venv changes what runs without touching
    the tree or the digest. What this list buys is narrow, and only holds because ``-S`` stops
    site.py auto-importing and child_env drops every inherited PYTHON* variable: nothing here
    RUNS unless the scorer imports it. That sentence was FALSE when the child merely dropped
    PYTHONHOME — PYTHONWARNINGS imports its filter's category module at startup — so treat it
    as a claim contingent on those two mechanisms, not a property of the directories.
    See g-release-os-boundary.
    """
    venv = _venv_root()
    if venv is None:
        paths = sysconfig.get_paths()  # not a venv: the base install IS the environment
    else:
        schemes = sysconfig.get_scheme_names()
        scheme = "venv" if "venv" in schemes else ("nt" if os.name == "nt" else "posix_prefix")
        paths = sysconfig.get_paths(scheme=scheme,
                                    vars={"base": str(venv), "platbase": str(venv)})
    dep_paths: list[Path] = []
    for key in ("purelib", "platlib"):  # identical in a venv, distinct in some system installs
        path = Path(paths[key])
        if path not in dep_paths:
            dep_paths.append(path)
    missing = [p for p in dep_paths if not p.is_dir()]
    if missing:
        raise LauncherError(
            f"derived dependency paths that do not exist: {missing}. The child runs under -S "
            "and gets its deps only from here, so this would hand it an environment its "
            "imports cannot be satisfied from."
        )
    return dep_paths


def child_command(tree_root: Path, script_args: Sequence[str]) -> list[str]:
    """The child argv: the scorer AS IT EXISTS IN THE CHECKOUT, never the origin copy.

    ``-S`` IS LOAD-BEARING. Without it CPython runs ``site.py`` at startup, which imports
    ``sitecustomize`` and executes every ``import`` line in every ``.pth`` in site-packages —
    all of it before the first byte of the scorer, and none of it named by the digest.
    ``PYTHONNOUSERSITE`` does NOT cover this: it disables the USER site directory only, while
    the vector that matters is the interpreter's own site-packages, where anything pip ever
    installed can drop a ``.pth`` (coverage and debuggers do exactly this, routinely).

    That hook can import a manifest module from the correct tree and then rebind a function on
    it. The digest still matches — the source bytes on disk were never touched — and
    check_scorer_import_origins() still passes, because ``__file__`` is untouched too. The run
    would stamp scorer_source_verified_preexec=True over code the digest never hashed. This is
    demonstrated, not theorised: test_startup_hooks_cannot_run_before_the_scorer pins it.

    ``-S`` does not make the runtime trustworthy — the interpreter, the stdlib, and every
    installed dependency remain unhashed, and a hostile import hook could spoof ``__file__``
    anyway. It removes the auto-execution: under ``-S`` nothing in site-packages runs unless
    the scorer imports it.
    """
    return [sys.executable, "-S", str(tree_root / CALIBRATE_REL), *script_args]


def launch(tree_root: Path, script_args: Sequence[str], *, pycache_dir: Path) -> int:
    """Hash ``tree_root``'s manifest, then run the scorer from it under that digest.

    ORDER IS THE POINT: the digest is computed before the child interpreter exists, so it
    necessarily precedes the compilation of every file it names. Nothing that touches the
    tree may be inserted between the hash and the exec.

    That ordering is the guarantee; the checkout only narrows who could exploit the gap. It
    does not eliminate them — the path is published by ``git worktree list`` and the 0444 is
    reversible by this uid (see exclusive_checkout) — which is exactly why the child
    re-checks the digest instead of trusting that the window was quiet.
    """
    digest = manifest_digest(tree_root)
    pycache_dir.mkdir(parents=True, exist_ok=True)
    if any(pycache_dir.rglob("*.pyc")):
        raise LauncherError(
            f"bytecode cache {pycache_dir} is not empty — a verified run needs a cache "
            "CPython cannot serve the scorer from"
        )
    print(f"[launcher] tree={tree_root} digest={digest[:12]}", file=sys.stderr)
    proc = subprocess.run(
        child_command(tree_root, script_args),
        env=child_env(tree_root, digest, pycache_dir),
        cwd=str(tree_root / "backend"),
    )
    return proc.returncode


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--rev", default="HEAD",
        help="Commit to run from (default HEAD). The run executes from a throwaway worktree "
             "of this rev, so working-tree edits never reach a release run.",
    )
    parser.add_argument(
        "script_args", nargs=argparse.REMAINDER,
        help="Arguments forwarded to calibrate_opening_scores_v2.py (put them after --).",
    )
    args = parser.parse_args(argv)
    if args.script_args and args.script_args[0] == "--":
        args.script_args = args.script_args[1:]
    return args


def main(argv: list[str] | None = None) -> int:
    # FIRST, before anything is read or hashed: a launcher that was started wrong has already
    # been compromised, and everything below it would be vouching with borrowed authority.
    require_isolated_launcher()
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    repo_root = Path(__file__).resolve().parents[2]
    with exclusive_checkout(repo_root, args.rev) as tree:
        # Inside the same private temp parent as the worktree: created fresh, dies with it.
        return launch(tree, args.script_args, pycache_dir=tree.parent / "pycache")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LauncherError as exc:
        print(f"[launcher] refusing to run: {exc}", file=sys.stderr)
        sys.exit(2)
