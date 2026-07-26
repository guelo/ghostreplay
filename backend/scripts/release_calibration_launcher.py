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
   back. What defends against it is where the run happens — and since g-release-os-boundary
   that is a READ-ONLY VOLUME, not merely a private directory.

   ``sealed_checkout`` builds a disk image holding everything the run executes, attaches it
   read-only, and unlinks the backing file. A same-uid process attempting to write any of it
   gets EROFS from the kernel, and gets EROFS after a ``chmod u+w`` that appears to succeed —
   which is the clearest available statement that mode bits were never the mechanism. The
   guarantee holds until the child exits, because the close fence lands after the last score.

   ``exclusive_checkout`` is the older, weaker path and is now reachable only through
   ``--no-boundary``: it removes the AMBIENT hazard (a shared tree that editors and other
   agents write continuously) but a process running as the same user can still reach the
   tree, so a run under it stamps ``scorer_source_verified_preexec=False`` and no release
   will accept it.
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

4. A MUTATED RUNTIME. This is the one that made g-release-os-boundary blocking rather than
   nice-to-have, and no amount of source hashing reaches it: a concurrent ``pip install``
   into a shared venv changes the code that executes without touching the tree, the
   manifest, or the digest. A boundary drawn around the checkout alone would have had a hole
   in the middle of its own threat model.

   So the volume carries the whole execution input: the checkout, the interpreter, the
   standard library and lib-dynload, the installed dependencies, the frozen cohort artifact,
   and every non-Apple dylib in the closure. That last one is not padding — ``otool -L``
   shows the interpreter references ``libpython`` and ``libintl`` by ABSOLUTE PATH into
   ``~/.pyenv`` and ``/opt/homebrew``, so copying the prefix alone still executes mutable
   host code until ``DYLD_LIBRARY_PATH`` redirects it.

   THE STAGING WINDOW IS THE PART THAT NEEDS AN ARGUMENT, because the volume is built by
   COPYING from exactly the mutable trees this hazard is about. ``cp -Rc`` clones each file
   atomically, so no single file is torn — but it walks, so an install landing mid-walk is
   copied ahead of the walk for some packages and behind it for others, and the result is a
   HYBRID that never existed anywhere. Hashing it afterwards names the hybrid precisely,
   which is the trap: the digest looks like proof while describing a runtime nobody
   assembled. A true whole-tree snapshot needs privileges this launcher deliberately does
   not require, so once the image is built, attached and unlinked, every sealed byte is
   COMPARED against what says it should be there, and a volume that disagrees REFUSES the run
   (``_require_sealed_bytes_are_their_sources``).

   WHAT THAT COMPARISON IS WORTH DEPENDS ON THE BASELINE, and the difference is not a
   technicality. The checkout is compared against the COMMIT, the artifact against a
   DESCRIPTOR held open since before it was judged, the provenance record against a digest of
   the bytes this process itself wrote: three immutable baselines, and between them they cover
   the code, the data, and the record naming the cohort. The interpreter, the dependency roots
   and the dylib closure are compared against LIVE HOST TREES, which detects the concurrent
   install this hazard is about but cannot prove a single instant of trees that keep moving.
   Both statements belong here; only one of them is a guarantee.

   AND MATCHING BYTES ARE NOT A CLOSED BOUNDARY. A symlink is content — its target string —
   so a link that matches its baseline exactly, whether committed or copied out of the
   interpreter prefix, passes every comparison above and still lands wherever it says. The
   volume then holds a name whose bytes are off it and stay writable for the whole run. So
   every link on the volume is required to LAND on the volume
   (``_require_symlinks_stay_on_the_volume``); "the right bytes" and "no way out" are two
   questions and the checks for them are separate.

   SIX FAULTS, EACH DEMONSTRATED BEFORE IT WAS FIXED, are why it reads like this. A first
   version compared METADATA — mode, size, mtime, inode — which a same-size edit reverted with
   ``os.utime`` walks straight through. It compared the writable STAGE and then handed that
   same stage to ``hdiutil create``. It reached the sealed side through calls that FOLLOW
   SYMLINKS while checking names only, so a link staged in place of a file was followed off
   the volume and compared equal to the very file it pointed at. It took the checkout's
   baseline from the staging tree, so an edit landing before that read was blessed rather than
   caught. It accepted links that pointed OFF the volume, because they matched. And it checked
   the sealed ``.git`` for being a regular file without checking what it POINTED AT, which let
   the sealed checkout answer as a different repository and quietly emptied the scorer's
   forbidden-root set. Every one of them looked like a working check while it was one.

WHAT IS STILL NOT VOUCHED FOR
-----------------------------
The signed system volume — ``/usr/lib`` and ``/System``, the dyld shared cache. Sealed by the
OS and not writable even by root, taken host-provided by decision, with the OS build RECORDED
on the cohort (``os_build``) so which build is answerable afterwards.

git's administrative directory, which stays in the origin's mutable ``.git``. Git keeps
working from inside the volume (the worktree registration is repointed at the mount), but
nothing it says from in there is an attestation — ``source_revision`` and
``source_dirty_paths`` are AUDIT fields. The revision that IS sealed is the one this launcher
resolves before the checkout exists (``_resolve_revision``), handed over separately. The other
thing that used to be derived from git in there was GOVERNANCE — which working trees a private
result must not be written into — and an edit to ``<admin>/commondir`` took the origin checkout
straight out of that answer. It is no longer derived in there: the set is measured on the host
and carried (``_forbidden_root_identities``), so what git says from inside can only ever ADD to
what is refused.

A same-uid process can still ``hdiutil detach`` the volume. That breaks the run loudly — the
scorer re-measures the boundary after the last score — rather than corrupting it quietly.

And, as ever, a hostile OPERATOR, who can commit the change and have it sealed like anything
else. This whole line of work defends against ACCIDENT and CONCURRENCY on a machine that also
runs editors, agents, and package installs. It is not a defence against the person running the
release, and must never be described as one.

WHY IT IS A SEPARATE PROGRAM, AND WHY IT RUNS TWICE
---------------------------------------------------
A process cannot vouch for its own compilation, so the vouching code must not be the
vouched-for code: this file imports nothing from ``app.*`` and nothing from the scorer, and
its own bytes are deliberately NOT in the manifest it hashes. It reads the manifest by
PARSING the checkout's scorer source (never importing it) so the two digest constructions
cannot drift apart.

It then runs a SECOND time, as ``--inner``, from the mounted volume on the mounted
interpreter. The outer process is host code on a host interpreter — both mutable, both
outside any boundary — so a digest it computed would be a statement made by code something
else could have edited, about bytes that were still writable when they were read. Computing
it inside makes it sealed code, on a sealed interpreter, over sealed bytes: the window
between the hash and the child's import is not narrowed, it is gone.

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

``--mount-cohort-provenance`` is the ONE deliberate exception to that, and it carries DATA,
never code: ``select-release`` must select against the candidate provenance record the
capture run just wrote, which is still an uncommitted working-tree diff at approval time.
See ``mount_cohort_provenance``.

A ``--artifact`` in the SCRIPT ARGS is staged onto the volume and the argument is REWRITTEN
to the sealed copy, so the run scores data the kernel is holding still rather than a file in
a writable private store. One consequence has to be handled here rather than downstream: the
scorer's own governance rule refuses an artifact that lives inside a checkout, and after the
rewrite it would only ever see the staged path. So the OPERATOR'S path is judged first, by
``_refuse_repo_interior_artifact``, before anything is created — and the file is OPENED before
it is judged and copied from that descriptor afterwards (``_hold_artifact``), because a name
checked at one moment and reopened at another is not reliably the same file.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Mirrors cal.SCORER_SOURCE_DIGEST_ENV. NOT imported from it: importing the scorer would
# compile the very code this launcher exists to vouch for. Pinned by a test instead.
SCORER_SOURCE_DIGEST_ENV = "GHOSTREPLAY_SCORER_SOURCE_DIGEST"

# Mirrors cal.COHORT_PROVENANCE_DIGEST_ENV, for the same reason and under the same
# discipline (pinned equal by a test).
COHORT_PROVENANCE_DIGEST_ENV = "GHOSTREPLAY_COHORT_PROVENANCE_SHA256"

# The candidate provenance record, repo-relative. Deliberately NOT in SCORER_SOURCE_FILES
# (a test pins that): it is DATA the run selects against, not code the digest binds, and
# folding it into the manifest would make every capture invalidate the scorer digest.
COHORT_PROVENANCE_REL = "backend/scripts/fixtures/cohort_provenance.json"

# The scorer, repo-relative. It is both what we exec and where the manifest is read from,
# and it must appear IN that manifest — see read_manifest.
CALIBRATE_REL = "backend/scripts/calibrate_opening_scores_v2.py"

_MANIFEST_NAME = "SCORER_SOURCE_FILES"

# --- The OS boundary (g-release-os-boundary) -------------------------------------------
#
# Mirrors of the scorer's constants, under the same discipline as SCORER_SOURCE_DIGEST_ENV
# (not imported — importing the scorer would compile it; pinned equal by tests).
RELEASE_BOUNDARY_ENV = "GHOSTREPLAY_RELEASE_BOUNDARY"
RUNTIME_IMAGE_DIGEST_ENV = "GHOSTREPLAY_RUNTIME_IMAGE_SHA256"
SEALED_REVISION_ENV = "GHOSTREPLAY_SEALED_REVISION"

# The working trees a private path must not land in, as ``(st_dev, st_ino)`` pairs, measured
# on the HOST before the volume exists and handed to the child as a floor it cannot be talked
# below. The scorer derives the same set from ``git worktree list``, and that derivation is
# only as honest as git's administrative directory — which stays writable, outside the
# boundary, by construction. Editing ``<admin>/commondir`` repoints git while the sealed
# ``.git`` file and the admin inode are both untouched, and the origin checkout drops out of
# the set that exists to keep production-derived data out of it. Reproduced before this was
# written. See _forbidden_root_identities.
SEALED_FORBIDDEN_ROOTS_ENV = "GHOSTREPLAY_SEALED_FORBIDDEN_ROOTS"

# app.opening_graph reads this. The disk cache is a pickle validated by (version, two
# mtimes), i.e. the one mutable scoring input a sealed run would otherwise keep — and
# pickle.loads on it is a writable path to arbitrary code execution inside the boundary.
GRAPH_NO_DISK_CACHE_ENV = "GHOSTREPLAY_OPENING_GRAPH_NO_DISK_CACHE"

# The only mechanism implemented today. Named, not boolean, because the scorer records WHICH
# boundary was established and a Linux mechanism is expected to land beside this one.
MACOS_MECHANISM = "macos-hdiutil-udro"

# The outer/inner handshake, declared so the OUTER launcher can check it before spending
# 30s building a volume it cannot use.
#
# THE TWO LAUNCHERS ARE NOT THE SAME FILE. The outer one is whatever the operator invoked;
# the inner one is the copy AT ``--rev``, because the run executes from a REVISION and that
# is the whole point. So they can be different versions, and the interesting direction is a
# new outer against an old rev: the checked-out launcher does not know ``--inner``, argparse
# rejects it, and the operator gets "unrecognized arguments: --inner --sealed-revision" from
# a program they did not knowingly run. Observed on the first real end-to-end run of this
# code, against a HEAD that predated it.
#
# Bump when the argv the outer passes to the inner changes shape.
#
# 2 (g-sealed-gov-roots): ``--forbidden-roots``. It has to be measured on the host, where the
# origin's git state is still the thing being described, and it cannot be re-derived inside —
# so it is passed, and an inner that would silently ignore it must not be handed a run.
INNER_PROTOCOL_VERSION: int = 2

# Volume layout, relative to the mount point. The INNER launcher derives every one of these
# from its own __file__ rather than being told: a launcher that can be TOLD where the sealed
# deps are can be told to look outside the volume, and the layout is the one thing it must
# not take on faith. See _sealed_run_from_self.
_VOLUME_TREE = "tree"
_VOLUME_PYTHON = "py"
_VOLUME_DEPS = "deps"
_VOLUME_DYLIBS = "dylibs"
_VOLUME_INPUTS = "inputs"

# The gitfile a linked worktree carries in place of an administrative directory. Named because
# it is the one entry on the volume that POINTS somewhere, and what it points at decides what
# git tells the child (see _require_sealed_checkout_is_the_commit).
_GITFILE = ".git"


@dataclass(frozen=True)
class SealedRun:
    """What the INNER launcher measured about the read-only volume it is running from.

    Constructed only by _sealed_run_from_self(), i.e. only after every field has been proven
    to sit on a read-only filesystem. Its presence in launch()/child_env() is what separates a
    sealed release run from the --no-boundary dev run that shares the same code path.
    """
    mechanism: str
    runtime_image_sha256: str
    revision: str
    volume: Path
    dep_paths: tuple[Path, ...]
    dylibs: Path
    scratch: Path


def _boundary_mechanism() -> str | None:
    """The OS boundary mechanism available on this platform, or None if there is none.

    THE EXTENSION POINT. Linux would want an unprivileged user + mount namespace holding a
    private tmpfs copy, remounted read-only — genuinely same-uid-proof, because a mount that
    has no path in the parent namespace cannot be reached from it. It is deliberately not
    built here: ubuntu-24.04 restricts unprivileged user namespaces by default via AppArmor,
    so the mechanism is unreliable in exactly the environment that would use it, and it needs
    its own test surface. Adding it later touches this function and _seal_volume, not the
    scorer — the scorer's gate is a measurement of its own filesystem, not a list of
    mechanism names.

    A bind mount of the shared tree would NOT qualify and must not be added here: it makes
    the tree read-only to the CHILD while leaving every other process writing the same
    inodes, which is the opposite of the property this bead exists for.
    """
    return MACOS_MECHANISM if sys.platform == "darwin" else None


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
        # -z for the same reason the scorer's forbidden-root listing uses it: the porcelain
        # path is printed raw, and a directory name may contain a newline that splitlines()
        # would cut in half.
        ["git", "-C", str(repo_root), "worktree", "list", "--porcelain", "-z"],
        capture_output=True,
    )
    if listed.returncode != 0:
        return None
    target = tree.resolve()
    return any(
        Path(os.fsdecode(field[len(b"worktree "):])).resolve() == target
        for field in listed.stdout.split(b"\0")
        if field.startswith(b"worktree ")
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

    NO LONGER THE RELEASE PATH. g-release-os-boundary replaced it with sealed_checkout, and
    this is now reachable only through ``--no-boundary``: a run under it stamps
    ``scorer_source_verified_preexec=False``, so select-release and the Phase-3 preflight
    refuse its output exactly as they refuse a bare run. It stays because dev runs, report
    runs, and platforms with no boundary mechanism still need a launcher — and because the
    honest way to describe what the boundary added is to keep what it was added to.

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
    between the hash and the import. Closing THAT is what sealed_checkout does, by putting
    the bytes on a filesystem the kernel refuses writes to. What is here removes the ambient
    hazard and makes any remaining write deliberate rather than incidental — which is worth
    having for a dev run, and is not worth a release.
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


# --- Sealing the volume (OUTER launcher) -----------------------------------------------


def _run(*args: str, what: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise LauncherError(f"{what} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def _tree_digest(root: Path, *, what: str) -> str:
    """SHA-256 over a whole directory tree: every path, type, mode, link target, and byte.

    THE FRAMING IS INJECTIVE, and it has to be argued rather than assumed. An earlier version
    separated variable-length fields with NUL bytes and nothing else, which is NOT a unique
    encoding: a single file named ``a`` holding the bytes ``X\\0b\\0file:420\\0Y`` fed the hash
    exactly the stream that two files ``a``->``X`` and ``b``->``Y`` did — 0644 is 420 decimal —
    so two different trees shared one digest. A value a winner is BOUND to must name its input,
    so every entry is hashed on its own and folded in as a FIXED-WIDTH 32-byte block, the path
    is LENGTH-PREFIXED, the type tag and mode are fixed-width, and the one variable-length
    field per entry (content, or link target) is terminal. Concatenated fixed-width blocks
    cannot be reparsed, so distinct trees give distinct digests.

    DIRECTORIES ARE ENTRIES TOO, for two reasons. A symlinked directory appears in ``os.walk``
    under ``dirnames``, and ``followlinks=False`` means it is never descended into — so a
    files-only walk did not hash it AT ALL, and its target was invisible to a digest whose
    whole claim is that it covers the tree. And an empty directory is not inert on an import
    path: it is a namespace package, so its presence changes what ``import`` resolves to.

    Symlinks are hashed by TARGET, never followed: following them would hash a file twice under
    two names and, for a link pointing out of the tree, would fold outside bytes into a digest
    whose entire claim is that it covers inside ones.

    An unexpected node type (fifo, socket, device) REFUSES rather than being skipped or opened
    — opening a fifo would block this process forever, and skipping it would leave bytes the
    digest silently does not describe.

    DIRECTORIES ONLY, checked rather than assumed. ``os.walk`` over a file yields nothing, so
    calling this on one would hand back the digest of the empty stream — the same value for
    every file there is, silently. ``_file_digest`` is the single-file case, kept a separate
    function so the two cannot be confused at a call site.
    """
    if not root.is_dir() or root.is_symlink():
        raise LauncherError(
            f"{what}: {root} is not a directory, so it cannot be tree-hashed. Refusing rather "
            "than returning the digest of an empty walk"
        )
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        # A new list: sorting `dirnames` in place above is what makes the DESCENT canonical,
        # and this must not disturb it.
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix().encode("utf-8")
            info = path.lstat()
            entry = hashlib.sha256()
            entry.update(len(rel).to_bytes(8, "big"))
            entry.update(rel)
            if stat.S_ISLNK(info.st_mode):
                entry.update(b"L")
                entry.update(os.readlink(path).encode("utf-8"))
            elif stat.S_ISDIR(info.st_mode):
                entry.update(b"D")
                entry.update((info.st_mode & 0o777).to_bytes(2, "big"))
            elif stat.S_ISREG(info.st_mode):
                entry.update(b"F")
                entry.update((info.st_mode & 0o777).to_bytes(2, "big"))
                with path.open("rb") as handle:
                    while chunk := handle.read(1 << 20):
                        entry.update(chunk)
            else:
                raise LauncherError(
                    f"{path} in {what} is neither a regular file, a directory, nor a symlink, "
                    "so the digest cannot describe it; refusing rather than binding a winner "
                    "to a digest with a hole in it"
                )
            h.update(entry.digest())
    return h.hexdigest()


def _file_digest(path: Path) -> str:
    """SHA-256 of one file's bytes — the single-file half of _tree_digest."""
    h = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _fd_digest(fd: int) -> str:
    """SHA-256 of the bytes behind an OPEN descriptor, re-read from the start.

    The descriptor and not a path, because the point of holding the artifact open is that a
    path is a name something else can repoint between two uses (see _hold_artifact).
    """
    os.lseek(fd, 0, os.SEEK_SET)
    h = hashlib.sha256()
    while chunk := os.read(fd, 1 << 20):
        h.update(chunk)
    return h.hexdigest()


_ENTRY_DIR = "directory"
_ENTRY_FILE = "regular file"


@contextmanager
def _sealed_file(path: Path, *, device: int, what: str) -> Iterator[tuple[int, os.stat_result]]:
    """Open a file that has to BE on the sealed volume, not merely be NAMED on it.

    THE HOLE THIS CLOSES. Every check below used to reach the sealed side through ``stat()``
    and ``open()`` on a path, both of which FOLLOW symlinks, while the entry lists guarding
    them compared NAMES only. A symlink staged where a file was expected therefore passed all
    of them at once: it was found under the right name, followed to the live host file, and
    compared equal to it — by construction, because it WAS it. Reproduced for
    ``inputs/<artifact>`` and, with the identical shape, for ``dylibs/<leaf>``. The volume
    then carried a name whose bytes lived OUTSIDE it, mutable for the whole run, and
    ``runtime_image_sha256`` recorded the link's target STRING as though it were the artifact.

    ``O_NOFOLLOW`` refuses the link rather than following it, and the device check answers the
    question a name cannot: these bytes are on the filesystem the kernel froze. Both, because
    they refuse different things — a link is not the only way to name something elsewhere.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise LauncherError(
            f"{what} at {path} could not be opened on the sealed volume ({exc.strerror}). A "
            "symlink here would be read THROUGH, off the volume and into bytes nothing froze, "
            "so it is refused rather than followed."
        ) from None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise LauncherError(
                f"{what} at {path} is not a regular file, so the bytes behind that name are "
                "not the bytes the volume was sealed with. Refusing."
            )
        if opened.st_dev != device:
            raise LauncherError(
                f"{what} at {path} is on device {opened.st_dev}, and the sealed volume is "
                f"device {device}. The name is on the volume and the file is not. Refusing."
            )
        yield fd, opened
    finally:
        os.close(fd)


def _same_file(sealed: Path, source: Path, *, device: int) -> bool:
    """One sealed file against its live source: mode, and every byte.

    A source that has since VANISHED is a mismatch, not a skip. A deleted library is exactly
    the change being looked for, and treating an unreadable path as "nothing to compare" would
    make deletion the one mutation that passed. A problem on the SEALED side is a different
    thing and is not swallowed here: it raises out of ``_sealed_file`` with its own reason.
    """
    with _sealed_file(sealed, device=device, what=f"the sealed {sealed.name}") as (fd, opened):
        try:
            if opened.st_mode & 0o777 != source.stat().st_mode & 0o777:
                return False
            return _fd_digest(fd) == _file_digest(source)
        except OSError:
            return False


def _require_exact_entries(directory: Path, expected: Mapping[str, str], *, what: str) -> None:
    """This directory holds these names, each of the KIND named, and nothing else.

    The per-subtree digests prove what is INSIDE each staged root. This is what proves nothing
    else was staged ALONGSIDE them — a write into the stage landing where no source maps (a new
    top-level directory, an extra library in ``dylibs/``, a second file in ``inputs/``) would
    otherwise be sealed, hashed, and handed to the child on its import path with nothing having
    ever compared it to anything.

    THE KIND IS CHECKED, not just the name, and the two are not interchangeable. A name-only
    list says ``inputs/`` holds ``cohort.json``; it does not say ``cohort.json`` is a file
    rather than a symlink pointing off the volume at the mutable original. That was the shape
    of the escape (see _sealed_file), and a list that only counts names cannot see it.
    """
    try:
        actual = set(os.listdir(directory))
    except OSError as exc:
        raise LauncherError(f"cannot list {what} at {directory}: {exc.strerror}") from None
    unexpected = sorted(actual - set(expected))
    missing = sorted(set(expected) - actual)
    if unexpected or missing:
        raise LauncherError(
            f"{what} does not hold what was staged into it"
            + (f"; UNEXPECTED: {unexpected}" if unexpected else "")
            + (f"; MISSING: {missing}" if missing else "")
            + ". Something wrote to the stage while the volume was being built, so the volume "
            "holds bytes no source vouches for. Refusing."
        )
    for name, kind in sorted(expected.items()):
        info = (directory / name).lstat()
        actual_kind = (
            _ENTRY_DIR if stat.S_ISDIR(info.st_mode)
            else _ENTRY_FILE if stat.S_ISREG(info.st_mode)
            else "symlink" if stat.S_ISLNK(info.st_mode)
            else "neither a file nor a directory"
        )
        if actual_kind != kind:
            raise LauncherError(
                f"{name} in {what} is a {actual_kind} where a {kind} was staged. A name on the "
                "volume that resolves to something else is a hole the size of whatever it "
                "points at, and it would be followed by every check after this one. Refusing."
            )


def _require_symlinks_stay_on_the_volume(mount: Path, *, device: int) -> list[str]:
    """Every symlink anywhere on the volume must LAND on the volume. Returns the ones that do not.

    THE HOLE THIS CLOSES, and it is a different one from _sealed_file. That function stops a
    symlink from standing IN FOR a sealed file — a name the checks then follow off the volume.
    This stops a symlink from being a legitimate, correctly-identified part of the sealed tree
    that nonetheless POINTS OUT of it. Both comparisons that meet a link do the right thing with
    it and neither notices: ``_tree_digest`` and ``_sealed_blob_id`` hash a link by its TARGET
    STRING and never follow it, so a link with the same target on both sides compares equal — as
    it should, since that IS its content — and a committed link naming an absolute host path
    matches its commit exactly. The volume is then verified, read-only, and carries a door.
    Reproduced both ways before this was written: a committed ``pkg/linked.txt`` pointing at a
    file outside, read THROUGH the sealed checkout, and its content changed afterwards from the
    host while the run was live; and the same shape in ``py/``, where nothing needs committing.

    SO THE RULE IS ABOUT WHERE IT LANDS, not what it says. The target is resolved against the
    link's own directory (``realpath``, so a chain through several links is followed to its end)
    and required to be the mount or under it. A link that lands inside and names nothing is
    allowed: the volume is read-only, so a target that is absent now stays absent. A landing
    that EXISTS is also required to be on the sealed device, because "under the mount point" is
    a statement about spelling and this one has to be about bytes — the same reason
    ``_sealed_file`` compares ``st_dev`` rather than trusting the path it was handed.

    Relative links that stay inside are the common case and are untouched: the interpreter
    prefix is full of them (measured on the prefix this repository runs on: 13, all relative,
    none escaping) and they mean the same thing on the volume that they meant at the source.
    """
    root = mount.resolve()
    escaping: list[str] = []
    for dirpath, dirnames, filenames in os.walk(mount):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            if not path.is_symlink():
                continue
            target = os.readlink(path)
            landing = Path(os.path.realpath(path.parent / target))
            rel = path.relative_to(mount).as_posix()
            if landing != root and root not in landing.parents:
                escaping.append(f"{rel} -> {target} (lands at {landing})")
                continue
            try:
                if os.stat(landing).st_dev != device:
                    escaping.append(f"{rel} -> {target} (lands on another device at {landing})")
            except OSError:
                pass  # absent, and the volume is read-only, so it stays absent
    return escaping


def _commit_blobs(repo_root: Path, revision: str) -> dict[str, tuple[str, str]]:
    """``path -> (git file mode, object id)`` for every blob in a commit.

    ``-z`` because a path is bytes and git QUOTES the awkward ones in its default output: a
    checkout holding a file with a newline or a quote in its name would otherwise parse into
    two paths, or one wrong one, and this listing is exactly what "and nothing else is here"
    gets measured against.
    """
    listing = _git("-C", str(repo_root), "ls-tree", "-r", "-z", revision)
    blobs: dict[str, tuple[str, str]] = {}
    for record in listing.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        mode, kind, oid = meta.split()
        if kind != "blob":
            raise LauncherError(
                f"{path} in {revision} is a git {kind} entry rather than a blob. A submodule "
                "is a second repository this launcher does not stage and cannot vouch for; "
                "refusing rather than sealing a checkout it can only partly describe"
            )
        blobs[path] = (mode, oid)
    return blobs


def _git_mode(info: os.stat_result) -> str:
    """The file mode git would record for this node: git tracks the EXEC BIT and nothing else."""
    if stat.S_ISLNK(info.st_mode):
        return "120000"
    return "100755" if info.st_mode & 0o111 else "100644"


def _sealed_blob_id(path: Path, info: os.stat_result, *, device: int, algorithm: str) -> str:
    """Git's own name for this sealed node's content: ``H(b"blob <len>\\0" + bytes)``.

    Computed here rather than obtained from anywhere, so the comparison is between what the
    VOLUME holds and what the COMMIT holds, with nothing in between that a staging directory
    could have influenced. The algorithm is the repository's object format, asked for rather
    than assumed — a SHA-256 repository names its blobs with 64 hex characters and this has
    to speak whichever one it is told.

    A symlink is hashed by TARGET, which is what git stores for mode 120000, and is never
    followed: following it would hash bytes from off the volume into a value whose whole
    purpose is to say what is on it.
    """
    h = hashlib.new(algorithm)
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path).encode("utf-8")
        h.update(b"blob %d\0" % len(target))
        h.update(target)
        return h.hexdigest()
    with _sealed_file(path, device=device, what="a sealed checkout file") as (fd, opened):
        h.update(b"blob %d\0" % opened.st_size)
        while chunk := os.read(fd, 1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _gitfile_admin(path: Path, *, device: int) -> Path | None:
    """The administrative directory a worktree's ``.git`` FILE names, or None if it names none.

    A linked worktree's ``.git`` is a one-line text file, ``gitdir: <absolute path>``. Read
    through ``_sealed_file`` like everything else, size-capped because this is a pointer and a
    large one is already wrong, and parsed leniently: anything this cannot read as a gitdir line
    comes back as None, which the caller treats as a mismatch rather than as permission.
    """
    with _sealed_file(path, device=device, what=f"the sealed {_GITFILE}") as (fd, opened):
        if opened.st_size > 4096:
            return None
        text = os.read(fd, 4096).decode("utf-8", "replace")
    line, _, _ = text.partition("\n")
    prefix = "gitdir: "
    if not line.startswith(prefix):
        return None
    named = line[len(prefix):].strip()
    return Path(named) if named else None


def _require_sealed_checkout_is_the_commit(
    tree: Path,
    *,
    repo_root: Path,
    revision: str,
    device: int,
    record_digest: str | None,
    admin: Path,
) -> list[str]:
    """The sealed checkout against GIT'S OBJECT STORE — not against the staging copy it was
    made from. Returns every disagreement; raises only when the question cannot be asked.

    WHAT THIS REPLACED, AND WHY. The previous version digested the staging tree the moment
    ``git worktree add`` and the provenance mount finished, and compared the sealed tree
    against that. The baseline was therefore taken FROM THE WRITABLE STAGE, so an edit landing
    between the checkout and the digest was not caught — it was BLESSED, folded into the value
    everything downstream compared against, while ``sealed_revision`` went on naming the
    commit the operator asked for. The gap was small, and it sat in the one place where the
    run states what code it is.

    A commit does not have that problem: it is content-addressed and immutable, git already
    holds the identity of every byte in it, and an editor saving into a worktree cannot reach
    it. So each sealed file is hashed in git's own framing and compared to the object id the
    commit lists, the exec bit (all git tracks of a mode) is compared, and anything the commit
    does not list is a finding rather than a detail — including a DIRECTORY, because an empty
    one on an import path is a namespace package and changes what ``import`` resolves to.

    TWO ENTRIES ARE NOT IN THE COMMIT AND ARE NOT SUPPOSED TO BE, and each is REQUIRED as well
    as permitted — an entry that may be here is not an entry that may be absent, and an earlier
    version only ever checked the commit's own paths for absence, so deleting either one passed.

    The first is the mounted cohort provenance record, which the launcher itself wrote and
    hashed before the child existed — compared here against THAT hash, so its baseline is bytes
    held in this process rather than anything on disk.

    The second is git's ``.git`` file, and WHAT IT POINTS AT IS NOW BOUND. It used to be checked
    only for being a regular file, which was a hole with a reach nothing else on this volume
    has: it is a gitfile, so it decides which repository git answers as from inside the
    checkout. Repointed at a second valid repository — reproduced — this whole function still
    passed, while ``git worktree list`` from the sealed tree stopped naming the origin checkout
    at all. The scorer builds its private-path forbidden-root set from exactly that listing
    (``_private_path_forbidden_roots``), so the redirect would have made the real origin working
    tree an ACCEPTABLE destination for production-derived output — a governance invariant
    turned off by editing one line of text in the stage. It is therefore required to name THIS
    RUN'S administrative entry, compared by ``(st_dev, st_ino)`` rather than by string.

    THAT BINDING IS NOT THE WHOLE OF IT, and the review that asked for it said so: it stops the
    VOLUME redirecting git, and the administrative directory the volume correctly points at is
    still writable. Editing ``<admin>/commondir`` repoints git while this check sees a gitfile
    naming the same admin inode as before — reproduced — and the scorer's forbidden-root set
    loses the origin checkout. What closes THAT is not another check here: it is
    ``_forbidden_root_identities``, measured on the host and carried to the child as a floor
    (g-sealed-gov-roots), so the governance rule no longer rests on what git says from inside.
    Nothing in this function makes git an attestation, and nothing should.

    THE ONE ASSUMPTION, stated because it is checkable: that a checked-out file holds its
    blob's bytes. A repository with an eol or smudge filter breaks that, and would make this
    refuse every run rather than accept a bad one — the safe direction, and a loud one. This
    repository sets no ``text``, ``eol`` or ``filter`` attribute on any path (verified with
    ``git check-attr`` over ``ls-files``).
    """
    algorithm = _git("-C", str(repo_root), "rev-parse", "--show-object-format")
    if algorithm not in ("sha1", "sha256"):
        raise LauncherError(
            f"the repository names its objects with {algorithm!r}, which this check does not "
            "know how to compute; refusing rather than skipping the checkout"
        )
    blobs = _commit_blobs(repo_root, revision)
    # The administrative entry `.git` has to name. Absent means the question cannot be asked,
    # which is not the same as the answer being yes.
    wanted_admin = _path_identity(admin)
    if wanted_admin is None:
        raise LauncherError(
            f"this run's git administrative directory ({admin}) cannot be identified on disk, so "
            f"it cannot be shown that the sealed {_GITFILE} names it; refusing the volume"
        )
    # None means "checked against wanted_admin below"; a string means "must digest to this".
    extras: dict[str, str | None] = {_GITFILE: None}
    if record_digest is not None:
        extras[COHORT_PROVENANCE_REL] = record_digest
        # Whether the commit also carries a record is the operator's business: the mount
        # overwrites it on purpose, so the commit's version is the wrong baseline for it.
        blobs.pop(COHORT_PROVENANCE_REL, None)
    directories = {
        parent.as_posix()
        for path in (*blobs, *extras)
        for parent in PurePosixPath(path).parents
        if parent.as_posix() != "."
    }

    wrong: list[str] = []
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(tree):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(tree).as_posix()
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                if rel not in directories:
                    wrong.append(f"{_VOLUME_TREE}/{rel}/ is a directory {revision} does not have")
                continue
            seen.add(rel)
            if rel in extras:
                expected = extras[rel]
                if expected is None:
                    named = _gitfile_admin(path, device=device)
                    if named is None or _path_identity(named) != wanted_admin:
                        wrong.append(
                            f"{_VOLUME_TREE}/{rel} names {named} where this run's git "
                            f"administrative directory is {admin}; a gitfile decides which "
                            "repository git answers as from inside the volume, and the scorer's "
                            "private-path rule is built from that answer"
                        )
                    continue
                with _sealed_file(path, device=device, what=f"the sealed {rel}") as (fd, _):
                    if _fd_digest(fd) != expected:
                        wrong.append(
                            f"{_VOLUME_TREE}/{rel} is not the record the launcher mounted"
                        )
                continue
            if rel not in blobs:
                wrong.append(f"{_VOLUME_TREE}/{rel} is not in {revision}")
                continue
            mode, oid = blobs[rel]
            if _git_mode(info) != mode:
                wrong.append(
                    f"{_VOLUME_TREE}/{rel} is mode {_git_mode(info)} and {revision} has {mode}"
                )
            elif _sealed_blob_id(path, info, device=device, algorithm=algorithm) != oid:
                wrong.append(f"{_VOLUME_TREE}/{rel} does not hold the bytes {revision} has")
    wrong.extend(
        f"{_VOLUME_TREE}/{rel} is in {revision} and missing from the volume"
        for rel in sorted(set(blobs) - seen)
    )
    # The permitted extras are also the REQUIRED extras. Checking only the commit's paths for
    # absence let `.git` be deleted outright and the volume still pass — the check said "this
    # may be here", and nothing said "and it has to be".
    wrong.extend(
        f"{_VOLUME_TREE}/{rel} has to be on the volume and is missing from it"
        for rel in sorted(set(extras) - seen)
    )
    return wrong


def _require_sealed_bytes_are_their_sources(
    mount: Path,
    *,
    repo_root: Path,
    revision: str,
    dep_paths: Sequence[Path],
    dylib_sources: Mapping[str, Path],
    provenance_digest: str | None,
    artifact: _HeldArtifact | None,
    admin: Path,
) -> None:
    """Everything on the frozen volume, against the thing that says what it should be.

    WHAT THIS REPLACED, AND WHY. The first attempt fingerprinted each staging source's
    METADATA — mode, size, mtime_ns, inode — before and after staging. That is defeated by
    precisely the attack this boundary exists to stop: two same-size edits, staged, then
    reverted with ``os.utime`` restoring ``mtime_ns``, leave both fingerprints identical while
    the volume holds the edited bytes and the source holds the original. Reproduced against
    the old code before this was written. Metadata is a statement about a file's DESCRIPTION;
    only content is a statement about the file.

    IT ALSO RAN ON THE WRONG BYTES. The old check measured the writable stage and then handed
    that same still-writable stage to ``hdiutil create``, so everything it proved was about a
    directory that anything could rewrite immediately afterwards. This runs on the MOUNT,
    after the image is built, attached, and its backing file unlinked — so the bytes compared
    are the bytes the child will execute, on a filesystem the kernel will not let anything
    change, with no window left after the comparison at all.

    THE BASELINES ARE NOT ALL THE SAME STRENGTH, and saying so is the point of this paragraph.

    IMMUTABLE, so the comparison is conclusive: the CHECKOUT is compared against the COMMIT
    (``_require_sealed_checkout_is_the_commit``) — content-addressed, already written, out of
    reach of anything editing a working tree. The ARTIFACT is compared against the descriptor
    ``_hold_artifact`` opened before it was judged and has held open ever since; a name cannot
    be repointed out from under a descriptor. The PROVENANCE RECORD is compared against the
    digest this process took of the bytes it wrote. For the three things a release actually
    stands on — the code, the data, and the record that says which cohort it is — the volume
    either holds them or the run stops.

    LIVE, so the comparison is a detector and not a proof: ``py/``, ``deps/`` and ``dylibs/``
    are host trees, and they are compared against those trees AS THEY ARE NOW. Each file that
    is read matches its source at the instant it is read, so a ``pip install`` or ``brew
    upgrade`` that lands during staging CAN be caught here — that is the hazard this exists for,
    and this is the thing that would notice it. Not WILL be caught: whether it is depends on
    where the install lands relative to a walk this cannot make atomic. It does NOT prove the
    volume is any single instant of those trees, and an earlier version of this docstring
    claimed it did. It was WRONG, and the review that said so demonstrated it: with a source
    moved to A1 while ``a.py`` was read, back to A0, then to B1 while ``b.py`` was read, the
    volume's A1/B1 pair compared equal to a source that began and ended A0/B0 and never held
    both at once. Nothing in user space fixes that — a whole-tree snapshot needs privileges this
    launcher deliberately does not require, and a second pass over the same walk would only be
    the same walk again. So the limit is recorded here instead, where the next person to trust
    this reads it.

    AND SEALED BYTES ARE NOT YET A SEALED BOUNDARY. Everything above is about whether the
    volume holds the right bytes. ``_require_symlinks_stay_on_the_volume`` is about whether the
    volume is the only place the child can get to through it: a link that correctly matches its
    baseline can still land off the volume, so it runs first, and a hole there makes the rest of
    the question academic.
    """
    device = mount.stat().st_dev
    top = {name: _ENTRY_DIR for name in (_VOLUME_TREE, _VOLUME_PYTHON, _VOLUME_DEPS,
                                         _VOLUME_DYLIBS)}
    if artifact is not None:
        top[_VOLUME_INPUTS] = _ENTRY_DIR
    _require_exact_entries(mount, top, what="the sealed volume")
    _require_exact_entries(mount / _VOLUME_DEPS,
                           {str(n): _ENTRY_DIR for n in range(len(dep_paths))},
                           what="the sealed dependency roots")
    _require_exact_entries(mount / _VOLUME_DYLIBS,
                           {leaf: _ENTRY_FILE for leaf in dylib_sources},
                           what="the sealed dylib closure")
    if artifact is not None:
        _require_exact_entries(mount / _VOLUME_INPUTS, {artifact.resolved.name: _ENTRY_FILE},
                               what="the sealed inputs")

    # BEFORE the content comparisons, because it is a different question and the weaker one to
    # fail: those ask whether the volume holds the right bytes, this asks whether the volume is
    # where the child's reads END UP. A link that matches its baseline exactly still lets a read
    # through the volume land on a mutable host file.
    escaping = _require_symlinks_stay_on_the_volume(mount, device=device)
    if escaping:
        listed = "\n  ".join(escaping)
        raise LauncherError(
            "the sealed volume carries symlinks that leave it:\n  "
            f"{listed}\n"
            "Each is a name on a read-only volume whose bytes are somewhere else and stay "
            "writable for the whole run, so what the child reads through it is not what was "
            "sealed and the image digest does not describe it. Refusing. A committed link like "
            "this has to be removed from the revision; one in the interpreter prefix or a "
            "dependency root means that tree is not self-contained and cannot be sealed by "
            "copying."
        )

    # Every mismatch, not just the first: the operator's next move depends on whether one
    # package moved or the whole venv did, and the run is refusing either way so the extra
    # reading costs nothing anybody is waiting on.
    moved: list[str] = _require_sealed_checkout_is_the_commit(
        mount / _VOLUME_TREE, repo_root=repo_root, revision=revision, device=device,
        record_digest=provenance_digest, admin=admin,
    )
    if (_tree_digest(mount / _VOLUME_PYTHON, what="the sealed interpreter")
            != _tree_digest(Path(sys.base_prefix), what="the interpreter prefix")):
        moved.append(f"{_VOLUME_PYTHON}/ <- {sys.base_prefix}")
    for ordinal, dep_path in enumerate(dep_paths):
        if (_tree_digest(mount / _VOLUME_DEPS / str(ordinal), what="a sealed dependency root")
                != _tree_digest(dep_path, what="a dependency root")):
            moved.append(f"{_VOLUME_DEPS}/{ordinal}/ <- {dep_path}")
    for leaf, source in sorted(dylib_sources.items()):
        if not _same_file(mount / _VOLUME_DYLIBS / leaf, source, device=device):
            moved.append(f"{_VOLUME_DYLIBS}/{leaf} <- {source}")
    if artifact is not None:
        sealed = mount / _VOLUME_INPUTS / artifact.resolved.name
        with _sealed_file(sealed, device=device, what="the sealed artifact") as (fd, opened):
            if (opened.st_mode & 0o777 != artifact.mode
                    or _fd_digest(fd) != _fd_digest(artifact.fd)):
                moved.append(f"{_VOLUME_INPUTS}/{artifact.resolved.name} <- {artifact.resolved}")
    if not moved:
        return
    listed = "\n  ".join(moved)
    raise LauncherError(
        "the sealed volume does not match what it is supposed to hold:\n  "
        f"{listed}\n"
        "The volume holds a mix of before and after — a runtime that never existed as a whole "
        "— and its digest would name that mix as if it were a release runtime. Refusing. Wait "
        "for any concurrent install, build or editor save to finish, then re-run."
    )


def _clone_tree(source: Path, destination: Path) -> None:
    """Copy a directory tree into the stage, preferring APFS clonefile (``cp -Rc``).

    Speed is the lesser reason. ``cp -c`` is COPY-ON-WRITE, so each file is cloned as an
    instantaneous snapshot: a write landing in the venv a millisecond later rewrites the
    ORIGINAL's blocks and leaves the clone's untouched. A byte-for-byte copy of 646MB of
    site-packages takes seconds, and a concurrent write during those seconds can yield a TORN
    FILE — half the old version, half the new — which is a worse input than either version.

    WHAT THE CLONE DOES NOT BUY, stated because an earlier version of this docstring claimed
    it did: per-file atomicity is not tree atomicity. This walks, so an install landing
    mid-walk still produces a hybrid TREE. _require_sealed_bytes_are_their_sources is what
    covers that, and it is also what makes the non-clone fallback below safe to keep.

    ``-R`` (never ``-L``) so symlinks are copied AS symlinks: the interpreter prefix is full
    of them, and following them would both explode the size and silently flatten a link that
    points outside the tree into a copy of whatever it pointed at.

    ``-p`` because the copy has to be COMPARABLE to its source, not merely equivalent to it.
    Without it the destination's modes come from the process umask: measured, ``cp -Rc`` under
    ``umask 077`` turns a 0705 directory into 0700 while preserving file modes exactly, so the
    volume would differ from the source in a way that depends on an ambient setting nobody
    passed. Under the usual 022 nothing changes; under anything stricter the source comparison
    would refuse a run that was in fact clean.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    cloned = subprocess.run(["cp", "-Rpc", str(source), str(destination)],
                            capture_output=True, text=True)
    if cloned.returncode == 0:
        return
    # Not an error: clonefile needs both ends on the same APFS volume, and a release machine
    # may legitimately have TMPDIR elsewhere. Fall back to a real copy, which is slower and
    # loses per-file atomicity too — a tear it introduces leaves the staged file unequal to
    # its source, so _require_sealed_bytes_are_their_sources catches it and the run refuses.
    _run("cp", "-Rp", str(source), str(destination), what=f"copying {source} into the stage")


_MACHO_MAGIC = frozenset({
    b"\xcf\xfa\xed\xfe",  # 64-bit little-endian (arm64, x86_64)
    b"\xce\xfa\xed\xfe",  # 32-bit little-endian
    b"\xca\xfe\xba\xbe",  # universal ("fat")
    b"\xbe\xba\xfe\xca",  # universal, byte-swapped
})


def _macho_files(root: Path) -> list[Path]:
    """Every regular file under ``root`` whose first four bytes say Mach-O.

    By MAGIC, not by extension. A loadable image is not required to be named ``*.so`` or
    ``*.dylib`` — the interpreter itself is neither — and a dylib this misses is a dylib whose
    dependencies never get staged, which is precisely the hole that makes a boundary
    decorative. Cheap enough to be exhaustive: a 4-byte read over ~32k files.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                with path.open("rb") as handle:
                    if handle.read(4) in _MACHO_MAGIC:
                        found.append(path)
            except OSError:
                continue
    return found


def _linked_dylibs(binaries: Sequence[Path]) -> set[str]:
    """The ABSOLUTE install names every one of ``binaries`` loads, via ``otool -L``.

    ``@rpath`` / ``@loader_path`` / ``@executable_path`` entries are deliberately NOT resolved
    here. Resolving them means reimplementing dyld's search order — LC_RPATH chains, fallback
    paths, the shared cache — and a reimplementation that is subtly wrong fails SILENTLY, by
    staging the wrong file or nothing at all. In practice they resolve inside the wheel that
    ships them (numpy, psycopg_binary and friends put their private copies in ``.dylibs/``),
    which travels with the deps clone anyway. Completeness is proven at RUNTIME instead, by
    the scorer enumerating every image it actually loaded — see check_execution_boundary().
    Static staging is best effort; the enumeration is the proof.
    """
    names: set[str] = set()
    batch: list[Path] = []

    def drain() -> None:
        if not batch:
            return
        # otool exits non-zero on a file it cannot parse; one bad file must not lose the
        # whole batch, so failures fall through to the per-file retry below.
        listed = subprocess.run(["otool", "-L", *[str(p) for p in batch]],
                                capture_output=True, text=True)
        if listed.returncode != 0 and len(batch) > 1:
            for one in batch:
                names.update(_linked_dylibs([one]))
            return
        for line in listed.stdout.splitlines():
            if not line.startswith(("\t", " ")):
                continue  # a "<file>:" header, not a dependency
            name = line.strip().split(" (compatibility", 1)[0].strip()
            if name.startswith("/"):
                names.add(name)

    for binary in binaries:
        batch.append(binary)
        if len(batch) >= 200:  # well inside ARG_MAX, few enough processes to be fast
            drain()
            batch = []
    drain()
    return names


# /usr/lib and /System are the dyld shared cache on the SIGNED SYSTEM VOLUME: cryptographically
# sealed by the OS, not writable by root, and not present on disk as ordinary files at all.
# They are the one thing this boundary accepts host-provided, and the OS BUILD is recorded on
# the cohort so "which /usr/lib" is answerable after the fact. Kept in sync with the scorer's
# copy by a test.
SEALED_SYSTEM_PREFIXES = ("/usr/lib/", "/System/")


def _stage_dylib_closure(stage: Path, dylibs: Path) -> dict[str, Path]:
    """Copy every non-system dylib the staged binaries reference, transitively, into a FLAT
    ``dylibs/`` directory the child gets as DYLD_LIBRARY_PATH.

    Returns staged leaf name -> SOURCE PATH, which is what
    _require_sealed_bytes_are_their_sources needs in order to say what each sealed library is
    supposed to be a copy of. These sources are DISCOVERED rather than known up front — the
    closure is transitive — so unlike the interpreter prefix and the dependency roots, nothing
    upstream could have named them. A ``brew upgrade gettext`` mid-run is the same hazard as a
    ``pip install``, one file wide.

    Flat because that is how DYLD_LIBRARY_PATH works: dyld takes the LEAF NAME of a requested
    install path and looks for it in each listed directory, which is exactly what lets a copy
    inside the volume win over an absolute path pointing at ``/opt/homebrew`` or ``~/.pyenv``.

    Two leaves with the same name from different directories therefore cannot both be honoured,
    and the run REFUSES rather than picking one — a silent pick would load one library under
    another's name, which is worse than not starting.
    """
    dylibs.mkdir(parents=True, exist_ok=True)
    if shutil.which("otool") is None:
        raise LauncherError(
            "otool is not available, so the non-system dylib closure cannot be staged. The "
            "interpreter loads libpython and libintl by ABSOLUTE PATH, so without this the "
            "run would execute mutable host code from outside the boundary. Install the "
            "Xcode command line tools (xcode-select --install)."
        )
    staged: dict[str, Path] = {}
    # RESOLVED, because everything compared against it below is resolved: mkdtemp hands back
    # /var/folders/... while a resolved path spells the same directory /private/var/folders/...
    # An unresolved compare would call every file already inside the stage "outside" it and
    # copy the whole volume into dylibs/.
    stage_root = stage.resolve()
    pending = _linked_dylibs(_macho_files(stage))
    while pending:
        source = Path(pending.pop())
        if source.as_posix().startswith(SEALED_SYSTEM_PREFIXES):
            continue
        try:
            resolved = source.resolve()  # /opt/homebrew/opt/X is a symlink into ../Cellar/X
        except OSError:
            continue
        if resolved.is_relative_to(stage_root):
            continue  # already inside the volume-to-be; it will be sealed with everything else
        if not resolved.is_file():
            continue
        leaf = source.name
        previous = staged.get(leaf)
        if previous is not None:
            if previous == resolved:
                continue
            raise LauncherError(
                f"two different libraries are both named {leaf!r} ({previous} and {resolved}). "
                "DYLD_LIBRARY_PATH resolves by leaf name, so only one of them could be "
                "redirected into the boundary and the other would load from the host — "
                "refusing rather than silently loading one under the other's name"
            )
        destination = dylibs / leaf
        shutil.copy2(resolved, destination)
        staged[leaf] = resolved
        # Transitive: the copy has its own absolute references (libssl needs libcrypto).
        pending.update(_linked_dylibs([destination]))
    return staged


_LAUNCHER_REL = "backend/scripts/release_calibration_launcher.py"
_PROTOCOL_NAME = "INNER_PROTOCOL_VERSION"


def read_inner_protocol_version(tree_root: Path) -> int | None:
    """The ``INNER_PROTOCOL_VERSION`` the launcher IN ``tree_root`` declares, or None.

    Read by PARSING, never by importing, for the same reason read_manifest is: importing the
    checked-out launcher would execute code from the revision inside the process that is
    supposed to be vouching for it, one interpreter before the boundary exists.

    None means the revision predates the sealed protocol entirely — it has no such constant.
    """
    try:
        text = (tree_root / _LAUNCHER_REL).read_text(encoding="utf-8")
    except OSError as exc:
        raise LauncherError(f"cannot read the launcher in the checkout: {exc}") from None
    for node in ast.parse(text).body:
        targets = (
            [node.target] if isinstance(node, ast.AnnAssign)
            else node.targets if isinstance(node, ast.Assign)
            else []
        )
        if not any(isinstance(t, ast.Name) and t.id == _PROTOCOL_NAME for t in targets):
            continue
        if node.value is None:
            continue
        with suppress(ValueError):
            value = ast.literal_eval(node.value)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def _require_compatible_inner(tree_root: Path, rev: str) -> None:
    """Refuse a revision whose launcher cannot be the inner half of this run.

    BEFORE the volume is built, not after: staging the interpreter and the dependency tree
    costs ~30s, and every second of it would be spent on a run that dies at the handshake.
    """
    declared = read_inner_protocol_version(tree_root)
    if declared == INNER_PROTOCOL_VERSION:
        return
    if declared is None:
        raise LauncherError(
            f"the launcher at rev {rev} predates the OS boundary (g-release-os-boundary): it "
            f"declares no {_PROTOCOL_NAME}, so it cannot run as the sealed inner launcher. "
            "The run executes from the REVISION, so the boundary has to exist there — commit "
            "or choose a --rev that contains it. To run this rev anyway, pass --no-boundary; "
            "the result will stamp scorer_source_verified_preexec=False and no release will "
            "accept it."
        )
    raise LauncherError(
        f"the launcher at rev {rev} declares {_PROTOCOL_NAME}={declared}, but this one speaks "
        f"{INNER_PROTOCOL_VERSION}. The two halves exchange a fixed argv, so a mismatch would "
        "fail at the handshake after the volume was already built."
    )


def _resolve_revision(repo_root: Path, rev: str) -> str:
    """``rev`` as a 40-hex commit, resolved in the ORIGIN repo before anything is staged.

    THE SEALED REVISION. Git inside the volume keeps working (the worktree registration is
    repointed at the mount), but its administrative directory stays OUTSIDE the boundary, in
    the origin's mutable ``.git`` — so nothing git says from inside is sealed, and the scorer
    treats it as audit-only. This value is resolved out here, from the origin, before the
    checkout exists, and handed in; it is the one revision fact the attestation trusts.
    """
    resolved = _git("-C", str(repo_root), "rev-parse", f"{rev}^{{commit}}")
    if len(resolved) != 40 or not all(c in "0123456789abcdef" for c in resolved):
        raise LauncherError(f"could not resolve {rev!r} to a commit in {repo_root}")
    return resolved


def _require_read_only(path: Path, what: str) -> int:
    """``path``'s device id, having proven its filesystem is mounted READ-ONLY.

    ``ST_RDONLY`` is the whole point of choosing this mechanism. Mode bits are advisory
    against the same uid — the owner reverts them — but a read-only MOUNT is enforced by the
    kernel for every process regardless of uid, and unlike a sandbox profile it is
    OBSERVABLE FROM INSIDE. That is what lets the scorer gate on a measurement instead of on
    a launcher's promise.
    """
    try:
        status = os.statvfs(path)
    except OSError as exc:
        raise LauncherError(f"cannot stat the filesystem holding {what} ({path}): {exc}") from None
    if not status.f_flag & os.ST_RDONLY:
        raise LauncherError(
            f"{what} ({path}) is not on a read-only filesystem, so the sealed run would be "
            "sealed in name only"
        )
    return os.stat(path).st_dev


def _detach_image(mount: Path) -> None:
    detached = subprocess.run(["hdiutil", "detach", str(mount)], capture_output=True, text=True)
    if detached.returncode == 0:
        return
    # Busy is the normal reason (a straggling child, Spotlight). -force is safe here: the
    # volume is read-only, so nothing can be lost by tearing it down.
    forced = subprocess.run(["hdiutil", "detach", "-force", str(mount)],
                            capture_output=True, text=True)
    if forced.returncode != 0:
        print(
            f"[launcher] could not detach the sealed volume at {mount}: "
            f"{(forced.stderr or detached.stderr).strip()}\n"
            f"[launcher] run `hdiutil detach -force {mount}` to reclaim it",
            file=sys.stderr,
        )


def _require_main_worktree(repo_root: Path) -> None:
    """Refuse unless ``repo_root`` is git's MAIN working tree.

    ``repo_root`` is derived from ``__file__``, i.e. it is whichever checkout the operator
    happened to launch from. For everything else that is harmless — ``worktree add`` reaches
    the same object store from any of them, and the run executes from a REVISION either way.
    For the mount it is not: the record being mounted is UNCOMMITTED, so it exists in exactly
    one checkout, the one the capture run wrote it in. Launched from a linked worktree, the
    mount would instead pick up whatever that worktree has committed at
    ``COHORT_PROVENANCE_REL`` — a STALE record — and a stale record paired with its own
    matching artifact passes every downstream trust gate. The approval would look clean while
    describing a cohort nobody captured today.

    So the mismatch is refused rather than papered over by reading from the main worktree
    behind the operator's back: if the capture record is not in the checkout they are
    standing in, the run they are about to approve is not the one they think it is.

    ``--git-dir == --git-common-dir`` IS the main-worktree test; a linked worktree's git dir
    is ``<common>/worktrees/<name>``. Fails closed — ``_git`` raises on a non-zero git.
    """
    lines = _git(
        "-C", str(repo_root), "rev-parse", "--path-format=absolute",
        "--git-dir", "--git-common-dir",
    ).splitlines()
    if len(lines) != 2:
        raise LauncherError(
            "could not determine whether this is git's main working tree, so the candidate "
            "provenance record cannot be mounted (it exists only as an uncommitted edit in "
            "the checkout the capture ran in)"
        )
    if Path(lines[0].strip()).resolve() != Path(lines[1].strip()).resolve():
        raise LauncherError(
            f"{repo_root} is a LINKED git worktree, not the main checkout. "
            "--mount-cohort-provenance carries the UNCOMMITTED record the capture run just "
            "wrote, which lives in exactly one checkout; from here it would mount whatever "
            "this worktree has COMMITTED instead — a stale record that, paired with its own "
            "artifact, passes every trust gate. Run the release launcher from the checkout "
            "the capture ran in."
        )


def mount_cohort_provenance(repo_root: Path, tree: Path) -> str:
    """Copy the ORIGIN WORKING TREE's cohort provenance record into ``tree`` and return the
    SHA-256 of the bytes copied.

    WHY A MOUNT AND NOT A ``--provenance-record`` OPTION. Approval happens BEFORE the record
    is committed, so at selection time the candidate record exists only as an uncommitted
    working-tree diff in the origin checkout — while the run itself executes from a worktree
    checked out from a REVISION, where ``COHORT_PROVENANCE_PATH`` resolves to the OLD
    committed record (or to nothing). The record cannot reach selection by itself. It must
    also not reach it by a path the operator names: the scorer's ``build_selection_inputs``
    treats the record as a TRUST BOUNDARY precisely because a caller-supplied path lets a
    caller pass an unapproved artifact plus a freshly generated matching record. The mount
    keeps the operator out of the naming, and the bytes are hashed HERE — before the child
    interpreter exists — exactly like the manifest digest.

    The ORIGIN working tree means git's MAIN working tree, and that is CHECKED, not assumed:
    see _require_main_worktree for why a linked-worktree launch would otherwise mount a
    stale record that passes every gate.

    Both ends go through ``manifest_path``, so the same "resolves inside the tree" proof the
    hashed files get covers the record's source and destination. ``os.replace`` onto the
    destination overwrites whatever the checkout committed there; clobbering is correct HERE
    and only here, because the destination is inside a throwaway worktree.
    """
    _require_main_worktree(repo_root)
    source = manifest_path(repo_root, COHORT_PROVENANCE_REL)
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise LauncherError(
            f"cannot read the candidate provenance record {COHORT_PROVENANCE_REL} in the "
            f"origin working tree: {exc.strerror}. --mount-cohort-provenance carries the "
            "record the capture run just wrote; run capture-cohort first, or drop the flag"
        ) from None
    digest = hashlib.sha256(data).hexdigest()
    destination = manifest_path(tree, COHORT_PROVENANCE_REL)
    temp = destination.with_name(f"{destination.name}.mount-{os.getpid()}")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(temp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, destination)
        except BaseException:
            with suppress(OSError):
                os.unlink(temp)
            raise
        destination.chmod(0o444)
        written = destination.read_bytes()
    except OSError as exc:
        raise LauncherError(
            f"cannot mount {COHORT_PROVENANCE_REL} into the checkout: {exc.strerror}"
        ) from None
    if hashlib.sha256(written).hexdigest() != digest:
        raise LauncherError(
            f"the mounted {COHORT_PROVENANCE_REL} does not hash to the bytes read from the "
            "origin working tree — refusing to hand the child a digest it cannot honour"
        )
    return digest


@dataclass(frozen=True)
class SealedVolume:
    """Where the OUTER launcher put everything, once the volume is mounted.

    Paths only. The PROOF that they are read-only is deliberately not taken here and not
    passed along: the outer launcher runs on the host, from mutable code, and a claim it
    makes about its own work is worth nothing. Every one of these is re-derived and
    re-measured from inside — see _sealed_run_from_self.
    """
    parent: Path
    mount: Path
    tree: Path
    interpreter: Path
    pycache: Path
    scratch: Path
    revision: str
    mechanism: str
    provenance_digest: str | None
    script_args: tuple[str, ...]


def _artifact_argument(script_args: Sequence[str]) -> tuple[int | None, str | None]:
    """Locate ``--artifact`` in the operator's arguments: (index of the VALUE token, value).

    ``--artifact=X`` and ``--artifact X`` are both recognised, and the LAST occurrence wins,
    because that is what argparse does downstream — a checker that inspected the first and a
    stager that rewrote the last would disagree about which file the release ran against, and
    the disagreement would be silent. Shared by the governance check and the stager for
    exactly that reason.
    """
    index: int | None = None
    value: str | None = None
    for position, token in enumerate(script_args):
        if token == "--artifact" and position + 1 < len(script_args):
            index, value = position + 1, script_args[position + 1]
        elif token.startswith("--artifact="):
            index, value = position, token.split("=", 1)[1]
    return index, value


def _path_identity(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` — the filesystem's own name for a path, or None if it is absent.

    Not a resolved string. This repository lives on a case-insensitive filesystem, where
    ``/users/...`` and ``/Users/...`` are one file that compares unequal, and identity closes
    hard links and bind mounts in the same move. The scorer's ``_path_identity`` is the same
    rule; see _refuse_repo_interior_artifact for why both exist.
    """
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


@dataclass(frozen=True)
class _HeldArtifact:
    """The operator's ``--artifact``: judged, and HELD OPEN from the judgement to the copy.

    ``fd`` is the entire point. A path is a name something else can repoint; a descriptor is
    the file. See _hold_artifact.
    """
    index: int
    fd: int
    resolved: Path
    mode: int


@contextmanager
def _hold_artifact(repo_root: Path, script_args: Sequence[str]) -> Iterator[_HeldArtifact | None]:
    """Open the operator's ``--artifact`` ONCE, judge what was opened, and hold it open.

    THE HOLE THIS CLOSES, which is not the one _refuse_repo_interior_artifact closes. Checking
    a path and then reopening it by name to copy it are two different files whenever anything
    can write the namespace in between — and "in between" here is not an instant: a revision is
    resolved, a temp directory is made, a worktree is added and a provenance record is mounted
    between the two. Demonstrated against the previous version: a symlink pointing outside
    every checkout passed the governance check, was retargeted at a file in this repository,
    and that file was staged onto the volume and sealed. So the descriptor is opened FIRST, and
    everything after it — the judgement, the copy, and the verification of the sealed copy —
    refers to that descriptor and never to the name again.

    Binding the judgement to the descriptor takes one more step, because the rule is about
    where the file LIVES and a descriptor has no parents. So the path is resolved, opened with
    ``O_NOFOLLOW`` (the resolved name is by construction not a symlink; if it is one now,
    something changed it underneath and the run refuses), and the opened inode is required to
    be the inode that path names. Anyone wanting the copy to read a file elsewhere has to make
    the JUDGED path name that file at the moment it is judged — which is the honest question,
    and the one the rule below answers.

    ONE NAME ONLY, via ``st_nlink``, and it is ELIGIBILITY HYGIENE rather than a guarantee — a
    distinction worth keeping straight, because the two look alike from the outside. A second
    hard link to the same bytes could sit inside a checkout while the name given here sits
    outside it, and no check of one path can see the other, so an artifact with more than one
    name is refused as INELIGIBLE rather than judged on whichever name it was offered under. It
    does not make anything durable: nothing stops a second link being created a moment later,
    and what defends the bytes is the descriptor above, not the count. Refusing a
    hardlink-deduplicated store is a real cost, and the right trade for fail-closed release
    tooling on those terms and no stronger ones.
    """
    index, value = _artifact_argument(script_args)
    if index is None or value is None:
        yield None
        return
    if not Path(value).is_absolute():
        raise LauncherError(
            f"--artifact {value!r} must be an ABSOLUTE path: the launcher stages it into the "
            "sealed volume, and a relative path would resolve against a directory nobody chose"
        )
    resolved = Path(value).resolve()
    try:
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise LauncherError(
            f"--artifact {value} could not be opened ({exc.strerror}), so it cannot be sealed"
        ) from None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise LauncherError(
                f"--artifact {resolved} is not a regular file, so it cannot be sealed"
            )
        if opened.st_nlink != 1:
            raise LauncherError(
                f"--artifact {resolved} has {opened.st_nlink} hard links, so it has names this "
                "check cannot see and one of them may be inside a checkout. The sealed artifact "
                "must have exactly one name, and it must be the one given here."
            )
        try:
            named = os.lstat(resolved)
        except OSError as exc:
            raise LauncherError(
                f"--artifact {resolved} vanished while it was being opened ({exc.strerror})"
            ) from None
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise LauncherError(
                f"--artifact {resolved} does not name the file that was opened from it — it "
                "changed underneath. Refusing rather than judging one file and sealing another."
            )
        _refuse_repo_interior_artifact(repo_root, resolved)
        yield _HeldArtifact(index=index, fd=fd, resolved=resolved, mode=opened.st_mode & 0o777)
    finally:
        os.close(fd)


def _forbidden_root_identities(repo_root: Path, *, what: str) -> list[tuple[int, int]]:
    """Every working tree of this repository, as ``(st_dev, st_ino)``, measured on the HOST.

    THE SET: the executing checkout, the common directory's parent (the origin working tree),
    and every registered worktree. Identities rather than paths, because this filesystem is
    case-insensitive and a path string compares unequal to itself spelled differently — the
    same reason the scorer's copy of this rule uses identity.

    WHY IT IS MEASURED HERE AND CARRIED, rather than derived where it is used. The scorer asks
    git for this set, from inside the sealed volume, and git answers through the origin's
    ADMINISTRATIVE DIRECTORY — which stays writable and outside the boundary by construction.
    Editing ``<admin>/commondir`` mid-run makes git name a different repository entirely: the
    origin checkout leaves the set, and a production-derived result may then be written into
    the real working tree, one ``git add .`` from being committed. Reproduced against the code
    before this existed, with the sealed ``.git`` file and the admin inode both unchanged, so
    nothing the volume verifies could see it.

    This runs on the host before any volume exists, and what it returns is handed to the child
    as a FLOOR (see child_env). The child still asks git as well and takes the union: a
    worktree created after the seal is caught by the live answer, and no mid-run edit can
    shrink the set below what was measured here.

    FAILS CLOSED, twice. If git cannot say which trees exist, the answer is not "none of them".
    If none of the trees git named can be identified on disk, that is an unusable answer rather
    than a clean bill of health.
    """
    common = Path(_git("-C", str(repo_root), "rev-parse", "--path-format=absolute",
                       "--git-common-dir")).resolve()
    listed = subprocess.run(
        # -z: the porcelain path is printed raw, and a directory name may contain a newline.
        ["git", "-C", str(common), "worktree", "list", "--porcelain", "-z"],
        capture_output=True,
    )
    if listed.returncode != 0:
        raise LauncherError(
            "could not list the registered git worktrees, so it cannot be shown that "
            f"{what} sits outside every checkout; refusing rather than continuing"
        )
    roots = {repo_root.resolve(), common.parent}
    roots.update(
        Path(os.fsdecode(field[len(b"worktree "):]))
        for field in listed.stdout.split(b"\0")
        if field.startswith(b"worktree ")
    )
    identities = sorted({i for i in map(_path_identity, roots) if i is not None})
    if not identities:
        raise LauncherError(
            "none of the working trees git listed could be identified on disk, so the "
            f"private-path rule cannot be evaluated and {what} is refused"
        )
    return identities


def _refuse_repo_interior_artifact(repo_root: Path, resolved: Path) -> None:
    """Refuse an ``--artifact`` that lives inside any working tree, BEFORE it is copied.

    THE HOLE THIS CLOSES. The scorer enforces this rule itself
    (``_refuse_repo_interior_path``): release inputs hold production-derived private data and
    must not sit in a checkout, one ``git add .`` from being committed. But sealing the
    artifact REWRITES the argument, so under the boundary the child no longer sees the
    operator's path — it sees ``<mount>/inputs/<name>``, which is inside no working tree and
    passes trivially. The launcher's own convenience therefore disabled a governance
    invariant: an artifact anywhere in any checkout became acceptable simply by being staged.
    So the ORIGINAL path is judged here, before the copy, and the child's check stays exactly
    where it was for the unsealed path.

    THE RULE IS DUPLICATED, DELIBERATELY, and this is the one place in the launcher where
    that is true. The scorer's copy cannot be imported: it lives in a module that imports
    ``app.*`` and needs the venv, while this runs on the host interpreter under ``-I -S``
    before any volume exists. The duplicate is kept honest by being STRICTER on every axis it
    could drift — unknown git state refuses, an unidentifiable root refuses — so drift costs
    a false refusal, never a false acceptance.

    FAILS CLOSED. If git cannot say which trees exist, the answer is not "none of them".

    TAKES AN ALREADY-RESOLVED PATH, and takes it from _hold_artifact rather than re-deriving it
    from the arguments, so that the path judged here is provably the path the held descriptor
    was opened from. Re-resolving the operator's string would reintroduce the exact gap that
    caller exists to close.
    """
    forbidden = set(_forbidden_root_identities(repo_root, what="--artifact"))
    for candidate in (resolved, *resolved.parents):
        if _path_identity(candidate) in forbidden:
            raise LauncherError(
                f"--artifact {resolved.name!r} resolves INSIDE a repository working tree. "
                "Release inputs hold production-derived private data and must live at an "
                "access-controlled path OUTSIDE every checkout (governance invariant). "
                "Sealing it would COPY it onto the volume and hide the violation from the "
                "scorer's own check, which only ever sees the staged path."
            )


def _stage_artifact(
    stage: Path,
    mount: Path,
    script_args: Sequence[str],
    artifact: _HeldArtifact | None,
) -> tuple[str, ...]:
    """Copy the frozen cohort artifact into the volume and repoint ``--artifact`` at it.

    The artifact is the release's other ground truth, and until now it was read LIVE from a
    private store while the run was in flight — outside the boundary, writable by this uid,
    for the whole duration. Sealing the code that scores while leaving the data it scores
    mutable would be a boundary with a hole exactly the width of the thing being decided.

    The option is rewritten rather than merely added because the scorer takes ONE
    ``--artifact``; the operator still names which artifact, and the launcher decides where
    the child reads it from. ``--artifact=X`` and ``--artifact X`` are both accepted, because
    a release that silently ran against the unsealed original over a spelling difference is
    the failure this is here to prevent.

    NEITHER THE GOVERNANCE CHECK NOR THE OPEN IS HERE. Rewriting the path is what hides the
    operator's choice from the scorer, so the original is judged by _hold_artifact before any
    of this runs — before the worktree, the stage, or the image exist — and this function is
    handed the DESCRIPTOR that judgement was made against. Reopening by name here is exactly
    what let a symlink be retargeted between the two and launder a repository file onto the
    volume, so the name is never resolved again.

    The staged file is named after the RESOLVED path, not the operator's spelling. If the
    argument was a ``latest.parquet`` symlink, the volume holds ``2026-07-01.parquet`` and says
    so: putting an alias on a sealed volume would name the bytes something other than what they
    are, in the one place where naming bytes is the whole job.
    """
    rewritten = list(script_args)
    if artifact is None:
        return tuple(rewritten)
    inputs = stage / _VOLUME_INPUTS
    inputs.mkdir(parents=True, exist_ok=True)
    destination = inputs / artifact.resolved.name
    os.lseek(artifact.fd, 0, os.SEEK_SET)
    with open(artifact.fd, "rb", closefd=False) as origin, destination.open("wb") as sealed_copy:
        shutil.copyfileobj(origin, sealed_copy, 1 << 20)
    # The source's mode, from the descriptor's own fstat rather than from a fresh stat of the
    # path, for the same reason as everything else here. _require_sealed_bytes_are_their_sources
    # compares it, so it has to come from the thing that was judged.
    destination.chmod(artifact.mode)
    # The MOUNT path, written now rather than left as a placeholder to be substituted later.
    # The mount point is known before any staging happens, and a placeholder scheme would have
    # to recognise its own markers inside operator-supplied arguments — a path that genuinely
    # began with the marker would be rewritten into something nobody asked for.
    sealed = str(mount / _VOLUME_INPUTS / artifact.resolved.name)
    rewritten[artifact.index] = (
        f"--artifact={sealed}" if rewritten[artifact.index].startswith("--artifact=") else sealed
    )
    return tuple(rewritten)


@contextmanager
def sealed_checkout(
    repo_root: Path,
    rev: str,
    *,
    mount_provenance: bool,
    script_args: Sequence[str],
) -> Iterator[SealedVolume]:
    """The OS boundary (g-release-os-boundary): a read-only volume holding everything the run
    executes, mounted before a single byte is hashed and held until the child exits.

    WHAT THIS IS, AND WHY IT IS NOT exclusive_checkout. exclusive_checkout removed the
    AMBIENT hazard — a shared tree that editors and other agents write continuously — but not
    the boundary: 0700 excludes other UNIX users rather than same-uid processes, 0444 is
    revertible by its owner, and `git worktree list` published the path. Anything running as
    this uid could still change-and-revert a manifest file between the hash and the import.

    Here the bytes are on a filesystem the KERNEL refuses writes to, for every process, at
    any uid. Measured, not assumed: a write returns EROFS, and so does a write after a
    successful `chmod u+w` — which is the clearest possible statement that mode bits were
    never the mechanism.

    THE SCOPE IS THE WHOLE EXECUTION INPUT, not the git tree. A boundary around the tree
    alone would have left the hazard that made this bead release-blocking in the first place:
    a concurrent `pip install` changes the code that runs without touching the tree, the
    manifest, or the digest. So the volume also carries the interpreter, the stdlib and
    lib-dynload, the installed dependencies, the frozen artifact, and every non-Apple dylib
    the closure reaches — including libpython and libintl, which the interpreter loads by
    ABSOLUTE PATH and which a naive prefix copy therefore does NOT capture (verified with
    otool; the fix is the flat dylibs/ directory plus DYLD_LIBRARY_PATH).

    THE BACKING FILE IS UNLINKED. A same-uid process can open and write an attached .dmg —
    tested, it succeeds — so leaving it on disk would leave a second, WRITABLE path to every
    sealed byte. Unlinking while attached keeps the mount and the eventual detach working
    (also tested), and afterwards no path to the backing store exists at all.

    WHAT IS STILL OUTSIDE, and is recorded rather than claimed: the signed system volume
    (/usr/lib, /System), whose OS BUILD the cohort carries; and git's administrative
    directory, which stays in the origin's mutable .git — so nothing git says from inside is
    an attestation (see _resolve_revision). A same-uid process can still `hdiutil detach` the
    volume, which breaks the run loudly instead of corrupting it quietly. And this is not a
    defence against a hostile operator, who can simply commit the change.
    """
    # FIRST, before a temp directory, a worktree, or a copy exists: staging the artifact is
    # what conceals the operator's chosen path from the scorer's own governance check, so a
    # path that check would refuse must never reach the stage. Held OPEN for the whole of the
    # rest of this function, because a path that was judged and then reopened by name is not
    # necessarily the same file twice.
    with _hold_artifact(repo_root, script_args) as artifact:
        revision = _resolve_revision(repo_root, rev)
        parent = Path(tempfile.mkdtemp(prefix="ghostreplay-release-"))
        parent.chmod(0o700)
        stage = parent / "stage"
        mount = parent / "mnt"
        image = parent / "release.dmg"
        pycache = parent / "work" / "pycache"
        scratch = parent / "work" / "scratch"
        tree_stage = stage / _VOLUME_TREE
        dep_paths = _audited_dep_paths()
        admin: Path | None = None
        attached = False
        # Set the instant `worktree add` returns, NOT when the administrative directory is
        # resolved a line later. Anything failing in between still leaves a REGISTRATION
        # behind, and the temp parent is deleted on the way out — so keying cleanup off
        # `admin` would point the origin repo at a directory that no longer exists, with
        # nothing to show for it.
        worktree_added = False
        # Where git currently believes the worktree lives. It MOVES when the registration is
        # repointed, and cleanup has to follow it: a failure before the repoint leaves the
        # entry naming the staging path, and pruning the mount path instead would leak a
        # registration pointing at a directory this function is about to delete.
        registered: Path = tree_stage
        try:
            for writable in (pycache, scratch, mount):
                writable.mkdir(parents=True, exist_ok=True)
            _git("-C", str(repo_root), "worktree", "add", "--detach", str(tree_stage), revision)
            worktree_added = True
            # Ask git for the administrative directory rather than assuming
            # <repo>/.git/worktrees/tree: `worktree add` uniquifies the name when one already
            # exists, and repointing the wrong entry below would leave THIS run's registration
            # dangling at a deleted staging path. It is also what the sealed `.git` is required
            # to name, so it is asked for BEFORE anything is staged — a value read back out of
            # the stage later would be the thing under suspicion vouching for itself.
            admin = Path(
                _git("-C", str(tree_stage), "rev-parse", "--path-format=absolute", "--git-dir")
            )
            # Before any staging: the inner half of this run is the launcher AT THIS REVISION,
            # and a revision that predates the boundary cannot provide one.
            _require_compatible_inner(tree_stage, rev)
            # BEFORE the freeze because the record has to be sealed with the checkout, and the
            # digest it returns is the record's baseline: bytes this process read and hashed,
            # held in memory, compared against the volume afterwards. Nothing on disk between
            # here and there is the baseline for anything — an earlier version digested the
            # staged CHECKOUT here and compared against that, which blessed any edit that
            # landed before this line instead of catching it.
            provenance_digest = (
                mount_cohort_provenance(repo_root, tree_stage) if mount_provenance else None
            )
            staged_args = _stage_artifact(stage, mount, script_args, artifact)
            _clone_tree(Path(sys.base_prefix), stage / _VOLUME_PYTHON)
            for ordinal, dep_path in enumerate(dep_paths):
                _clone_tree(dep_path, stage / _VOLUME_DEPS / str(ordinal))
            dylib_sources = _stage_dylib_closure(stage, stage / _VOLUME_DYLIBS)

            _run("hdiutil", "create", "-srcfolder", str(stage), "-format", "UDRO",
                 "-volname", "ghostreplay-release", "-quiet", str(image),
                 what="building the sealed image")
            _run("hdiutil", "attach", "-readonly", "-nobrowse", "-owners", "off",
                 "-mountpoint", str(mount), str(image), what="attaching the sealed image")
            attached = True
            # ORDER IS LOAD-BEARING. The backing file must die before anything is hashed or
            # run: it is writable by this uid while attached, so until it is gone there is a
            # writable path to every byte the digest is about to describe.
            os.unlink(image)
            # Only now is there something worth checking. Every earlier version of this check
            # measured the writable stage and then handed that same writable stage to
            # `hdiutil create`, which left everything it proved behind a window it could not
            # see across. These are the frozen bytes, and nothing can change them after this
            # returns.
            _require_sealed_bytes_are_their_sources(
                mount, repo_root=repo_root, revision=revision, dep_paths=dep_paths,
                dylib_sources=dylib_sources, provenance_digest=provenance_digest,
                artifact=artifact, admin=admin,
            )
            # Repoint the worktree registration at the mount, THEN drop the writable staging
            # copy, so that exactly one path to these bytes exists and it is read-only. git
            # keeps working from the mount (its index and administrative files live in the
            # origin, which stays writable) and `git worktree list` now names the mount —
            # which is what keeps _private_path_forbidden_roots refusing result paths written
            # into the run's own tree.
            (admin / "gitdir").write_text(f"{mount / _VOLUME_TREE / '.git'}\n", encoding="utf-8")
            registered = mount / _VOLUME_TREE
            shutil.rmtree(stage, ignore_errors=True)

            interpreter = (
                mount / _VOLUME_PYTHON / "bin"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
            )
            if not interpreter.is_file():
                raise LauncherError(
                    f"the sealed interpreter is missing at {interpreter}; the staged prefix "
                    f"({sys.base_prefix}) does not have the expected bin/python layout"
                )
            _require_read_only(mount, "the sealed volume")
            yield SealedVolume(
                parent=parent, mount=mount, tree=mount / _VOLUME_TREE, interpreter=interpreter,
                pycache=pycache, scratch=scratch, revision=revision, mechanism=MACOS_MECHANISM,
                provenance_digest=provenance_digest,
                script_args=staged_args,
            )
        finally:
            if attached:
                _detach_image(mount)
            if worktree_added:
                # After the detach the worktree path is gone, which is the state `prune`
                # reclaims. _remove_worktree deletes first and prunes second for that reason.
                _remove_worktree(repo_root, registered)
            shutil.rmtree(parent, ignore_errors=True)


# --- Measuring the volume from inside (INNER launcher) ---------------------------------


def runtime_image_digest(volume: Path) -> str:
    """SHA-256 over every byte on the sealed volume, in a canonical walk.

    This is what a winner gets bound to. ``runtime_python`` and ``runtime_chess_version`` are
    version STRINGS — they say what the runtime called itself, not what it was, and two
    builds of "3.12.7" with different patches agree on both. This names the actual bytes of
    the interpreter, the stdlib, every dependency, the checkout, and the artifact.

    Computed HERE, by sealed code running on the sealed interpreter, over content that the
    kernel will not let anything change. The outer launcher could have hashed the image file
    instead, but that hash would have been taken by mutable host code over a file that was
    still writable when it was read — a number, not an attestation.

    The construction is _tree_digest, which is also what the outer launcher compares the sealed
    interpreter and dependency roots against. ONE construction on purpose: two hashers over the
    same bytes are two chances to disagree about what "the runtime" means, and the value the
    winner carries has to be the value that was checked. The checkout and the artifact are
    checked against baselines of their own — a commit and a held descriptor, neither of which
    is a directory to be walked — and this digest covers their sealed bytes all the same.
    """
    return _tree_digest(volume, what="the sealed volume")


def _sealed_run_from_self(revision: str, mechanism: str, scratch: Path) -> SealedRun:
    """Re-derive the volume layout from this file's own location and PROVE it is read-only.

    Derived, never told. The inner launcher is handed its arguments by the outer one, and an
    inner launcher that accepts "the sealed deps are over there" can be pointed outside the
    volume by anything that can shape that command line. Its own ``__file__`` is the one thing
    it cannot be lied to about: it is the file CPython actually compiled.

    Every component must be on the SAME read-only device — same-device because a read-only
    mount nested inside the volume would otherwise satisfy the check while coming from
    somewhere else entirely.
    """
    here = Path(__file__).resolve()
    volume = here.parents[3]
    tree = volume / _VOLUME_TREE
    if here.parents[2] != tree:
        raise LauncherError(
            f"the inner launcher is at {here}, which is not {_VOLUME_TREE}/backend/scripts/ "
            "inside a sealed volume — it cannot vouch for a boundary it is not standing in"
        )
    device = _require_read_only(here, "the inner launcher")
    dep_root = volume / _VOLUME_DEPS
    dep_paths = tuple(sorted(p for p in dep_root.iterdir() if p.is_dir())) if dep_root.is_dir() else ()
    if not dep_paths:
        raise LauncherError(f"no sealed dependency directories under {dep_root}")
    checks: list[tuple[Path, str]] = [
        (tree, "the sealed checkout"),
        (Path(sys.executable).resolve(), "the interpreter"),
        (Path(sysconfig.get_paths()["stdlib"]), "the standard library"),
        *[(p, "a sealed dependency directory") for p in dep_paths],
    ]
    for path, what in checks:
        if _require_read_only(path, what) != device:
            raise LauncherError(
                f"{what} ({path}) is read-only but on a DIFFERENT filesystem from the sealed "
                "volume, so it is not the copy this run sealed"
            )
    return SealedRun(
        mechanism=mechanism,
        runtime_image_sha256=runtime_image_digest(volume),
        revision=revision,
        volume=volume,
        dep_paths=dep_paths,
        dylibs=volume / _VOLUME_DYLIBS,
        scratch=scratch,
    )


def child_env(
    tree_root: Path,
    digest: str,
    pycache_dir: Path,
    *,
    provenance_digest: str | None = None,
    sealed: SealedRun | None = None,
    forbidden_roots: Sequence[tuple[int, int]] | None = None,
) -> dict[str, str]:
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

    ``GHOSTREPLAY_COHORT_PROVENANCE_SHA256`` joins them when a record was mounted. It is
    added HERE, after the ``PYTHON*`` strip and alongside the scorer digest, so the
    allowlist discipline above holds for it too.

    ``GHOSTREPLAY_SEALED_FORBIDDEN_ROOTS`` is the fourth load-bearing one, and unlike the rest
    it is not something the child could measure for itself. The child's private-path rule asks
    git which working trees exist, and git answers through an administrative directory that
    stays writable outside the boundary — so the answer can be changed while the run is live,
    and the origin checkout can be made to disappear from the set that keeps
    production-derived data out of it. This carries the set measured on the host before the
    volume existed. The child uses it as a FLOOR and still asks git, so the two failure
    directions are covered separately: a tree created after the seal is caught by git, and a
    tree removed from git's answer is still in here.

    Read ``-S`` in child_command for what the startup-hook half of this buys, and
    ``_audited_dep_paths`` for what "audited" is and is not worth.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env[SCORER_SOURCE_DIGEST_ENV] = digest
    # Set it, or REMOVE it — the rule every boundary variable below follows, and this one has
    # the sharpest edge: an INHERITED forbidden-root floor would be a set of inode numbers from
    # some other machine, some other repository, or some other week, and every one of them
    # would be a root this run's private paths are compared against instead of the real ones.
    env.pop(SEALED_FORBIDDEN_ROOTS_ENV, None)
    if forbidden_roots is not None:
        env[SEALED_FORBIDDEN_ROOTS_ENV] = json.dumps(
            [list(identity) for identity in sorted(forbidden_roots)],
            separators=(",", ":"),
        )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPYCACHEPREFIX"] = str(pycache_dir)
    env["PYTHONNOUSERSITE"] = "1"
    dep_paths = sealed.dep_paths if sealed is not None else _audited_dep_paths()
    env["PYTHONPATH"] = os.pathsep.join(str(p) for p in dep_paths)
    # Set it, or REMOVE it. The strip above only covers PYTHON*, so an inherited
    # GHOSTREPLAY_COHORT_PROVENANCE_SHA256 would otherwise survive into a run that mounted
    # nothing — handing the child a digest no launcher computed, which is the whole thing
    # the mount exists to rule out.
    env.pop(COHORT_PROVENANCE_DIGEST_ENV, None)
    if provenance_digest is not None:
        env[COHORT_PROVENANCE_DIGEST_ENV] = provenance_digest
    # Same rule, same reason, for every boundary variable: SET, or REMOVE. An inherited
    # GHOSTREPLAY_RELEASE_BOUNDARY surviving into an unsealed run would describe a boundary
    # nobody built. The scorer does not take these as proof — it measures its own filesystem
    # — but a value it can read at all must be one this launcher chose to write.
    for name in (RELEASE_BOUNDARY_ENV, RUNTIME_IMAGE_DIGEST_ENV, SEALED_REVISION_ENV,
                 GRAPH_NO_DISK_CACHE_ENV):
        env.pop(name, None)
    if sealed is not None:
        env[RELEASE_BOUNDARY_ENV] = sealed.mechanism
        env[RUNTIME_IMAGE_DIGEST_ENV] = sealed.runtime_image_sha256
        env[SEALED_REVISION_ENV] = sealed.revision
        env[GRAPH_NO_DISK_CACHE_ENV] = "1"
        # The interpreter loads libpython and libintl by ABSOLUTE PATH into ~/.pyenv and
        # /opt/homebrew, so a copy of the prefix alone still executes mutable host code (seen
        # with otool, and again by enumerating loaded images). dyld resolves a requested
        # install path's LEAF NAME against DYLD_LIBRARY_PATH first, which is what lets the
        # sealed copies win. If this is ever stripped — a restricted binary in the chain would
        # do it — the scorer's loaded-image check fails the run rather than letting it run
        # half-sealed.
        env["DYLD_LIBRARY_PATH"] = str(sealed.dylibs)
        # Scratch inside the private 0700 parent, not the operator's shared TMPDIR: it is in
        # the enumerated writable set, and a release should not leave its intermediates in a
        # directory everything else on the machine also writes.
        env["TMPDIR"] = str(sealed.scratch)
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


def launch(
    tree_root: Path,
    script_args: Sequence[str],
    *,
    pycache_dir: Path,
    provenance_digest: str | None = None,
    sealed: SealedRun | None = None,
    forbidden_roots: Sequence[tuple[int, int]] | None = None,
) -> int:
    """Hash ``tree_root``'s manifest, then run the scorer from it under that digest.

    ORDER IS THE POINT: the digest is computed before the child interpreter exists, so it
    necessarily precedes the compilation of every file it names. Nothing that touches the
    tree may be inserted between the hash and the exec.

    WITH ``sealed``, that ordering stops being the whole guarantee and becomes a formality.
    The bytes are on a read-only volume before this function is called and stay there until
    the child exits, so there is no window to insert anything INTO — this is running as the
    inner launcher, from sealed code on the sealed interpreter, and the hash it takes is over
    content the kernel will not let anything change. The child still re-checks the digest,
    because a run that verifies what it was told is worth more than one that assumes it.

    WITHOUT ``sealed`` — a ``--no-boundary`` dev run, or capture, which runs in the main
    worktree by design — the ordering is all there is, and the child records that by stamping
    ``scorer_source_verified_preexec=False``.
    """
    digest = manifest_digest(tree_root)
    pycache_dir.mkdir(parents=True, exist_ok=True)
    if any(pycache_dir.rglob("*.pyc")):
        raise LauncherError(
            f"bytecode cache {pycache_dir} is not empty — a verified run needs a cache "
            "CPython cannot serve the scorer from"
        )
    if sealed is not None:
        print(f"[launcher] boundary={sealed.mechanism} image={sealed.runtime_image_sha256[:12]} "
              f"rev={sealed.revision[:12]}", file=sys.stderr)
    print(f"[launcher] tree={tree_root} digest={digest[:12]}", file=sys.stderr)
    proc = subprocess.run(
        child_command(tree_root, script_args),
        env=child_env(tree_root, digest, pycache_dir,
                      provenance_digest=provenance_digest, sealed=sealed,
                      forbidden_roots=forbidden_roots),
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
        "--mount-cohort-provenance", action="store_true",
        help="Copy the ORIGIN WORKING TREE's backend/scripts/fixtures/cohort_provenance.json "
             "into the checkout and hand the child its SHA-256. Required by select-release, "
             "whose candidate record is still uncommitted at approval time. OFF by default: a "
             "dev or report run under the launcher has no candidate record to mount.",
    )
    parser.add_argument(
        "--no-boundary", action="store_true",
        help="Run WITHOUT the OS boundary: an exclusive worktree only, as before "
             "g-release-os-boundary. The child then stamps scorer_source_verified_preexec="
             "False, so select-release and the Phase-3 preflight refuse the result exactly as "
             "they refuse a bare run. For dev and report runs, and for platforms with no "
             "boundary mechanism. NEVER for a release.",
    )
    parser.add_argument(
        "--inner", action="store_true",
        help=argparse.SUPPRESS,  # set by the outer launcher; not an operator-facing option
    )
    parser.add_argument("--sealed-revision", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--mechanism", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--pycache-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--scratch-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--provenance-digest", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--forbidden-roots", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "script_args", nargs=argparse.REMAINDER,
        help="Arguments forwarded to calibrate_opening_scores_v2.py (put them after --).",
    )
    args = parser.parse_args(argv)
    if args.script_args and args.script_args[0] == "--":
        args.script_args = args.script_args[1:]
    return args


def _inner_main(args: argparse.Namespace) -> int:
    """The launcher, running FROM the sealed volume ON the sealed interpreter.

    WHY THERE IS A SECOND LAUNCHER AT ALL. The outer one runs from the shared working tree on
    the host interpreter — both mutable, both outside any boundary. A digest it computes is a
    statement made by code that something else could have edited a moment earlier, about
    bytes that were still writable when it read them. Moving the hash in here makes it sealed
    code, on a sealed interpreter, over sealed bytes: the window between the hash and the
    child's import is not narrowed, it is gone, because after the mount there is no writable
    path to any of it.

    It refuses unless it can prove it is standing where it thinks it is (_sealed_run_from_self).
    """
    if not args.sealed_revision or not args.mechanism or not args.pycache_dir:
        raise LauncherError("--inner requires --sealed-revision, --mechanism and --pycache-dir")
    sealed = _sealed_run_from_self(
        revision=args.sealed_revision,
        mechanism=args.mechanism,
        scratch=Path(args.scratch_dir) if args.scratch_dir else Path(args.pycache_dir).parent,
    )
    provenance_digest = args.provenance_digest
    if provenance_digest is not None:
        # Re-hash the record THROUGH the read-only mount. The outer launcher verified its copy
        # into the staging directory, but the image was built after that, and a digest handed
        # to the child must describe the bytes the child will actually read.
        mounted = manifest_path(sealed.volume / _VOLUME_TREE, COHORT_PROVENANCE_REL).read_bytes()
        if hashlib.sha256(mounted).hexdigest() != provenance_digest:
            raise LauncherError(
                f"the sealed {COHORT_PROVENANCE_REL} does not hash to what the launcher "
                "recorded before imaging — refusing to hand the child a digest it cannot honour"
            )
    return launch(
        sealed.volume / _VOLUME_TREE, args.script_args,
        pycache_dir=Path(args.pycache_dir),
        provenance_digest=provenance_digest,
        sealed=sealed,
        forbidden_roots=_parse_forbidden_roots(args.forbidden_roots),
    )


def _parse_forbidden_roots(raw: str | None) -> list[tuple[int, int]]:
    """The host-measured working-tree identities, as the outer launcher wrote them.

    REQUIRED, not optional, on this path: a sealed run whose child cannot be given the floor is
    a sealed run whose private-path rule can be talked out of the origin checkout, and the
    right answer to "the outer did not send one" is to stop rather than to fall back on the
    thing being defended against. The protocol version exists so this refusal is not how an
    old outer/new inner mismatch is discovered — it is the last line of defence, not the first.
    """
    if not raw:
        raise LauncherError(
            "the outer launcher sent no --forbidden-roots, so the child would decide which "
            "paths are inside a checkout using only what git says from inside the volume — "
            "which is what an edit to the origin's administrative directory changes. Refusing"
        )
    try:
        parsed = json.loads(raw)
        identities = [(int(dev), int(ino)) for dev, ino in parsed]
    except (TypeError, ValueError) as exc:
        raise LauncherError(
            f"--forbidden-roots is not a list of (device, inode) pairs ({type(exc).__name__}); "
            "refusing rather than running with a private-path floor nobody can read"
        ) from None
    if not identities:
        raise LauncherError(
            "--forbidden-roots is empty, so no working tree would be forbidden; refusing"
        )
    return identities


def _outer_main(args: argparse.Namespace, repo_root: Path) -> int:
    """Establish the boundary, then hand the run to a launcher that lives inside it."""
    mechanism = _boundary_mechanism()
    if args.no_boundary:
        # The pre-g-release-os-boundary path, kept verbatim and reachable only on purpose.
        with exclusive_checkout(repo_root, args.rev) as tree:
            provenance_digest = (
                mount_cohort_provenance(repo_root, tree) if args.mount_cohort_provenance else None
            )
            print("[launcher] NO BOUNDARY: this run cannot be spent on a release "
                  "(scorer_source_verified_preexec will be False)", file=sys.stderr)
            return launch(tree, args.script_args, pycache_dir=tree.parent / "pycache",
                          provenance_digest=provenance_digest)
    if mechanism is None:
        raise LauncherError(
            f"no OS boundary mechanism exists on {sys.platform!r}, and a release run requires "
            "one (g-release-os-boundary). Only macOS is implemented — see _boundary_mechanism "
            "for why a Linux mount namespace is not built yet. Pass --no-boundary to run "
            "anyway; the result will stamp scorer_source_verified_preexec=False and neither "
            "select-release nor the preflight will accept it."
        )
    # BEFORE the volume exists, on the host, while the origin's git state is still the thing
    # being described. Carried to the child rather than re-derived there: from inside, git
    # answers through an administrative directory that stays writable, and an edit to it while
    # the run is live takes the origin checkout out of the answer (g-sealed-gov-roots).
    forbidden_roots = _forbidden_root_identities(repo_root, what="the private-path rule")
    with sealed_checkout(repo_root, args.rev, mount_provenance=args.mount_cohort_provenance,
                         script_args=args.script_args) as volume:
        command = [
            str(volume.interpreter), "-I", "-S",
            str(volume.tree / "backend/scripts/release_calibration_launcher.py"),
            "--inner",
            "--sealed-revision", volume.revision,
            "--mechanism", volume.mechanism,
            "--pycache-dir", str(volume.pycache),
            "--scratch-dir", str(volume.scratch),
        ]
        if volume.provenance_digest is not None:
            command += ["--provenance-digest", volume.provenance_digest]
        command += ["--forbidden-roots",
                    json.dumps([list(i) for i in forbidden_roots], separators=(",", ":"))]
        command += ["--", *volume.script_args]
        # The inner launcher gets a clean PYTHON* environment for the same reason the child
        # does, plus the dylib redirect — it is a Python process on the sealed interpreter and
        # every hazard child_env exists to close applies to it one interpreter earlier.
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env["DYLD_LIBRARY_PATH"] = str(volume.mount / _VOLUME_DYLIBS)
        env["TMPDIR"] = str(volume.scratch)
        return subprocess.run(command, env=env).returncode


def main(argv: list[str] | None = None) -> int:
    # FIRST, before anything is read or hashed: a launcher that was started wrong has already
    # been compromised, and everything below it would be vouching with borrowed authority.
    # True of the inner launcher too — it is a fresh interpreter, so it is a fresh chance to
    # have been started without -I -S.
    require_isolated_launcher()
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.inner:
        return _inner_main(args)
    return _outer_main(args, Path(__file__).resolve().parents[2])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LauncherError as exc:
        print(f"[launcher] refusing to run: {exc}", file=sys.stderr)
        sys.exit(2)
