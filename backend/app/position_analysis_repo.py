"""DB-level writer for position_analysis native rows (Phase 3).

All live/native writes to ``position_analysis`` route through
:func:`write_position_analysis_row` so the replacement policy lives in one
tested place.

Native vs. backfill ownership is structural, keyed on ``source_cache_id``:

* **Native** rows (this module) always carry ``source_cache_id IS NULL``. The
  helper stamps NULL on every action it takes (insert / replace / merge) — it
  NEVER reads ``source_cache_id`` from the caller — so a native write can never
  masquerade as a backfilled row.
* **Backfill** rows (Phase 2, :mod:`app.position_analysis_backfill`) carry the
  originating ``analysis_cache.id`` in ``source_cache_id``. Backfill's upsert
  guard skips any row with ``source_cache_id IS NULL`` (a native winner), so once
  this helper touches a row — even one a prior backfill created — that row flips
  to native-owned and backfill will never clobber it again.

Transaction ownership: the caller owns the session and its transaction.
:func:`write_position_analysis_row` is a within-transaction helper; it reads,
decides, and stages ORM operations, but does NOT commit.  The caller is
responsible for committing or rolling back.

Concurrency model (two axes):

* **Missing-key insert race** — guarded by a SAVEPOINT. A concurrent insert of
  the same ``normalized_fen`` (the unique key) raises an IntegrityError that is
  caught, the savepoint rolls back (the caller's transaction stays intact), and
  the now-present row is re-read and re-decided as replace/merge/keep. A
  non-unique IntegrityError (NOT NULL / FK) leaves no row and is re-raised.
* **Existing-row lost update** — on Postgres the existing-row read takes
  ``SELECT ... FOR UPDATE`` (see :func:`_load_existing`), so two native writers
  serialize on the row instead of both deciding from a stale read and the later
  commit clobbering the earlier stronger update. SQLite has no row locks: callers
  on SQLite MUST serialize position writes (the canonical precompute is the only
  native producer and runs single-writer per position; the Phase-4 wiring that
  batches these writes is expected to route them through the same in-process
  write lock / ``BEGIN IMMEDIATE`` engine as :mod:`app.analysis_cache_repo`).
"""
from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app.analysis_profiles import IDENTITY_FIELDS, get_profile
from app.evidence_contracts import contract_satisfied
from app.models import PositionAnalysisRow
from app.position_analysis_policy import (
    POSITION_FACT_FIELDS,
    POSITION_METADATA_FIELDS,
    PositionDecision,
    PositionReason,
    PositionRow,
    decide_position_analysis_replacement,
    position_populated_fields_of,
)

log = logging.getLogger("position_analysis_repo")

# Columns a native write copies from the caller's data dict (excludes id /
# created_at / updated_at and, deliberately, source_cache_id — the helper owns
# that column and always stamps NULL, see module docstring).
_POSITION_WRITABLE_FIELDS = (
    "fen",
    "source",
    "evidence_contract_id",
    *POSITION_FACT_FIELDS,
    *POSITION_METADATA_FIELDS,
)


def _identity_verified(data: dict) -> bool:
    profile = get_profile(data.get("analysis_profile_id"))
    if profile is None:
        return False
    return all(data.get(f) == getattr(profile, f) for f in IDENTITY_FIELDS)


def _project_position(data: dict) -> PositionRow:
    contract_id = data.get("evidence_contract_id")
    return PositionRow(
        analysis_profile_id=data.get("analysis_profile_id"),
        evidence_contract_id=contract_id,
        identity_verified=_identity_verified(data),
        contract_satisfied=contract_satisfied(contract_id, data),
        populated_fields=position_populated_fields_of(data),
        values={f: data.get(f) for f in POSITION_FACT_FIELDS},
    )


def _row_to_dict(row: PositionAnalysisRow) -> dict:
    out: dict = {}
    for f in _POSITION_WRITABLE_FIELDS:
        out[f] = getattr(row, f, None)
    out["normalized_fen"] = row.normalized_fen
    return out


def _build_position_merged(
    existing: PositionAnalysisRow, incoming: dict, incoming_contract: str | None
) -> dict | None:
    """Merge incoming position facts into existing row, return merged dict or None.

    Returns None when the merged result fails the contract's semantic validation
    (treated as MERGE_CONFLICT_KEEP by the caller).
    """
    merged = _row_to_dict(existing)
    for f in POSITION_FACT_FIELDS:
        if merged.get(f) is None and incoming.get(f) is not None:
            merged[f] = incoming[f]
    merged["evidence_contract_id"] = incoming_contract
    if not contract_satisfied(incoming_contract, merged):
        return None
    return merged


def _load_existing(db: Session, normalized_fen: str) -> PositionAnalysisRow | None:
    """Read the current winner for ``normalized_fen``, locking it on Postgres.

    On Postgres the row is read ``FOR UPDATE`` so two concurrent native writers
    serialize on it: the second blocks until the first commits, then re-reads the
    fresh row and decides from current — not stale — state. This closes the
    lost-update window where both read the same weaker/backfilled row and the
    later commit silently overwrites the earlier stronger one. SQLite has no row
    locks; see the module docstring for its single-writer serialization contract.
    """
    query = db.query(PositionAnalysisRow).filter(
        PositionAnalysisRow.normalized_fen == normalized_fen
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        query = query.with_for_update(of=PositionAnalysisRow)
    return query.first()


def _apply_replace(existing_row: PositionAnalysisRow, data: dict) -> None:
    for f in _POSITION_WRITABLE_FIELDS:
        if f in data:
            setattr(existing_row, f, data[f])
    # A native write owns the row: flip it back to native-owned so a later
    # backfill never clobbers it (backfill skips source_cache_id IS NULL).
    existing_row.source_cache_id = None
    existing_row.updated_at = func.now()


def _apply_merge(
    existing_row: PositionAnalysisRow, data: dict, normalized_fen: str
) -> PositionReason | None:
    """Apply a MERGE to ``existing_row``; return a failure reason or None on success."""
    merged = _build_position_merged(
        existing_row, data, data.get("evidence_contract_id")
    )
    if merged is None:
        log.warning(
            "position_analysis merge failed contract re-validation for %s",
            normalized_fen,
        )
        return PositionReason.MERGE_CONFLICT_KEEP
    for f in (*POSITION_FACT_FIELDS, "evidence_contract_id"):
        if f in merged:
            setattr(existing_row, f, merged[f])
    existing_row.source_cache_id = None  # native-owned (see _apply_replace)
    existing_row.updated_at = func.now()
    return None


def write_position_analysis_row(db: Session, data: dict) -> PositionReason:
    """Stage a native position_analysis write within ``db``'s open transaction.

    The caller owns the session; this function stages ORM adds/updates but does
    NOT commit.  Returns the policy reason for the action taken (or not taken).

    ``data`` must include ``normalized_fen`` and declare
    ``evidence_contract_id="position-complete-v1"``.  Non-authoritative writes
    (browser-game profile) and invalid contract claims are rejected with an
    appropriate reason before any DB read.

    ``source_cache_id`` from ``data`` is ignored: a native write always stamps
    NULL (see module docstring). Pass backfill provenance through the backfill
    path, not here.

    Concurrency: the missing-key INSERT runs inside a SAVEPOINT. If a competing
    transaction inserts the same ``normalized_fen`` between this call's read and
    insert, the unique-constraint IntegrityError is caught, the savepoint is
    rolled back (leaving the caller's transaction intact), and the now-present
    row is re-read and re-decided as replace/merge/keep.
    """
    normalized_fen = data.get("normalized_fen")
    if not normalized_fen:
        raise ValueError("write_position_analysis_row requires normalized_fen")

    incoming_proj = _project_position(data)
    existing_row = _load_existing(db, normalized_fen)
    existing_proj: PositionRow | None = (
        _project_position(_row_to_dict(existing_row)) if existing_row else None
    )
    decision, reason = decide_position_analysis_replacement(existing_proj, incoming_proj)

    if decision is PositionDecision.INSERT:
        cols = {"normalized_fen": normalized_fen}
        for f in _POSITION_WRITABLE_FIELDS:
            if f in data:
                cols[f] = data[f]
        cols["source_cache_id"] = None  # native rows are always source_cache_id NULL
        # updated_at must be set explicitly so the audit timestamp is populated even
        # when an onupdate trigger does not fire (e.g. bulk upserts).
        cols["updated_at"] = func.now()
        try:
            with db.begin_nested():  # SAVEPOINT around the racy insert
                db.add(PositionAnalysisRow(**cols))
                db.flush()
        except IntegrityError:
            # The savepoint context manager has already rolled back to the
            # SAVEPOINT (the outer transaction is intact). Only the unique-key race
            # is recoverable: it leaves a row present for this normalized_fen. If
            # the re-read finds NO row, this IntegrityError was something else
            # (e.g. a NOT NULL / FK violation) and nothing was written — re-raise
            # rather than silently report NEW_KEY for a row that does not exist.
            existing_row = _load_existing(db, normalized_fen)
            if existing_row is None:
                raise
            existing_proj = _project_position(_row_to_dict(existing_row))
            decision, reason = decide_position_analysis_replacement(
                existing_proj, incoming_proj
            )
            if decision is PositionDecision.REPLACE:
                _apply_replace(existing_row, data)
            elif decision is PositionDecision.MERGE:
                fail = _apply_merge(existing_row, data, normalized_fen)
                if fail is not None:
                    return fail
            # KEEP/INSERT(unreachable): nothing to do.
        log.debug("position_analysis %s -> %s", normalized_fen, reason.value)
        return reason

    if decision is PositionDecision.REPLACE:
        _apply_replace(existing_row, data)
    elif decision is PositionDecision.MERGE:
        fail = _apply_merge(existing_row, data, normalized_fen)
        if fail is not None:
            return fail

    # KEEP: nothing to do.
    log.debug("position_analysis %s -> %s", normalized_fen, reason.value)
    return reason
