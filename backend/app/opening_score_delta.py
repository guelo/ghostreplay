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

from app.models import GameSession, SessionMove
from app.opening_cache import (
    has_opening_evidence,
    load_cached_rows,
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


def compute_opening_score_delta(
    db: Session, session: GameSession
) -> list[OpeningScoreDeltaItem]:
    """Recompute and diff the played-opening chain for an ended session.

    Forces a bounded synchronous recompute so the after-scores reflect this
    session, walks the played chain (same as ``get_session_openings``), and diffs
    each crossed opening's fresh score against the session baseline. Returns the
    chain broadest -> deepest; empty when no opening was crossed. Never raises —
    on any failure the list is empty and the caller simply shows nothing.
    """
    try:
        # Lazy import mirrors load_cached_rows: opening_score_scheduler imports
        # opening_cache at module load, so a top-level import risks a cycle.
        from app.opening_score_scheduler import refresh_now

        player_color = session.player_color

        # Best-effort: block briefly so the after-scores include this session's
        # evidence. On timeout/False the cached after-scores are served as-is
        # (delta may lag by one recompute) — acceptable; don't block the banner.
        try:
            refresh_now(session.user_id, player_color)
        except Exception:  # noqa: BLE001 — recompute is advisory here
            logger.warning(
                "opening delta refresh_now failed session_id=%s", session.id,
                exc_info=True,
            )

        chain = played_opening_chain(
            _session_played_fens(db, session.id), get_opening_roots()
        )
        if not chain:
            return []

        _, cached_rows = load_cached_rows(db, session.user_id, player_color)
        rows_by_key = {row.opening_key: row for row in cached_rows}

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
        return items
    except Exception:  # noqa: BLE001 — delta is supplementary; never 500 the end
        logger.warning(
            "opening delta computation failed session_id=%s",
            getattr(session, "id", None),
            exc_info=True,
        )
        return []
