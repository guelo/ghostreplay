"""Verified recovery exports for the SRS opportunity fold (g-srs-fold-recovery).

A fold deletes raw rows. For seven days after the first one, this module is the
only thing that can put them back, so its single job is to make "the export
exists and is exactly these rows" a fact the fold transaction can rely on BEFORE
it deletes anything.

Three properties, in the order they are established:

1. **Written, flushed and re-read.** The bytes are written to a temporary file,
   ``fsync``-ed, atomically renamed into place and its directory ``fsync``-ed,
   and then read back off the filesystem — not out of the buffer that produced
   them. A write that only reached a page cache is not a recovery artifact.
2. **Hashed twice, for two different questions.** ``artifact_sha256`` answers
   "is this the file we wrote?"; ``rowset_hash`` answers "are these the rows we
   deleted?". A file can be byte-intact and describe a different batch, or hold
   the right rows and be truncated, and only one of the two digests notices each.
3. **Outside every lock.** All of this is filesystem I/O with unbounded worst-case
   latency. The fold's critical transaction has a 500 ms deadline and holds a
   per-user lock, so no byte of it may happen in there. The caller exports first
   and opens the transaction afterwards.

The canonical rowset encoding is VERSIONED. Adding a field to the raw table
changes the digest of identical rows, and a restore that cannot reproduce the
version it is reading must refuse rather than compare two incomparable digests.

Artifacts are not an archive. They expire with their manifest after seven days
(:mod:`app.opportunity_fold_recovery`), and a permanent one would be the
unbounded raw history this epic exists to remove, kept under another name.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.srs_math import as_utc

# Bump when the canonical encoding below changes in any way that alters the
# digest of an unchanged row. Readers refuse a version they do not implement.
CANONICAL_HASH_VERSION = 1
# The artifact's own document shape, independent of the rowset encoding: a new
# header field bumps this without invalidating rowset_hash comparisons.
ARTIFACT_VERSION = 1

EXPORT_DIR_ENV = "GHOSTREPLAY_SRS_FOLD_EXPORT_DIR"
# Beside the backend package, not in /tmp: an artifact that a reboot can delete
# is not a seven-day recovery guarantee. Deployments that keep state elsewhere
# point the environment variable at it.
DEFAULT_EXPORT_DIRNAME = ".srs_fold_exports"

# NULL has to encode as something no real timestamp can produce. occurred_at is
# genuinely nullable on legacy rows and COALESCE-ing it to created_at here would
# make the export unable to restore the original fact.
_NULL_TOKEN = "-"


class FoldExportError(RuntimeError):
    """The export could not be written, read back or verified.

    Raised before any deletion, always. The fold's answer to it is to abandon
    this batch, not to proceed: rows whose export failed are rows that could not
    be recovered.
    """


@dataclass(frozen=True)
class FoldRow:
    """One raw ``blunder_opportunity_events`` row, exactly as stored.

    ``session_started_at`` and ``blunder_created_at`` ride along because the fold
    needs them for the prefix and the eligibility predicate, but they are NOT
    part of the canonical rowset hash: they belong to other tables, which a
    restore does not recreate and must not assert about.
    """

    id: int
    blunder_id: int
    session_id: uuid.UUID
    occurred_at: datetime | None
    created_at: datetime
    opportunity: bool
    reached: bool
    session_started_at: datetime
    blunder_created_at: datetime | None

    @property
    def event_time(self) -> datetime:
        """``t`` in the shared counter contract: ``COALESCE(occurred_at, created_at)``."""
        return as_utc(self.occurred_at if self.occurred_at is not None else self.created_at)

    def restore_values(self) -> dict:
        """The exact column values a restore re-inserts. Ids and NULLs preserved."""
        return {
            "id": self.id,
            "blunder_id": self.blunder_id,
            "session_id": self.session_id,
            "occurred_at": self.occurred_at,
            "created_at": self.created_at,
            "opportunity": self.opportunity,
            "reached": self.reached,
        }


def _stamp(value: datetime | None) -> str:
    return _NULL_TOKEN if value is None else as_utc(value).isoformat()


def _encode_v1(row: FoldRow) -> str:
    """Version 1: every stored column of the raw row, pipe separated.

    Every field is the STORED value. Normalizing ``occurred_at`` to
    ``created_at`` here, as the counter predicates do, would make two genuinely
    different rows hash the same and let a reprepare accept a rowset it should
    have rejected.
    """
    return "|".join(
        (
            str(row.id),
            str(row.blunder_id),
            str(row.session_id),
            _stamp(row.occurred_at),
            _stamp(row.created_at),
            "1" if row.opportunity else "0",
            "1" if row.reached else "0",
        )
    )


# Every encoding this release can REPRODUCE, not just the one it writes. A
# release that bumps CANONICAL_HASH_VERSION still has to verify the artifacts
# written by the release before it: those batches are inside their seven days,
# and a deploy that made them unreadable would end their recovery early. Old
# entries are therefore never removed while any artifact can still be in flight.
_ENCODERS = {1: _encode_v1}


def canonical_rowset_hash(
    rows: list[FoldRow], *, version: int = CANONICAL_HASH_VERSION
) -> str:
    """Digest the rows a fold is about to delete, order-independently.

    Sorted by id, so the digest is a property of the SET rather than of whatever
    order the planner returned.

    A version this release does not implement can only be a NEWER one — written
    by a release that has since been rolled back — and there is nothing useful to
    do with it: comparing two incomparable digests would either reject every
    artifact or, worse, accept one by collision.
    """
    encode = _ENCODERS.get(version)
    if encode is None:
        raise FoldExportError(
            f"canonical rowset hash version {version} is not implemented by this "
            f"release (which writes version {CANONICAL_HASH_VERSION} and can "
            f"verify {sorted(_ENCODERS)})"
        )
    digest = hashlib.sha256()
    digest.update(f"srs-fold-rowset-v{version}\n".encode())
    for row in sorted(rows, key=lambda r: r.id):
        digest.update(encode(row).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def export_dir() -> Path:
    """Where artifacts live. Created on demand, never guessed at read time."""
    configured = os.environ.get(EXPORT_DIR_ENV)
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / DEFAULT_EXPORT_DIRNAME


def artifact_path(batch_id: uuid.UUID, *, user_id: int) -> Path:
    """One file per batch. The user id is in the name for operator triage only."""
    return export_dir() / f"fold-{user_id}-{batch_id}.json"


@dataclass(frozen=True)
class ExportedBatch:
    """A verified artifact on disk, and the two digests that identify it."""

    batch_id: uuid.UUID
    user_id: int
    path: Path
    artifact_sha256: str
    rowset_hash: str
    hash_version: int
    row_count: int


def write_export(
    rows: list[FoldRow], *, batch_id: uuid.UUID, user_id: int, prepared_at: datetime
) -> ExportedBatch:
    """Serialize, flush, re-read and verify. Returns only on a proven artifact.

    The rename is what makes a half-written file impossible to mistake for a
    complete one: readers only ever see the final name, and a crash mid-write
    leaves a ``.partial`` that the expiry sweep removes on age like any other
    orphan.
    """
    if not rows:
        raise FoldExportError("refusing to export an empty fold batch")
    rowset_hash = canonical_rowset_hash(rows)
    document = {
        "artifact_version": ARTIFACT_VERSION,
        "hash_version": CANONICAL_HASH_VERSION,
        "batch_id": str(batch_id),
        "user_id": user_id,
        "prepared_at": as_utc(prepared_at).isoformat(),
        "rowset_hash": rowset_hash,
        "rows": [
            {
                "id": row.id,
                "blunder_id": row.blunder_id,
                "session_id": str(row.session_id),
                "occurred_at": _stamp(row.occurred_at),
                "created_at": _stamp(row.created_at),
                "opportunity": row.opportunity,
                "reached": row.reached,
                "session_started_at": _stamp(row.session_started_at),
                "blunder_created_at": _stamp(row.blunder_created_at),
            }
            for row in sorted(rows, key=lambda r: r.id)
        ],
    }
    payload = json.dumps(document, indent=None, sort_keys=True).encode()
    expected_sha = hashlib.sha256(payload).hexdigest()

    target = artifact_path(batch_id, user_id=user_id)
    directory = target.parent
    try:
        # Owner-only, both of them: these files hold one user's production rows
        # verbatim, and the default umask would publish them to every account on
        # the host. The mode is applied again after the open because a umask can
        # only ever take bits away, and a chmod on an EXISTING directory is left
        # alone — a deployment that points this at shared storage has made a
        # deliberate choice this function should not quietly reverse.
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = directory / f"{target.name}.partial"
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, target)
        # The rename itself has to reach the disk, or a crash can leave the
        # directory entry pointing nowhere while the data survives.
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as err:
        raise FoldExportError(f"could not write fold export {target}: {err}") from err

    # Read back from the FILESYSTEM. Comparing the buffer we just serialized
    # against itself would prove nothing about what actually landed.
    verified = read_export(target)
    if verified.artifact_sha256 != expected_sha:
        raise FoldExportError(
            f"fold export {target} read back with digest {verified.artifact_sha256}, "
            f"expected {expected_sha}"
        )
    if verified.rowset_hash != rowset_hash:
        raise FoldExportError(
            f"fold export {target} describes rowset {verified.rowset_hash}, "
            f"expected {rowset_hash}"
        )
    if verified.row_count != len(rows):
        raise FoldExportError(
            f"fold export {target} holds {verified.row_count} rows, expected {len(rows)}"
        )
    return ExportedBatch(
        batch_id=batch_id,
        user_id=user_id,
        path=target,
        artifact_sha256=expected_sha,
        rowset_hash=rowset_hash,
        hash_version=CANONICAL_HASH_VERSION,
        row_count=len(rows),
    )


@dataclass(frozen=True)
class LoadedExport:
    """An artifact read back off disk, with its own recomputed digests."""

    batch_id: uuid.UUID
    user_id: int
    path: Path
    artifact_sha256: str
    rowset_hash: str
    hash_version: int
    row_count: int
    rows: list[FoldRow]


def _parse_stamp(raw: str) -> datetime | None:
    return None if raw == _NULL_TOKEN else as_utc(datetime.fromisoformat(raw))


def read_export(path: Path) -> LoadedExport:
    """Load an artifact and recompute both digests from what is on disk.

    Recomputed, never trusted: ``rowset_hash`` is stored in the document for
    triage, and a restore that read it back and reported it as verification
    would authenticate the file against itself.
    """
    try:
        payload = path.read_bytes()
    except OSError as err:
        raise FoldExportError(f"could not read fold export {path}: {err}") from err
    artifact_sha256 = hashlib.sha256(payload).hexdigest()
    try:
        document = json.loads(payload)
        hash_version = int(document["hash_version"])
        rows = [
            FoldRow(
                id=int(row["id"]),
                blunder_id=int(row["blunder_id"]),
                session_id=uuid.UUID(row["session_id"]),
                occurred_at=_parse_stamp(row["occurred_at"]),
                created_at=_parse_stamp(row["created_at"]),
                opportunity=bool(row["opportunity"]),
                reached=bool(row["reached"]),
                session_started_at=_parse_stamp(row["session_started_at"]),
                blunder_created_at=_parse_stamp(row["blunder_created_at"]),
            )
            for row in document["rows"]
        ]
        loaded = LoadedExport(
            batch_id=uuid.UUID(document["batch_id"]),
            user_id=int(document["user_id"]),
            path=path,
            artifact_sha256=artifact_sha256,
            rowset_hash=canonical_rowset_hash(rows, version=hash_version),
            hash_version=hash_version,
            row_count=len(rows),
            rows=rows,
        )
    except FoldExportError:
        raise
    except (KeyError, TypeError, ValueError) as err:
        raise FoldExportError(f"fold export {path} is malformed: {err}") from err
    return loaded


def discard_export(path: Path) -> bool:
    """Remove an artifact. Returns whether a file was actually there.

    Used by expiry and by a fold whose transaction rolled back after the export
    was written — but ONLY when that transaction provably committed nothing.
    Deleting the artifact of a committed batch would delete its recovery.
    """
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as err:
        raise FoldExportError(f"could not remove fold export {path}: {err}") from err
