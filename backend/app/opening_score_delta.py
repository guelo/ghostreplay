"""Freshness-bound terminal and live-boundary opening-score deltas.

A game or drill that ends reports how the *played* openings' scores changed,
broadest -> deepest. The "before" side is the per-session baseline captured
shortly after session start (``GameSession.opening_score_baseline``). The "after"
side prefers a provably-fresh persisted batch, then a freshness-bound
process-local scoped publication computed on the immediate terminal lane
independently of the full batch writer, and finally a warm batch as an explicitly
unverified fallback.

Why a baseline is required: opening scores are cumulative over all evidence and
live play feeds ``request_recompute`` incrementally as moves upload, so by the
time a session ends the cached score already reflects most of that session. There
is no "pre-session" score left to diff against unless it was captured up front.

ASYNC CAPTURE: each start transaction stores a durable per-user/shared/registry
watermark. The worker may accept a batch built later only after composing two
proofs: the batch equals current relevant state, and current relevant state still
equals the session's start state. This recovers cold/stale starts without using a
wall-clock ordering claim and keeps post-start evidence fail-closed.

The terminal and live-boundary helpers are best-effort and never raise. A live
failure remains pending until terminal fallback; a terminal failure degrades to
"no delta shown" rather than breaking the rating or drill contract.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from pydantic import BaseModel
from sqlalchemy import case, func, literal, select, update
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.game_phase import (
    OPENING_PHASE_PROTOCOL_VERSION,
    divide,
    prove_complete_standard_line,
)
from app.models import (
    EvidenceEpoch,
    GameSession,
    OpeningScoreBatch,
    OpeningScoreBatchSharedScope,
    OpeningScoreCursor,
    SessionMove,
    SharedEvidenceScopeInvalidation,
    SharedEvidenceScopeVersion,
    UserOpeningScore,
)
from app.opening_cache import (
    SCORE_MODEL_VERSION,
    _is_batch_fresh,
    current_cache_epoch,
    current_evidence_seq,
    has_opening_evidence,
    list_cached_opening_scores,
    opening_score_inputs_fingerprint,
)
from app.opening_evidence import (
    EvidenceOverlay,
    OverlaySharedScope,
    ReplayCacheStats,
    SharedScopeSnapshot,
    overlay_evidence,
    overlay_evidence_with_active_prefix,
    replay_cache_telemetry,
    session_is_evidence_eligible,
    shared_scope_digest,
    shared_scope_snapshot,
)
from app.opening_graph import get_opening_graph
from app.opening_rootcalc import (
    RootCalcConfig,
    SYNTHETIC_INITIAL_FEN,
    SYNTHETIC_ROOT_FAMILY,
    compute_scoped_root_scores,
    root_calc_config_fingerprint,
)
from app.opening_roots import get_opening_roots, played_opening_chain
from app.opening_transposition_artifact import load_strict_densified_edges
from app.session_contracts import ply_after_expr

logger = logging.getLogger(__name__)

OPENING_BASELINE_SCHEMA_VERSION = 2
OPENING_BOUNDARY_PUBLICATION_ENV = "OPENING_BOUNDARY_PUBLICATION_ENABLED"


def _read_opening_boundary_publication_switch() -> bool:
    """Read the emergency switch once at process import; invalid fails closed."""

    raw = os.getenv(OPENING_BOUNDARY_PUBLICATION_ENV)
    if raw is None:
        return False
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    logger.error(
        "%s=%r is invalid; opening-boundary publication is disabled",
        OPENING_BOUNDARY_PUBLICATION_ENV,
        raw,
    )
    return False


OPENING_BOUNDARY_PUBLICATION_ENABLED = (
    _read_opening_boundary_publication_switch()
)


class BaselineSnapshotSource(str, Enum):
    """Closed scheduler/result vocabulary for baseline capture."""

    CACHED_FRESH = "cached_fresh"
    EMPTY_NO_EVIDENCE = "empty_no_evidence"
    SKIPPED_STALE = "skipped_stale"
    SKIPPED_COLD = "skipped_cold"
    SKIPPED_RECOMPUTE_INFLIGHT = "skipped_recompute_inflight"
    SKIPPED_QUARANTINED_EMPTY = "skipped_quarantined_empty"
    RACED_EVIDENCE_OR_ALREADY_SET = "raced_evidence_or_already_set"
    ALREADY_SET = "already_set"
    MISSING_SESSION = "missing_session"
    NOT_ACTIVE = "not_active"
    SESSION_MISMATCH = "session_mismatch"
    WATERMARK_MISSING = "watermark_missing"
    WATERMARK_MISMATCH = "watermark_mismatch"
    FAILED = "failed"


BASELINE_RETRYABLE_SOURCES = frozenset(
    {
        BaselineSnapshotSource.SKIPPED_STALE,
        BaselineSnapshotSource.SKIPPED_COLD,
        BaselineSnapshotSource.SKIPPED_RECOMPUTE_INFLIGHT,
        BaselineSnapshotSource.RACED_EVIDENCE_OR_ALREADY_SET,
    }
)
BASELINE_TERMINAL_SOURCES = frozenset(BaselineSnapshotSource) - BASELINE_RETRYABLE_SOURCES


class BaselineWatermarkMismatch(str, Enum):
    SEQ = "seq"
    REGISTRY = "registry"
    SHARED_SCOPE = "shared_scope"
    SHARED_INVALIDATION = "shared_invalidation"
    EPOCH_CORRUPTION = "epoch_corruption"


def _serialize_baseline(
    scores: dict[str, float], *, registry_fingerprint: str | None = None
) -> str:
    """Serialize a baseline with the score model/config compatibility boundary."""
    graph = get_opening_graph()
    routing_snapshot = load_strict_densified_edges(graph)
    routing_edge_fingerprint = routing_snapshot.fingerprint
    if (
        registry_fingerprint is not None
        and registry_fingerprint
        != opening_score_inputs_fingerprint(
            graph,
            get_opening_roots(),
            routing_snapshot,
        )
    ):
        raise RuntimeError(
            "Opening baseline routing snapshot does not match the scored batch"
        )
    return json.dumps(
        {
            "schema_version": OPENING_BASELINE_SCHEMA_VERSION,
            "model_version": SCORE_MODEL_VERSION,
            "root_calc_config_fingerprint": root_calc_config_fingerprint(),
            "routing_edge_fingerprint": routing_edge_fingerprint,
            "scores": scores,
        }
    )


def _has_baseline_relevant_root(rows: list[UserOpeningScore]) -> bool:
    """Whether a persisted batch has a root that a played-opening chain can use."""

    return any(
        row.opening_family != SYNTHETIC_ROOT_FAMILY
        and row.opening_key != SYNTHETIC_INITIAL_FEN
        for row in rows
    )


def _is_evidence_backed_empty_baseline(
    db: Session,
    user_id: int,
    player_color: str,
    rows: list[UserOpeningScore],
) -> bool:
    """Infer the persisted shape that is unsafe to snapshot as an empty baseline.

    This intentionally folds clean rootless evidence into the same terminal branch
    as an all-quarantined batch: without a persisted discriminator those shapes are
    indistinguishable, and suppressing a rare ``is_new`` is safer than relabelling an
    established repertoire wholesale.
    """

    return not _has_baseline_relevant_root(rows) and has_opening_evidence(
        db, user_id, player_color
    )


def _parse_compatible_baseline(payload: str | None) -> dict[str, float] | None:
    """Return same-model scores, or None when deltas must be suppressed.

    Legacy bare maps and every malformed, unknown-version, cross-model, or
    cross-config envelope fail closed. The current after-score may still render;
    only the before/new/delta claims are suppressed.
    """
    if payload is None:
        return None
    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    schema_version = parsed.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version != OPENING_BASELINE_SCHEMA_VERSION
        or parsed.get("model_version") != SCORE_MODEL_VERSION
        or parsed.get("root_calc_config_fingerprint")
        != root_calc_config_fingerprint()
        or parsed.get("routing_edge_fingerprint")
        != load_strict_densified_edges(get_opening_graph()).fingerprint
    ):
        return None
    scores = parsed.get("scores")
    if not isinstance(scores, dict):
        return None
    compatible: dict[str, float] = {}
    for key, value in scores.items():
        if (
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0.0 <= value <= 100.0
        ):
            return None
        compatible[key] = float(value)
    return compatible


def capture_baseline_watermark(
    db: Session,
    user_id: int,
    player_color: str,
) -> tuple[int, int, str] | None:
    """Capture the complete start-state watermark without poisoning ``db``.

    The per-user sequence and global epoch are read by one SQL statement. The
    SAVEPOINT isolates a failed read/fingerprint calculation; it does not create
    the counter snapshot semantics. Missing epoch state fails closed to ``None``.
    """
    seq_value = (
        select(OpeningScoreCursor.evidence_seq)
        .where(
            OpeningScoreCursor.user_id == user_id,
            OpeningScoreCursor.player_color == player_color,
        )
        .scalar_subquery()
    )
    epoch_value = (
        select(EvidenceEpoch.value)
        .where(EvidenceEpoch.id == 1)
        .scalar_subquery()
    )
    try:
        with db.begin_nested():
            evidence_seq, evidence_epoch = db.execute(
                select(func.coalesce(seq_value, 0), epoch_value)
            ).one()
            graph = get_opening_graph()
            routing_snapshot = load_strict_densified_edges(graph)
            registry_fingerprint = opening_score_inputs_fingerprint(
                graph, get_opening_roots(), routing_snapshot
            )
        if evidence_epoch is None:
            logger.warning("opening baseline watermark capture source=missing_epoch")
            return None
        return int(evidence_seq), int(evidence_epoch), registry_fingerprint
    except Exception:  # noqa: BLE001 - start remains best-effort
        logger.warning(
            "opening baseline watermark capture source=failed",
            exc_info=True,
        )
        return None


class OpeningScoreDeltaItem(BaseModel):
    """One played opening's before -> after score change.

    ``before``/``after`` are the raw cached opening scores (same units the
    /openings cards render). ``delta`` is ``after - before`` when both are known,
    else None. ``is_new`` is True only when a baseline exists but lacked this
    opening (a brand-new opening crossed for the first time this session) — it is
    deliberately False when the baseline is missing entirely, since then we can't
    distinguish new from pre-existing and only show the after-score.
    """

    opening_key: str
    opening_name: str
    opening_family: str
    eco: str | None
    depth: int
    before: float | None
    after: float | None
    delta: float | None
    is_new: bool


SCOPED_DELTA_PUBLICATION_CAPACITY = 128
SCOPED_DELTA_GENERATION_CAPACITY = 256
SCOPED_DELTA_SOURCE_TERMINAL = "terminal"
SCOPED_DELTA_SOURCE_OPENING_BOUNDARY = "opening_boundary"
BOUNDARY_DELTA_RECOVERY_COOLDOWN_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class ActivePrefixSnapshot:
    rows: tuple[SessionMove, ...]
    prefix_digest: str
    reconciliation_token: str


def _drop_cached_active_prefix_token(session_id) -> None:
    with _scoped_delta_lock:
        _active_prefix_tokens_by_session.pop(str(session_id), None)


def _ordered_active_prefix_rows(
    db: Session,
    session_id,
    through_ply: int,
) -> list[SessionMove]:
    color_order = case((SessionMove.color == "white", 0), else_=1)
    ply = ply_after_expr(SessionMove.move_number, SessionMove.color)
    return (
        db.query(SessionMove)
        .filter(SessionMove.session_id == session_id, ply <= through_ply)
        .order_by(
            SessionMove.move_number.asc(),
            color_order.asc(),
            SessionMove.id.asc(),
        )
        .populate_existing()
        .all()
    )


def _active_prefix_digest(
    session: GameSession,
    rows: list[SessionMove],
) -> str:
    """Hash every field consumed while scoring the active prefix."""

    payload = {
        "started_at": session.started_at.isoformat(),
        "rows": [
            [
                row.move_number,
                row.color,
                row.fen_before,
                row.fen_after,
                row.move_san,
                row.eval_delta,
                row.eval_cp,
                row.best_move_eval_cp,
            ]
            for row in rows
        ],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def active_prefix_snapshot(
    db: Session,
    session: GameSession,
) -> ActivePrefixSnapshot | None:
    """Return the current exact active-prefix proof and opaque client token."""

    marker = session.opening_middle_ply
    if (
        session.status != "active"
        or session.opening_phase_protocol_version
        != OPENING_PHASE_PROTOCOL_VERSION
        or session_is_evidence_eligible(session)
        or marker is None
        or marker < 1
        or session.opening_middle_candidate_ply != marker
        or session.opening_score_baseline is None
        or session.opening_phase_exhausted
    ):
        _drop_cached_active_prefix_token(session.id)
        return None
    rows = _ordered_active_prefix_rows(db, session.id, marker)
    prefix_digest = _active_prefix_digest(session, rows)
    baseline_digest = hashlib.sha256(
        session.opening_score_baseline.encode("utf-8")
    ).hexdigest()
    token_payload = "|".join(
        (
            str(session.id),
            str(session.move_line_revision),
            str(marker),
            prefix_digest,
            baseline_digest,
        )
    )
    reconciliation_token = hashlib.sha256(
        token_payload.encode("utf-8")
    ).hexdigest()

    # The row digest is cheaper than reparsing SAN and rerunning the divider.
    # Ordinary post-boundary uploads still need to echo the opaque token, but a
    # move beyond the marker leaves this digest unchanged and reuses the exact
    # proof. Any scorer-consumed prefix edit changes the token and must re-prove.
    with _scoped_delta_lock:
        cached_proof = _active_prefix_proof_cache.get(reconciliation_token)
        if cached_proof is not None:
            _active_prefix_proof_cache.move_to_end(reconciliation_token)
    if cached_proof is None:
        proof = prove_complete_standard_line(rows, marker)
        cached_proof = proof.verdict.value == "passed"
        if cached_proof:
            boards = [*proof.premove_boards]
            if proof.final_board is not None:
                boards.append(proof.final_board)
            cached_proof = divide(boards).middle == marker
        with _scoped_delta_lock:
            _active_prefix_proof_cache[reconciliation_token] = cached_proof
            _active_prefix_proof_cache.move_to_end(reconciliation_token)
            while (
                len(_active_prefix_proof_cache)
                > SCOPED_DELTA_GENERATION_CAPACITY
            ):
                _active_prefix_proof_cache.popitem(last=False)
    if not cached_proof:
        _drop_cached_active_prefix_token(session.id)
        return None
    cache_identity = (
        session.move_line_revision,
        marker,
        baseline_digest,
    )
    with _scoped_delta_lock:
        session_key = str(session.id)
        _active_prefix_tokens_by_session[session_key] = (
            cache_identity,
            reconciliation_token,
        )
        _active_prefix_tokens_by_session.move_to_end(session_key)
        while (
            len(_active_prefix_tokens_by_session)
            > SCOPED_DELTA_GENERATION_CAPACITY
        ):
            _active_prefix_tokens_by_session.popitem(last=False)
    return ActivePrefixSnapshot(
        rows=tuple(rows),
        prefix_digest=prefix_digest,
        reconciliation_token=reconciliation_token,
    )


def opening_boundary_reconciliation_token(
    db: Session,
    session: GameSession,
    *,
    allow_cached: bool = False,
) -> str | None:
    marker = session.opening_middle_ply
    if allow_cached and (
        session.status == "active"
        and session.opening_phase_protocol_version
        == OPENING_PHASE_PROTOCOL_VERSION
        and not session_is_evidence_eligible(session)
        and marker is not None
        and marker >= 1
        and session.opening_middle_candidate_ply == marker
        and session.opening_score_baseline is not None
        and not session.opening_phase_exhausted
    ):
        identity = (
            session.move_line_revision,
            marker,
            hashlib.sha256(
                session.opening_score_baseline.encode("utf-8")
            ).hexdigest(),
        )
        with _scoped_delta_lock:
            session_key = str(session.id)
            cached = _active_prefix_tokens_by_session.get(session_key)
            if cached is not None and cached[0] == identity:
                _active_prefix_tokens_by_session.move_to_end(session_key)
                return cached[1]
    snapshot = active_prefix_snapshot(db, session)
    return snapshot.reconciliation_token if snapshot is not None else None


def enqueue_opening_boundary_delta(
    db: Session,
    session: GameSession,
    *,
    force_reenqueue: bool = False,
    allow_cached_token: bool = False,
) -> str | None:
    """Best-effort enqueue of one currently valid durable active marker."""

    token = opening_boundary_reconciliation_token(
        db,
        session,
        allow_cached=allow_cached_token,
    )
    if token is None:
        return None
    if not force_reenqueue:
        with _scoped_delta_lock:
            existing = _scoped_delta_requests.get(str(session.id))
            if (
                existing is not None
                and existing.source == SCOPED_DELTA_SOURCE_OPENING_BOUNDARY
                and existing.reconciliation_token == token
            ):
                # Ordinary post-boundary uploads echo the same token but must not
                # recompute the unchanged prefix. Poll misses and explicit proof
                # retries opt into recovery re-enqueue below.
                return token
    try:
        from app.opening_score_delta_lane import (
            DeltaLaneEnqueueOutcome,
            enqueue_scoped_delta,
            is_scoped_delta_scheduled,
        )

        if force_reenqueue:
            if is_scoped_delta_scheduled(
                session.user_id,
                session.player_color,
                session.id,
                source=SCOPED_DELTA_SOURCE_OPENING_BOUNDARY,
                reconciliation_token=token,
            ):
                return token
            now = time.monotonic()
            recovery_key = (str(session.id), token)
            with _scoped_delta_lock:
                last_enqueue = _boundary_delta_enqueue_times.get(recovery_key)
                if (
                    last_enqueue is not None
                    and now - last_enqueue
                    < BOUNDARY_DELTA_RECOVERY_COOLDOWN_SECONDS
                ):
                    _boundary_delta_enqueue_times.move_to_end(recovery_key)
                    return token

        outcome = enqueue_scoped_delta(
            session.user_id,
            session.player_color,
            session.id,
            source=SCOPED_DELTA_SOURCE_OPENING_BOUNDARY,
            reconciliation_token=token,
        )
        if outcome in {
            DeltaLaneEnqueueOutcome.ENQUEUED,
            DeltaLaneEnqueueOutcome.COALESCED,
        }:
            recovery_key = (str(session.id), token)
            with _scoped_delta_lock:
                _boundary_delta_enqueue_times[recovery_key] = time.monotonic()
                _boundary_delta_enqueue_times.move_to_end(recovery_key)
                while (
                    len(_boundary_delta_enqueue_times)
                    > SCOPED_DELTA_GENERATION_CAPACITY
                ):
                    _boundary_delta_enqueue_times.popitem(last=False)
    except Exception:
        logger.warning(
            "opening boundary delta enqueue failed session_id=%s",
            session.id,
            exc_info=True,
        )
        # The durable marker/token remains valid. Returning it lets the response
        # expose recovery ownership; the first poll can re-enqueue the same work.
        return token
    return token


def refresh_opening_boundary_publication_state(
    db: Session,
    session: GameSession,
    *,
    now: datetime | None = None,
    force_database_check: bool = False,
) -> bool:
    """Derive ready/marker state from locked database columns, never stale ORM.

    Returns whether this call created the durable publication marker.  The
    caller owns commit and must enqueue scoped work only after that commit.
    """

    # Request handlers hold a fresh session-row lock. Before a candidate exists,
    # their ordinary uploads must preserve the one-lock/no-extra-read hot path.
    # Baseline workers opt into the database check because their ORM identity may
    # predate a concurrently committed candidate.
    if not force_database_check and (
        session.status != "active"
        or session.opening_phase_protocol_version
        != OPENING_PHASE_PROTOCOL_VERSION
        or session.opening_middle_candidate_ply is None
    ):
        return False

    ready_at = now or datetime.now(timezone.utc)
    db.execute(
        update(GameSession)
        .where(
            GameSession.id == session.id,
            GameSession.status == "active",
            GameSession.opening_phase_protocol_version
            == OPENING_PHASE_PROTOCOL_VERSION,
            GameSession.opening_score_baseline.is_not(None),
            GameSession.opening_middle_candidate_ply.is_not(None),
            GameSession.opening_middle_ready_at.is_(None),
            GameSession.opening_phase_exhausted.is_(False),
        )
        .values(opening_middle_ready_at=ready_at)
        .execution_options(synchronize_session=False)
    )
    marker_created = False
    if OPENING_BOUNDARY_PUBLICATION_ENABLED:
        marker_created = bool(
            db.execute(
                update(GameSession)
                .where(
                    GameSession.id == session.id,
                    GameSession.status == "active",
                    GameSession.opening_phase_protocol_version
                    == OPENING_PHASE_PROTOCOL_VERSION,
                    GameSession.opening_score_baseline.is_not(None),
                    GameSession.opening_middle_candidate_ply.is_not(None),
                    GameSession.opening_middle_ready_at.is_not(None),
                    GameSession.opening_middle_ply.is_(None),
                    GameSession.opening_phase_exhausted.is_(False),
                )
                .values(
                    opening_middle_ply=GameSession.opening_middle_candidate_ply
                )
                .execution_options(synchronize_session=False)
            ).rowcount
        )
    db.refresh(
        session,
        attribute_names=[
            "status",
            "move_line_revision",
            "opening_phase_protocol_version",
            "opening_score_baseline",
            "opening_middle_candidate_ply",
            "opening_middle_ready_at",
            "opening_middle_ply",
            "opening_phase_exhausted",
        ],
    )
    return marker_created


@dataclass(frozen=True, slots=True)
class ScopedDeltaRequest:
    session_id: uuid.UUID
    generation: int
    source: str = "terminal"
    reconciliation_token: str | None = None


@dataclass(frozen=True, slots=True)
class _ScopedDeltaCandidate:
    request: ScopedDeltaRequest
    session_id: str
    user_id: int
    player_color: str
    session_mode: str
    status: str
    drill_state: str | None
    drill_terminal_reason: str | None
    played_root_keys: tuple[str, ...]
    source: str
    line_revision: int | None = None
    opening_middle_ply: int | None = None
    prefix_digest: str | None = None
    reconciliation_token: str | None = None
    active_rows: tuple[SessionMove, ...] = ()
    session_ts: datetime | str | None = None


@dataclass(frozen=True, slots=True)
class _PreparedScopedGroup:
    overlay: EvidenceOverlay
    candidates: tuple[_ScopedDeltaCandidate, ...]
    scope: OverlaySharedScope
    shared_snapshot: SharedScopeSnapshot


@dataclass(frozen=True, slots=True)
class ScopedDeltaPublication:
    """Process-local, freshness-bound score result for one session."""

    session_id: str
    generation: int
    user_id: int
    player_color: str
    session_mode: str
    status: str
    drill_state: str | None
    drill_terminal_reason: str | None
    played_root_keys: tuple[str, ...]
    source: str
    line_revision: int | None
    opening_middle_ply: int | None
    prefix_digest: str | None
    reconciliation_token: str | None
    scored_roots: tuple[tuple[str, float], ...]
    registry_fingerprint: str
    evidence_seq: int
    cache_epoch: int
    shared_raw_fens: tuple[str, ...]
    shared_norm_fens: tuple[str, ...]
    scoped_shared_digest: str
    computed_at: datetime


# One worker AND one replica are load-bearing for this cache, exactly as for the
# scheduler that writes it.  Adding either requires moving publications and
# request generations to a shared store; a process-local miss is safe, but
# accepting another process's absent generation as current would not be.
_scoped_delta_lock = threading.Lock()
_scoped_delta_publications: OrderedDict[str, ScopedDeltaPublication] = OrderedDict()
_scoped_delta_generations: OrderedDict[str, int] = OrderedDict()
_scoped_delta_requests: OrderedDict[str, ScopedDeltaRequest] = OrderedDict()
_active_prefix_proof_cache: OrderedDict[str, bool] = OrderedDict()
_active_prefix_tokens_by_session: OrderedDict[
    str,
    tuple[tuple[int, int, str], str],
] = OrderedDict()
_boundary_delta_enqueue_times: OrderedDict[tuple[str, str], float] = OrderedDict()
_scoped_delta_generation_counter = itertools.count(1)


def reserve_scoped_delta_generation(
    session_id,
    *,
    source: str = "terminal",
    reconciliation_token: str | None = None,
) -> ScopedDeltaRequest:
    """Assign the next monotonic generation before scoped work is enqueued."""
    parsed = (
        session_id
        if isinstance(session_id, uuid.UUID)
        else uuid.UUID(str(session_id))
    )
    key = str(parsed)
    with _scoped_delta_lock:
        existing = _scoped_delta_requests.get(key)
        if source == SCOPED_DELTA_SOURCE_OPENING_BOUNDARY and existing is not None:
            if existing.source == SCOPED_DELTA_SOURCE_TERMINAL or (
                existing.source == source
                and existing.reconciliation_token == reconciliation_token
            ):
                _scoped_delta_generations.move_to_end(key)
                _scoped_delta_requests.move_to_end(key)
                return existing
        generation = next(_scoped_delta_generation_counter)
        request = ScopedDeltaRequest(
            session_id=parsed,
            generation=generation,
            source=source,
            reconciliation_token=reconciliation_token,
        )
        _scoped_delta_generations[key] = generation
        _scoped_delta_generations.move_to_end(key)
        _scoped_delta_requests[key] = request
        _scoped_delta_requests.move_to_end(key)
        while len(_scoped_delta_generations) > SCOPED_DELTA_GENERATION_CAPACITY:
            evicted_key, _ = _scoped_delta_generations.popitem(last=False)
            _scoped_delta_requests.pop(evicted_key, None)
            _scoped_delta_publications.pop(evicted_key, None)
    return request


def _publish_scoped_delta(publication: ScopedDeltaPublication) -> bool:
    """Compare-and-swap publication against the latest requested generation."""
    with _scoped_delta_lock:
        if (
            _scoped_delta_generations.get(publication.session_id)
            != publication.generation
        ):
            return False
        _scoped_delta_publications[publication.session_id] = publication
        _scoped_delta_publications.move_to_end(publication.session_id)
        while len(_scoped_delta_publications) > SCOPED_DELTA_PUBLICATION_CAPACITY:
            _scoped_delta_publications.popitem(last=False)
        return True


def _current_scoped_delta(session_id) -> ScopedDeltaPublication | None:
    key = str(session_id)
    with _scoped_delta_lock:
        publication = _scoped_delta_publications.get(key)
        if publication is None:
            return None
        if _scoped_delta_generations.get(key) != publication.generation:
            return None
        _scoped_delta_publications.move_to_end(key)
        return publication


def _rearm_scoped_delta_epoch(
    publication: ScopedDeltaPublication,
    epoch: int,
) -> ScopedDeltaPublication | None:
    """Re-arm one still-current publication after an equal scoped digest."""
    with _scoped_delta_lock:
        current = _scoped_delta_publications.get(publication.session_id)
        if (
            current != publication
            or _scoped_delta_generations.get(publication.session_id)
            != publication.generation
        ):
            return None
        rearmed = replace(publication, cache_epoch=epoch)
        _scoped_delta_publications[publication.session_id] = rearmed
        _scoped_delta_publications.move_to_end(publication.session_id)
        return rearmed


def reset_scoped_delta_cache() -> None:
    """Test/lifecycle hook: clear publications and request bindings."""
    with _scoped_delta_lock:
        _scoped_delta_publications.clear()
        _scoped_delta_generations.clear()
        _scoped_delta_requests.clear()
        _active_prefix_proof_cache.clear()
        _active_prefix_tokens_by_session.clear()
        _boundary_delta_enqueue_times.clear()


def _capture_baseline_json(
    db: Session,
    user_id: int,
    player_color: str,
    *,
    skip_when_inflight: bool,
) -> tuple[str | None, str]:
    """Capture current scores for the legacy synchronous-before-insert surface.

    Returns ``(json_or_none, source)``. No ``try/except``, no rollback, no logging,
    no timing ``finally`` — the two best-effort wrappers own those, and unexpected
    DB errors propagate to them. ``json_or_none`` is a same-model envelope whose
    ``scores`` may be empty (valid baseline for a user with no evidence, so the
    session's first openings later read as new), or None (no confident baseline).

    Guards, in order:

    - No cached batch: an empty envelope with ``"empty_no_evidence"`` when
      ``has_opening_evidence``
      is false, else ``(None, "skipped_cold")`` (a cold cache with evidence cannot
      prove a baseline; persisting one would falsely mark every existing opening
      "new" at session end).
    - ``skip_when_inflight`` (synchronous start path, g-1iul): while a recompute is
      RUNNING, the freshness digest (``_is_batch_fresh`` -> ``raw_evidence_inputs_digest``)
      GIL-serializes against it (the ~9.6s pathology), so degrade to
      ``(None, "skipped_recompute_inflight")`` WITHOUT paying it. The evidence probe
      only runs in the no-batch case (to keep a brand-new user's empty baseline).
      The async worker passes ``skip_when_inflight=False``: it is off the request
      thread, so correctness comes from the strict date/freshness proof instead.
    - Freshness: a provably-stale batch returns ``(None, "skipped_stale")``.
    - Baseline relevance: after freshness is proven, evidence plus zero surviving
      non-synthetic named-root rows returns
      ``(None, "skipped_quarantined_empty")``. The persisted shape intentionally
      covers both an all-quarantined batch and indistinguishable clean rootless
      evidence.
    - Otherwise, a fresh batch returns
      ``(json.dumps({key: score}), "cached_fresh")``.

    The async worker does not call this helper: it uses the durable session
    watermark and the two-proof acceptance path below.
    """
    # Cheap indexed batch+rows read first (no fingerprint/digest).
    batch, rows = list_cached_opening_scores(db, user_id, player_color)

    if skip_when_inflight:
        # IN-FLIGHT-ONLY gate (lazy imports avoid scheduler/cache cycles). Either
        # opening-score worker can make the O(evidence) digest GIL-serialize; a
        # merely pending entry is idle and is deliberately not gated here.
        from app.opening_score_delta_lane import is_delta_lane_inflight
        from app.opening_score_scheduler import is_recompute_inflight

        if is_recompute_inflight(
            user_id, player_color
        ) or is_delta_lane_inflight(user_id, player_color):
            if batch is not None:
                # Running worker is rebuilding the batch; we cannot prove the
                # current one fresh without paying the digest it would serialize
                # against. Degrade to no-baseline (NO evidence probe, NO digest).
                return None, "skipped_recompute_inflight"
            # No batch: only now pay the evidence probe to keep a brand-new user's
            # valid empty baseline. Confined to the batch-is-None case.
            if has_opening_evidence(db, user_id, player_color):
                return None, "skipped_recompute_inflight"
            return _serialize_baseline({}), "empty_no_evidence"

    if batch is None:
        # No batch yet. Distinguish a brand-new user (no evidence -> valid empty
        # baseline) from a cold cache that still has evidence (cannot prove a
        # baseline -> skip, else session-end falsely marks every opening "new").
        if has_opening_evidence(db, user_id, player_color):
            return None, "skipped_cold"
        return _serialize_baseline({}), "empty_no_evidence"

    if not _is_batch_fresh(db, batch, rows):
        # Cache exists but is provably stale (evidence/registry drift or legacy
        # branch keys). Persisting it would reintroduce the misattribution.
        return None, "skipped_stale"
    if _is_evidence_backed_empty_baseline(db, user_id, player_color, rows):
        return None, BaselineSnapshotSource.SKIPPED_QUARANTINED_EMPTY.value
    return _serialize_baseline(
        {row.opening_key: row.opening_score for row in rows},
        registry_fingerprint=batch.registry_fingerprint,
    ), "cached_fresh"


def snapshot_opening_baseline(
    db: Session, user_id: int, player_color: str
) -> str | None:
    """Best-effort SYNCHRONOUS baseline capture — thin wrapper over
    ``_capture_baseline_json`` for direct tests and any legacy callers.

    After g-mxeo, production start handlers no longer call this; the baseline is
    captured asynchronously by ``run_baseline_snapshot_job`` on the
    ``OpeningBaselineScheduler`` worker. This wrapper preserves the original
    contract: it never raises, rolls back best-effort on failure, returns None on
    failure, gates the O(evidence) digest behind the in-flight-only probe
    (``skip_when_inflight=True``), and logs the ``opening_baseline_snapshot ...``
    line. The synchronous capture runs BEFORE the session INSERT, so its fresh
    current-state batch is already a valid pre-session baseline.
    """
    t0 = time.perf_counter()
    source = "failed"
    try:
        result, source = _capture_baseline_json(
            db, user_id, player_color, skip_when_inflight=True
        )
        return result
    except Exception:  # noqa: BLE001 — best-effort snapshot must never block start
        # A failed read can abort the transaction (esp. Postgres); roll back so
        # the caller's session-create insert/commit is not poisoned. Guard the
        # rollback itself so a degenerate session state still degrades to None.
        source = "failed"
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "opening_baseline_snapshot source=failed user_id=%s color=%s",
            user_id,
            player_color,
            exc_info=True,
        )
        return None
    finally:
        # Fields go IN THE MESSAGE: the root formatter prints %(message)s only, so
        # extra= kwargs would be dropped. snapshot_ms proves the snapshot is fast;
        # the route's duration_ms (HTTPLoggingMiddleware) proves start latency.
        logger.info(
            "opening_baseline_snapshot user_id=%s color=%s source=%s snapshot_ms=%.2f",
            user_id,
            player_color,
            source,
            (time.perf_counter() - t0) * 1000.0,
        )


def _baseline_watermark(session: GameSession) -> tuple[int, int, str] | None:
    values = (
        session.baseline_watermark_seq,
        session.baseline_watermark_epoch,
        session.baseline_watermark_fingerprint,
    )
    if any(value is None for value in values):
        return None
    return int(values[0]), int(values[1]), str(values[2])


def _batch_start_mismatch(
    db: Session,
    batch: OpeningScoreBatch,
    session: GameSession,
) -> BaselineWatermarkMismatch | None:
    """Proof 2: does current batch-relevant state still equal session start?"""
    watermark = _baseline_watermark(session)
    if watermark is None:
        return BaselineWatermarkMismatch.EPOCH_CORRUPTION
    watermark_seq, watermark_epoch, watermark_fingerprint = watermark

    live_seq = current_evidence_seq(db, session.user_id, session.player_color)
    if batch.evidence_seq != watermark_seq or live_seq != watermark_seq:
        return BaselineWatermarkMismatch.SEQ
    if batch.registry_fingerprint != watermark_fingerprint:
        return BaselineWatermarkMismatch.REGISTRY

    live_epoch = current_cache_epoch(db)
    if live_epoch is None or live_epoch < watermark_epoch:
        return BaselineWatermarkMismatch.EPOCH_CORRUPTION
    if live_epoch == watermark_epoch:
        return None

    exact_change = db.execute(
        select(literal(1))
        .select_from(OpeningScoreBatchSharedScope)
        .join(
            SharedEvidenceScopeVersion,
            (
                SharedEvidenceScopeVersion.kind
                == OpeningScoreBatchSharedScope.kind
            )
            & (
                SharedEvidenceScopeVersion.fen
                == OpeningScoreBatchSharedScope.fen
            ),
        )
        .where(
            OpeningScoreBatchSharedScope.batch_id == batch.id,
            SharedEvidenceScopeVersion.last_changed_epoch > watermark_epoch,
        )
        .limit(1)
    ).first()
    if exact_change is not None:
        return BaselineWatermarkMismatch.SHARED_SCOPE

    scoped_kinds = (
        select(OpeningScoreBatchSharedScope.kind)
        .where(OpeningScoreBatchSharedScope.batch_id == batch.id)
        .distinct()
    )
    invalidated = db.execute(
        select(literal(1))
        .select_from(SharedEvidenceScopeInvalidation)
        .where(
            SharedEvidenceScopeInvalidation.kind.in_(scoped_kinds),
            SharedEvidenceScopeInvalidation.last_changed_epoch > watermark_epoch,
        )
        .limit(1)
    ).first()
    if invalidated is not None:
        return BaselineWatermarkMismatch.SHARED_INVALIDATION
    return None


def _conditional_store_baseline(
    db: Session,
    session: GameSession,
    baseline_json: str,
) -> bool:
    """Linearization write after both proofs; reruns no proof implicitly."""
    watermark = _baseline_watermark(session)
    if watermark is None:
        return False
    watermark_seq, watermark_epoch, watermark_fingerprint = watermark
    stmt = (
        update(GameSession)
        .where(
            GameSession.id == session.id,
            GameSession.status == "active",
            GameSession.user_id == session.user_id,
            GameSession.player_color == session.player_color,
            GameSession.opening_score_baseline.is_(None),
            GameSession.baseline_watermark_seq == watermark_seq,
            GameSession.baseline_watermark_epoch == watermark_epoch,
            GameSession.baseline_watermark_fingerprint == watermark_fingerprint,
        )
        .values(opening_score_baseline=baseline_json)
        .execution_options(synchronize_session=False)
    )
    stored = db.execute(stmt).rowcount == 1
    if stored:
        # The baseline UPDATE owns the row lock. Derive readiness/publication
        # from current database columns so a candidate committed while the
        # worker was computing cannot be hidden by this stale ORM identity.
        refresh_opening_boundary_publication_state(
            db,
            session,
            force_database_check=True,
        )
    return stored


def _empty_start_mismatch(
    db: Session,
    session: GameSession,
) -> BaselineWatermarkMismatch | None:
    """Historical proof for the no-batch/no-evidence special case."""
    watermark = _baseline_watermark(session)
    if watermark is None:
        return BaselineWatermarkMismatch.EPOCH_CORRUPTION
    watermark_seq, _watermark_epoch, watermark_fingerprint = watermark
    if current_evidence_seq(db, session.user_id, session.player_color) != watermark_seq:
        return BaselineWatermarkMismatch.SEQ
    graph = get_opening_graph()
    routing_snapshot = load_strict_densified_edges(graph)
    current_fingerprint = opening_score_inputs_fingerprint(
        graph, get_opening_roots(), routing_snapshot
    )
    if current_fingerprint != watermark_fingerprint:
        return BaselineWatermarkMismatch.REGISTRY
    return None


def run_baseline_snapshot_job(
    db: Session, session_id, user_id: int, player_color: str
) -> str:
    """Prove and persist one session's start baseline; never raise."""
    t0 = time.perf_counter()
    source = BaselineSnapshotSource.FAILED.value
    mismatch_reason: BaselineWatermarkMismatch | None = None
    try:
        session = db.get(GameSession, session_id)
        if session is None:
            source = BaselineSnapshotSource.MISSING_SESSION.value
            return source
        if session.opening_score_baseline is not None:
            source = BaselineSnapshotSource.ALREADY_SET.value
            return source
        if session.status != "active":
            source = BaselineSnapshotSource.NOT_ACTIVE.value
            return source
        if (user_id, player_color) != (session.user_id, session.player_color):
            source = BaselineSnapshotSource.SESSION_MISMATCH.value
            return source
        if _baseline_watermark(session) is None:
            source = BaselineSnapshotSource.WATERMARK_MISSING.value
            return source

        batch, rows = list_cached_opening_scores(
            db, session.user_id, session.player_color
        )
        if batch is None:
            mismatch_reason = _empty_start_mismatch(db, session)
            if mismatch_reason is not None:
                source = BaselineSnapshotSource.WATERMARK_MISMATCH.value
                return source
            if has_opening_evidence(db, session.user_id, session.player_color):
                source = BaselineSnapshotSource.SKIPPED_COLD.value
                return source
            baseline_json = _serialize_baseline({})
            source = BaselineSnapshotSource.EMPTY_NO_EVIDENCE.value
        else:
            # Proof 1 is mandatory even when the batch stamps equal the watermark:
            # both counters are lower bounds sampled before the evidence read.
            if not _is_batch_fresh(db, batch, rows):
                source = BaselineSnapshotSource.SKIPPED_STALE.value
                return source
            mismatch_reason = _batch_start_mismatch(db, batch, session)
            if mismatch_reason is not None:
                source = BaselineSnapshotSource.WATERMARK_MISMATCH.value
                return source
            if _is_evidence_backed_empty_baseline(
                db, session.user_id, session.player_color, rows
            ):
                source = BaselineSnapshotSource.SKIPPED_QUARANTINED_EMPTY.value
                return source
            baseline_json = _serialize_baseline(
                {row.opening_key: row.opening_score for row in rows},
                registry_fingerprint=batch.registry_fingerprint,
            )
            source = BaselineSnapshotSource.CACHED_FRESH.value

        persisted = _conditional_store_baseline(db, session, baseline_json)
        db.commit()
        if not persisted:
            source = BaselineSnapshotSource.RACED_EVIDENCE_OR_ALREADY_SET.value
        elif session.opening_middle_ply is not None:
            enqueue_opening_boundary_delta(db, session)
        return source
    except Exception:  # noqa: BLE001 - worker job must never crash the scheduler
        source = BaselineSnapshotSource.FAILED.value
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "opening_baseline_job source=failed session_id=%s user_id=%s color=%s",
            session_id,
            user_id,
            player_color,
            exc_info=True,
        )
        return source
    finally:
        logger.info(
            "opening_baseline_job session_id=%s user_id=%s color=%s source=%s "
            "mismatch_reason=%s snapshot_ms=%.2f",
            session_id,
            user_id,
            player_color,
            source,
            mismatch_reason.value if mismatch_reason is not None else None,
            (time.perf_counter() - t0) * 1000.0,
        )


def fill_opening_baselines_for_batch(
    batch_id: int,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> int:
    """Best-effort push-fill of active session baselines for one durable batch."""
    db = session_factory()
    try:
        batch = (
            db.query(OpeningScoreBatch)
            .filter(OpeningScoreBatch.id == batch_id)
            .populate_existing()
            .one_or_none()
        )
        if batch is None:
            return 0
        rows = (
            db.query(UserOpeningScore)
            .filter(
                UserOpeningScore.batch_id == batch.id,
                UserOpeningScore.user_id == batch.user_id,
                UserOpeningScore.player_color == batch.player_color,
            )
            .all()
        )
        # Proof 1 runs once for the exact durable batch. The scoped re-arm it may
        # perform writes only cache_epoch through an independent session and does
        # not change either historical proof.
        if not _is_batch_fresh(db, batch, rows):
            return 0

        if _is_evidence_backed_empty_baseline(
            db, batch.user_id, batch.player_color, rows
        ):
            logger.info(
                "opening baseline push-fill source=%s",
                BaselineSnapshotSource.SKIPPED_QUARANTINED_EMPTY.value,
            )
            return 0

        baseline_json = _serialize_baseline(
            {row.opening_key: row.opening_score for row in rows},
            registry_fingerprint=batch.registry_fingerprint,
        )
        candidates = (
            db.query(GameSession)
            .filter(
                GameSession.user_id == batch.user_id,
                GameSession.player_color == batch.player_color,
                GameSession.status == "active",
                GameSession.opening_score_baseline.is_(None),
                GameSession.baseline_watermark_seq.is_not(None),
                GameSession.baseline_watermark_epoch.is_not(None),
                GameSession.baseline_watermark_fingerprint.is_not(None),
            )
            .all()
        )
        filled = 0
        publishable_session_ids: list[uuid.UUID] = []
        for session in candidates:
            mismatch = _batch_start_mismatch(db, batch, session)
            if mismatch is not None:
                logger.info(
                    "opening baseline push-fill rejected mismatch_reason=%s",
                    mismatch.value,
                )
                continue
            if _conditional_store_baseline(db, session, baseline_json):
                filled += 1
                if session.opening_middle_ply is not None:
                    publishable_session_ids.append(session.id)
        db.commit()
        for session_id in publishable_session_ids:
            session = db.get(GameSession, session_id, populate_existing=True)
            if session is not None:
                enqueue_opening_boundary_delta(db, session)
        return filled
    except Exception:  # noqa: BLE001 - optional scheduler side effect
        db.rollback()
        logger.warning(
            "opening baseline push-fill failed batch_id=%s",
            batch_id,
            exc_info=True,
        )
        return 0
    finally:
        db.close()


def _session_played_fens(db: Session, session_id) -> list[str]:
    """FENs after each move of the session, in move order (white before black)."""
    color_order = case((SessionMove.color == "white", 0), else_=1)
    moves = (
        db.query(SessionMove)
        .filter(SessionMove.session_id == session_id)
        .order_by(SessionMove.move_number.asc(), color_order.asc())
        .all()
    )
    return [move.fen_after for move in moves]


def is_scoped_delta_request_current(request: ScopedDeltaRequest) -> bool:
    """Return whether ``request`` is still the latest reserved generation."""
    with _scoped_delta_lock:
        return (
            _scoped_delta_generations.get(str(request.session_id))
            == request.generation
        )


# Backwards-compatible private spelling retained for focused Phase-2 tests.
_scoped_request_is_current = is_scoped_delta_request_current


def publish_scoped_opening_score_deltas(
    db: Session,
    user_id: int,
    player_color: str,
    requests: tuple[ScopedDeltaRequest, ...],
    *,
    on_complete: Callable[[dict[str, object]], None] | None = None,
) -> int:
    """Build and publish terminal or active-boundary scoped root scores.

    Terminal candidates continue sharing one historical overlay. Each active
    candidate gets a private overlay consisting of that same durable historical
    evidence plus exactly its proven opening prefix; it never enters the durable
    evidence graph or replay cache.

    Returns the number of publications committed to the process-local cache.
    Unexpected failures propagate to the lane's best-effort retry boundary.
    """
    started = time.perf_counter()
    stage_ms: dict[str, float] = {}
    sessions: list[_ScopedDeltaCandidate] = []
    replay_cache_stats = ReplayCacheStats()

    def finish(outcome: str, published: int = 0) -> int:
        # Every exit leaves the lane-owned Session in the same clean state.
        # The success path already rolls back before CPU scoring to release its
        # connection; a second rollback here is intentionally harmless.  Early
        # rejects, however, have performed reads and would otherwise hand an open
        # READ COMMITTED transaction to the following whole-graph recompute.
        db.rollback()
        total_ms = round((time.perf_counter() - started) * 1000.0, 3)
        report: dict[str, object] = {
            "outcome": outcome,
            "request_count": len(requests),
            "candidate_count": len(sessions),
            "published_count": published,
            "stage_ms": dict(stage_ms),
            "total_ms": total_ms,
        }
        report.update(replay_cache_telemetry(replay_cache_stats))
        if on_complete is not None:
            try:
                on_complete(report)
            except Exception:
                logger.exception("scoped opening delta timing callback failed")
        logger.info(
            "scoped_opening_delta outcome=%s request_count=%s candidate_count=%s "
            "published_count=%s session_load_ms=%s counter_ms=%s overlay_ms=%s "
            "digest_ms=%s score_ms=%s publish_ms=%s total_ms=%.3f "
            "replay_cache_builds=%s replay_cache_probed_sessions=%s "
            "replay_cache_l1_hits=%s replay_cache_l2_hits=%s "
            "replay_cache_raw_derivations=%s "
            "replay_cache_persisted_upserts=%s "
            "replay_cache_l2_read_failed=%s "
            "replay_cache_l2_write_failed=%s",
            outcome,
            len(requests),
            len(sessions),
            published,
            stage_ms.get("session_load"),
            stage_ms.get("counter"),
            stage_ms.get("overlay"),
            stage_ms.get("digest"),
            stage_ms.get("score"),
            stage_ms.get("publish"),
            total_ms,
            replay_cache_stats.build_count,
            replay_cache_stats.probed_sessions,
            replay_cache_stats.l1_hits,
            replay_cache_stats.l2_hits,
            replay_cache_stats.raw_derivations,
            replay_cache_stats.persisted_upserts,
            replay_cache_stats.l2_read_failed,
            replay_cache_stats.l2_write_failed,
        )
        return published

    if not requests:
        return finish("no_requests")

    graph = get_opening_graph()
    roots = get_opening_roots()
    routing_snapshot = load_strict_densified_edges(graph)
    stage_started = time.perf_counter()
    for request in sorted(requests, key=lambda item: str(item.session_id)):
        if not is_scoped_delta_request_current(request):
            continue
        session = db.get(GameSession, request.session_id, populate_existing=True)
        if session is None:
            continue
        if (session.user_id, session.player_color) != (user_id, player_color):
            logger.warning("scoped opening delta rejected mismatched session routing")
            continue
        active_snapshot: ActivePrefixSnapshot | None = None
        if request.source == SCOPED_DELTA_SOURCE_OPENING_BOUNDARY:
            active_snapshot = active_prefix_snapshot(db, session)
            if (
                active_snapshot is None
                or request.reconciliation_token
                != active_snapshot.reconciliation_token
            ):
                continue
            played_fens = [row.fen_after for row in active_snapshot.rows]
        elif request.source == SCOPED_DELTA_SOURCE_TERMINAL:
            if not session_is_evidence_eligible(session):
                continue
            played_fens = _session_played_fens(db, session.id)
        else:
            continue
        chain = tuple(played_opening_chain(played_fens, roots))
        if not chain:
            continue
        sessions.append(
            _ScopedDeltaCandidate(
                request=request,
                session_id=str(session.id),
                user_id=session.user_id,
                player_color=session.player_color,
                session_mode=session.session_mode,
                status=session.status,
                drill_state=session.drill_state,
                drill_terminal_reason=session.drill_terminal_reason,
                played_root_keys=tuple(root.opening_key for root in chain),
                source=request.source,
                line_revision=(
                    session.move_line_revision
                    if active_snapshot is not None
                    else None
                ),
                opening_middle_ply=(
                    session.opening_middle_ply
                    if active_snapshot is not None
                    else None
                ),
                prefix_digest=(
                    active_snapshot.prefix_digest
                    if active_snapshot is not None
                    else None
                ),
                reconciliation_token=(
                    active_snapshot.reconciliation_token
                    if active_snapshot is not None
                    else None
                ),
                active_rows=(
                    active_snapshot.rows if active_snapshot is not None else ()
                ),
                session_ts=session.started_at,
            )
        )
    stage_ms["session_load"] = round(
        (time.perf_counter() - stage_started) * 1000.0, 3
    )
    if not sessions:
        return finish("no_candidates")

    # Immutable lower-bound stamp BEFORE every overlay/shared-evidence read.
    stage_started = time.perf_counter()
    registry_fingerprint = opening_score_inputs_fingerprint(
        graph, roots, routing_snapshot
    )
    evidence_seq = current_evidence_seq(db, user_id, player_color)
    cache_epoch = current_cache_epoch(db)
    stage_ms["counter"] = round(
        (time.perf_counter() - stage_started) * 1000.0, 3
    )
    if cache_epoch is None:
        return finish("missing_epoch")

    prepared: list[_PreparedScopedGroup] = []
    terminal_candidates = tuple(
        candidate
        for candidate in sessions
        if candidate.source == SCOPED_DELTA_SOURCE_TERMINAL
    )
    boundary_candidates = tuple(
        candidate
        for candidate in sessions
        if candidate.source == SCOPED_DELTA_SOURCE_OPENING_BOUNDARY
    )

    overlay_started = time.perf_counter()
    overlay_inputs: list[tuple[EvidenceOverlay, tuple[_ScopedDeltaCandidate, ...]]] = []
    if terminal_candidates:
        overlay = overlay_evidence(db, user_id, player_color, graph)
        replay_cache_stats = replay_cache_stats.merged(overlay.replay_cache_stats)
        overlay_inputs.append((overlay, terminal_candidates))
    for candidate in boundary_candidates:
        overlay = overlay_evidence_with_active_prefix(
            db,
            user_id,
            player_color,
            graph,
            list(candidate.active_rows),
            through_ply=candidate.opening_middle_ply or 0,
            session_ts=candidate.session_ts,
        )
        if overlay is None:
            continue
        replay_cache_stats = replay_cache_stats.merged(overlay.replay_cache_stats)
        overlay_inputs.append((overlay, (candidate,)))
    stage_ms["overlay"] = round(
        (time.perf_counter() - overlay_started) * 1000.0, 3
    )
    if not overlay_inputs:
        return finish("no_publishable_overlays")

    digest_started = time.perf_counter()
    for overlay, candidates in overlay_inputs:
        scope = overlay.shared_scope
        shared_snapshot = shared_scope_snapshot(
            db, scope.raw_fens, scope.norm_fens
        )
        # Load-bearing row-identity equality: the move rows whose viewer
        # associations influenced this overlay are exactly those hashed here.
        if shared_snapshot.move_row_ids != scope.move_row_ids:
            continue
        prepared.append(
            _PreparedScopedGroup(
                overlay=overlay,
                candidates=candidates,
                scope=scope,
                shared_snapshot=shared_snapshot,
            )
        )
    stage_ms["digest"] = round(
        (time.perf_counter() - digest_started) * 1000.0, 3
    )
    if not prepared:
        return finish("scope_identity_drift")

    # A changed counter means the reads above may span incompatible snapshots.
    # Discard; never stamp the result with the later values.
    if (
        current_evidence_seq(db, user_id, player_color) != evidence_seq
        or current_cache_epoch(db) != cache_epoch
    ):
        return finish("counter_drift")

    # Release the checked-out connection before CPU scoring.  Everything needed
    # below is immutable process memory, and the stamp remains the pre-read one.
    db.rollback()
    computed_at = datetime.now(timezone.utc)
    stage_started = time.perf_counter()
    scored_groups = []
    for group in prepared:
        requested_keys = [
            opening_key
            for candidate in group.candidates
            for opening_key in candidate.played_root_keys
        ]
        scores = compute_scoped_root_scores(
            player_color,
            graph,
            group.overlay,
            roots,
            requested_keys,
            RootCalcConfig(),
            computed_at,
            routing_snapshot=routing_snapshot,
        )
        scored_groups.append((group, scores))
    stage_ms["score"] = round(
        (time.perf_counter() - stage_started) * 1000.0, 3
    )

    stage_started = time.perf_counter()
    published = 0
    for group, scores in scored_groups:
        for candidate in group.candidates:
            if (
                candidate.source == SCOPED_DELTA_SOURCE_OPENING_BOUNDARY
                and any(key not in scores for key in candidate.played_root_keys)
            ):
                continue
            publication = ScopedDeltaPublication(
                session_id=candidate.session_id,
                generation=candidate.request.generation,
                user_id=candidate.user_id,
                player_color=candidate.player_color,
                session_mode=candidate.session_mode,
                status=candidate.status,
                drill_state=candidate.drill_state,
                drill_terminal_reason=candidate.drill_terminal_reason,
                played_root_keys=candidate.played_root_keys,
                source=candidate.source,
                line_revision=candidate.line_revision,
                opening_middle_ply=candidate.opening_middle_ply,
                prefix_digest=candidate.prefix_digest,
                reconciliation_token=candidate.reconciliation_token,
                scored_roots=tuple(
                    (opening_key, scores[opening_key].opening_score)
                    for opening_key in candidate.played_root_keys
                    if opening_key in scores
                ),
                registry_fingerprint=registry_fingerprint,
                evidence_seq=evidence_seq,
                cache_epoch=cache_epoch,
                shared_raw_fens=group.scope.raw_fens,
                shared_norm_fens=group.scope.norm_fens,
                scoped_shared_digest=group.shared_snapshot.digest,
                computed_at=computed_at,
            )
            published += int(_publish_scoped_delta(publication))
    stage_ms["publish"] = round(
        (time.perf_counter() - stage_started) * 1000.0, 3
    )
    return finish("published" if published else "superseded", published)


def _validated_scoped_score_map(
    db: Session,
    session: GameSession,
    played_root_keys: tuple[str, ...],
    *,
    expected_source: str,
    expected_reconciliation_token: str | None = None,
) -> dict[str, float] | None:
    """Return a scoped score map only while every binding/proof still holds."""
    publication = _current_scoped_delta(session.id)
    if publication is None:
        return None
    if (
        publication.source != expected_source
        or publication.reconciliation_token
        != expected_reconciliation_token
        or publication.user_id != session.user_id
        or publication.player_color != session.player_color
        or publication.session_mode != session.session_mode
        or publication.status != session.status
        or publication.drill_state != session.drill_state
        or publication.drill_terminal_reason != session.drill_terminal_reason
        or publication.played_root_keys != played_root_keys
    ):
        return None
    if publication.source == SCOPED_DELTA_SOURCE_TERMINAL:
        if not session_is_evidence_eligible(session):
            return None
    elif publication.source == SCOPED_DELTA_SOURCE_OPENING_BOUNDARY:
        snapshot = active_prefix_snapshot(db, session)
        if (
            snapshot is None
            or publication.status != "active"
            or publication.line_revision != session.move_line_revision
            or publication.opening_middle_ply != session.opening_middle_ply
            or publication.prefix_digest != snapshot.prefix_digest
            or publication.reconciliation_token
            != snapshot.reconciliation_token
        ):
            return None
    else:
        return None

    graph = get_opening_graph()
    roots = get_opening_roots()
    routing_snapshot = load_strict_densified_edges(graph)
    if publication.registry_fingerprint != opening_score_inputs_fingerprint(
        graph, roots, routing_snapshot
    ):
        return None
    if (
        current_evidence_seq(db, session.user_id, session.player_color)
        != publication.evidence_seq
    ):
        return None

    # Sample the live epoch before a possible scoped read so a successful re-arm
    # is a lower bound on the evidence that read observed.
    epoch = current_cache_epoch(db)
    if epoch is None:
        return None
    if epoch != publication.cache_epoch:
        if (
            shared_scope_digest(
                db,
                publication.shared_raw_fens,
                publication.shared_norm_fens,
            )
            != publication.scoped_shared_digest
        ):
            return None
        publication = _rearm_scoped_delta_epoch(publication, epoch)
        if publication is None:
            return None
    else:
        # Close the request-generation race after all DB checks.
        current = _current_scoped_delta(session.id)
        if current is None or current.generation != publication.generation:
            return None
        publication = current
    return dict(publication.scored_roots)


def _delta_items_from_cache(
    db: Session, session: GameSession
) -> tuple[
    list[OpeningScoreDeltaItem],
    OpeningScoreBatch | None,
    list[UserOpeningScore],
    str,
]:
    """Build the played-chain delta from the latest cached batch — CHEAP.

    Reads the latest batch + rows via ``list_cached_opening_scores`` (indexed
    batch+rows lookup, NO fingerprint) and never touches the scheduler. The
    expensive O(evidence) freshness proof (``_is_batch_fresh`` ->
    ``raw_evidence_inputs_digest``) is deliberately NOT done here (g-xmhv): the
    terminal POST never needs it, and the poll runs it at most once, gated behind a
    cheaper signal (see ``read_opening_score_delta``).

    Returns ``(items, batch, rows, status)`` where ``status`` is one of:

    - ``"no_chain"``: empty played chain -> ``items == []``; nothing will ever
      appear, so the poll stops after one read.
    - ``"skipped_cold"``: no batch yet -> ``items == []``; the poll keeps going
      until a batch builds (no all-``None`` banner is shown meanwhile).
    - ``"warm"``: ``items`` built from the batch rows for ANY freshness with a
      compatible same-model baseline.
    - ``"warm_suppressed"``: same warm after-score behavior, but the baseline is
      absent, legacy, malformed, or from another score configuration/model.
      Before/new/delta claims are suppressed.

    A warm "after" is best-effort: ``opening_score`` is a 0-100 mastery score the
      just-played plies can move either way, so a slightly stale "after" may
      transiently over- or under-state the eventual fresh delta — corrected once
      the poll's freshness read lands.

    ``batch`` / ``rows`` are returned so the poll caller can run ``_is_batch_fresh``
    on them without a second query (both empty for the cold/no-chain cases).
    """
    chain = played_opening_chain(
        _session_played_fens(db, session.id), get_opening_roots()
    )
    if not chain:
        return [], None, [], "no_chain"

    batch, rows = list_cached_opening_scores(
        db, session.user_id, session.player_color
    )
    if batch is None:
        return [], None, [], "skipped_cold"

    items, baseline_compatible = _delta_items_from_score_map(
        session,
        chain,
        {row.opening_key: row.opening_score for row in rows},
    )
    status = "warm" if baseline_compatible else "warm_suppressed"
    return items, batch, rows, status


def _delta_items_from_score_map(
    session: GameSession,
    chain,
    scores_by_key: dict[str, float],
) -> tuple[list[OpeningScoreDeltaItem], bool]:
    """Apply one delta/baseline contract to either batch or scoped scores."""
    baseline = _parse_compatible_baseline(session.opening_score_baseline)
    items: list[OpeningScoreDeltaItem] = []
    for root in chain:
        after = scores_by_key.get(root.opening_key)
        if baseline is None:
            before: float | None = None
            is_new = False
        else:
            before = baseline.get(root.opening_key)
            is_new = before is None
        delta = (
            after - before
            if after is not None and before is not None
            else None
        )
        items.append(
            OpeningScoreDeltaItem(
                opening_key=root.opening_key,
                opening_name=root.opening_name,
                opening_family=root.opening_family,
                eco=root.eco,
                depth=root.depth,
                before=before,
                after=after,
                delta=delta,
                is_new=is_new,
            )
        )
    return items, baseline is not None


def compute_opening_score_delta(
    db: Session, session: GameSession
) -> list[OpeningScoreDeltaItem]:
    """Diff the played-opening chain for an ended session — NON-BLOCKING.

    Serves the latest WARM cached batch immediately (no scheduler wait) and
    enqueues a BACKGROUND recompute so the cache converges; the frontend then polls
    ``read_opening_score_delta`` (GET /api/openings/score-delta/{session_id}) for
    the provably-fresh delta and overwrites the banner in place. Walks the played
    chain (same as ``get_session_openings``) and diffs each crossed opening's cached
    score against the session baseline, broadest -> deepest; empty when no opening
    was crossed. Never raises — on any failure the list is empty and the caller
    simply shows nothing.

    NON-BLOCKING (g-fix-end-latency): the prior implementation forced a synchronous
    ``refresh_now`` (up to 5s) and a cold ``load_cached_rows`` (another 5s) before
    returning, holding the whole terminal action hostage to freshen a supplementary
    banner. The warm "after" served here is best-effort and is corrected by the poll
    (see ``_delta_items_from_cache``).

    CACHE-ONLY (g-xmhv): this path NEVER proves freshness. The O(evidence) digest
    (``raw_evidence_inputs_digest``) that the freshness proof paid was the real ~9s
    cost at game-end — building the warm items from
    ``list_cached_opening_scores`` is cheap; PROVING them fresh is not. So we serve
    the warm items UNVERIFIED, log ``source=cached_unverified``, and let the poll
    reconcile. Fixes the residual 9.95s ``/api/game/end``.
    """
    t0 = time.perf_counter()
    source = "failed"
    try:
        items, _batch, _rows, status = _delta_items_from_cache(db, session)
        # The terminal POST never proves freshness: warm items are UNVERIFIED and
        # reconciled by the poll. ``no_chain`` / ``skipped_cold`` log as-is.
        if status == "warm":
            source = "cached_unverified"
        elif status == "warm_suppressed":
            source = "cached_suppressed"
        else:
            source = status
        # Submit the immediate played-chain publication and the ordinary debounced
        # whole-graph convergence independently. Each facade is swallowing in
        # production, but separate boundaries preserve the contract under an
        # injected/test failure too. Lazy imports avoid scheduler/cache cycles.
        from app.opening_score_delta_lane import enqueue_scoped_delta
        from app.opening_score_scheduler import OpeningScoreTrigger, request_recompute

        try:
            enqueue_scoped_delta(
                session.user_id,
                session.player_color,
                session.id,
            )
        except Exception:
            logger.warning(
                "opening score delta lane submission failed session_id=%s",
                session.id,
                exc_info=True,
            )
        try:
            request_recompute(
                session.user_id,
                session.player_color,
                source=OpeningScoreTrigger.SCORE_DELTA,
            )
        except Exception:
            logger.warning(
                "opening score whole-graph submission failed session_id=%s",
                session.id,
                exc_info=True,
            )
        return items
    except Exception:  # noqa: BLE001 — delta is supplementary; never 500 the end
        source = "failed"
        logger.warning(
            "opening delta computation failed session_id=%s",
            getattr(session, "id", None),
            exc_info=True,
        )
        return []
    finally:
        # Fields go IN THE MESSAGE: the root formatter prints %(message)s only, so
        # extra= kwargs would be dropped. source proves which path served; compute_ms
        # proves the end path no longer blocks (same pattern as snapshot above).
        logger.info(
            "opening_score_delta source=%s compute_ms=%.2f",
            source,
            (time.perf_counter() - t0) * 1000.0,
        )


def read_opening_score_delta(
    db: Session,
    session: GameSession,
    *,
    reconciliation_token: str | None = None,
) -> tuple[list[OpeningScoreDeltaItem], bool]:
    """Non-blocking poll reader for GET /api/openings/score-delta/{session_id}.

    Fresh-source precedence is:

    1. a provably-fresh persisted batch;
    2. a freshness-bound process-local scoped publication;
    3. any warm batch as stale-while-revalidate fallback.

    Scheduler pending/in-flight state is deliberately absent from this decision:
    the evidence proof decides freshness, so a scoped result can become visible
    while the independent whole-graph worker remains blocked. The reader never
    waits; an active scoped miss only re-enqueues the same token best-effort.
    Any failure degrades to ``([], False)``.
    """
    try:
        authoritative = db.get(GameSession, session.id, populate_existing=True)
        if authoritative is None:
            return [], False
        active_snapshot: ActivePrefixSnapshot | None = None
        if reconciliation_token is not None:
            active_snapshot = active_prefix_snapshot(db, authoritative)
            if (
                active_snapshot is None
                or active_snapshot.reconciliation_token
                != reconciliation_token
            ):
                return [], False
            played_fens = [row.fen_after for row in active_snapshot.rows]
        else:
            played_fens = _session_played_fens(db, authoritative.id)
        chain = played_opening_chain(played_fens, get_opening_roots())
        if not chain:
            return [], True
        played_root_keys = tuple(root.opening_key for root in chain)

        if active_snapshot is not None:
            scoped_scores = _validated_scoped_score_map(
                db,
                authoritative,
                played_root_keys,
                expected_source=SCOPED_DELTA_SOURCE_OPENING_BOUNDARY,
                expected_reconciliation_token=reconciliation_token,
            )
            if scoped_scores is None:
                enqueue_opening_boundary_delta(
                    db,
                    authoritative,
                    force_reenqueue=True,
                )
                return [], False
            items, _ = _delta_items_from_score_map(
                authoritative, chain, scoped_scores
            )
            return items, True

        batch, rows = list_cached_opening_scores(
            db, authoritative.user_id, authoritative.player_color
        )
        if batch is not None and _is_batch_fresh(db, batch, rows):
            items, _ = _delta_items_from_score_map(
                authoritative,
                chain,
                {row.opening_key: row.opening_score for row in rows},
            )
            return items, True

        scoped_scores = _validated_scoped_score_map(
            db,
            authoritative,
            played_root_keys,
            expected_source=SCOPED_DELTA_SOURCE_TERMINAL,
        )
        if scoped_scores is not None:
            items, _ = _delta_items_from_score_map(
                authoritative, chain, scoped_scores
            )
            return items, True

        if batch is not None:
            items, _ = _delta_items_from_score_map(
                authoritative,
                chain,
                {row.opening_key: row.opening_score for row in rows},
            )
            return items, False
        return [], False
    except Exception:  # noqa: BLE001 — supplementary poll must never raise
        logger.warning(
            "opening delta poll failed session_id=%s",
            getattr(session, "id", None),
            exc_info=True,
        )
        return [], False
