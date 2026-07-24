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
from collections import Counter
from contextlib import nullcontext
from itertools import groupby

from sqlalchemy import tuple_
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.analysis_cache_policy import (
    Decision,
    Reason,
    decide_analysis_cache_replacement,
    declared_profile_inactive,
    incoming_is_valid,
    project_cache_row,
)
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

# Retries for PostgreSQL deadlock / serialization-failure aborts (see
# _run_postgresql). The batch is fully re-run on a fresh session — safe because
# the whole read-decide-write unit is a single transaction.
_PG_MAX_RETRIES = 8

# Max bind parameters per statement. PostgreSQL caps the extended protocol at
# 65535; SQLite's SQLITE_MAX_VARIABLE_NUMBER is 32766 (>= 3.32). We stay well
# under the tighter (SQLite) limit so an oversized batch is split into several
# ordered statements instead of the driver raising and rolling back the whole
# transaction (losing every row). Chunks of a key-sorted run stay key-ordered,
# so the ascending lock-acquisition invariant is preserved across chunks.
_MAX_BIND_PARAMS = 30_000

# Bounded re-resolution passes for the PG-only vanished-row (TOCTOU) recovery: a
# key that conflicts on insert, is deleted before the lock, is recovered, then is
# re-created by a concurrent writer must be re-decided. Each pass consumes one
# such race; a small cap guards against a pathological repeat-deleter.
_MAX_TOCTOU_PASSES = 3


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
        proj_e, proj_i = project_cache_row(existing), project_cache_row(data)

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


def _insert_cols(data: dict) -> dict:
    """Column dict for an INSERT of ``data``.

    Absent keys are OMITTED (never sent as ``None``) so column server defaults
    apply — critically ``source`` (NOT NULL, ``server_default="game"``). The
    derived ``normalized_fen_before`` is always present.
    """
    cols = {
        k: data.get(k)
        for k in ("fen_before", "move_uci", *_WRITABLE_FIELDS)
        if k in data
    }
    cols["normalized_fen_before"] = _normalized(data["fen_before"])
    return cols


def _signature_runs(valid_rows: list[dict]) -> list[list[dict]]:
    """Split key-sorted valid rows into maximal contiguous same-signature runs.

    A "signature" is the set of present INSERT columns; a multi-row ``.values([...])``
    needs a uniform column set, so rows are grouped by signature. Grouping is done
    over CONTIGUOUS runs only (never reordered), so insert execution still visits
    keys in global ascending order — which lowers the probability that concurrent
    ``ON CONFLICT DO NOTHING`` inserts of the same new key swap order and deadlock
    on Postgres speculative-insertion locks (fewer retries). It is a deadlock-
    probability heuristic, not a guarantee of deadlock-freedom; any 40P01/40001
    that still occurs is retried by ``_run_batch_with_retry``. Every real caller
    emits a single signature → exactly one run → one INSERT.
    """
    return [list(g) for _, g in groupby(valid_rows, key=lambda r: frozenset(r["cols"]))]


def _param_chunks(rows: list[dict], params_per_row: int):
    """Yield key-ordered slices of ``rows`` that each stay under the bind-param
    budget (:data:`_MAX_BIND_PARAMS`).

    Rows arrive key-sorted, and each yielded slice is contiguous, so global
    ascending key order is preserved across chunks — the ascending order that
    lowers deadlock probability (fewer retries) for concurrent inserts / lock
    acquisitions. It is a probability heuristic, not a guarantee of deadlock-
    freedom; any 40P01/40001 that still occurs is retried by
    ``_run_batch_with_retry``.
    """
    assert params_per_row > 0, "params_per_row must be positive"
    step = max(1, _MAX_BIND_PARAMS // params_per_row)
    for i in range(0, len(rows), step):
        yield rows[i : i + step]


def _insert_missing(session: Session, rows: list[dict], *, insert) -> set[tuple[str, str]]:
    """``INSERT ... ON CONFLICT DO NOTHING`` over key-sorted ``rows``; return the
    SET of freshly inserted keys.

    Rows are grouped into contiguous same-signature runs (a multi-row ``VALUES``
    insert needs a uniform column set), and each run is split into bind-param-
    budgeted chunks. Both groupings preserve global ascending key order, so
    concurrent inserts of the same new key are much less likely to swap order and
    deadlock on Postgres speculative-insertion locks — a deadlock-probability
    heuristic, not a proof; any 40P01/40001 that still occurs is retried by
    ``_run_batch_with_retry``. ``RETURNING`` yields only rows actually inserted
    (DO NOTHING skips conflicts); callers use the SET, never position.
    (``RETURNING`` requires SQLite >= 3.35 — a documented floor for this module.)
    """
    inserted: set[tuple[str, str]] = set()
    for run in _signature_runs(rows):
        for chunk in _param_chunks(run, len(run[0]["cols"])):
            stmt = (
                insert(AnalysisCache)
                .values([r["cols"] for r in chunk])
                .on_conflict_do_nothing(
                    index_elements=[AnalysisCache.fen_before, AnalysisCache.move_uci]
                )
                .returning(AnalysisCache.fen_before, AnalysisCache.move_uci)
            )
            for row in session.execute(stmt):
                inserted.add((row[0], row[1]))
    return inserted


def _lock_existing(
    session: Session, conflicted: list[dict], *, for_update: bool
) -> dict[tuple[str, str], AnalysisCache]:
    """Read (optionally ``FOR UPDATE``-lock) the rows for key-sorted ``conflicted``.

    Split into bind-param-budgeted chunks over the two-column key tuple; each
    chunk's ``ORDER BY (fen_before, move_uci)`` and the contiguous key-sorted
    slicing mean locks are acquired in one global ascending order, which lowers
    deadlock probability (fewer retries) but does not guarantee deadlock-freedom;
    correctness under any 40P01/40001 rests on ``_run_batch_with_retry``.
    """
    existing: dict[tuple[str, str], AnalysisCache] = {}
    for chunk in _param_chunks(conflicted, 2):  # 2 bind params per key tuple
        query = (
            session.query(AnalysisCache)
            .filter(
                tuple_(AnalysisCache.fen_before, AnalysisCache.move_uci).in_(
                    [r["key"] for r in chunk]
                )
            )
            .order_by(AnalysisCache.fen_before, AnalysisCache.move_uci)
        )
        if for_update:
            query = query.with_for_update(of=AnalysisCache)
        for row in query.all():
            existing[(row.fen_before, row.move_uci)] = row
    return existing


def _resolve_conflict(
    r: dict,
    existing_row: AnalysisCache,
    reason_by_key: dict[tuple[str, str], Reason],
) -> None:
    """Apply the replacement policy for one pre-existing key (in-memory decide +
    ORM mutation); record the resulting Reason."""
    key, data, incoming_proj = r["key"], r["data"], r["proj"]
    existing_proj = project_cache_row(_row_to_dict(existing_row))
    decision, reason = decide_analysis_cache_replacement(existing_proj, incoming_proj)
    if decision is Decision.REPLACE:
        _apply_update(existing_row, data, full=True)
    elif decision is Decision.MERGE:
        merged = _build_merged(
            _row_to_dict(existing_row), data, data.get("evidence_contract_id")
        )
        if merged is None:
            reason = Reason.MERGE_CONFLICT_KEEP
        else:
            _apply_update(existing_row, merged, full=False)
    # KEEP: nothing to write.
    reason_by_key[key] = reason


def _run_batch(
    session: Session,
    surviving: list[dict],
    *,
    insert,
    for_update: bool,
) -> list[tuple[tuple[str, str], Reason]]:
    """Set-based read-decide-write for an already-deduped, key-sorted batch.

    ``insert`` is the dialect INSERT constructor (``postgresql_insert`` /
    ``sqlite_insert``); ``for_update`` toggles the ``FOR UPDATE`` lock on the
    conflict SELECT (Postgres only). Replaces the per-row loop with a bounded
    number of statements while preserving verdict semantics and the returned
    ``(key, Reason)`` cardinality/order exactly. Commits the whole batch once.
    Returns one result per surviving key in key-sorted order.
    """
    reason_by_key: dict[tuple[str, str], Reason] = {}

    # Steps 1-2: partition validity in memory (invalid rows do no DB work) and
    # hoist normalize_fen once per valid row. valid_rows stays key-sorted.
    valid_rows: list[dict] = []
    for data in surviving:
        key = _key(data)
        proj = project_cache_row(data)
        if not incoming_is_valid(proj):
            reason_by_key[key] = Reason.INVALID_INCOMING_KEEP
            continue
        # A row claiming a RETIRED profile is refused storage BEFORE the insert,
        # mirroring decide_analysis_cache_replacement's gate. Without this the
        # insert path would persist it as a phantom NEW_KEY for a missing key,
        # never reaching the replacement decision (g-reuse-d21-search P1).
        if declared_profile_inactive(proj):
            reason_by_key[key] = Reason.INACTIVE_PROFILE_KEEP
            continue
        valid_rows.append(
            {"data": data, "key": key, "proj": proj, "cols": _insert_cols(data)}
        )

    # Step 3: insert missing keys in global key order (RETURNING -> the SET of
    # freshly inserted keys). NEW_KEY for each; the rest pre-existed.
    inserted_keys = _insert_missing(session, valid_rows, insert=insert)
    for r in valid_rows:
        if r["key"] in inserted_keys:
            reason_by_key[r["key"]] = Reason.NEW_KEY

    # Steps 4-6: resolve pre-existing (conflicted) keys. A key can vanish between
    # the insert-conflict and the lock (PG-only TOCTOU: a concurrent deleter). We
    # recover it with an ordered ON CONFLICT DO NOTHING insert rather than a bare
    # add: that keeps the recovery on the same IntegrityError-free path as Step 3
    # (a concurrent writer that re-created the key wins the DO NOTHING instead of
    # aborting the whole batch), and re-decides any it kept. This recovery insert
    # is NOT deadlock-free — re-inserting under a concurrent deleter can invert the
    # ascending lock order — so a resulting 40P01/40001 is absorbed by the bounded
    # whole-transaction retry (_run_batch_with_retry), not prevented here.
    pending = [r for r in valid_rows if r["key"] not in inserted_keys]
    for _ in range(_MAX_TOCTOU_PASSES):
        if not pending:
            break
        existing_by_key = _lock_existing(session, pending, for_update=for_update)
        vanished: list[dict] = []
        for r in pending:
            existing_row = existing_by_key.get(r["key"])
            if existing_row is None:
                vanished.append(r)
            else:
                _resolve_conflict(r, existing_row, reason_by_key)
        if not vanished:
            pending = []
            break
        recovered = _insert_missing(session, vanished, insert=insert)
        for r in vanished:
            if r["key"] in recovered:
                reason_by_key[r["key"]] = Reason.NEW_KEY
        # Keys a concurrent writer re-created (not recovered) loop back to be
        # re-locked and re-decided against that live row.
        pending = [r for r in vanished if r["key"] not in recovered]

    # Terminal resolution for keys that oscillated past the pass budget (vanished
    # at every lock, lost every re-insert under a persistent concurrent deleter).
    # One final lock+decide pins whatever is now visible; anything STILL absent
    # was neither written nor resolved, so it must NOT be reported as an insert.
    # Report RECOVERY_ABORTED_KEEP and warn instead of a phantom NEW_KEY.
    if pending:
        existing_by_key = _lock_existing(session, pending, for_update=for_update)
        for r in pending:
            existing_row = existing_by_key.get(r["key"])
            if existing_row is not None:
                _resolve_conflict(r, existing_row, reason_by_key)
            else:
                reason_by_key[r["key"]] = Reason.RECOVERY_ABORTED_KEEP
                log.warning(
                    "analysis_cache recovery aborted for %r after %d passes: "
                    "concurrent-writer churn, incoming row not stored",
                    r["key"],
                    _MAX_TOCTOU_PASSES,
                )

    # Step 7: one commit for the whole batch.
    session.commit()

    # Step 8: results in key-sorted survivor order (invalid interleaved), matching
    # the per-row loop's return cardinality/order exactly.
    return [(_key(data), reason_by_key[_key(data)]) for data in surviving]


def _log_batch_summary(results: list[tuple[tuple[str, str], Reason]]) -> None:
    """One aggregate audit line per batch (replaces the per-row wall of logs).

    The per-key verdicts are still emitted at DEBUG so "which FEN/move was
    rejected and why" stays answerable from logs (the sole production caller
    discards the returned list) without the INFO-level wall of the old loop.
    """
    counts = Counter(reason.value for _, reason in results)
    rendered = ", ".join(f"{verdict}={counts[verdict]}" for verdict in sorted(counts))
    log.info("analysis_cache batch: %d rows -> %s", len(results), rendered)
    if log.isEnabledFor(logging.DEBUG):
        for (fen, uci), reason in results:
            log.debug("analysis_cache %s::%s -> %s", fen, uci, reason.value)


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

    if not surviving:
        # Every row was rejected in dedupe: no survivor to write, so skip the
        # dispatch entirely rather than take the SQLite write lock / open a PG
        # transaction only to commit nothing.
        results = []
    elif dialect == "sqlite":
        # Dedicated IMMEDIATE-mode engine for file DBs (BEGIN IMMEDIATE is scoped
        # to this engine, never the shared/read engine); caller bind for :memory:.
        write_engine = _sqlite_write_engine(bind)
        results = _run_sqlite(sessionmaker(bind=write_engine), surviving)
    else:  # postgresql: batched ON CONFLICT insert + one FOR UPDATE lock select
        results = _run_postgresql(sessionmaker(bind=bind), surviving)

    results = dedupe_results + results
    _log_batch_summary(results)
    return results


def _retryable_error_label(exc: OperationalError, dialect: str) -> str:
    """Short greppable classification of a retryable error for the retry log.

    PostgreSQL: the SQLSTATE (``40P01`` deadlock / ``40001`` serialization) pulled
    from ``exc.orig`` (psycopg2 ``pgcode`` / psycopg3 ``sqlstate``), falling back
    to the PG wording when the driver surfaces no code. SQLite: ``locked``/``busy``.
    Only ever called on an already-``is_retryable`` error, so ``unknown`` means a
    classifier/label drift rather than a spurious retry.
    """
    if dialect == "postgresql":
        orig = getattr(exc, "orig", None)
        code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
        if code:
            return code
        text = str(exc).lower()
        if "deadlock" in text:
            return "40P01"
        if "could not serialize" in text:
            return "40001"
        return "unknown"
    text = str(exc).lower()
    if "locked" in text:
        return "locked"
    if "busy" in text:
        return "busy"
    return "unknown"


def _run_batch_with_retry(
    factory,
    surviving: list[dict],
    *,
    insert,
    for_update: bool,
    is_retryable,
    max_retries: int,
    dialect: str,
    lock=None,
) -> list[tuple[tuple[str, str], Reason]]:
    """Run one batch on a fresh session, retrying transient conflicts.

    Shared driver for both dialects (they differ only in the INSERT constructor,
    the ``FOR UPDATE`` toggle, the retryable-error classifier, the retry bound,
    the ``dialect`` label, and — SQLite only — the in-process write lock held for
    the attempt). Only ``is_retryable`` errors are retried with bounded backoff;
    any other exception rolls back and propagates. The whole read-decide-write
    unit is one transaction, so a full re-run is safe.

    Every retry AND the final exhaustion emit a structured WARNING carrying the
    dialect, the classified error (PG SQLSTATE 40P01/40001 or SQLite BUSY/locked),
    the attempt number + retry bound, and the batch size — so persistent
    concurrent-writer churn (the risks-section watch-item) is greppable rather
    than a silent attempt counter. The ``dialect`` label is passed by the caller
    so the log needs no open session to name the backend.
    """
    attempt = 0
    while True:
        with (lock if lock is not None else nullcontext()):
            session = factory()
            try:
                return _run_batch(
                    session, surviving, insert=insert, for_update=for_update
                )
            except OperationalError as exc:
                session.rollback()
                if not is_retryable(exc):
                    raise
                attempt += 1
                exhausted = attempt > max_retries
                log.warning(
                    "analysis_cache batch retry %s dialect=%s error=%s "
                    "attempt=%d max_retries=%d batch_size=%d",
                    "exhausted" if exhausted else "scheduled",
                    dialect,
                    _retryable_error_label(exc, dialect),
                    attempt,
                    max_retries,
                    len(surviving),
                )
                if exhausted:
                    raise
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
        time.sleep(0.05 * attempt)


def _is_busy_error(exc: OperationalError) -> bool:
    """True for a SQLite BUSY/locked error (the competing writer's reservation)."""
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _is_retryable_pg_error(exc: OperationalError) -> bool:
    """True for a PostgreSQL deadlock or serialization failure (safe to re-run).

    ``40P01`` = deadlock_detected, ``40001`` = serialization_failure. The vanished-
    row recovery can, under a concurrent deleter, briefly invert the ascending
    lock order and trip the deadlock detector; re-running the whole batch on a
    fresh transaction resolves it (mirrors the SQLite BUSY retry).
    """
    orig = getattr(exc, "orig", None)
    pgcode = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    if pgcode in ("40P01", "40001"):
        return True
    # Fallback for a driver that doesn't surface the SQLSTATE: match the actual
    # PG wording ("could not serialize access due to ...", "deadlock detected").
    text = str(exc).lower()
    return "deadlock" in text or "could not serialize" in text


def _run_sqlite(factory, surviving: list[dict]) -> list[tuple[tuple[str, str], Reason]]:
    """Run the batch under BEGIN IMMEDIATE, retrying on SQLITE_BUSY.

    The whole batch is one transaction (the BEGIN IMMEDIATE is emitted on first
    statement). On a BUSY/locked error it rolls back and retries with bounded
    backoff, holding the in-process write lock for each attempt. No FOR UPDATE —
    the write reservation already serializes writers.
    """
    return _run_batch_with_retry(
        factory,
        surviving,
        insert=sqlite_insert,
        for_update=False,
        is_retryable=_is_busy_error,
        max_retries=_SQLITE_MAX_RETRIES,
        dialect="sqlite",
        lock=_sqlite_write_lock,
    )


def _run_postgresql(factory, surviving: list[dict]) -> list[tuple[tuple[str, str], Reason]]:
    """Run the batch with a single FOR UPDATE lock select, retrying deadlocks."""
    return _run_batch_with_retry(
        factory,
        surviving,
        insert=postgresql_insert,
        for_update=True,
        is_retryable=_is_retryable_pg_error,
        max_retries=_PG_MAX_RETRIES,
        dialect="postgresql",
    )
