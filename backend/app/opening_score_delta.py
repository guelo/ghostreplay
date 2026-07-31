"""End-of-session opening-score deltas (g-xanz).

A game or drill that ends reports how the *played* openings' scores changed,
broadest -> deepest. The "before" side is the per-session baseline captured
shortly after session start (``GameSession.opening_score_baseline``). The "after"
side prefers a provably-fresh persisted batch, then a freshness-bound
process-local scoped publication computed before the full batch writer, and
finally a warm batch as an explicitly unverified fallback.

Why a baseline is required: opening scores are cumulative over all evidence and
live play feeds ``request_recompute`` incrementally as moves upload, so by the
time a session ends the cached score already reflects most of that session. There
is no "pre-session" score left to diff against unless it was captured up front.

ASYNC CAPTURE (g-mxeo): proving the cached batch fresh costs an O(all-evidence)
digest that ballooned start latency, so capture no longer runs inline on the
``/start`` request. The start handler enqueues a job on ``OpeningBaselineScheduler``
and returns immediately; the worker calls ``run_baseline_snapshot_job`` shortly
after. Because that races this session's own evidence, the worker persists a
baseline ONLY when the pre-session cached batch is provably fresh AND dated
STRICTLY BEFORE ``session.started_at`` (``computed_at`` is an evidence-read upper
bound — see ``opening_cache._utcnow``). Otherwise the baseline stays NULL and the
end-of-session delta degrades to "no delta". A post-session baseline is never
written.

The terminal and poll helpers are best-effort and never raise: the delta is
supplementary to the end-of-session response (rating change, drill contract), so
a failure degrades to "no delta shown" rather than breaking the endpoint.
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from pydantic import BaseModel
from sqlalchemy import case, select, update
from sqlalchemy.orm import Session

from app.models import (
    Blunder,
    BlunderReview,
    GameSession,
    OpeningScoreBatch,
    SessionMove,
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
    overlay_evidence,
    session_is_evidence_eligible,
    shared_scope_digest,
    shared_scope_snapshot,
)
from app.opening_graph import get_opening_graph
from app.opening_rootcalc import (
    RootCalcConfig,
    compute_scoped_root_scores,
    root_calc_config_fingerprint,
)
from app.opening_roots import get_opening_roots, played_opening_chain

logger = logging.getLogger(__name__)

OPENING_BASELINE_SCHEMA_VERSION = 1


def _serialize_baseline(scores: dict[str, float]) -> str:
    """Serialize a baseline with the score model/config compatibility boundary."""
    return json.dumps(
        {
            "schema_version": OPENING_BASELINE_SCHEMA_VERSION,
            "model_version": SCORE_MODEL_VERSION,
            "root_calc_config_fingerprint": root_calc_config_fingerprint(),
            "scores": scores,
        }
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


def _as_utc(value: datetime) -> datetime:
    """Coerce a datetime to timezone-aware UTC, treating naive as UTC.

    SQLite test paths and older rows can return naive datetimes; normalizing both
    sides before an ordering comparison avoids the aware/naive ``TypeError``.
    Mirrors the guard around ``opening_cache.recompute_opening_scores_if_needed``.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


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


@dataclass(frozen=True, slots=True)
class ScopedDeltaRequest:
    session_id: uuid.UUID
    generation: int


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


@dataclass(frozen=True, slots=True)
class ScopedDeltaPublication:
    """Process-local, freshness-bound score result for one terminal session."""

    session_id: str
    generation: int
    user_id: int
    player_color: str
    session_mode: str
    status: str
    drill_state: str | None
    drill_terminal_reason: str | None
    played_root_keys: tuple[str, ...]
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
_scoped_delta_generation_counter = itertools.count(1)


def reserve_scoped_delta_generation(session_id) -> ScopedDeltaRequest:
    """Assign the next monotonic generation before terminal work is enqueued."""
    parsed = (
        session_id
        if isinstance(session_id, uuid.UUID)
        else uuid.UUID(str(session_id))
    )
    key = str(parsed)
    generation = next(_scoped_delta_generation_counter)
    with _scoped_delta_lock:
        _scoped_delta_generations[key] = generation
        _scoped_delta_generations.move_to_end(key)
        while len(_scoped_delta_generations) > SCOPED_DELTA_GENERATION_CAPACITY:
            evicted_key, _ = _scoped_delta_generations.popitem(last=False)
            _scoped_delta_publications.pop(evicted_key, None)
    return ScopedDeltaRequest(session_id=parsed, generation=generation)


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


def _capture_baseline_json(
    db: Session,
    user_id: int,
    player_color: str,
    *,
    not_after: datetime | None,
    skip_when_inflight: bool,
) -> tuple[str | None, str]:
    """Capture current scores in the versioned baseline envelope — PURE.

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
    - ``not_after`` date guard (async worker, g-mxeo): if the batch's normalized
      ``computed_at >= not_after``, the batch may already reflect this session's
      evidence (``computed_at`` is an evidence-read upper bound), so return
      ``(None, "skipped_post_session_batch")`` BEFORE paying the O(evidence) digest.
      The predicate is strict: a batch tying ``started_at`` at clock resolution
      cannot be proven pre-session and is rejected.
    - Freshness: a provably-stale batch returns ``(None, "skipped_stale")``; a fresh
      batch returns ``(json.dumps({key: score}), "cached_fresh")``.
    """
    # Cheap indexed batch+rows read first (no fingerprint/digest).
    batch, rows = list_cached_opening_scores(db, user_id, player_color)

    if skip_when_inflight:
        # IN-FLIGHT-ONLY gate (lazy import: the scheduler imports opening_cache at
        # module load, so a top-level import risks a cycle). A RUNNING recompute is
        # the only thing that makes the O(evidence) digest serialize against the
        # worker; a pending/debounced entry is idle and NOT gated here.
        from app.opening_score_scheduler import is_recompute_inflight

        if is_recompute_inflight(user_id, player_color):
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

    # Date guard (g-mxeo): reject a batch that may already reflect this session's
    # evidence BEFORE paying the O(evidence) freshness digest. Strict ``>=``: a
    # batch whose computed_at ties started_at cannot be proven pre-session.
    if not_after is not None and _as_utc(batch.computed_at) >= _as_utc(not_after):
        return None, "skipped_post_session_batch"

    if not _is_batch_fresh(db, batch, rows):
        # Cache exists but is provably stale (evidence/registry drift or legacy
        # branch keys). Persisting it would reintroduce the misattribution.
        return None, "skipped_stale"
    return _serialize_baseline(
        {row.opening_key: row.opening_score for row in rows}
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
    line. It passes ``not_after=None`` (no date guard): a synchronous capture runs
    BEFORE the session INSERT, so it races nothing.
    """
    t0 = time.perf_counter()
    source = "failed"
    try:
        result, source = _capture_baseline_json(
            db, user_id, player_color, not_after=None, skip_when_inflight=True
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


def run_baseline_snapshot_job(
    db: Session, session_id, user_id: int, player_color: str
) -> str:
    """Async opening-baseline capture job for ``OpeningBaselineScheduler`` — NEVER
    raises. Returns a ``source`` string (also logged) describing the outcome.

    The queued ``user_id``/``player_color`` are UNTRUSTED routing hints, not
    authoritative capture inputs. A stale or mis-routed duplicate enqueue carrying
    the wrong pair would otherwise capture ANOTHER user's/color's cached scores and
    persist them onto this session. So capture always keys off the SESSION ROW's own
    ``user_id``/``player_color``; the queued copies are used only for logging and one
    cheap sanity check.

    Flow:

    1. Session missing -> ``"missing_session"``.
    2. Baseline already set -> ``"already_set"`` (idempotent).
    3. Session not active -> ``"not_active"`` (cheap early-exit; the UPDATE re-checks
       it atomically).
    4. Queued hints disagree with the row -> ``"session_mismatch"`` (surface the
       upstream bug; correctness does not depend on it — capture uses the row).
    5. Capture from the ROW via ``_capture_baseline_json`` with the strict date guard
       (``not_after=session.started_at``) and ``skip_when_inflight=False``.
    6. None -> leave the baseline NULL, return the helper's source.
    7. Persist via a single conditional UPDATE that re-checks ``status="active"``,
       the captured identity, NULL baseline, and the absence of any session-scoped
       evidence — atomic with the write. ``rowcount == 1`` returns the helper source;
       otherwise ``"raced_evidence_or_already_set"``.
    8. Unexpected errors -> guarded rollback, ``"failed"``, never crash the worker.
    """
    t0 = time.perf_counter()
    source = "failed"
    try:
        session = db.get(GameSession, session_id)
        if session is None:
            source = "missing_session"
            return source
        if session.opening_score_baseline is not None:
            source = "already_set"
            return source
        if session.status != "active":
            source = "not_active"
            return source
        if (user_id, player_color) != (session.user_id, session.player_color):
            # Mis-routed / stale enqueue. Capture would use the row either way, so
            # correctness does not depend on this check; it surfaces the upstream
            # bug rather than silently doing the right thing.
            source = "session_mismatch"
            return source

        json_str, source = _capture_baseline_json(
            db,
            session.user_id,
            session.player_color,
            not_after=session.started_at,
            skip_when_inflight=False,
        )
        if json_str is None:
            return source

        # Defense-in-depth persist (typed SQLAlchemy Core so the UUID id binds on
        # both the SQLite test schema — id TEXT — and Postgres — id UUID). A single
        # conditional UPDATE re-checks status/identity, NULL-baseline idempotency,
        # and the absence of any session-scoped evidence, ATOMIC with the write. The
        # session_moves.session_id index covers its check; the review/blunder checks
        # may table-scan but run once per session start on the background worker.
        #
        # AIRTIGHTNESS: these NOT EXISTS clauses — not the date guard — are the real
        # correctness guarantee. They directly assert the invariant that matters
        # ("has this session fed any evidence yet?") and are clock-INDEPENDENT; the
        # date guard is a cheap clock-DEPENDENT early-out that can be fooled by
        # DB/app clock skew. So the clauses must enumerate EVERY session-scoped
        # source that feeds ``raw_evidence_inputs_digest``: session_moves (SM|),
        # ghost-target blunders via source_session_id (GT|), and blunder_reviews
        # (BR|). (The digest's analysis_cache/position_analysis grains are global,
        # not session-attributable, so they are intentionally absent here.)
        # MAINTENANCE: any NEW session-scoped evidence source added to
        # ``raw_evidence_inputs_digest`` MUST also get a NOT EXISTS clause here —
        # otherwise a session contributing only that source would slip past this
        # airtight check and be protected by the clock-dependent date guard alone.
        # (Since SESSION_EVIDENCE_ELIGIBLE_SQL excludes in-progress sessions from
        # the digest, a brand-new active session can no longer feed evidence at
        # all — these clauses are belt-and-suspenders on top of that narrowing.)
        stmt = (
            update(GameSession)
            .where(GameSession.id == session_id)
            .where(GameSession.status == "active")
            .where(GameSession.user_id == session.user_id)
            .where(GameSession.player_color == session.player_color)
            .where(GameSession.opening_score_baseline.is_(None))
            .where(
                ~select(SessionMove.id)
                .where(SessionMove.session_id == GameSession.id)
                .exists()
            )
            .where(
                ~select(BlunderReview.id)
                .where(BlunderReview.session_id == GameSession.id)
                .exists()
            )
            .where(
                ~select(Blunder.id)
                .where(Blunder.source_session_id == GameSession.id)
                .exists()
            )
            .values(opening_score_baseline=json_str)
        )
        persisted = db.execute(stmt).rowcount == 1
        db.commit()
        if not persisted:
            source = "raced_evidence_or_already_set"
        return source
    except Exception:  # noqa: BLE001 — worker job must never crash the scheduler
        source = "failed"
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
        # Fields go IN THE MESSAGE (root formatter prints %(message)s only).
        logger.info(
            "opening_baseline_job session_id=%s user_id=%s color=%s source=%s snapshot_ms=%.2f",
            session_id,
            user_id,
            player_color,
            source,
            (time.perf_counter() - t0) * 1000.0,
        )


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


def _scoped_request_is_current(request: ScopedDeltaRequest) -> bool:
    with _scoped_delta_lock:
        return (
            _scoped_delta_generations.get(str(request.session_id))
            == request.generation
        )


def publish_scoped_opening_score_deltas(
    db: Session,
    user_id: int,
    player_color: str,
    requests: tuple[ScopedDeltaRequest, ...],
) -> int:
    """Build and publish coalesced terminal-session root scores without a batch.

    Runs on the existing serialized opening-score scheduler thread immediately
    before its whole-graph recompute.  The queued ``user_id``/``player_color`` are
    routing hints only: every session row is reloaded and must agree.  All valid
    sessions share one overlay and one union-of-roots calculation, while each
    publication retains its own authoritative state, played order, and request
    generation.

    Returns the number of publications committed to the process-local cache.
    Unexpected failures propagate to the scheduler's best-effort boundary, which
    rolls this DB session back and still runs the ordinary whole recompute.
    """
    started = time.perf_counter()
    stage_ms: dict[str, float] = {}
    sessions: list[_ScopedDeltaCandidate] = []

    def finish(outcome: str, published: int = 0) -> int:
        # Every exit leaves the scheduler-owned Session in the same clean state.
        # The success path already rolls back before CPU scoring to release its
        # connection; a second rollback here is intentionally harmless.  Early
        # rejects, however, have performed reads and would otherwise hand an open
        # READ COMMITTED transaction to the following whole-graph recompute.
        db.rollback()
        logger.info(
            "scoped_opening_delta outcome=%s request_count=%s candidate_count=%s "
            "published_count=%s session_load_ms=%s counter_ms=%s overlay_ms=%s "
            "digest_ms=%s score_ms=%s publish_ms=%s total_ms=%.3f",
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
            (time.perf_counter() - started) * 1000.0,
        )
        return published

    if not requests:
        return finish("no_requests")

    graph = get_opening_graph()
    roots = get_opening_roots()
    stage_started = time.perf_counter()
    for request in sorted(requests, key=lambda item: str(item.session_id)):
        if not _scoped_request_is_current(request):
            continue
        session = db.get(GameSession, request.session_id, populate_existing=True)
        if session is None:
            continue
        if (session.user_id, session.player_color) != (user_id, player_color):
            logger.warning("scoped opening delta rejected mismatched session routing")
            continue
        if not session_is_evidence_eligible(session):
            continue
        chain = tuple(played_opening_chain(_session_played_fens(db, session.id), roots))
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
            )
        )
    stage_ms["session_load"] = round(
        (time.perf_counter() - stage_started) * 1000.0, 3
    )
    if not sessions:
        return finish("no_candidates")

    # Immutable lower-bound stamp BEFORE every overlay/shared-evidence read.
    stage_started = time.perf_counter()
    registry_fingerprint = opening_score_inputs_fingerprint(graph, roots)
    evidence_seq = current_evidence_seq(db, user_id, player_color)
    cache_epoch = current_cache_epoch(db)
    stage_ms["counter"] = round(
        (time.perf_counter() - stage_started) * 1000.0, 3
    )
    if cache_epoch is None:
        return finish("missing_epoch")

    stage_started = time.perf_counter()
    overlay = overlay_evidence(db, user_id, player_color, graph)
    stage_ms["overlay"] = round(
        (time.perf_counter() - stage_started) * 1000.0, 3
    )
    scope = overlay.shared_scope
    stage_started = time.perf_counter()
    shared_snapshot = shared_scope_snapshot(
        db, scope.raw_fens, scope.norm_fens
    )
    stage_ms["digest"] = round(
        (time.perf_counter() - stage_started) * 1000.0, 3
    )
    # Load-bearing row-identity equality: the move rows whose viewer associations
    # influenced the overlay must be exactly those hashed into its shared proof.
    if shared_snapshot.move_row_ids != scope.move_row_ids:
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
    requested_keys = [
        opening_key
        for candidate in sessions
        for opening_key in candidate.played_root_keys
    ]
    stage_started = time.perf_counter()
    scores = compute_scoped_root_scores(
        player_color,
        graph,
        overlay,
        roots,
        requested_keys,
        RootCalcConfig(),
        computed_at,
    )
    stage_ms["score"] = round(
        (time.perf_counter() - stage_started) * 1000.0, 3
    )

    stage_started = time.perf_counter()
    published = 0
    for candidate in sessions:
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
            scored_roots=tuple(
                (opening_key, scores[opening_key].opening_score)
                for opening_key in candidate.played_root_keys
                if opening_key in scores
            ),
            registry_fingerprint=registry_fingerprint,
            evidence_seq=evidence_seq,
            cache_epoch=cache_epoch,
            shared_raw_fens=scope.raw_fens,
            shared_norm_fens=scope.norm_fens,
            scoped_shared_digest=shared_snapshot.digest,
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
) -> dict[str, float] | None:
    """Return a scoped score map only while every binding/proof still holds."""
    publication = _current_scoped_delta(session.id)
    if publication is None:
        return None
    if (
        publication.user_id != session.user_id
        or publication.player_color != session.player_color
        or publication.session_mode != session.session_mode
        or publication.status != session.status
        or publication.drill_state != session.drill_state
        or publication.drill_terminal_reason != session.drill_terminal_reason
        or publication.played_root_keys != played_root_keys
        or not session_is_evidence_eligible(session)
    ):
        return None

    graph = get_opening_graph()
    roots = get_opening_roots()
    if publication.registry_fingerprint != opening_score_inputs_fingerprint(
        graph, roots
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
        # Enqueue a BACKGROUND recompute so the cache converges (cold builds its
        # first batch; stale refreshes) for the poll to read. Lazy import mirrors
        # the historical load_cached_rows pattern: opening_score_scheduler imports
        # opening_cache at module load, so a top-level import risks a cycle.
        from app.opening_score_scheduler import OpeningScoreTrigger, request_recompute

        request_recompute(
            session.user_id,
            session.player_color,
            source=OpeningScoreTrigger.SCORE_DELTA,
            terminal_session_id=session.id,
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
    db: Session, session: GameSession
) -> tuple[list[OpeningScoreDeltaItem], bool]:
    """Non-blocking poll reader for GET /api/openings/score-delta/{session_id}.

    Fresh-source precedence is:

    1. a provably-fresh persisted batch;
    2. a freshness-bound process-local scoped publication;
    3. any warm batch as stale-while-revalidate fallback.

    Scheduler pending/in-flight state is deliberately absent from this decision:
    the evidence proof decides freshness, so a scoped result can become visible
    while the same scheduler thread is blocked in the later whole-batch commit.
    The reader never enqueues or waits. Best-effort: any failure degrades to
    ``([], False)``.
    """
    try:
        authoritative = db.get(GameSession, session.id, populate_existing=True)
        if authoritative is None:
            return [], False
        chain = played_opening_chain(
            _session_played_fens(db, authoritative.id),
            get_opening_roots(),
        )
        if not chain:
            return [], True
        played_root_keys = tuple(root.opening_key for root in chain)

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
            db, authoritative, played_root_keys
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
