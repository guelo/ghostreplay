"""DB-level writer for position_analysis native rows (Phase 3).

All live/native writes to ``position_analysis`` route through
:func:`write_position_analysis_row` so the replacement policy lives in one
tested place.

*Native* writes are those produced by the canonical precompute pipeline directly
(``source_cache_id IS NULL``).  Backfill writes (Phase 2) have their own path in
:mod:`app.position_analysis_backfill` and set ``source_cache_id`` to the originating
``analysis_cache.id``.  The backfill respects the native-row protection guard
(``source_cache_id IS NULL`` rows are never overwritten by backfill).

Transaction ownership: the caller owns the session and its transaction.
:func:`write_position_analysis_row` is a within-transaction helper; it reads,
decides, and stages ORM operations, but does NOT commit.  The caller is
responsible for committing or rolling back.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app.analysis_profiles import IDENTITY_FIELDS, get_profile
from app.evidence_contracts import POSITION_COMPLETE, contract_satisfied
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

# Columns written to position_analysis by native writes (excludes id / created_at).
_POSITION_WRITABLE_FIELDS = (
    "fen",
    "source",
    "evidence_contract_id",
    "source_cache_id",
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


def write_position_analysis_row(db: Session, data: dict) -> PositionReason:
    """Stage a native position_analysis write within ``db``'s open transaction.

    The caller owns the session; this function stages ORM adds/updates but does
    NOT commit.  Returns the policy reason for the action taken (or not taken).

    ``data`` must include ``normalized_fen`` and declare
    ``evidence_contract_id="position-complete-v1"``.  Non-authoritative writes
    (browser-game profile) and invalid contract claims are rejected with an
    appropriate reason before any DB read.

    ``source_cache_id`` should be ``None`` for native writes (omit the key or
    pass ``None`` explicitly); the backfill path sets it to the originating
    ``analysis_cache.id``.
    """
    normalized_fen = data.get("normalized_fen")
    if not normalized_fen:
        raise ValueError("write_position_analysis_row requires normalized_fen")

    # Fast-path validity check before touching the DB.
    incoming_proj = _project_position(data)

    existing_row = (
        db.query(PositionAnalysisRow)
        .filter(PositionAnalysisRow.normalized_fen == normalized_fen)
        .first()
    )

    existing_proj: PositionRow | None = (
        _project_position(_row_to_dict(existing_row)) if existing_row else None
    )

    decision, reason = decide_position_analysis_replacement(existing_proj, incoming_proj)

    if decision is PositionDecision.INSERT:
        cols = {"normalized_fen": normalized_fen}
        for f in _POSITION_WRITABLE_FIELDS:
            if f in data:
                cols[f] = data[f]
        cols.setdefault("source_cache_id", None)
        # updated_at must be set explicitly on INSERT so the audit timestamp is
        # populated even when the onupdate trigger does not fire (e.g. bulk upserts).
        cols["updated_at"] = func.now()
        db.add(PositionAnalysisRow(**cols))
        db.flush()  # make the new row visible to subsequent queries in the same tx

    elif decision is PositionDecision.REPLACE:
        for f in _POSITION_WRITABLE_FIELDS:
            if f in data:
                setattr(existing_row, f, data[f])
        existing_row.updated_at = func.now()

    elif decision is PositionDecision.MERGE:
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
        existing_row.updated_at = func.now()

    # KEEP: nothing to do.
    log.debug("position_analysis %s -> %s", normalized_fen, reason.value)
    return reason
