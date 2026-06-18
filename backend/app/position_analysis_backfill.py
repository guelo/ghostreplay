"""Backfill canonical position winners into ``position_analysis`` (Phase 2).

Reads existing ``analysis_cache`` rows, GROUPS them by ``normalized_fen``, picks
exactly one canonical winner per group via :mod:`app.position_analysis_policy`,
writes the winner into ``position_analysis``, and appends best-move / mate-winner
disagreements to ``position_analysis_conflicts``.

Importable + tested (mirrors ``scripts/backfill_ghost_graph.py``): a thin CLI in
``scripts/backfill_position_analysis.py`` calls :func:`backfill_position_analysis`.

Idempotency / safety contracts:
  * **Group first, then pick** — the winner is chosen by explicit
    strength-then-deterministic dominance, never by DB conflict-clause ordering.
  * **Conditional no-op** — an unchanged recomputed winner does not touch the row,
    so ``updated_at`` never churns on reruns.
  * **Native-row protection** — backfill owns ONLY rows it wrote
    (``source_cache_id IS NOT NULL``); it never overwrites a Phase-3 native write
    (``source_cache_id IS NULL``).
  * **Append-only conflicts** — disagreements are appended and deduped by content
    signature; prior rows are never deleted.
  * Never sources engine truth from ``OpeningPositionScore`` (aggregate win-rate
    data, no engine fields).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.analysis_profiles import AUTHORITATIVE_PROFILE_PRIORITY
from app.evidence_contracts import RESOLVER_COMPLETE_V2, _validate_position_complete
from app.fen import normalize_fen
from app.models import AnalysisCache, PositionAnalysisConflict, PositionAnalysisRow
from app.position_analysis_policy import (
    POSITION_FACT_FIELDS,
    POSITION_METADATA_FIELDS,
    PositionCandidate,
    conflict_signature,
    is_eligible_position_candidate,
    position_conflict_axes,
    select_position_winner,
)

log = logging.getLogger("position_analysis_backfill")

POSITION_COMPLETE_CONTRACT = "position-complete-v1"

# Columns the eligibility gate reads from a cache row (identity + grain facts).
_ELIGIBILITY_FIELDS = (
    "analysis_profile_id",
    "evidence_contract_id",
    "engine_name",
    "engine_version",
    "engine_build",
    "eval_file_id",
    "eval_file_small_id",
    "search_limit_type",
    "search_limit_value",
    "threads",
    "hash_mb",
    "multipv",
    "analyzer_protocol_version",
    "profile_manifest_digest",
    *POSITION_FACT_FIELDS,
)


@dataclass
class PositionBackfillStats:
    groups_scanned: int = 0
    candidates_eligible: int = 0
    winners_inserted: int = 0
    winners_updated: int = 0
    winners_unchanged: int = 0
    skipped_existing_protected: int = 0
    conflicts_recorded: int = 0
    conflicts_skipped_duplicate: int = 0
    skipped_no_eligible: int = 0
    skipped_unparseable_fen: int = 0


def _normalized_or_none(fen: str) -> str | None:
    try:
        return normalize_fen(fen)
    except Exception:
        return None


def _eligibility_dict(row: AnalysisCache) -> dict:
    return {f: getattr(row, f, None) for f in _ELIGIBILITY_FIELDS}


def _build_candidate(row: AnalysisCache, normalized_fen: str) -> PositionCandidate:
    return PositionCandidate(
        cache_id=row.id,
        normalized_fen=normalized_fen,
        fen=row.fen_before,
        profile_id=row.analysis_profile_id,
        contract_id=row.evidence_contract_id,
        source=row.source,
        best_move_uci=row.best_move_uci,
        best_move_san=row.best_move_san,
        best_line_uci=row.best_line_uci,
        best_eval=row.best_eval,
        best_eval_mate=row.best_eval_mate,
        metadata={f: getattr(row, f, None) for f in POSITION_METADATA_FIELDS},
    )


def _winner_stamp(winner: PositionCandidate) -> dict:
    """Field-set written to ``position_analysis`` for a backfilled winner.

    ``source`` / ``evidence_contract_id`` are the position grain's own values; the
    legacy-v2 provenance is preserved via ``source_cache_id``. ``metadata`` carries
    the engine/search columns and never overwrites those fixed fields (it excludes
    both by construction).
    """
    stamp = {
        "normalized_fen": winner.normalized_fen,
        "fen": winner.fen,
        "best_move_uci": winner.best_move_uci,
        "best_move_san": winner.best_move_san,
        "best_line_uci": winner.best_line_uci,
        "best_eval": winner.best_eval,
        "best_eval_mate": winner.best_eval_mate,
        "source": "precomputed",
        "evidence_contract_id": POSITION_COMPLETE_CONTRACT,
        "source_cache_id": winner.cache_id,
    }
    stamp.update(winner.metadata)
    return stamp


def _stamp_position_contract_data(stamp: dict) -> dict:
    return {
        "best_move_uci": stamp["best_move_uci"],
        "best_line_uci": stamp["best_line_uci"],
        "best_eval": stamp["best_eval"],
        "best_eval_mate": stamp["best_eval_mate"],
    }


def _stamp_equals_row(stamp: dict, row: PositionAnalysisRow) -> bool:
    """True when every stamped field already matches the stored row (conditional
    no-op; id / created_at / updated_at are not part of the stamp)."""
    return all(getattr(row, k) == v for k, v in stamp.items())


def _json_or_none(value) -> str | None:
    return None if value is None else json.dumps(value, separators=(",", ":"))


def _stored_conflict_signature(row: PositionAnalysisConflict) -> str:
    """Reconstruct a stored conflict row's content signature for dedupe."""
    axes = {
        "candidate_cache_ids": json.loads(row.candidate_cache_ids)
        if row.candidate_cache_ids
        else None,
        "best_move_disagreement": json.loads(row.best_move_disagreement)
        if row.best_move_disagreement
        else None,
        "pv_disagreement": json.loads(row.pv_disagreement)
        if row.pv_disagreement
        else None,
        "best_eval_disagreement": json.loads(row.best_eval_disagreement)
        if row.best_eval_disagreement
        else None,
        "best_eval_mate_disagreement": json.loads(row.best_eval_mate_disagreement)
        if row.best_eval_mate_disagreement
        else None,
    }
    return conflict_signature(axes, row.policy_reason)


def _load_candidate_rows(
    db: Session, *, normalized_fen: str | None, limit: int | None
) -> list[AnalysisCache]:
    """SQL pre-filter; Python applies the full eligibility check afterwards.

    The pre-filter cheaply discards the obvious non-candidates (non-v2 contracts,
    non-authoritative profiles, missing best move / PV); ``AUTHORITATIVE_PROFILE_PRIORITY``
    lists exactly the authoritative profiles (a new one must be added there).

    A targeted run (``normalized_fen``) ALSO loads rows whose
    ``normalized_fen_before`` is NULL — legacy rows written before that column
    existed whose group key is only known after a Python ``normalize_fen``. Without
    this, a targeted repair would silently miss the very rows a full run would
    backfill. ``--limit`` is intentionally NOT applied to a targeted run: a single
    position is already bounded.

    A non-targeted ``--limit`` run is a DRY-RUN-ONLY smoke (the caller enforces
    this). A row cap cannot be made group-safe in general: NULL-normalized legacy
    rows are not SQL-orderable, so a cap can load one sibling of a FEN while its
    NULL/non-NULL sibling lies beyond the cap — in either NULLs-first (SQLite) or
    NULLs-last (Postgres) ordering. Rather than persist a winner from such a partial
    candidate set, ``--limit`` simply never persists.
    """
    query = db.query(AnalysisCache).filter(
        AnalysisCache.evidence_contract_id == RESOLVER_COMPLETE_V2,
        AnalysisCache.analysis_profile_id.in_(AUTHORITATIVE_PROFILE_PRIORITY),
        AnalysisCache.best_move_uci.isnot(None),
        AnalysisCache.best_line_uci.isnot(None),
    )
    if normalized_fen is not None:
        return (
            query.filter(
                or_(
                    AnalysisCache.normalized_fen_before == normalized_fen,
                    AnalysisCache.normalized_fen_before.is_(None),
                )
            )
            .order_by(AnalysisCache.normalized_fen_before.asc(), AnalysisCache.id.asc())
            .all()
        )
    query = query.order_by(
        AnalysisCache.normalized_fen_before.asc(), AnalysisCache.id.asc()
    )
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def _group_by_normalized_fen(
    rows: list[AnalysisCache], stats: PositionBackfillStats
) -> dict[str, list[AnalysisCache]]:
    """Group rows by ``normalized_fen`` (NULL ``normalized_fen_before`` falls back
    to a Python ``normalize_fen``; an unparseable FEN is counted and skipped)."""
    groups: dict[str, list[AnalysisCache]] = {}
    for row in rows:
        nf = row.normalized_fen_before or _normalized_or_none(row.fen_before)
        if nf is None:
            stats.skipped_unparseable_fen += 1
            continue
        groups.setdefault(nf, []).append(row)
    return groups


def _upsert_winner(
    db: Session,
    normalized_fen: str,
    stamp: dict,
    stats: PositionBackfillStats,
) -> int | None:
    """Insert / conditionally update the winner with the ownership guard.

    Returns the id of the ``position_analysis`` row this backfill OWNS for the FEN
    (insert/update/unchanged), or ``None`` when no backfilled winner was written —
    i.e. the FEN is held by a protected native row. A conflict recorded for a
    protected FEN therefore carries ``position_analysis_id = NULL`` rather than
    falsely pointing at the unrelated native winner.
    """
    existing = (
        db.query(PositionAnalysisRow)
        .filter(PositionAnalysisRow.normalized_fen == normalized_fen)
        .one_or_none()
    )
    if existing is None:
        new_row = PositionAnalysisRow(**stamp)
        db.add(new_row)
        db.flush()  # assign id for the conflict FK
        stats.winners_inserted += 1
        return new_row.id
    if existing.source_cache_id is None:
        # A future Phase-3 native winner: backfill never clobbers it AND never
        # claims it as the backfill-selected winner.
        stats.skipped_existing_protected += 1
        return None
    if _stamp_equals_row(stamp, existing):
        stats.winners_unchanged += 1
        return existing.id
    for key, value in stamp.items():
        setattr(existing, key, value)
    stats.winners_updated += 1
    return existing.id


def _record_conflict(
    db: Session,
    normalized_fen: str,
    position_analysis_id: int | None,
    candidates: list[PositionCandidate],
    policy_reason: str,
    stats: PositionBackfillStats,
) -> None:
    """Append a conflict row, deduped by content signature (append-only)."""
    axes = position_conflict_axes(candidates)
    signature = conflict_signature(axes, policy_reason)
    existing = (
        db.query(PositionAnalysisConflict)
        .filter(PositionAnalysisConflict.normalized_fen == normalized_fen)
        .all()
    )
    if any(_stored_conflict_signature(c) == signature for c in existing):
        stats.conflicts_skipped_duplicate += 1
        return
    db.add(
        PositionAnalysisConflict(
            normalized_fen=normalized_fen,
            position_analysis_id=position_analysis_id,
            candidate_cache_ids=_json_or_none(axes["candidate_cache_ids"]),
            candidate_summaries=_json_or_none(axes["candidate_summaries"]),
            best_move_disagreement=_json_or_none(axes["best_move_disagreement"]),
            pv_disagreement=_json_or_none(axes["pv_disagreement"]),
            best_eval_disagreement=_json_or_none(axes["best_eval_disagreement"]),
            best_eval_mate_disagreement=_json_or_none(
                axes["best_eval_mate_disagreement"]
            ),
            policy_reason=policy_reason,
        )
    )
    stats.conflicts_recorded += 1


def backfill_position_analysis(
    db: Session,
    *,
    normalized_fen: str | None = None,
    limit: int | None = None,
    batch_size: int = 500,
    progress_every: int = 1000,
    dry_run: bool = False,
) -> PositionBackfillStats:
    """Backfill one canonical winner per ``normalized_fen`` from ``analysis_cache``.

    When ``dry_run`` the work is performed (so stats are accurate) but a single
    rollback at the end discards every write. ``normalized_fen`` targets a single
    position (e.g. g-ul4p repair).

    ``limit`` is a DRY-RUN-ONLY smoke knob and requires ``dry_run`` — a row cap can
    load a partial candidate set for a ``normalized_fen`` (a NULL/non-NULL sibling
    may lie beyond the cap under either NULLs-first or NULLs-last ordering), so it
    must never persist a winner. A real backfill is the unlimited run, which loads
    every row of every group and is internally batched via ``batch_size``.
    """
    if limit is not None and not dry_run:
        raise ValueError(
            "limit requires dry_run=True: a row cap can load an incomplete "
            "candidate set for a position, so a limited run must not persist. "
            "Run the unlimited backfill (batched via batch_size) for real writes."
        )

    stats = PositionBackfillStats()
    rows = _load_candidate_rows(db, normalized_fen=normalized_fen, limit=limit)
    groups = _group_by_normalized_fen(rows, stats)

    if normalized_fen is not None:
        # Targeted: keep only the requested position. NULL-normalized rows from
        # OTHER FENs were loaded to catch legacy matches and are discarded here.
        groups = {nf: g for nf, g in groups.items() if nf == normalized_fen}

    for nf, group_rows in groups.items():
        stats.groups_scanned += 1
        candidates = [
            _build_candidate(row, nf)
            for row in group_rows
            if is_eligible_position_candidate(_eligibility_dict(row))
        ]
        if not candidates:
            stats.skipped_no_eligible += 1
            continue
        stats.candidates_eligible += len(candidates)

        selection = select_position_winner(candidates)
        stamp = _winner_stamp(selection.winner)
        if not _validate_position_complete(_stamp_position_contract_data(stamp)):
            # Unreachable given eligibility already validated the projection; guard
            # against a future regression rather than persist an invalid winner.
            log.warning(
                "position_analysis backfill: winner failed position-complete "
                "re-validation for %s (cache_id=%s); skipping",
                nf,
                selection.winner.cache_id,
            )
            stats.skipped_no_eligible += 1
            continue

        position_id = _upsert_winner(db, nf, stamp, stats)
        if selection.is_conflict:
            _record_conflict(
                db, nf, position_id, candidates, selection.policy_reason, stats
            )

        if not dry_run and batch_size > 0 and stats.groups_scanned % batch_size == 0:
            db.commit()
        if progress_every > 0 and stats.groups_scanned % progress_every == 0:
            print(
                f"groups={stats.groups_scanned} "
                f"inserted={stats.winners_inserted} updated={stats.winners_updated} "
                f"unchanged={stats.winners_unchanged} "
                f"protected={stats.skipped_existing_protected} "
                f"conflicts={stats.conflicts_recorded}",
                flush=True,
            )

    if dry_run:
        db.rollback()
    else:
        db.commit()
    return stats
