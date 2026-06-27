"""End-of-session opening-score deltas (g-xanz).

A game or drill that ends recomputes the user's opening scores and reports how
the *played* openings' scores changed, broadest -> deepest. The "after" side is
the freshly-recomputed cached score; the "before" side is the per-session
baseline snapshotted at session start (``GameSession.opening_score_baseline``).

Why a baseline is required: opening scores are cumulative over all evidence and
live play feeds ``request_recompute`` incrementally as moves upload, so by the
time a session ends the cached score already reflects most of that session. There
is no "pre-session" score left to diff against unless it was captured up front.

Both helpers are best-effort and never raise: the delta is supplementary to the
end-of-session response (rating change, drill contract), so a failure degrades to
"no delta shown" rather than breaking the endpoint.
"""

from __future__ import annotations

import json
import logging
import time

from pydantic import BaseModel
from sqlalchemy import case
from sqlalchemy.orm import Session

from app.models import GameSession, OpeningScoreBatch, SessionMove, UserOpeningScore
from app.opening_cache import (
    _is_batch_fresh,
    has_opening_evidence,
    list_cached_opening_scores,
    proven_fresh_opening_scores,
)
from app.opening_roots import get_opening_roots, played_opening_chain

logger = logging.getLogger(__name__)


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


def snapshot_opening_baseline(
    db: Session, user_id: int, player_color: str
) -> str | None:
    """Capture the user's current opening scores as a JSON ``{key: score}`` map.

    Returns the JSON string to persist into ``GameSession.opening_score_baseline``
    (``"{}"`` when the user has no opening evidence yet — a valid empty baseline,
    so the session's first openings later read as new), or None when no confident
    baseline can be captured (delta then omitted at session end). Best-effort: any
    failure leaves the baseline NULL rather than blocking session creation.

    NON-BLOCKING (g-fix-start-latency): this runs on the game/drill start hot path
    and must NEVER wait on the opening-score scheduler. It reads the latest cached
    batch directly via ``proven_fresh_opening_scores`` (no ``refresh_now``, no
    enqueue, no scheduler probe) and only returns a confident baseline when that
    batch is PROVABLY current — registry + raw-input fingerprints match and no
    stale branch keys. A stale or cold-with-evidence cache yields NULL instead:
    diffing a stale "before" against the end-of-session fresh "after" would
    over-attribute prior sessions' gains to the just-ended one, so degrading to
    "no delta" is preferred over a misattributed one. Only a user with genuinely
    no evidence gets the empty ``"{}"`` baseline.
    """
    t0 = time.perf_counter()
    source = "failed"
    try:
        batch, rows, is_fresh = proven_fresh_opening_scores(db, user_id, player_color)
        if batch is None:
            # No batch yet. Distinguish a brand-new user (no evidence -> valid
            # empty baseline) from a cold cache that still has evidence (cannot
            # prove a baseline -> skip, else session-end falsely marks every
            # existing opening "new").
            if has_opening_evidence(db, user_id, player_color):
                source = "skipped_cold"
                return None
            source = "empty_no_evidence"
            return "{}"
        if not is_fresh:
            # Cache exists but is provably stale (evidence/registry drift or legacy
            # branch keys). Persisting it would reintroduce the misattribution.
            source = "skipped_stale"
            return None
        source = "cached_fresh"
        return json.dumps({row.opening_key: row.opening_score for row in rows})
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
    (``raw_evidence_inputs_digest``) that ``proven_fresh_opening_scores`` paid was
    the real ~9s cost at game-end — building the warm items from
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
