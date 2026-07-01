"""End-of-session opening-score deltas (g-xanz).

A game or drill that ends recomputes the user's opening scores and reports how
the *played* openings' scores changed, broadest -> deepest. The "after" side is
the freshly-recomputed cached score; the "before" side is the per-session
baseline captured shortly after session start (``GameSession.opening_score_baseline``).

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

Both public helpers are best-effort and never raise: the delta is supplementary
to the end-of-session response (rating change, drill contract), so a failure
degrades to "no delta shown" rather than breaking the endpoint.
"""

from __future__ import annotations

import json
import logging
import time
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
    _is_batch_fresh,
    has_opening_evidence,
    list_cached_opening_scores,
)
from app.opening_roots import get_opening_roots, played_opening_chain

logger = logging.getLogger(__name__)


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


def _capture_baseline_json(
    db: Session,
    user_id: int,
    player_color: str,
    *,
    not_after: datetime | None,
    skip_when_inflight: bool,
) -> tuple[str | None, str]:
    """Capture the current opening scores as a JSON ``{key: score}`` map — PURE.

    Returns ``(json_or_none, source)``. No ``try/except``, no rollback, no logging,
    no timing ``finally`` — the two best-effort wrappers own those, and unexpected
    DB errors propagate to them. ``json_or_none`` is ``"{}"`` (valid empty baseline
    for a user with no evidence, so the session's first openings later read as new),
    a JSON score map, or None (no confident baseline — delta then omitted).

    Guards, in order:

    - No cached batch: ``("{}", "empty_no_evidence")`` when ``has_opening_evidence``
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
            return "{}", "empty_no_evidence"

    if batch is None:
        # No batch yet. Distinguish a brand-new user (no evidence -> valid empty
        # baseline) from a cold cache that still has evidence (cannot prove a
        # baseline -> skip, else session-end falsely marks every opening "new").
        if has_opening_evidence(db, user_id, player_color):
            return None, "skipped_cold"
        return "{}", "empty_no_evidence"

    # Date guard (g-mxeo): reject a batch that may already reflect this session's
    # evidence BEFORE paying the O(evidence) freshness digest. Strict ``>=``: a
    # batch whose computed_at ties started_at cannot be proven pre-session.
    if not_after is not None and _as_utc(batch.computed_at) >= _as_utc(not_after):
        return None, "skipped_post_session_batch"

    if not _is_batch_fresh(db, batch, rows):
        # Cache exists but is provably stale (evidence/registry drift or legacy
        # branch keys). Persisting it would reintroduce the misattribution.
        return None, "skipped_stale"
    return json.dumps({row.opening_key: row.opening_score for row in rows}), "cached_fresh"


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
    - ``"warm"``: ``items`` built from the batch rows for ANY freshness. A warm
      "after" is best-effort: ``opening_score`` is a 0-100 mastery score the
      just-played plies can move either way, so a slightly stale "after" may
      transiently over- or under-state the eventual fresh delta — corrected once
      the poll's freshness read lands.

    ``batch`` / ``rows`` are returned so the poll caller can run ``_is_batch_fresh``
    on them without a second query (both empty for the non-``"warm"`` cases).
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

    rows_by_key = {row.opening_key: row for row in rows}

    baseline: dict[str, float] | None = None
    if session.opening_score_baseline:
        try:
            parsed = json.loads(session.opening_score_baseline)
            if isinstance(parsed, dict):
                baseline = parsed
        except (ValueError, TypeError):
            baseline = None

    items: list[OpeningScoreDeltaItem] = []
    for root in chain:
        row = rows_by_key.get(root.opening_key)
        after = row.opening_score if row is not None else None
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
    return items, batch, rows, "warm"


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
        source = "cached_unverified" if status == "warm" else status
        # Enqueue a BACKGROUND recompute so the cache converges (cold builds its
        # first batch; stale refreshes) for the poll to read. Lazy import mirrors
        # the historical load_cached_rows pattern: opening_score_scheduler imports
        # opening_cache at module load, so a top-level import risks a cycle.
        from app.opening_score_scheduler import request_recompute

        request_recompute(session.user_id, session.player_color)
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

    Returns ``(items, is_fresh)`` from the latest cached batch WITHOUT blocking the
    scheduler — no ``refresh_now`` and (unlike the immediate compute) no
    ``request_recompute``: re-enqueuing on every poll would push the scheduler's
    ``quiet_window`` debounce forward and *delay* convergence. ``is_fresh`` tells the
    frontend when to stop polling (no chain / already-fresh) vs keep going (cold,
    batch still building / recompute pending). Best-effort: any failure degrades to
    ``([], False)``.

    Freshness costs an O(evidence) digest, so it is proven at most ONCE per poll
    (g-xmhv): only a quiescent scheduler reaches ``_is_batch_fresh``. While a
    recompute is pending/in-flight (``is_recompute_scheduled``) the batch is, by
    definition, not yet known-fresh — return ``False`` CHEAPLY and let the next poll
    re-check once the worker settles. This is what kills the 9-17s poll GETs.
    """
    try:
        items, batch, rows, status = _delta_items_from_cache(db, session)
        if status == "no_chain":
            return items, True  # nothing will ever appear -> stop polling
        if status == "skipped_cold":
            return items, False  # batch still building -> keep polling
        # status == "warm": items are served for any freshness; decide the poll-stop
        # signal. Cheap NOT-fresh gate first (lazy import: the scheduler imports
        # opening_cache at module load, so a top-level import risks a cycle).
        from app.opening_score_scheduler import is_recompute_scheduled

        if is_recompute_scheduled(session.user_id, session.player_color):
            return items, False  # pending/in-flight recompute -> not yet fresh
        # Quiescent: the O(evidence) fingerprint is the ONLY thing that may assert
        # is_fresh=True, and it runs at most once here.
        return items, _is_batch_fresh(db, batch, rows)
    except Exception:  # noqa: BLE001 — supplementary poll must never raise
        logger.warning(
            "opening delta poll failed session_id=%s",
            getattr(session, "id", None),
            exc_info=True,
        )
        return [], False
