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
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app.evidence_policy import verify_identity
from app.analysis_trust import (
    cache_row_as_position_dict,
    position_trust_flags,
    source_rank,
)
from app.evidence_contracts import contract_satisfied
from app.models import AnalysisCache, PositionAnalysisRow, decode_uci_line
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
    return verify_identity(data)


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


# ---------------------------------------------------------------------------
# Phase 4 — read-only trusted-position resolution for payload consumers
# ---------------------------------------------------------------------------
#
# The single resolver every payload-producing consumer (tree_eval, session export,
# /api/analysis/lookup) calls so the "storage winner OR trusted legacy v2
# projection" two-tier logic lives in exactly one tested place. It owns
# position-grain ranking locally (``_legacy_position_sort_key``) rather than
# importing ``tree_eval`` — that reverse edge would form a cycle
# (position_analysis_repo → tree_eval → position_analysis_repo).


@dataclass(frozen=True)
class TrustedPosition:
    """Trusted position-grain evidence resolved for one normalized FEN.

    Evals are WHITE-RELATIVE (as stored). At least one of ``best_eval`` /
    ``best_eval_mate`` is non-None for any resolved (trusted) position — the
    position-complete contract guarantees a usable position eval — but callers
    needing a side-to-move-relative eval must sign-convert themselves.
    """

    best_move_uci: str
    best_move_san: str | None
    best_line_uci: list[str] | None
    best_eval: int | None  # white-relative
    best_eval_mate: int | None  # white-relative
    # Profile the winning row was produced with. Carried so a consumer that
    # derives a cross-grain eval loss (e.g. /api/analysis/lookup's Phase-6
    # ``position_eval_loss_cp``) can prove the position best_eval and the move
    # row's played_eval came from the SAME search strength before subtracting
    # them (see ``compare_search_strength``). None for legacy/unknown ids.
    analysis_profile_id: str | None


def get_position_analysis(
    db: Session, normalized_fen: str
) -> PositionAnalysisRow | None:
    """Read the stored winner for ``normalized_fen`` (no lock; read path).

    Distinct from :func:`_load_existing`, which takes ``FOR UPDATE`` for the write
    path. Consumers must never lock on a read.
    """
    return (
        db.query(PositionAnalysisRow)
        .filter(PositionAnalysisRow.normalized_fen == normalized_fen)
        .first()
    )


def get_position_analyses(
    db: Session, normalized_fens: Iterable[str]
) -> dict[str, PositionAnalysisRow]:
    """Batch-read stored winners for several normalized FENs (one ``IN`` query).

    Required so ``tree_eval`` / ``session`` / ``lookup`` never issue one query per
    position. Missing FENs are simply absent from the result map.
    """
    norms = list(dict.fromkeys(n for n in normalized_fens if n))
    if not norms:
        return {}
    rows = (
        db.query(PositionAnalysisRow)
        .filter(PositionAnalysisRow.normalized_fen.in_(norms))
        .all()
    )
    return {r.normalized_fen: r for r in rows}


def _legacy_position_sort_key(row: AnalysisCache) -> tuple:
    """Position-grain ranking over trusted legacy ``analysis_cache`` rows.

    Mirrors the deleted ``tree_eval._root_sort_key``: prefer mate data, then the
    canonical complete best-move row (``move_uci == best_move_uci``), then the
    deterministic source preference, then lowest id. It ranks rows that have ALREADY
    passed the position trust gate, at the NORMALIZED-FEN grain (callers must not
    prefer an exact full-FEN row first — that could miss a trusted mate/stronger row
    at a clock variant of the same normalized position).
    """
    return (
        0 if row.best_eval_mate is not None else 1,
        0 if (row.best_move_uci is not None and row.move_uci == row.best_move_uci) else 1,
        source_rank(row.source),
        row.id,
    )


def _trusted_position_from_row(row) -> TrustedPosition:
    """Build a :class:`TrustedPosition` from a storage OR analysis_cache row.

    Both row types expose the same position columns (plus
    ``analysis_profile_id``); ``best_line_uci`` is decoded from its space-joined
    storage form to a list.
    """
    return TrustedPosition(
        best_move_uci=row.best_move_uci,
        best_move_san=row.best_move_san,
        best_line_uci=decode_uci_line(row.best_line_uci),
        best_eval=row.best_eval,
        best_eval_mate=row.best_eval_mate,
        analysis_profile_id=row.analysis_profile_id,
    )


def resolve_trusted_positions(
    db: Session, normalized_fens: Iterable[str]
) -> dict[str, TrustedPosition | None]:
    """Resolve trusted position evidence for several normalized FENs (batched).

    Two tiers per FEN, each as a single ``IN`` query to avoid N+1:

    1. **Storage** — the ``position_analysis`` winner, used iff it is
       position-trusted.
    2. **Trusted legacy fallback** — the strongest position-trusted
       ``resolver-complete-v2`` ``analysis_cache`` row at the SAME normalized FEN,
       ranked by :func:`_legacy_position_sort_key` at the normalized grain.

    A ``None`` value for a FEN means no trusted position exists; the caller takes
    its own (untrusted) fallback.
    """
    norms = list(dict.fromkeys(n for n in normalized_fens if n))
    result: dict[str, TrustedPosition | None] = {n: None for n in norms}
    if not norms:
        return result

    storage = get_position_analyses(db, norms)
    remaining: list[str] = []
    for n in norms:
        row = storage.get(n)
        if row is not None and position_trust_flags(_row_to_dict(row))[2]:
            result[n] = _trusted_position_from_row(row)
        else:
            remaining.append(n)

    if not remaining:
        return result

    cache_rows = (
        db.query(AnalysisCache)
        .filter(AnalysisCache.normalized_fen_before.in_(remaining))
        .all()
    )
    by_norm: dict[str, list[AnalysisCache]] = {}
    for r in cache_rows:
        if not position_trust_flags(cache_row_as_position_dict(r))[2]:
            continue
        by_norm.setdefault(r.normalized_fen_before, []).append(r)
    for n in remaining:
        rows = by_norm.get(n)
        if rows:
            result[n] = _trusted_position_from_row(min(rows, key=_legacy_position_sort_key))
    return result


def resolve_trusted_position(
    db: Session, normalized_fen: str
) -> TrustedPosition | None:
    """Single-FEN convenience wrapper over :func:`resolve_trusted_positions`."""
    return resolve_trusted_positions(db, [normalized_fen]).get(normalized_fen)
