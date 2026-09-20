"""Synthetic-only input capture for the disposable storage spike.

Nothing in this module is imported by the application. Semantic field lists are
derived from the current durable models so newly persisted fields fail loudly
instead of silently disappearing from the comparison.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import random
from types import SimpleNamespace
from unittest.mock import patch
import uuid

import chess
from sqlalchemy import select, update, delete
from sqlalchemy.orm import Session

from app import opening_cache as oc
from app.fen import normalize_fen
from app.models import (
    Base,
    EvidenceEpoch,
    GameSession,
    SessionMove,
    OpeningPositionScore,
    OpeningPositionEdge,
    OpeningScoreBatch,
    OpeningScoreBatchSharedScope,
    UserOpeningScore,
)
from app.opening_evidence import reset_session_evidence_cache

MODELS = {
    "roots": UserOpeningScore,
    "positions": OpeningPositionScore,
    "edges": OpeningPositionEdge,
    "scope": OpeningScoreBatchSharedScope,
}
KEYS = {
    "roots": ("opening_key",),
    "positions": ("normalized_fen",),
    "edges": ("parent_fen", "child_fen"),
    "scope": ("kind", "fen"),
}
EXCLUDED = {"id", "batch_id", "user_id", "player_color", "computed_at"}
FIELDS = {
    name: tuple(c.name for c in model.__table__.columns if c.name not in EXCLUDED)
    for name, model in MODELS.items()
}
REASONS = (
    "cache_miss",
    "registry_drift",
    "stale_branch_keys",
    "evidence_change",
    "decay_staleness",
)
OWNER = 1
COLOR = "black"
START = datetime(2026, 1, 1, 12, 10, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Payload:
    roots: tuple[tuple, ...]
    positions: tuple[tuple, ...]
    edges: tuple[tuple, ...]
    scope: tuple[tuple, ...]

    def rows(self, name):
        return [
            dict(zip(FIELDS[name], row, strict=True)) for row in getattr(self, name)
        ]


@dataclass(frozen=True)
class Candidate:
    payload: Payload
    computed_at: datetime
    freshness: oc.FreshnessSnapshot
    reason: str
    cause: str
    period: str
    scorer_ms: float = 0.0


def key(name, row):
    return tuple(row[FIELDS[name].index(k)] for k in KEYS[name])


def sorted_rows(name, rows):
    return tuple(
        sorted(rows, key=lambda row: tuple(str(x).encode() for x in key(name, row)))
    )


def capture(db, batch):
    groups = {}
    for name, model in MODELS.items():
        cols = [getattr(model, field) for field in FIELDS[name]]
        groups[name] = sorted_rows(
            name,
            (
                tuple(row)
                for row in db.execute(select(*cols).where(model.batch_id == batch.id))
            ),
        )
    return Payload(**groups)


def changed(previous: Payload, current: Payload):
    result = {}
    all_stable = True
    for name in MODELS:
        before = {key(name, row): row for row in getattr(previous, name)}
        after = {key(name, row): row for row in getattr(current, name)}
        common = before.keys() & after.keys()
        confidence = (
            FIELDS[name].index("confidence") if "confidence" in FIELDS[name] else None
        )
        stable_indices = [i for i in range(len(FIELDS[name])) if i != confidence]
        stable_changed = sum(
            any(before[k][i] != after[k][i] for i in stable_indices) for k in common
        )
        membership = len(before.keys() ^ after.keys())
        result[name] = {
            "before": len(before),
            "after": len(after),
            "common": len(common),
            "inserted": len(after.keys() - before.keys()),
            "deleted": len(before.keys() - after.keys()),
            "stable_changed": stable_changed,
            "confidence_changed": sum(
                before[k][confidence] != after[k][confidence] for k in common
            )
            if confidence is not None
            else 0,
            "fields": {
                field: sum(before[k][i] != after[k][i] for k in common)
                for i, field in enumerate(FIELDS[name])
            },
        }
        all_stable &= not (stable_changed or membership)
    return {"stable_equal": bool(all_stable), "groups": result}


def score_payload(scores, positions, overlay, freshness):
    roots = []
    for score in scores:
        row = dict(vars(score))
        for prefix, attr, value in (
            ("strongest", "strongest_branch", "score"),
            ("weakest", "weakest_branch", "score"),
            ("underexposed", "underexposed_branch", "value"),
        ):
            branch = getattr(score, attr)
            row[f"{prefix}_branch_name"] = branch.opening_name if branch else None
            row[f"{prefix}_branch_key"] = branch.opening_key if branch else None
            row[f"{prefix}_branch_{value}"] = branch.value if branch else None
        roots.append(tuple(row[f] for f in FIELDS["roots"]))
    return Payload(
        sorted_rows("roots", roots),
        sorted_rows(
            "positions",
            (tuple(getattr(row, f) for f in FIELDS["positions"]) for row in positions),
        ),
        sorted_rows(
            "edges",
            (
                tuple(getattr(row, f) for f in FIELDS["edges"])
                for row in overlay.edges.values()
            ),
        ),
        sorted_rows(
            "scope",
            [(fen, "raw") for fen in freshness.shared_raw_fens]
            + [(fen, "norm") for fen in freshness.shared_norm_fens],
        ),
    )


def replay_objects(candidate):
    roots = []
    for row in candidate.payload.rows("roots"):
        for prefix, value in (
            ("strongest", "score"),
            ("weakest", "score"),
            ("underexposed", "value"),
        ):
            row[f"{prefix}_branch"] = (
                SimpleNamespace(
                    opening_name=row[f"{prefix}_branch_name"],
                    opening_key=row[f"{prefix}_branch_key"],
                    value=row[f"{prefix}_branch_{value}"],
                )
                if row[f"{prefix}_branch_name"]
                else None
            )
        roots.append(SimpleNamespace(**row))
    positions = [SimpleNamespace(**row) for row in candidate.payload.rows("positions")]
    overlay = SimpleNamespace(
        edges={
            i: SimpleNamespace(**row)
            for i, row in enumerate(candidate.payload.rows("edges"))
        }
    )
    return roots, positions, overlay


def scale(candidate, copies):
    """Explicit persistence-only replicas, NOT legal FENs or new scorer evidence."""
    if copies == 1:
        return candidate
    groups = {}
    # Preserve identity-string sharing across payload groups. Replication must
    # not manufacture separate equal FEN strings for every edge/root/scope row
    # and then mistake that fixture overhead for required cache memory.
    identities = {}
    for name in MODELS:
        rows = []
        for copy in range(copies):
            for row in candidate.payload.rows(name):
                for field in row:
                    if (
                        field
                        in {
                            "normalized_fen",
                            "parent_fen",
                            "child_fen",
                            "fen",
                            "opening_key",
                        }
                        or field.endswith("_branch_key")
                    ) and row[field] is not None:
                        identity = (row[field], copy)
                        if identity not in identities:
                            identities[identity] = f"{row[field]}|replica:{copy:04d}"
                        row[field] = identities[identity]
                rows.append(tuple(row[f] for f in FIELDS[name]))
        groups[name] = sorted_rows(name, rows)
    payload = Payload(**groups)
    scope = payload.rows("scope")
    freshness = replace(
        candidate.freshness,
        shared_raw_fens=tuple(r["fen"] for r in scope if r["kind"] == "raw"),
        shared_norm_fens=tuple(r["fen"] for r in scope if r["kind"] == "norm"),
    )
    return replace(candidate, payload=payload, freshness=freshness)


def seed_session(db, serial, at, graph, *, drill=False):
    rng = random.Random(serial + 87123)
    board = chess.Board()
    session_id = uuid.UUID(int=serial + 1)
    session = GameSession(
        id=session_id,
        user_id=OWNER,
        player_color=COLOR,
        started_at=at,
        ended_at=at + timedelta(minutes=4),
        status="ended",
        result="win",
        engine_elo=1500,
    )
    if drill:
        session.session_mode = "drill"
        session.drill_state = "converted"
        session.converted_at = at + timedelta(minutes=1)
        session.normal_started_at = session.converted_at
        session.rated_start_ply = 4
        session.drill_root_reached_ply = 2
    db.add(session)
    db.flush()
    for ply in range(24):
        node = graph.get_node(normalize_fen(board.fen()))
        choices = sorted(node.children) if node and node.children else []
        legal = sorted(m.uci() for m in board.legal_moves)
        if not legal:
            break
        move = chess.Move.from_uci(rng.choice(choices or legal))
        before, san = board.fen(), board.san(move)
        board.push(move)
        db.add(
            SessionMove(
                session_id=session_id,
                move_number=ply // 2 + 1,
                color="white" if ply % 2 == 0 else "black",
                move_san=san,
                fen_before=before,
                fen_after=board.fen(),
                eval_delta=None if ply == 3 else rng.choice([0, 0, 15, 40, 100]),
            )
        )
    oc.bump_evidence_seq(db, OWNER, COLOR)
    db.commit()
    return session_id


def build_timeline(engine, *, sessions=64, repetitions=20):
    """Run the actual gate, evidence collector, scorer and writer at controlled times."""
    import time

    Base.metadata.create_all(engine)
    graph = oc.get_opening_graph()
    continuous, bucketed, events = [], [], []
    real_build = oc._build_cached_scores
    clock = {"now": START}
    measured = {}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"] if tz else clock["now"].replace(tzinfo=None)

    def record_build(
        color, graph, overlay, roots, computed_at, routing_snapshot, **kwargs
    ):
        start = time.perf_counter()
        scores, positions = real_build(
            color, graph, overlay, roots, computed_at, routing_snapshot, **kwargs
        )
        measured["scorer_ms"] = (time.perf_counter() - start) * 1000
        measured["objects"] = (scores, positions, overlay)
        bucket_at = computed_at.replace(minute=0, second=0, microsecond=0)
        bs, bp = real_build(color, graph, overlay, roots, bucket_at, routing_snapshot)
        measured["bucket_objects"] = (bs, bp, overlay)
        return scores, positions

    with (
        Session(engine) as db,
        patch.object(oc, "datetime", Clock),
        patch.object(oc, "_utcnow", side_effect=lambda: clock["now"]),
        patch.object(oc, "_build_cached_scores", side_effect=record_build),
        patch.object(oc, "capture"),
    ):
        db.add(EvidenceEpoch(id=1, value=0))
        db.commit()

        def request(cause, period):
            previous = oc.get_latest_opening_score_batch(db, OWNER, COLOR)
            old_epoch = previous.cache_epoch if previous else None
            result = oc.recompute_opening_scores_if_needed(db, OWNER, COLOR)
            if result.batch is not None:
                # The best-effort re-arm commits from an independent session, so
                # re-read the marker rather than trusting the returned snapshot.
                result = replace(
                    result, batch=oc.get_latest_opening_score_batch(db, OWNER, COLOR)
                )
            event = {
                "at": clock["now"].isoformat(),
                "cause": cause,
                "period": period,
                "disposition": result.disposition.value,
                "reason": result.reason,
                "epoch_rearmed": result.disposition == oc.RecomputeDisposition.CACHED
                and result.batch.cache_epoch != old_epoch,
            }
            if result.disposition == oc.RecomputeDisposition.REBUILT:
                batch = result.batch
                payload = capture(db, batch)
                scope = payload.rows("scope")
                freshness = oc.FreshnessSnapshot(
                    batch.inputs_fingerprint,
                    batch.evidence_seq,
                    batch.cache_epoch,
                    tuple(r["fen"] for r in scope if r["kind"] == "raw"),
                    tuple(r["fen"] for r in scope if r["kind"] == "norm"),
                    batch.scoped_shared_digest,
                )
                scored_payload = score_payload(*measured["objects"], freshness)
                assert payload == scored_payload
                payload = scored_payload  # keep scorer/graph string sharing
                candidate = Candidate(
                    payload,
                    clock["now"],
                    freshness,
                    result.reason,
                    cause,
                    period,
                    measured["scorer_ms"],
                )
                if continuous:
                    event["changes"] = changed(continuous[-1].payload, payload)
                continuous.append(candidate)
                bucketed.append(
                    replace(
                        candidate,
                        payload=score_payload(*measured["bucket_objects"], freshness),
                    )
                )
            events.append(event)
            db.rollback()

        request("empty_initial_request", "startup_control")
        for serial in range(sessions):
            seed_session(
                db,
                serial,
                START - timedelta(days=7, minutes=serial * 10),
                graph,
                drill=serial % 3 == 0,
            )
        request("seeded_history", "startup_control")
        request("frozen_time_noop", "forced_control")
        for i in range(repetitions):
            idle = i % 5 == 4
            clock["now"] += timedelta(days=2) if idle else timedelta(minutes=20)
            cause = (
                "daily_threshold"
                if idle
                else ("drill_terminal" if i % 2 else "normal_terminal")
            )
            if not idle:
                if i == 6:
                    db.execute(
                        update(SessionMove)
                        .where(
                            SessionMove.session_id == uuid.UUID(int=1),
                            SessionMove.color == COLOR,
                        )
                        .values(eval_delta=120)
                    )
                    oc.bump_evidence_seq(db, OWNER, COLOR)
                    db.commit()
                    reset_session_evidence_cache()
                    cause = "evidence_correction"
                elif i == 7:
                    db.execute(
                        delete(SessionMove).where(
                            SessionMove.session_id == uuid.UUID(int=2)
                        )
                    )
                    db.execute(
                        delete(GameSession).where(GameSession.id == uuid.UUID(int=2))
                    )
                    oc.bump_evidence_seq(db, OWNER, COLOR)
                    db.commit()
                    reset_session_evidence_cache()
                    cause = "evidence_deletion"
                else:
                    seed_session(
                        db,
                        sessions + i,
                        clock["now"] - timedelta(minutes=5),
                        graph,
                        drill=i % 2 == 1,
                    )
            request(cause, "idle" if idle else "active")
            request("warm_followup", "idle" if idle else "active")
            db.execute(update(EvidenceEpoch).values(value=EvidenceEpoch.value + 1))
            db.commit()
            request("unrelated_epoch", "idle" if idle else "active")
        # Real priority branches, kept outside steady-state denominators.
        batch = oc.get_latest_opening_score_batch(db, OWNER, COLOR)
        db.execute(
            update(OpeningScoreBatch)
            .where(OpeningScoreBatch.id == batch.id)
            .values(registry_fingerprint="synthetic-registry-transition")
        )
        db.commit()
        request("registry_transition", "forced_control")
        batch = oc.get_latest_opening_score_batch(db, OWNER, COLOR)
        row = db.scalars(
            select(UserOpeningScore).where(UserOpeningScore.batch_id == batch.id)
        ).first()
        row.strongest_branch_name, row.strongest_branch_key = (
            "legacy synthetic branch",
            None,
        )
        db.commit()
        request("legacy_branch_keys", "forced_control")
    reset_session_evidence_cache()
    return continuous, bucketed, events


def summarize_timeline(candidates, bucketed, events):
    summary = {}
    for period in (
        "all",
        "steady",
        "active",
        "idle",
        "startup_control",
        "forced_control",
    ):
        selected = [
            e
            for e in events
            if period == "all"
            or e["period"] == period
            or (period == "steady" and e["period"] in ("active", "idle"))
        ]
        rebuilt = [e for e in selected if e["disposition"] == "rebuilt"]
        reasons = Counter(e["reason"] for e in rebuilt)
        summary[period] = {
            "requests": len(selected),
            "rebuilds": len(rebuilt),
            "dispositions": dict(Counter(e["disposition"] for e in selected)),
            "failures": 0,
            "epoch_rearms": sum(e["epoch_rearmed"] for e in selected),
            "reasons": {
                r: {
                    "count": reasons[r],
                    "fraction": reasons[r] / len(rebuilt) if rebuilt else 0,
                    "stable_equal": sum(
                        e.get("changes", {}).get("stable_equal", False)
                        for e in rebuilt
                        if e["reason"] == r
                    ),
                }
                for r in REASONS
            },
        }
    deviations = []
    for real, bucket in zip(candidates, bucketed, strict=True):
        for name in ("roots", "positions"):
            index = FIELDS[name].index("confidence")
            for a, b in zip(
                getattr(real.payload, name), getattr(bucket.payload, name), strict=True
            ):
                if a[index] is not None and b[index] is not None:
                    deviations.append(abs(a[index] - b[index]))
    return {
        "provenance": "deterministic synthetic sessions; real gate/overlay/scorer; no production frequencies",
        "seed": 87123,
        "start": events[0]["at"],
        "end": events[-1]["at"],
        "periods": summary,
        "events": events,
        "scorer_ms": [c.scorer_ms for c in candidates],
        "actual_sizes": [
            {name: len(getattr(c.payload, name)) for name in MODELS} for c in candidates
        ],
        "quantized_control": {
            "shipping": False,
            "bucket_seconds": 3600,
            "max_absolute_confidence_deviation": max(deviations, default=0),
            "changes": [
                changed(a.payload, b.payload) for a, b in zip(bucketed, bucketed[1:])
            ],
            "bucket_crossings": sum(
                a.computed_at.replace(minute=0) != b.computed_at.replace(minute=0)
                for a, b in zip(bucketed, bucketed[1:])
            ),
        },
    }
