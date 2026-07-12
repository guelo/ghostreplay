from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.centipawn_loss import centipawn_loss
from app.db import get_db
from app.models import Blunder, BlunderReview, GameSession, Position
from app.opening_cache import bump_evidence_seq
from app.opening_score_scheduler import request_recompute
from app.posthog_client import capture
from app.row_locks import for_no_key_update
from app.security import TokenPayload, get_current_user
from app.srs_math import as_utc, calculate_priority, expected_interval_hours

router = APIRouter(prefix="/api/srs", tags=["srs"])
logger = logging.getLogger(__name__)


class SrsReviewRequest(BaseModel):
    session_id: uuid.UUID = Field(..., description="Game session ID")
    blunder_id: int = Field(..., ge=1, description="Blunder target ID")
    passed: bool = Field(..., description="Whether the user passed the review")
    user_move: str = Field(..., min_length=1, max_length=10, description="Move the user played")
    eval_delta: int = Field(
        ...,
        description="Centipawn loss from best move; the server normalizes it to 0..1000 for storage and analytics",
    )
    idempotency_key: str | None = Field(
        None,
        max_length=64,
        description="Optional dedup key; retries with the same key return the first review.",
    )


class SrsReviewResponse(BaseModel):
    blunder_id: int
    pass_streak: int
    priority: float
    next_expected_review: datetime


def _get_session_or_404(db: Session, session_id: uuid.UUID) -> GameSession:
    game_session = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not game_session:
        raise HTTPException(status_code=404, detail="Game session not found")
    return game_session


def _ensure_session_owned_by_user(game_session: GameSession, user: TokenPayload) -> None:
    if game_session.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this game")


def _get_blunder_player_color(db: Session, blunder: Blunder) -> str | None:
    if blunder.source_session_id is not None:
        source_color = (
            db.query(GameSession.player_color)
            .filter(GameSession.id == blunder.source_session_id)
            .scalar()
        )
        if source_color is not None:
            return source_color
    return db.query(Position.active_color).filter(Position.id == blunder.position_id).scalar()


def _srs_response_for(blunder: Blunder, *, reviewed_at: datetime) -> SrsReviewResponse:
    interval_hours = expected_interval_hours(blunder.pass_streak)
    return SrsReviewResponse(
        blunder_id=blunder.id,
        pass_streak=blunder.pass_streak,
        priority=calculate_priority(
            pass_streak=blunder.pass_streak,
            last_reviewed_at=blunder.last_reviewed_at,
            created_at=blunder.created_at,
            now=reviewed_at,
        ),
        next_expected_review=reviewed_at + timedelta(hours=interval_hours),
    )


def _srs_response_from_review(blunder: Blunder, review: BlunderReview) -> SrsReviewResponse:
    """Reconstruct the ORIGINAL response for an idempotent retry.

    At review time ``last_reviewed_at == reviewed_at == now``, so the stored
    ``pass_streak_after`` and ``reviewed_at`` fully determine the original
    priority and next_expected_review — independent of any later reviews that
    have since mutated the blunder. ``reviewed_at`` is normalized to UTC so the
    echoed response is byte-identical across SQLite (naive) and Postgres (aware).
    ``pass_streak_after`` falls back to the live streak for pre-migration rows
    (which can never be matched by key anyway, as their key is NULL).
    """
    reviewed_at = as_utc(review.reviewed_at)
    pass_streak = (
        review.pass_streak_after
        if review.pass_streak_after is not None
        else blunder.pass_streak
    )
    interval_hours = expected_interval_hours(pass_streak)
    return SrsReviewResponse(
        blunder_id=blunder.id,
        pass_streak=pass_streak,
        priority=calculate_priority(
            pass_streak=pass_streak,
            last_reviewed_at=reviewed_at,
            created_at=blunder.created_at,
            now=reviewed_at,
        ),
        next_expected_review=reviewed_at + timedelta(hours=interval_hours),
    )


def _find_existing_review(db: Session, *, blunder_id: int, idempotency_key: str) -> BlunderReview | None:
    return (
        db.query(BlunderReview)
        .filter(
            BlunderReview.blunder_id == blunder_id,
            BlunderReview.idempotency_key == idempotency_key,
        )
        .first()
    )


@router.post("/review", response_model=SrsReviewResponse, status_code=200)
def review_blunder(
    request: SrsReviewRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> SrsReviewResponse:
    game_session = _get_session_or_404(db, request.session_id)
    _ensure_session_owned_by_user(game_session, user)

    # Lock the blunder row BEFORE the duplicate lookup and mutation so two
    # concurrent reviews for the same blunder serialize (no-op on SQLite).
    blunder = for_no_key_update(
        db.query(Blunder)
        .filter(Blunder.id == request.blunder_id, Blunder.user_id == user.user_id)
    ).first()
    if not blunder:
        raise HTTPException(status_code=404, detail="Blunder not found")

    if request.idempotency_key is not None:
        existing = _find_existing_review(
            db, blunder_id=request.blunder_id, idempotency_key=request.idempotency_key
        )
        if existing is not None:
            # Already applied — echo the original outcome without mutating again.
            return _srs_response_from_review(blunder, existing)

    reviewed_at = datetime.now(timezone.utc)
    blunder.pass_streak = blunder.pass_streak + 1 if request.passed else 0
    blunder.last_reviewed_at = reviewed_at

    # Server-authoritative normalization (g-no51): eval_delta_cp is WRITE-ONLY, so
    # the stored value must be the trust boundary — an old/non-browser client that
    # bypasses the frontend evalLoss cap cannot persist a mate-magnitude or negative
    # value. Emitted normalized to analytics too (no raw at-rest counterpart exists).
    normalized_eval_delta = centipawn_loss(request.eval_delta)

    db.add(
        BlunderReview(
            blunder_id=blunder.id,
            session_id=request.session_id,
            reviewed_at=reviewed_at,
            passed=request.passed,
            move_played_san=request.user_move,
            eval_delta_cp=normalized_eval_delta,
            idempotency_key=request.idempotency_key,
            pass_streak_after=blunder.pass_streak,
        )
    )
    # Opening-evidence counter (g-jact): a NEW review row is digest-visible, so
    # bump in the SAME txn as the insert (a rolled-back duplicate rolls back its
    # bump too). Color follows the digest's review scoping via
    # _get_blunder_player_color; None means neither color's digest consumes this
    # review (no source session AND no matching position color) — skip is
    # correct, not a gap. The in-place pass_streak/last_reviewed_at writes above
    # need NO bump: the digest reads only the review rows and fen_raw, never the
    # blunder's streak columns.
    try:
        db.flush()
        player_color = _get_blunder_player_color(db, blunder)
        if player_color is not None:
            bump_evidence_seq(db, blunder.user_id, player_color)
        db.commit()
    except IntegrityError:
        db.rollback()
        # Idempotent recovery ONLY when a key was supplied. A keyless review
        # cannot collide on the partial unique index (WHERE idempotency_key IS
        # NOT NULL), so an IntegrityError here is an UNRELATED constraint
        # failure and must not be mistaken for a duplicate.
        if request.idempotency_key is None:
            raise
        existing = _find_existing_review(
            db, blunder_id=request.blunder_id, idempotency_key=request.idempotency_key
        )
        if existing is None:
            raise  # genuinely unrelated failure — re-raise rather than swallow
        db.refresh(blunder)
        return _srs_response_from_review(blunder, existing)

    # player_color resolved above (pre-commit, alongside the evidence bump).
    recompute_queued = player_color is not None
    if recompute_queued:
        request_recompute(user.user_id, player_color)

    capture(
        str(user.user_id),
        "srs_review_recorded",
        {
            "passed": request.passed,
            "pass_streak": blunder.pass_streak,
            "eval_delta": normalized_eval_delta,
            "recompute_queued": recompute_queued,
        },
    )

    return _srs_response_for(blunder, reviewed_at=reviewed_at)
