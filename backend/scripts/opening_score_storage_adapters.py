"""Disposable raw-DDL storage adapters; deliberately not application repositories."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import sys
import time
from types import MappingProxyType
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import opening_cache as oc
from app.models import OpeningScoreBatch, OpeningScoreCursor
from scripts.opening_score_storage_workload import (
    Candidate,
    Payload,
    MODELS,
    FIELDS,
    KEYS,
    OWNER,
    COLOR,
    key,
    sorted_rows,
    capture,
    replay_objects,
)

CHUNK = 500
FORMAT = "spike-v1"
MACHINE_KEYS = {
    "normalized_fen",
    "parent_fen",
    "child_fen",
    "opening_key",
    "fen",
    "strongest_branch_key",
    "weakest_branch_key",
    "underexposed_branch_key",
}


def retained_size(value, seen=None):
    """CPython owned-object estimate; shared objects counted once within an entry."""
    if seen is None:
        seen = set()
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, (dict, MappingProxyType)):
        if isinstance(value, MappingProxyType):
            size += sys.getsizeof(dict(value))  # retained backing dict, not only proxy
        size += sum(
            retained_size(k, seen) + retained_size(v, seen) for k, v in value.items()
        )
    elif isinstance(value, (tuple, list)):
        size += sum(retained_size(v, seen) for v in value)
    elif isinstance(value, Payload):
        size += retained_size(vars(value), seen)
    return size


@dataclass(frozen=True)
class Entry:
    marker: int
    payload: Payload
    ids: MappingProxyType
    size: int


class PayloadCache:
    """One engine-bound, single-publisher LRU; no multi-writer protocol claimed."""

    def __init__(self, engine, max_entries=8, max_bytes=64 * 1024**2):
        self.engine = engine
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.entries = OrderedDict()
        self.bytes = self.peak_bytes = self.evictions = 0

    def evict(self, owner=OWNER, color=COLOR, format=FORMAT):
        old = self.entries.pop((owner, color, format), None)
        if old:
            self.bytes -= old.size

    def get(self, engine, marker, owner=OWNER, color=COLOR, format=FORMAT):
        if engine is not self.engine:
            raise ValueError("cache cannot cross database engines")
        identity = (owner, color, format)
        entry = self.entries.get(identity)
        if entry is None:
            return None
        if entry.marker != marker:
            self.evict(owner, color, format)
            return None
        self.entries.move_to_end(identity)
        return entry

    def put(self, marker, payload, ids, owner=OWNER, color=COLOR, format=FORMAT):
        identity = (owner, color, format)
        old = self.entries.get(identity)
        if old and old.marker > marker:
            return  # Delayed completion cannot displace a newer publication.
        # Payload is deeply immutable (tuples/scalars). Detach the final ID maps.
        detached_ids = MappingProxyType(
            {n: MappingProxyType(dict(v)) for n, v in ids.items()}
        )
        size = retained_size((payload, detached_ids, identity, marker)) + sys.getsizeof(
            Entry(marker, payload, detached_ids, 0)
        )
        self.evict(owner, color, format)
        if size > self.max_bytes or self.max_entries < 1:
            return
        while self.entries and (
            len(self.entries) >= self.max_entries or self.bytes + size > self.max_bytes
        ):
            _, removed = self.entries.popitem(last=False)
            self.bytes -= removed.size
            self.evictions += 1
        self.entries[identity] = Entry(marker, payload, detached_ids, size)
        self.bytes += size
        self.peak_bytes = max(self.peak_bytes, self.bytes)


def execute_many(conn, sql, rows):
    statement = text(sql)
    for start in range(0, len(rows), CHUNK):
        conn.execute(statement, rows[start : start + CHUNK])


def wire_bytes(rows):
    """Text-protocol DataRow bytes (5+2+4/column), excluding TLS/RowDescription.

    psycopg's default result format is text. This is a declared reconstruction,
    not socket capture; float formatting can differ by a few bytes from str().
    """
    return sum(
        7
        + sum(
            4
            + (
                0
                if value is None
                else len(
                    (
                        "t" if value is True else "f" if value is False else str(value)
                    ).encode()
                )
            )
            for value in row
        )
        for row in rows
    )


class CurrentAdapter:
    def __init__(self, engine, layout, fillfactor, *, cached=False):
        if layout not in {"B", "D"} or fillfactor not in {50, 100}:
            raise ValueError("baseline supports only B/D and fillfactor 50/100")
        self.engine, self.layout, self.fillfactor = engine, layout, fillfactor
        self.cache = PayloadCache(engine) if cached else None
        self.last_metrics = {}
        self.tables = ["marker", "roots", "positions", "edges", "scope"]
        if layout == "D":
            self.tables += ["roots_confidence", "positions_confidence"]

    def create(self):
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE marker (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                    "user_id bigint NOT NULL, player_color text NOT NULL, computed_at timestamptz NOT NULL, "
                    "evidence_seq bigint NOT NULL, cache_epoch bigint, scoped_shared_digest text, "
                    "inputs_fingerprint text, registry_fingerprint text, UNIQUE(user_id, player_color))"
                )
            )
            for name, model in MODELS.items():
                columns = [
                    "id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
                    "user_id bigint NOT NULL",
                    "player_color text NOT NULL",
                ]
                for field in FIELDS[name]:
                    if self.layout == "D" and field == "confidence":
                        continue
                    col = model.__table__.c[field]
                    dtype = col.type.compile(dialect=self.engine.dialect)
                    collate = ' COLLATE "C"' if field in MACHINE_KEYS else ""
                    columns.append(
                        f"{field} {dtype}{collate}"
                        + ("" if col.nullable else " NOT NULL")
                    )
                columns.append(
                    f"UNIQUE (user_id, player_color, {', '.join(KEYS[name])})"
                )
                ff = (
                    self.fillfactor
                    if self.layout == "B" and name in ("roots", "positions")
                    else 100
                )
                conn.execute(
                    text(
                        f"CREATE TABLE {name} ({', '.join(columns)}) WITH (fillfactor={ff})"
                    )
                )
                if self.layout == "D" and name in ("roots", "positions"):
                    conn.execute(
                        text(
                            f"CREATE TABLE {name}_confidence (id bigint PRIMARY KEY REFERENCES {name}(id) "
                            f"ON DELETE CASCADE, confidence double precision) WITH (fillfactor={self.fillfactor})"
                        )
                    )

    def load(self, conn):
        groups, ids, byte_count = {}, {}, 0
        for name in MODELS:
            columns = ", ".join(
                ("c." if self.layout == "D" and field == "confidence" else "b.") + field
                for field in FIELDS[name]
            )
            join = (
                f" JOIN {name}_confidence c ON c.id=b.id"
                if self.layout == "D" and name in ("roots", "positions")
                else ""
            )
            rows = [
                tuple(row)
                for row in conn.execute(
                    text(
                        f"SELECT b.id, {columns} FROM {name} b{join} WHERE b.user_id=:owner AND b.player_color=:color"
                    ),
                    {"owner": OWNER, "color": COLOR},
                )
            ]
            byte_count += wire_bytes(rows)
            ids[name] = {key(name, row[1:]): row[0] for row in rows}
            groups[name] = sorted_rows(name, (row[1:] for row in rows))
            if join:
                count = conn.execute(
                    text(
                        f"SELECT count(*) FROM {name} WHERE user_id=:owner AND player_color=:color"
                    ),
                    {"owner": OWNER, "color": COLOR},
                ).scalar_one()
                if count != len(rows):
                    raise ValueError("missing confidence row")
        return Payload(**groups), ids, byte_count

    def publish(self, candidate: Candidate):
        start = time.perf_counter()
        read_start = start
        hit, bytes_read, read_ms, diff_ms = False, 0, 0.0, 0.0
        try:
            with self.engine.begin() as conn:
                # Disposable single publisher; row lock guards the local cache marker.
                # Production advisory/supersession design belongs to the writer bead.
                marker = conn.execute(
                    text(
                        "SELECT id FROM marker WHERE user_id=:owner AND player_color=:color FOR UPDATE"
                    ),
                    {"owner": OWNER, "color": COLOR},
                ).scalar_one_or_none()
                cached = self.cache.get(self.engine, marker) if self.cache else None
                if cached:
                    previous, ids = (
                        cached.payload,
                        {n: dict(v) for n, v in cached.ids.items()},
                    )
                    hit = True
                else:
                    previous, ids, bytes_read = self.load(conn)
                read_ms = (time.perf_counter() - read_start) * 1000
                diff_start = time.perf_counter()
                operations = {}
                for name in MODELS:
                    old = {key(name, row): row for row in getattr(previous, name)}
                    new = {
                        key(name, row): row for row in getattr(candidate.payload, name)
                    }
                    columns = [
                        f
                        for f in FIELDS[name]
                        if not (self.layout == "D" and f == "confidence")
                    ]
                    indices = [FIELDS[name].index(f) for f in columns]
                    deleted = [
                        {"id": ids[name].pop(k)} for k in old.keys() - new.keys()
                    ]
                    inserted, updates, confidence = [], [], []
                    for k, row in new.items():
                        values = {
                            f: row[i] for f, i in zip(columns, indices, strict=True)
                        }
                        if k not in old:
                            inserted.append((k, values, row))
                        else:
                            if any(row[i] != old[k][i] for i in indices):
                                updates.append({**values, "id": ids[name][k]})
                            if self.layout == "D" and "confidence" in FIELDS[name]:
                                index = FIELDS[name].index("confidence")
                                if row[index] != old[k][index]:
                                    confidence.append(
                                        {"id": ids[name][k], "confidence": row[index]}
                                    )
                    operations[name] = (columns, deleted, inserted, updates, confidence)
                diff_ms = (time.perf_counter() - diff_start) * 1000
                for name, (
                    columns,
                    deleted,
                    inserted,
                    updates,
                    confidence,
                ) in operations.items():
                    execute_many(conn, f"DELETE FROM {name} WHERE id=:id", deleted)
                    if inserted:
                        values = [
                            {"owner": OWNER, "color": COLOR, **v}
                            for _, v, _ in inserted
                        ]
                        execute_many(
                            conn,
                            f"INSERT INTO {name} (user_id, player_color, {', '.join(columns)}) VALUES (:owner, :color, {', '.join(':' + f for f in columns)})",
                            values,
                        )
                        # New mappings only; no full identity reread on warm hits.
                        # A bounded natural-key lookup keeps executemany transport simple.
                        for offset in range(0, len(inserted), CHUNK):
                            chunk = inserted[offset : offset + CHUNK]
                            params = {"owner": OWNER, "color": COLOR}
                            predicates = []
                            for i, (k, _, _) in enumerate(chunk):
                                predicates.append(
                                    "("
                                    + " AND ".join(
                                        f"{f}=:k{i}_{j}"
                                        for j, f in enumerate(KEYS[name])
                                    )
                                    + ")"
                                )
                                params.update({f"k{i}_{j}": v for j, v in enumerate(k)})
                            found = conn.execute(
                                text(
                                    f"SELECT id, {', '.join(KEYS[name])} FROM {name} WHERE user_id=:owner AND player_color=:color AND ({' OR '.join(predicates)})"
                                ),
                                params,
                            ).all()
                            ids[name].update({tuple(row[1:]): row[0] for row in found})
                            bytes_read += wire_bytes(found)
                        if self.layout == "D" and name in ("roots", "positions"):
                            execute_many(
                                conn,
                                f"INSERT INTO {name}_confidence (id, confidence) VALUES (:id, :confidence)",
                                [
                                    {
                                        "id": ids[name][k],
                                        "confidence": row[
                                            FIELDS[name].index("confidence")
                                        ],
                                    }
                                    for k, _, row in inserted
                                ],
                            )
                    execute_many(
                        conn,
                        f"UPDATE {name} SET {', '.join(f + '=:' + f for f in columns)} WHERE id=:id",
                        updates,
                    )
                    execute_many(
                        conn,
                        f"UPDATE {name}_confidence SET confidence=:confidence WHERE id=:id",
                        confidence,
                    )
                conn.execute(
                    text(
                        "DELETE FROM marker WHERE user_id=:owner AND player_color=:color"
                    ),
                    {"owner": OWNER, "color": COLOR},
                )
                f = candidate.freshness
                marker = conn.execute(
                    text(
                        "INSERT INTO marker (user_id, player_color, computed_at, evidence_seq, cache_epoch, scoped_shared_digest, inputs_fingerprint, registry_fingerprint) "
                        "VALUES (:owner, :color, :at, :seq, :epoch, :digest, :fp, :registry) RETURNING id"
                    ),
                    {
                        "owner": OWNER,
                        "color": COLOR,
                        "at": candidate.computed_at,
                        "seq": f.evidence_seq,
                        "epoch": f.cache_epoch,
                        "digest": f.scoped_shared_digest,
                        "fp": f.inputs_fingerprint,
                        "registry": oc.opening_score_inputs_fingerprint(
                            oc.get_opening_graph(),
                            oc.get_opening_roots(),
                            oc.load_strict_densified_edges(oc.get_opening_graph()),
                        ),
                    },
                ).scalar_one()
            cache_start = time.perf_counter()
            cache_failed = False
            if self.cache:
                try:
                    self.cache.put(marker, candidate.payload, ids)
                except Exception:
                    # Cache bookkeeping after a confirmed durable commit is a
                    # performance miss, never a failed publication.
                    self.cache.evict()
                    cache_failed = True
            cache_ms = (time.perf_counter() - cache_start) * 1000
        except BaseException:
            if self.cache:
                self.cache.evict()
            raise
        self.last_metrics = {
            "publish_ms": (time.perf_counter() - start) * 1000,
            "read_ms": read_ms,
            "diff_ms": diff_ms,
            "read_bytes_estimate": bytes_read,
            "cache_hit": hit,
            "cache_accounting_ms": cache_ms,
            "cache_retention_failed": cache_failed,
            "cache_retained_bytes": self.cache.bytes if self.cache else 0,
            "final_id_map_counts": {n: len(v) for n, v in ids.items()},
        }
        return marker

    def verify(self, candidate):
        with self.engine.connect() as conn:
            actual, ids, _ = self.load(conn)
            assert actual == candidate.payload, "semantic payload differs"
            if self.cache:
                entry = next(iter(self.cache.entries.values()), None)
                if entry:
                    assert {n: dict(v) for n, v in entry.ids.items()} == ids

    def read(self, candidate):
        params = {
            "owner": OWNER,
            "color": COLOR,
            "keys": [
                r[FIELDS["positions"].index("normalized_fen")]
                for r in candidate.payload.positions[:32]
            ],
            "root_keys": [
                r[FIELDS["roots"].index("opening_key")]
                for r in candidate.payload.roots[:16]
            ],
            "parents": list(
                dict.fromkeys(
                    r[FIELDS["edges"].index("parent_fen")]
                    for r in candidate.payload.edges
                )
            )[:16],
        }
        start = time.perf_counter()  # same boundary as A: prepared bounded keys
        with self.engine.connect() as conn:
            marker = conn.execute(
                text(
                    "SELECT id FROM marker WHERE user_id=:owner AND player_color=:color"
                ),
                params,
            ).scalar_one()
            join = (
                " JOIN positions_confidence c ON c.id=b.id"
                if self.layout == "D"
                else ""
            )
            columns = ", ".join(
                ("c." if self.layout == "D" and f == "confidence" else "b.") + f
                for f in FIELDS["positions"]
            )
            rows = conn.execute(
                text(
                    f"SELECT {columns} FROM marker m JOIN positions b USING(user_id, player_color){join} WHERE m.id=:marker AND b.normalized_fen=ANY(:keys)"
                ),
                {**params, "marker": marker},
            ).all()
            edges = conn.execute(
                text(
                    "SELECT b.* FROM marker m JOIN edges b USING(user_id, player_color) WHERE m.id=:marker AND b.parent_fen=ANY(:parents)"
                ),
                {**params, "marker": marker},
            ).all()
            root_join = (
                " JOIN roots_confidence c ON c.id=b.id" if self.layout == "D" else ""
            )
            root_columns = ", ".join(
                ("c." if self.layout == "D" and f == "confidence" else "b.") + f
                for f in FIELDS["roots"]
            )
            roots = conn.execute(
                text(
                    f"SELECT {root_columns} FROM marker m JOIN roots b USING(user_id, player_color){root_join} WHERE m.id=:marker AND b.opening_key=ANY(:root_keys)"
                ),
                {**params, "marker": marker},
            ).all()
            assert conn.execute(
                text("SELECT EXISTS(SELECT 1 FROM marker WHERE id=:marker)"),
                {"marker": marker},
            ).scalar_one()
        return {
            "bounded_read_ms": (time.perf_counter() - start) * 1000,
            "bounded_rows": len(rows) + len(edges) + len(roots),
        }


class LegacyAdapter:
    def __init__(self, engine):
        self.engine, self.layout, self.fillfactor, self.cache = engine, "A", 100, None
        self.tables = [
            OpeningScoreBatch.__tablename__,
            OpeningScoreCursor.__tablename__,
        ] + [m.__tablename__ for m in MODELS.values()]
        self.last_metrics = {}

    def create(self):
        tables = [OpeningScoreBatch.__table__, OpeningScoreCursor.__table__] + [
            m.__table__ for m in MODELS.values()
        ]
        OpeningScoreBatch.metadata.create_all(self.engine, tables=tables)

    def publish(self, candidate):
        roots, positions, overlay = replay_objects(candidate)
        start = time.perf_counter()  # scorer-shaped input preparation is untimed
        with (
            Session(self.engine) as db,
            patch.object(oc, "_build_cached_scores", return_value=(roots, positions)),
        ):
            # A superseded publication deliberately aborts this isolated spike
            # sample: timing a discarded candidate as a successful write would
            # invalidate qualification. PublicationSuperseded propagates.
            batch = oc.recompute_opening_scores(
                db,
                OWNER,
                COLOR,
                overlay=overlay,
                freshness=candidate.freshness,
                computed_at=candidate.computed_at,
            )
            marker = batch.id
        self.last_metrics = {
            "publish_ms": (time.perf_counter() - start) * 1000,
            "read_ms": 0.0,
            "diff_ms": 0.0,
            "read_bytes_estimate": 0,
            "cache_hit": False,
            "cache_accounting_ms": 0.0,
            "cache_retained_bytes": 0,
        }
        return marker

    def verify(self, candidate):
        with Session(self.engine) as db:
            batch = oc.get_latest_opening_score_batch(db, OWNER, COLOR)
            assert capture(db, batch) == candidate.payload

    def read(self, candidate):
        keys = [
            r[FIELDS["positions"].index("normalized_fen")]
            for r in candidate.payload.positions[:32]
        ]
        parents = list(
            dict.fromkeys(
                r[FIELDS["edges"].index("parent_fen")] for r in candidate.payload.edges
            )
        )[:16]
        root_keys = [
            r[FIELDS["roots"].index("opening_key")]
            for r in candidate.payload.roots[:16]
        ]
        start = time.perf_counter()
        with Session(self.engine) as db:
            batch = oc.get_latest_opening_score_batch(db, OWNER, COLOR)
            # Raw equivalent of the existing indexed bounded reads also accepts
            # explicitly labeled persistence-only replica keys (not legal FENs).
            rows = db.execute(
                text(
                    "SELECT * FROM opening_position_scores WHERE batch_id=:batch AND normalized_fen=ANY(:keys)"
                ),
                {"batch": batch.id, "keys": keys},
            ).all()
            edges = db.execute(
                text(
                    "SELECT * FROM opening_position_edges WHERE batch_id=:batch AND parent_fen=ANY(:parents)"
                ),
                {"batch": batch.id, "parents": parents},
            ).all()
            roots = db.execute(
                text(
                    "SELECT * FROM user_opening_scores WHERE batch_id=:batch AND opening_key=ANY(:keys)"
                ),
                {"batch": batch.id, "keys": root_keys},
            ).all()
        return {
            "bounded_read_ms": (time.perf_counter() - start) * 1000,
            "bounded_rows": len(rows) + len(edges) + len(roots),
        }
