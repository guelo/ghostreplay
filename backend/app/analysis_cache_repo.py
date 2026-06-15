"""Shared, transaction-safe writer for analysis_cache rows.

All cache writers (session uploads, precompute, JeffML ingest) route through
:func:`write_analysis_cache_rows` so the quality-aware replacement policy and the
missing-key-safe locking protocol live in one place.

Transaction ownership: the helper receives ``caller_session`` only to (a) assert
the clean-session precondition and (b) derive a factory from its bind. It opens
its OWN session and owns the entire atomic read-decide-write transaction; it
never reads, writes, or commits through ``caller_session``.
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.analysis_cache_policy import (
    CacheRow,
    Decision,
    Reason,
    decide_analysis_cache_replacement,
    incoming_is_valid,
    populated_fields_of,
)
from app.analysis_profiles import IDENTITY_FIELDS, get_profile
from app.evidence_contracts import contract_satisfied, get_contract
from app.fen import normalize_fen
from app.models import AnalysisCache

log = logging.getLogger("analysis_cache_repo")

# Serializes SQLite read-decide-write units within this process. Cross-process
# serialization is provided by the dedicated write engine's BEGIN IMMEDIATE +
# busy_timeout (see _sqlite_write_engine); this in-process lock just avoids
# threads in the same process fighting over the same connection.
_sqlite_write_lock = threading.Lock()

# Dedicated, IMMEDIATE-mode write engines keyed by SQLite file URL. We never put
# BEGIN IMMEDIATE on the shared/application engine — that would force ordinary
# read-only sessions to take write reservations too. Instead the helper writes
# exclusively through these engines so the eager write lock is scoped to cache
# writes only.
_sqlite_write_engines: dict[str, object] = {}
_sqlite_config_lock = threading.Lock()

# How long SQLite waits for a competing writer's lock before raising BUSY.
_SQLITE_BUSY_TIMEOUT_MS = 10_000
_SQLITE_MAX_RETRIES = 8


def _sqlite_write_engine(bind):
    """Return the engine + factory to use for a SQLite write batch.

    For file-backed databases this is a dedicated engine configured to emit
    ``BEGIN IMMEDIATE`` (so the reserved write lock is taken before the read) with
    a ``busy_timeout`` so competing writers wait rather than fail. The dedicated
    engine is used ONLY by this helper, so application read transactions on the
    shared engine are never forced into write reservations.

    For in-memory databases (StaticPool, single connection, single process) the
    caller's own bind is reused — a separate engine wouldn't share the data — and
    correctness relies on the in-process write lock.
    """
    from sqlalchemy import create_engine, event

    url = bind.url
    if url.database in (None, "", ":memory:"):
        return bind
    key = str(url)
    with _sqlite_config_lock:
        engine = _sqlite_write_engines.get(key)
        if engine is not None:
            return engine
        engine = create_engine(str(url))

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - thin
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
            cur.close()

        @event.listens_for(engine, "begin")
        def _emit_begin_immediate(conn):  # pragma: no cover - thin
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        _sqlite_write_engines[key] = engine
        return engine

# Columns written to analysis_cache (excludes id / created_at autogen).
_METADATA_FIELDS = (
    "analysis_profile_id",
    "engine_name",
    "engine_version",
    "engine_build",
    "network_id",
    "search_limit_type",
    "search_limit_value",
    "threads",
    "hash_mb",
    "multipv",
    "eval_file_id",
    "eval_file_small_id",
    "analyzer_protocol_version",
    "profile_manifest_digest",
    "evidence_contract_id",
)
_EVIDENCE_FIELDS = (
    "best_move_uci",
    "best_move_san",
    "best_line_uci",
    "played_eval",
    "played_eval_mate",
    "best_eval",
    "best_eval_mate",
    "eval_delta",
    "classification",
)
_WRITABLE_FIELDS = ("move_san", "source", *_METADATA_FIELDS, *_EVIDENCE_FIELDS)


class UnsupportedDialectError(RuntimeError):
    """Raised when the bound dialect has no safe missing-key write protocol."""


def _identity_verified(data: dict) -> bool:
    """True when stored identity metadata matches the claimed profile."""
    profile = get_profile(data.get("analysis_profile_id"))
    if profile is None:
        return False
    return all(data.get(f) == getattr(profile, f) for f in IDENTITY_FIELDS)


def _project(data: dict) -> CacheRow:
    contract_id = data.get("evidence_contract_id")
    return CacheRow(
        analysis_profile_id=data.get("analysis_profile_id"),
        evidence_contract_id=contract_id,
        identity_verified=_identity_verified(data),
        contract_satisfied=contract_satisfied(contract_id, data),
        populated_fields=populated_fields_of(data),
        values={f: data.get(f) for f in _EVIDENCE_FIELDS},
    )


def _row_to_dict(row: AnalysisCache) -> dict:
    out = {
        "fen_before": row.fen_before,
        "move_uci": row.move_uci,
        "move_san": row.move_san,
        "source": row.source,
    }
    for f in (*_METADATA_FIELDS, *_EVIDENCE_FIELDS):
        out[f] = getattr(row, f, None)
    return out


def _key(data: dict) -> tuple[str, str]:
    return (data["fen_before"], data["move_uci"])


def _normalized(fen: str) -> str | None:
    """Normalized 4-field FEN for the indexed transposition fallback, or None.

    Derived from the immutable ``fen_before`` key — set only on INSERT (the key,
    and therefore this value, never changes on REPLACE/MERGE). Returns None on an
    unparseable FEN so the row still serves exact-key lookups.
    """
    try:
        return normalize_fen(fen)
    except Exception:
        return None


def _dedupe_batch(rows: list[dict]) -> tuple[list[dict], list[tuple[tuple[str, str], Reason]]]:
    """Collapse intra-batch duplicate keys deterministically (order-independent).

    Two rows for the same key may collapse only when ALL of the following hold,
    so the survivor never depends on input order:
      * same ``analysis_profile_id`` and identical identity metadata (same
        producer — otherwise the metadata to keep is ambiguous);
      * their contracts are comparable (one a registered superset of the other);
      * every overlapping non-null evidence field agrees;
      * one row's populated-field set is a superset of the other's (so "most
        complete" is well-defined).
    The most-complete row survives. Any violation rejects the key for the batch
    (logged ``DUPLICATE_CONFLICT``).

    Returns surviving rows plus (key, Reason) for keys rejected as conflicting.
    """
    from app.evidence_contracts import is_strict_successor, is_superset_or_successor

    def _collapse_pair(a: dict, b: dict) -> dict | None:
        """Union two comparable same-producer rows under the most-advanced contract.

        Returns the merged row, or ``None`` when the union fails the surviving
        contract's validation (treated as a conflict by the caller).
        """
        ca, cb = a.get("evidence_contract_id"), b.get("evidence_contract_id")
        if is_strict_successor(cb, ca):
            contract = cb
        elif is_strict_successor(ca, cb):
            contract = ca
        else:  # equal (or incomparable, already excluded): keep incumbent's
            contract = ca
        merged = dict(a)
        for f in _EVIDENCE_FIELDS:
            if merged.get(f) is None and b.get(f) is not None:
                merged[f] = b[f]
        merged["evidence_contract_id"] = contract
        if not contract_satisfied(contract, merged):
            return None
        return merged

    by_key: dict[tuple[str, str], dict] = {}
    rejected: list[tuple[tuple[str, str], Reason]] = []
    conflicted: set[tuple[str, str]] = set()

    # Producer identity for collapsing: profile + engine/search metadata + the
    # move SAN and provenance. evidence_contract_id is deliberately EXCLUDED here
    # — contract comparability is checked separately, so a minimal row and a
    # resolver-complete row from the same producer can still collapse.
    producer_fields = tuple(f for f in _METADATA_FIELDS if f != "evidence_contract_id") + (
        "move_san",
        "source",
    )

    def _producer_equal(a: dict, b: dict) -> bool:
        return all(a.get(f) == b.get(f) for f in producer_fields)

    for data in rows:
        key = _key(data)
        if key in conflicted:
            continue
        if key not in by_key:
            by_key[key] = data
            continue
        existing = by_key[key]
        proj_e, proj_i = _project(existing), _project(data)

        # Validity (contract + identity) trumps everything: a row that is invalid
        # — failed contract OR an unverifiable profile claim — must never suppress
        # a valid one, even if the two are otherwise identical. Prefer the valid
        # candidate without raising a conflict.
        valid_e, valid_i = incoming_is_valid(proj_e), incoming_is_valid(proj_i)
        if valid_e != valid_i:
            if valid_i:
                by_key[key] = data
            continue
        if not valid_e:  # both invalid: keep incumbent, rejected downstream
            continue

        # Both valid: collapse only when same producer with comparable, agreeing
        # evidence; otherwise the survivor would be ambiguous -> conflict.
        overlap = proj_e.populated_fields & proj_i.populated_fields
        agree = all(proj_e.values.get(f) == proj_i.values.get(f) for f in overlap)
        contracts_comparable = is_superset_or_successor(
            data.get("evidence_contract_id"), existing.get("evidence_contract_id")
        ) or is_superset_or_successor(
            existing.get("evidence_contract_id"), data.get("evidence_contract_id")
        ) or data.get("evidence_contract_id") == existing.get("evidence_contract_id")
        fields_comparable = (
            proj_i.populated_fields >= proj_e.populated_fields
            or proj_e.populated_fields >= proj_i.populated_fields
        )
        if (
            agree
            and contracts_comparable
            and fields_comparable
            and _producer_equal(existing, data)
        ):
            # Both valid and same producer: the survivor is the UNION of their
            # evidence under the most-advanced contract, computed order-
            # independently. This matches what sequential comparator merges would
            # produce (e.g. v1 with best_eval_mate + v2 without -> the merged row
            # carries best_eval_mate AND the v2 contract), instead of dropping
            # either the richer evidence or the successor contract.
            merged = _collapse_pair(existing, data)
            if merged is None:
                conflicted.add(key)
                del by_key[key]
                rejected.append((key, Reason.DUPLICATE_CONFLICT))
            else:
                by_key[key] = merged
        else:
            conflicted.add(key)
            del by_key[key]
            rejected.append((key, Reason.DUPLICATE_CONFLICT))

    return list(by_key.values()), rejected


def _build_merged(existing: dict, incoming: dict, incoming_contract: str | None) -> dict | None:
    """Construct a merge candidate: existing provenance + filled evidence fields.

    Returns ``None`` when the merged candidate fails its contract's validate.
    """
    merged = dict(existing)
    for f in _EVIDENCE_FIELDS:
        if merged.get(f) is None and incoming.get(f) is not None:
            merged[f] = incoming[f]
    merged["evidence_contract_id"] = incoming_contract
    if not contract_satisfied(incoming_contract, merged):
        return None
    return merged


def _apply_update(row: AnalysisCache, data: dict, *, full: bool) -> None:
    """Apply REPLACE (full) or MERGE (evidence + contract only) to an ORM row."""
    if full:
        for f in _WRITABLE_FIELDS:
            if f in data:
                setattr(row, f, data[f])
    else:
        for f in (*_EVIDENCE_FIELDS, "evidence_contract_id"):
            if f in data:
                setattr(row, f, data[f])


def _process_row(session: Session, data: dict) -> Reason:
    """Read-decide-write a single already-locked key. Caller holds the lock."""
    key = _key(data)
    existing_row = (
        session.query(AnalysisCache)
        .filter(
            AnalysisCache.fen_before == key[0],
            AnalysisCache.move_uci == key[1],
        )
        .with_for_update(of=AnalysisCache)
        .first()
        if session.bind and session.bind.dialect.name == "postgresql"
        else session.query(AnalysisCache)
        .filter(
            AnalysisCache.fen_before == key[0],
            AnalysisCache.move_uci == key[1],
        )
        .first()
    )

    incoming_proj = _project(data)
    existing_proj = _project(_row_to_dict(existing_row)) if existing_row else None
    decision, reason = decide_analysis_cache_replacement(existing_proj, incoming_proj)

    if decision is Decision.INSERT:
        cols = {k: data.get(k) for k in ("fen_before", "move_uci", *_WRITABLE_FIELDS) if k in data}
        cols["normalized_fen_before"] = _normalized(data["fen_before"])
        session.add(AnalysisCache(**cols))
    elif decision is Decision.REPLACE:
        _apply_update(existing_row, data, full=True)
    elif decision is Decision.MERGE:
        merged = _build_merged(
            _row_to_dict(existing_row), data, data.get("evidence_contract_id")
        )
        if merged is None:
            return Reason.MERGE_CONFLICT_KEEP
        _apply_update(existing_row, merged, full=False)
    # KEEP: nothing to do.
    return reason


def write_analysis_cache_rows(
    caller_session: Session,
    rows: list[dict],
) -> list[tuple[tuple[str, str], Reason]]:
    """Atomically apply the replacement policy to a batch of cache rows.

    Opens its own isolated session from ``caller_session``'s bind and owns the
    whole transaction. Returns one ``(key, Reason)`` per processed/rejected row.
    """
    if not rows:
        return []
    if caller_session.in_transaction():
        raise RuntimeError(
            "write_analysis_cache_rows requires a clean session "
            "(no open transaction); commit/rollback caller state first."
        )

    bind = caller_session.get_bind()
    dialect = bind.dialect.name if bind else ""
    if dialect not in ("sqlite", "postgresql"):
        raise UnsupportedDialectError(
            f"analysis_cache writes unsupported on dialect {dialect!r}"
        )

    surviving, dedupe_results = _dedupe_batch(rows)
    # Sort by key so concurrent overlapping batches lock in the same order.
    surviving.sort(key=_key)

    if dialect == "sqlite":
        # Dedicated IMMEDIATE-mode engine for file DBs (BEGIN IMMEDIATE is scoped
        # to this engine, never the shared/read engine); caller bind for :memory:.
        write_engine = _sqlite_write_engine(bind)
        results = _run_sqlite(sessionmaker(bind=write_engine), surviving)
    else:  # postgresql: insert-first + FOR UPDATE per key
        results = _run_postgresql(sessionmaker(bind=bind), surviving)

    results = dedupe_results + results
    for key, reason in results:
        log.info("analysis_cache %s::%s -> %s", key[0], key[1], reason.value)
    return results


def _run_sqlite(factory, surviving: list[dict]) -> list[tuple[tuple[str, str], Reason]]:
    """Run the batch under BEGIN IMMEDIATE, retrying on SQLITE_BUSY.

    The whole batch is one transaction (the BEGIN IMMEDIATE is emitted on first
    statement). On a BUSY/locked error the transaction is rolled back and the
    entire batch is retried with bounded backoff.
    """
    attempt = 0
    while True:
        with _sqlite_write_lock:
            session = factory()
            try:
                out: list[tuple[tuple[str, str], Reason]] = []
                for data in surviving:
                    out.append((_key(data), _process_row(session, data)))
                session.commit()
                return out
            except OperationalError as exc:
                session.rollback()
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                attempt += 1
                if attempt > _SQLITE_MAX_RETRIES:
                    raise
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
        time.sleep(0.05 * attempt)


def _run_postgresql(factory, surviving: list[dict]) -> list[tuple[tuple[str, str], Reason]]:
    session = factory()
    try:
        out: list[tuple[tuple[str, str], Reason]] = []
        for data in surviving:
            out.append((_key(data), _process_pg_row(session, data)))
        session.commit()
        return out
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _process_pg_row(session: Session, data: dict) -> Reason:
    """PostgreSQL: validate, insert-first, then FOR UPDATE + decide."""
    incoming_proj = _project(data)
    # Same validity gate as the comparator: contract satisfied AND no
    # unverifiable profile claim. The insert-success path must not bypass this.
    if not incoming_is_valid(incoming_proj):
        return Reason.INVALID_INCOMING_KEEP

    insert_cols = {
        k: data.get(k)
        for k in ("fen_before", "move_uci", *_WRITABLE_FIELDS)
        if k in data
    }
    insert_cols["normalized_fen_before"] = _normalized(data["fen_before"])
    stmt = (
        postgresql_insert(AnalysisCache)
        .values(insert_cols)
        .on_conflict_do_nothing(
            index_elements=[AnalysisCache.fen_before, AnalysisCache.move_uci]
        )
    )
    result = session.execute(stmt)
    if result.rowcount and result.rowcount > 0:
        # We inserted the (already-validated) row; nothing else to do.
        return Reason.NEW_KEY
    # Row pre-existed: lock it and run the comparator.
    return _process_row(session, data)
