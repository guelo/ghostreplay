"""Exact B50 persistence and the shared legacy/current publication boundary.

No production switch is exposed yet: callers default to legacy. Readers of current
rows must fence a handle after their bounded reads (or use a repeatable snapshot).
A generation orders publication, never evidence freshness.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from sqlalchemy import bindparam, delete, insert, select, text, update
from sqlalchemy.orm import Session

from app.models import (
    CurrentOpeningEdge,
    CurrentOpeningPosition,
    CurrentOpeningRoot,
    CurrentOpeningScope,
    OpeningPositionEdge,
    OpeningPositionScore,
    OpeningScoreBatch,
    OpeningScoreBatchSharedScope,
    UserOpeningScore,
)

OPENING_SCORE_LOCK_CLASSID = 0x47525343
CHUNK_SIZE = 500


class StorageFormat(str, Enum):
    LEGACY = "legacy"
    CURRENT = "current-b50-v1"


# Order is also the write/failure boundary order. Identity indexes support the
# reader's owner/color and parent-prefix lookups without redundant indexes.
_GROUPS = (
    (
        "roots",
        CurrentOpeningRoot.__table__,
        UserOpeningScore.__table__,
        ("opening_key",),
    ),
    (
        "positions",
        CurrentOpeningPosition.__table__,
        OpeningPositionScore.__table__,
        ("normalized_fen",),
    ),
    (
        "edges",
        CurrentOpeningEdge.__table__,
        OpeningPositionEdge.__table__,
        ("parent_fen", "child_fen"),
    ),
    (
        "scope",
        CurrentOpeningScope.__table__,
        OpeningScoreBatchSharedScope.__table__,
        ("kind", "fen"),
    ),
)
Scalar = str | int | float | bool | datetime | None
Row = Mapping[str, Scalar]


def _semantic_columns(table):
    return tuple(
        c for c in table.columns if c.name not in {"id", "user_id", "player_color"}
    )


def _value(value):
    # SQLite returns naive UTC timestamps; PostgreSQL returns aware timestamps.
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
    return value


@dataclass(frozen=True)
class ScorePayload:
    roots: tuple[Row, ...] = ()
    positions: tuple[Row, ...] = ()
    edges: tuple[Row, ...] = ()
    scope: tuple[Row, ...] = ()

    def __post_init__(self):
        for name, table, _, keys in _GROUPS:
            rows = []
            seen = set()
            columns = _semantic_columns(table)
            for source in getattr(self, name):
                if set(source) != {c.name for c in columns}:
                    raise ValueError(f"{name}: incomplete or unknown semantic fields")
                row = {k: _value(v) for k, v in source.items()}
                for c in columns:
                    value = row[c.name]
                    if value is None:
                        if not c.nullable:
                            raise ValueError(f"{name}.{c.name}: unexpected NULL")
                    elif type(value) is not c.type.python_type:
                        raise TypeError(f"{name}.{c.name}: incorrect scalar type")
                key = tuple(row[k] for k in keys)
                if key in seen:
                    raise ValueError(f"{name}: duplicate natural key")
                seen.add(key)
                rows.append(MappingProxyType(row))
            object.__setattr__(self, name, tuple(rows))


@dataclass(frozen=True)
class ScoreHandle:
    batch_id: int
    user_id: int
    player_color: str
    generation: int
    storage_format: StorageFormat

    @classmethod
    def from_batch(cls, batch: OpeningScoreBatch):
        return cls(
            batch.id,
            batch.user_id,
            batch.player_color,
            batch.generation,
            StorageFormat(batch.storage_format),
        )


class RetiredScoreHandle(LookupError):
    """An exact marker no longer exists; never substitute the current payload."""


class UnsupportedScoreStorage(RuntimeError):
    """Legacy readers cannot serve this marker until reader integration lands."""


class PublicationSuperseded(Exception):
    def __init__(self, batch: OpeningScoreBatch):
        super().__init__("opening score candidate superseded")
        self.batch = batch


def publication_lock_key(user_id: int, player_color: str) -> tuple[int, int]:
    if type(user_id) is not int or not -(2**63) <= user_id < 2**63:
        raise ValueError("user_id must be a signed int64")
    if player_color not in {"white", "black"}:
        raise ValueError("player_color must be white or black")
    digest = hashlib.sha256(
        b"ghostreplay:opening-score-publish:v1\0"
        + user_id.to_bytes(8, "big", signed=True)
    ).digest()
    bucket = int.from_bytes(digest[:4], "big") & 0x7FFFFFFF
    objid = (bucket << 1) | (player_color == "black")
    return OPENING_SCORE_LOCK_CLASSID, objid if objid < 2**31 else objid - 2**32


def acquire_publication_lock(db: Session, user_id: int, player_color: str) -> None:
    classid, objid = publication_lock_key(user_id, player_color)
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text(
                "SELECT pg_advisory_xact_lock(CAST(:classid AS integer), CAST(:objid AS integer))"
            ),
            {"classid": classid, "objid": objid},
        )
    elif db.get_bind().dialect.name == "sqlite":
        # Must be the first statement of a fresh publication transaction. A
        # deferred read transaction cannot safely be upgraded after diffing.
        db.execute(text("BEGIN IMMEDIATE"))
    else:
        raise ValueError("unsupported opening score storage dialect")


def _pair(table, user_id, player_color):
    return (table.c.user_id == user_id) & (table.c.player_color == player_color)


def latest_batch(db, user_id, player_color):
    return db.scalars(
        select(OpeningScoreBatch)
        .where(
            OpeningScoreBatch.user_id == user_id,
            OpeningScoreBatch.player_color == player_color,
        )
        .order_by(OpeningScoreBatch.generation.desc())
        .execution_options(populate_existing=True)
    ).first()


def handle_is_live(db: Session, handle: ScoreHandle) -> bool:
    return (
        db.scalar(
            select(OpeningScoreBatch.id).where(
                OpeningScoreBatch.id == handle.batch_id,
                OpeningScoreBatch.user_id == handle.user_id,
                OpeningScoreBatch.player_color == handle.player_color,
                OpeningScoreBatch.generation == handle.generation,
                OpeningScoreBatch.storage_format == handle.storage_format.value,
            )
        )
        is not None
    )


def read_payload(db: Session, handle: ScoreHandle) -> ScorePayload:
    """Full storage/conversion adapter, not an API reader.

    Caller holds the publication lock or a repeatable snapshot. API readers use
    bounded repository queries and validate their exact handle after the reads.
    """
    if not handle_is_live(db, handle):
        raise RetiredScoreHandle(handle.batch_id)
    result = {}
    for name, current, legacy, _ in _GROUPS:
        table = current if handle.storage_format is StorageFormat.CURRENT else legacy
        condition = (
            _pair(table, handle.user_id, handle.player_color)
            if table is current
            else table.c.batch_id == handle.batch_id
        )
        columns = [table.c[c.name] for c in _semantic_columns(current)]
        result[name] = tuple(
            dict(row)
            for row in db.execute(select(*columns).where(condition)).mappings()
        )
    return ScorePayload(**result)


def _many(db, statement, rows):
    for start in range(0, len(rows), CHUNK_SIZE):
        db.execute(statement, rows[start : start + CHUNK_SIZE])


def _apply_current(db, batch, payload):
    for name, table, _, keys in _GROUPS:
        condition = _pair(table, batch.user_id, batch.player_color)
        fields = [c.name for c in _semantic_columns(table)]
        existing = {
            tuple(row[k] for k in keys): dict(row)
            for row in db.execute(select(table).where(condition)).mappings()
        }
        incoming = {tuple(row[k] for k in keys): row for row in getattr(payload, name)}
        # Updates grouped by exactly the changed fields; confidence-only updates
        # do not rewrite stable fields. No epsilon, rounding, hashes or cache.
        changes = {}
        additions = []
        removals = []
        for key, row in incoming.items():
            old = existing.get(key)
            if old is None:
                additions.append(
                    dict(row, user_id=batch.user_id, player_color=batch.player_color)
                )
                continue
            changed = tuple(f for f in fields if _value(old[f]) != row[f])
            if changed:
                parameters = {f: row[f] for f in changed}
                parameters.update({f"key_{k}": row[k] for k in keys})
                changes.setdefault(changed, []).append(parameters)
        key_condition = condition
        for key in keys:
            key_condition &= table.c[key] == bindparam(f"key_{key}")
        for key in existing.keys() - incoming.keys():
            removals.append({f"key_{k}": v for k, v in zip(keys, key)})
        _many(db, delete(table).where(key_condition), removals)
        _many(db, insert(table), additions)
        for fields_changed, rows in changes.items():
            _many(
                db,
                update(table)
                .where(key_condition)
                .values({f: bindparam(f) for f in fields_changed}),
                rows,
            )


def _write_legacy(db, batch, payload):
    for name, _, table, _ in _GROUPS:
        started = time.monotonic()
        rows = []
        for row in getattr(payload, name):
            values = dict(row, batch_id=batch.id)
            if name != "scope":
                values.update(
                    user_id=batch.user_id,
                    player_color=batch.player_color,
                    computed_at=batch.computed_at,
                )
            rows.append(values)
        if name in {"roots", "scope"}:
            model = (
                UserOpeningScore if name == "roots" else OpeningScoreBatchSharedScope
            )
            db.add_all(model(**row) for row in rows)
        elif rows:
            # Preserve the production legacy transport; only current diffs use
            # the selected 500-row chunks. Let the dialect page this bulk insert.
            db.execute(insert(table), rows)
            # Preserve the legacy logger/event contract for external consumers.
            label, count_key = (
                ("position-score", "position_row_count")
                if name == "positions" else ("position-edge", "edge_row_count")
            )
            logging.getLogger("app.opening_cache").info(
                "opening %s rows staged", label,
                extra={
                    "user_id": batch.user_id,
                    "player_color": batch.player_color,
                    count_key: len(rows),
                    "stage_seconds": round(time.monotonic() - started, 4),
                },
            )
    db.flush()


def _retire(db, batch, *, keep_legacy=False):
    table = OpeningScoreBatch.__table__
    stale = (
        select(table.c.id)
        .where(
            _pair(table, batch.user_id, batch.player_color),
            table.c.id != batch.id,
        )
        .order_by(table.c.generation.desc())
    )
    if keep_legacy:
        stale = stale.offset(1)  # legacy readers still need the prior snapshot
    ids = list(db.scalars(stale))
    # Explicit deletes also uphold retirement in SQLite harnesses with FKs off.
    for start in range(0, len(ids), CHUNK_SIZE):
        chunk = ids[start : start + CHUNK_SIZE]
        for _, _, child, _ in _GROUPS:
            db.execute(delete(child).where(child.c.batch_id.in_(chunk)))
        db.execute(delete(table).where(table.c.id.in_(chunk)))


def _recover(engine, user_id, color):
    # A new Session alone is insufficient if the failed connection is reused.
    # invalidate() below removes it from the pool before this checkout.
    with Session(engine, expire_on_commit=False) as fresh:
        if engine.dialect.name == "postgresql":
            fresh.execute(text("SET LOCAL lock_timeout = '5s'"))
        acquire_publication_lock(fresh, user_id, color)
        batch = latest_batch(fresh, user_id, color)
        if batch is not None:
            fresh.expunge(batch)
        return batch


def _publish_scores(
    db: Session,
    batch: OpeningScoreBatch,
    payload: ScorePayload,
    *,
    storage_format: StorageFormat,
    locked: bool = False,
) -> OpeningScoreBatch:
    """Publish once, or recover a confirmed commit; absent attempts stay failures.

    The caller reserves generation and releases its capture transaction before
    CPU work. No automatic scoring retry, and no generation-as-freshness claim.
    """
    storage_format = StorageFormat(storage_format)
    user_id, color, generation = batch.user_id, batch.player_color, batch.generation
    publication_lock_key(user_id, color)
    if db.in_transaction() and not locked:
        raise ValueError("publication requires a fresh transaction")
    try:
        if not locked:
            acquire_publication_lock(db, user_id, color)
        previous = latest_batch(db, user_id, color)
        if previous is not None and previous.generation >= generation:
            db.expunge(previous)
            db.rollback()
            raise PublicationSuperseded(previous)
        batch.storage_format = storage_format.value
        if storage_format is StorageFormat.CURRENT:
            _apply_current(db, batch, payload)
        db.add(batch)
        db.flush()
        if storage_format is StorageFormat.LEGACY:
            _write_legacy(db, batch, payload)
            if (
                previous is not None
                and previous.storage_format != StorageFormat.LEGACY.value
            ):
                for _, table, _, _ in _GROUPS:
                    db.execute(delete(table).where(_pair(table, user_id, color)))
        _retire(
            db,
            batch,
            keep_legacy=(
                storage_format is StorageFormat.LEGACY
                and (
                    previous is None
                    or previous.storage_format == StorageFormat.LEGACY.value
                )
            ),
        )
        # Detach before commit to return captured metadata without a post-commit
        # refresh racing another publisher's retirement.
        db.refresh(batch)
        db.expunge(batch)
    except Exception:
        db.rollback()
        raise
    try:
        db.commit()
    except Exception:
        engine = db.get_bind().engine
        db.invalidate()
        recovered = _recover(engine, user_id, color)
        if recovered is not None and recovered.generation == generation:
            db.add(recovered)
            return recovered
        if recovered is not None and recovered.generation > generation:
            raise PublicationSuperseded(recovered) from None
        raise
    db.add(batch)
    return batch


def payload_query(handle: ScoreHandle, group: str):
    """Repository SELECT for sibling readers; add bounded FEN/parent predicates.

    Result columns retain legacy row shape (including captured computed_at).
    Exact marker identity is joined into every current query. Consumers must still
    fence after all queries to distinguish a valid empty result from retirement.
    """
    spec = next((g for g in _GROUPS if g[0] == group), None)
    if spec is None:
        raise ValueError("unknown opening payload group")
    _, current, legacy, _ = spec
    marker = OpeningScoreBatch.__table__
    identity = (
        (marker.c.id == handle.batch_id)
        & (marker.c.user_id == handle.user_id)
        & (marker.c.player_color == handle.player_color)
        & (marker.c.generation == handle.generation)
        & (marker.c.storage_format == handle.storage_format.value)
    )
    if handle.storage_format is StorageFormat.LEGACY:
        return (
            select(legacy)
            .join(marker, legacy.c.batch_id == marker.c.id)
            .where(identity)
        )
    columns = list(current.c) + [marker.c.id.label("batch_id"), marker.c.computed_at]
    return (
        select(*columns)
        .select_from(
            current.join(
                marker,
                (current.c.user_id == marker.c.user_id)
                & (current.c.player_color == marker.c.player_color),
            )
        )
        .where(identity)
    )


def publish_scores(
    db: Session,
    batch: OpeningScoreBatch,
    payload: ScorePayload,
    *,
    storage_format: StorageFormat = StorageFormat.LEGACY,
) -> OpeningScoreBatch:
    return _publish_scores(db, batch, payload, storage_format=storage_format)


def convert_pair(
    db: Session, user_id: int, player_color: str, target: StorageFormat
) -> OpeningScoreBatch | None:
    """Explicit per-pair conversion, without scoring or evidence reinterpretation.

    No startup sweep. Skip absent/already-converted pairs, otherwise reserve a
    generation and read the source under the publication lock. Preserve all
    evidence stamps and computed_at. An intervening higher
    reservation/publication supersedes this conversion normally. Conversely, a
    conversion can supersede an earlier-reserved rebuild with fresher evidence;
    the next evidence freshness check must schedule another rebuild.
    Requires a fresh transaction, including when there is nothing to convert.
    """
    from app.opening_cache import reserve_opening_score_generation

    target = StorageFormat(target)
    publication_lock_key(user_id, player_color)
    if db.in_transaction():
        raise ValueError("conversion requires a fresh transaction")
    # An unlocked preflight avoids reservations for no-ops. Recheck under the
    # publication lock after reservation; this read does not authorize writes.
    try:
        old = latest_batch(db, user_id, player_color)
        if old is None or old.storage_format == target.value:
            if old is not None:
                db.expunge(old)
            return old
    finally:
        db.rollback()
    generation = reserve_opening_score_generation(db, user_id, player_color)
    try:
        acquire_publication_lock(db, user_id, player_color)
        old = latest_batch(db, user_id, player_color)
        if old is None:
            db.rollback()
            return None
        if old.generation >= generation:
            db.expunge(old)
            db.rollback()
            raise PublicationSuperseded(old)
        if old.storage_format == target.value:
            db.expunge(old)
            db.rollback()
            return old
        payload = read_payload(db, ScoreHandle.from_batch(old))
        metadata = {
            c.name: getattr(old, c.name)
            for c in OpeningScoreBatch.__table__.columns
            if c.name not in {"id", "generation", "storage_format"}
        }
        batch = OpeningScoreBatch(generation=generation, **metadata)
        return _publish_scores(db, batch, payload, storage_format=target, locked=True)
    except Exception:
        db.rollback()
        raise
