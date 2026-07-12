from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.accuracy import (
    AccuracyMove,
    compute_game_accuracy,
    expected_total_moves_from_pgn,
)
from app.centipawn_loss import centipawn_loss, centipawn_loss_expr
from app.db import get_db
from app.models import (
    Blunder,
    BlunderReview,
    GameSession,
    RatingHistory,
    SessionMove,
    UserOpeningScore,
)
from app.opening_cache import list_cached_opening_scores
from app.rating import DEFAULT_RATING
from app.rating_scores import latest_rating_order, scores_for_row
from app.security import TokenPayload, get_current_user
from app.session_contracts import normal_play_started_at_expr, visible_session_filter

router = APIRouter(prefix="/api/stats", tags=["stats"])

# Consecutive-pass streak at which a blunder is considered "mastered". A
# BlunderReview whose pass_streak_after equals this value is the N-1 -> N
# crossing that counts as one training conversion.
MASTERY_THRESHOLD = 3


class GamesSummary(BaseModel):
    played: int
    score_pct: float | None
    wins: int
    losses: int
    draws: int
    avg_moves: float


class MoveQualityDistribution(BaseModel):
    inaccuracy: float
    mistake: float
    blunder: float


class MoveSummary(BaseModel):
    accuracy_pct: float | None
    mistake_free_game_rate: float | None
    # None (not an all-zeros dict) when there are zero classified player moves.
    quality_distribution: MoveQualityDistribution | None


class ColorSummary(BaseModel):
    games: int
    score_pct: float | None
    accuracy_pct: float | None


class ColorSplitSummary(BaseModel):
    white: ColorSummary
    black: ColorSummary


class TrainingSummary(BaseModel):
    # Review Retention (all-time): fraction of reviewed blunders currently held
    # (pass_streak >= 1).
    retention_pct: float | None
    reviewed_blunders: int
    retained_blunders: int
    # Review Pass Rate (windowed by reviewed_at).
    review_pass_rate: float | None
    reviews_total: int
    reviews_passed: int
    # Distinct blunders that crossed the mastery threshold in the window.
    conversions_in_window: int
    mastery_threshold: int


class TopCostlyBlunder(BaseModel):
    blunder_id: int
    eval_loss_cp: int
    bad_move_san: str
    best_move_san: str
    created_at: datetime


class LibrarySummary(BaseModel):
    blunders_total: int
    new_blunders_in_window: int
    avg_blunder_eval_loss_cp: int
    top_costly_blunders: list[TopCostlyBlunder]


class OpeningStat(BaseModel):
    opening_name: str
    opening_family: str
    player_color: str
    opening_score: float
    sample_size: int
    game_count: int


class OpeningsSummary(BaseModel):
    strongest: list[OpeningStat]
    weakest: list[OpeningStat]


class StatsSummaryResponse(BaseModel):
    window_days: int
    generated_at: datetime
    games: GamesSummary
    moves: MoveSummary
    colors: ColorSplitSummary
    training: TrainingSummary
    library: LibrarySummary
    openings: OpeningsSummary


def _round1(value: float) -> float:
    return round(value, 1)


def _rate(numerator: int, denominator: int) -> float | None:
    """Percentage, or None when the denominator is 0 (Rate contract)."""
    if denominator <= 0:
        return None
    return _round1((numerator * 100.0) / denominator)


def _score_pct(wins: int, losses: int, draws: int) -> float | None:
    """(W + 0.5*D) / decided * 100, or None when no games are decided."""
    decided = wins + losses + draws
    if decided == 0:
        return None
    return _round1((wins + 0.5 * draws) * 100.0 / decided)


def _opening_stat(row: UserOpeningScore) -> OpeningStat:
    return OpeningStat(
        opening_name=row.opening_name,
        opening_family=row.opening_family,
        player_color=row.player_color,
        opening_score=row.opening_score,
        sample_size=row.sample_size,
        game_count=row.game_count,
    )


class CurrentRatingResponse(BaseModel):
    current_rating: int
    is_provisional: bool
    games_played: int
    scores: dict


@router.get("/current-rating", response_model=CurrentRatingResponse)
def get_current_rating(
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> CurrentRatingResponse:
    latest = (
        db.query(RatingHistory)
        .filter(RatingHistory.user_id == user.user_id)
        .order_by(*latest_rating_order())
        .first()
    )
    if latest:
        return CurrentRatingResponse(
            current_rating=latest.rating,
            is_provisional=latest.is_provisional,
            games_played=latest.games_played,
            scores=scores_for_row(latest),
        )
    return CurrentRatingResponse(
        current_rating=DEFAULT_RATING,
        is_provisional=True,
        games_played=0,
        scores=scores_for_row(None),
    )


class RatingPoint(BaseModel):
    timestamp: datetime
    rating: int
    is_provisional: bool
    game_session_id: str
    scores: dict


class RatingHistoryResponse(BaseModel):
    ratings: list[RatingPoint]
    current_rating: int
    games_played: int
    scores: dict


@router.get("/rating-history", response_model=RatingHistoryResponse)
def get_rating_history(
    range: str = Query("all", pattern="^(7d|30d|90d|all)$"),
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> RatingHistoryResponse:
    query = (
        db.query(RatingHistory)
        .filter(RatingHistory.user_id == user.user_id)
        .order_by(RatingHistory.recorded_at.asc())
    )

    if range != "all":
        days = int(range.rstrip("d"))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        query = query.filter(RatingHistory.recorded_at >= cutoff)

    rows = query.all()

    # Get latest rating regardless of time filter
    latest = (
        db.query(RatingHistory)
        .filter(RatingHistory.user_id == user.user_id)
        .order_by(*latest_rating_order())
        .first()
    )

    return RatingHistoryResponse(
        ratings=[
            RatingPoint(
                timestamp=row.recorded_at,
                rating=row.rating,
                is_provisional=row.is_provisional,
                game_session_id=str(row.game_session_id),
                scores=scores_for_row(row),
            )
            for row in rows
        ],
        current_rating=latest.rating if latest else DEFAULT_RATING,
        games_played=latest.games_played if latest else 0,
        scores=scores_for_row(latest),
    )


# Perfect Streak is dropped from the /summary response and the stats page, but the
# standalone /achievements endpoint is retained: ChessGame's in-game streak toast
# still reads the personal best from it.
class PerfectStreakSummary(BaseModel):
    personal_best: int


class StatsAchievementsSummary(BaseModel):
    perfect_streak: PerfectStreakSummary


def _perfect_streak_summary(db: Session, user_id: int) -> StatsAchievementsSummary:
    rows = (
        db.query(
            SessionMove.session_id,
            SessionMove.classification,
        )
        .join(GameSession, GameSession.id == SessionMove.session_id)
        .filter(
            GameSession.user_id == user_id,
            SessionMove.color == GameSession.player_color,
            visible_session_filter(),
        )
        .order_by(
            GameSession.started_at.asc(),
            GameSession.id.asc(),
            SessionMove.move_number.asc(),
            SessionMove.id.asc(),
        )
        .all()
    )

    personal_best = 0
    current_session_id: uuid.UUID | None = None
    session_streak = 0

    for session_id, classification in rows:
        if session_id != current_session_id:
            current_session_id = session_id
            session_streak = 0

        if classification is None:
            continue
        if classification == "best":
            session_streak += 1
            personal_best = max(personal_best, session_streak)
        else:
            session_streak = 0

    return StatsAchievementsSummary(
        perfect_streak=PerfectStreakSummary(personal_best=personal_best)
    )


@router.get("/achievements", response_model=StatsAchievementsSummary)
def get_stats_achievements(
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> StatsAchievementsSummary:
    return _perfect_streak_summary(db, user.user_id)


@router.get("/summary", response_model=StatsSummaryResponse)
def get_stats_summary(
    window_days: int = Query(30, ge=0),
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> StatsSummaryResponse:
    allowed_window_days = {0, 7, 30, 90, 365}
    if window_days not in allowed_window_days:
        raise HTTPException(
            status_code=422,
            detail="window_days must be one of: 0, 7, 30, 90, 365",
        )

    now = datetime.now(timezone.utc)
    cutoff = None if window_days == 0 else now - timedelta(days=window_days)
    normal_started_at = normal_play_started_at_expr()

    session_query = db.query(GameSession).filter(GameSession.user_id == user.user_id, visible_session_filter())
    if cutoff is not None:
        session_query = session_query.filter(normal_started_at >= cutoff)
    sessions = session_query.all()
    session_ids = [session.id for session in sessions]

    move_count_by_session: dict[uuid.UUID, int] = {}
    if session_ids:
        move_count_rows = (
            db.query(SessionMove.session_id, func.count(SessionMove.id))
            .filter(SessionMove.session_id.in_(session_ids))
            .group_by(SessionMove.session_id)
            .all()
        )
        move_count_by_session = {
            row[0]: int(row[1]) for row in move_count_rows
        }

    played = len(sessions)
    wins = 0
    losses = 0
    draws = 0
    total_moves_across_sessions = 0

    per_color_games = {
        "white": {"games": 0, "wins": 0, "losses": 0, "draws": 0},
        "black": {"games": 0, "wins": 0, "losses": 0, "draws": 0},
    }
    # Ended (status == "ended") sessions only: the denominator for the
    # mistake-free rate and the population for accuracy. In-progress games must
    # not inflate either.
    ended_sessions: list[GameSession] = []
    color_by_session: dict[uuid.UUID, str] = {}

    for session in sessions:
        color_key = "black" if session.player_color == "black" else "white"
        color_by_session[session.id] = color_key
        per_color_games[color_key]["games"] += 1
        total_moves_across_sessions += move_count_by_session.get(session.id, 0)

        if session.status == "ended":
            ended_sessions.append(session)

        # Losses fold checkmate losses + resigns + abandons — all are losses from
        # the player's view — so W-L-D sums to decided games.
        if session.result == "checkmate_win":
            wins += 1
            per_color_games[color_key]["wins"] += 1
        elif session.result in ("checkmate_loss", "resign", "abandon"):
            losses += 1
            per_color_games[color_key]["losses"] += 1
        elif session.result == "draw":
            draws += 1
            per_color_games[color_key]["draws"] += 1

    avg_moves = (
        _round1(total_moves_across_sessions / played)
        if played > 0
        else 0.0
    )
    score_pct = _score_pct(wins, losses, draws)

    # --- Per-move quality + per-session blunder counts (player moves only) ---
    player_move_rows: list[tuple[uuid.UUID, str | None]] = []
    if session_ids:
        player_move_rows = (
            db.query(SessionMove.session_id, SessionMove.classification)
            .join(GameSession, GameSession.id == SessionMove.session_id)
            .filter(
                GameSession.id.in_(session_ids),
                SessionMove.color == GameSession.player_color,
            )
            .all()
        )

    classified_move_total = 0
    quality_counts = {"inaccuracy": 0, "mistake": 0, "blunder": 0}
    blunder_moves_by_session: dict[uuid.UUID, int] = defaultdict(int)
    for move_session_id, classification in player_move_rows:
        if classification is not None:
            classified_move_total += 1
        if classification in quality_counts:
            quality_counts[classification] += 1
        if classification == "blunder":
            blunder_moves_by_session[move_session_id] += 1

    if classified_move_total == 0:
        quality_distribution: MoveQualityDistribution | None = None
    else:
        quality_distribution = MoveQualityDistribution(
            inaccuracy=_round1(quality_counts["inaccuracy"] * 100.0 / classified_move_total),
            mistake=_round1(quality_counts["mistake"] * 100.0 / classified_move_total),
            blunder=_round1(quality_counts["blunder"] * 100.0 / classified_move_total),
        )

    # A game is clean if the player made zero blunder moves in it.
    clean_ended = sum(
        1 for session in ended_sessions
        if blunder_moves_by_session.get(session.id, 0) == 0
    )
    mistake_free_game_rate = _rate(clean_ended, len(ended_sessions))

    # --- Whole-game accuracy over ended sessions (mirrors history.py) ---
    accuracy_by_session: dict[uuid.UUID, int | None] = {}
    ended_session_ids = [session.id for session in ended_sessions]
    if ended_session_ids:
        color_order = case((SessionMove.color == "white", 0), else_=1)
        move_rows = (
            db.query(
                SessionMove.session_id,
                SessionMove.color,
                SessionMove.eval_cp,
                SessionMove.eval_mate,
            )
            .filter(SessionMove.session_id.in_(ended_session_ids))
            .order_by(SessionMove.move_number.asc(), color_order.asc())
            .all()
        )
        moves_by_session: dict[uuid.UUID, list[AccuracyMove]] = {}
        for row in move_rows:
            moves_by_session.setdefault(row.session_id, []).append(
                AccuracyMove(color=row.color, eval_cp=row.eval_cp, eval_mate=row.eval_mate)
            )
        for session in ended_sessions:
            accuracy_by_session[session.id] = compute_game_accuracy(
                moves_by_session.get(session.id, []),
                player_color=session.player_color,
                expected_total_moves=expected_total_moves_from_pgn(session.pgn),
            )

    def _mean_accuracy(ids: list[uuid.UUID]) -> float | None:
        values = [
            accuracy_by_session[sid]
            for sid in ids
            if accuracy_by_session.get(sid) is not None
        ]
        if not values:
            return None
        return _round1(sum(values) / len(values))  # type: ignore[arg-type]

    accuracy_pct = _mean_accuracy(ended_session_ids)

    colors: dict[str, ColorSummary] = {}
    for color in ("white", "black"):
        color_ended_ids = [
            sid for sid in ended_session_ids if color_by_session.get(sid) == color
        ]
        pg = per_color_games[color]
        colors[color] = ColorSummary(
            games=pg["games"],
            score_pct=_score_pct(pg["wins"], pg["losses"], pg["draws"]),
            accuracy_pct=_mean_accuracy(color_ended_ids),
        )

    # --- Training (SRS) --------------------------------------------------------
    # BlunderReview has no user_id, so every review query joins Blunder and
    # filters on Blunder.user_id.
    reviewed_blunders = int(
        (
            db.query(func.count(Blunder.id))
            .filter(Blunder.user_id == user.user_id, Blunder.last_reviewed_at.isnot(None))
            .scalar()
        )
        or 0
    )
    retained_blunders = int(
        (
            db.query(func.count(Blunder.id))
            .filter(Blunder.user_id == user.user_id, Blunder.pass_streak >= 1)
            .scalar()
        )
        or 0
    )
    retention_pct = _rate(retained_blunders, reviewed_blunders)

    reviews_base = (
        db.query(BlunderReview)
        .join(Blunder, Blunder.id == BlunderReview.blunder_id)
        .filter(Blunder.user_id == user.user_id)
    )
    if cutoff is not None:
        reviews_base = reviews_base.filter(BlunderReview.reviewed_at >= cutoff)
    reviews_total = reviews_base.count()
    reviews_passed = reviews_base.filter(BlunderReview.passed.is_(True)).count()
    review_pass_rate = _rate(reviews_passed, reviews_total)

    conversions_query = (
        db.query(func.count(func.distinct(BlunderReview.blunder_id)))
        .join(Blunder, Blunder.id == BlunderReview.blunder_id)
        .filter(
            Blunder.user_id == user.user_id,
            BlunderReview.pass_streak_after == MASTERY_THRESHOLD,
        )
    )
    if cutoff is not None:
        conversions_query = conversions_query.filter(BlunderReview.reviewed_at >= cutoff)
    conversions_in_window = int(conversions_query.scalar() or 0)

    # --- Library (all-time) ----------------------------------------------------
    blunders_total = int(
        (
            db.query(func.count(Blunder.id))
            .filter(Blunder.user_id == user.user_id)
            .scalar()
        )
        or 0
    )

    window_blunders_query = db.query(func.count(Blunder.id)).filter(Blunder.user_id == user.user_id)
    if cutoff is not None:
        window_blunders_query = window_blunders_query.filter(Blunder.created_at >= cutoff)
    new_blunders_in_window = int(window_blunders_query.scalar() or 0)

    # Normalize at read (g-no51): floor legacy negatives to 0 and cap >1000, so the
    # displayed average is over decisive-mistake-capped values, not mate pseudo-cp.
    avg_blunder_eval_loss_cp_raw = (
        db.query(func.avg(centipawn_loss_expr(Blunder.eval_loss_cp)))
        .filter(Blunder.user_id == user.user_id)
        .scalar()
    )
    avg_blunder_eval_loss_cp = (
        int(round(float(avg_blunder_eval_loss_cp_raw)))
        if avg_blunder_eval_loss_cp_raw is not None
        else 0
    )

    top_costly_blunders_rows = (
        db.query(Blunder)
        .filter(Blunder.user_id == user.user_id)
        .order_by(centipawn_loss_expr(Blunder.eval_loss_cp).desc(), Blunder.created_at.desc())
        .limit(5)
        .all()
    )
    top_costly_blunders = [
        TopCostlyBlunder(
            blunder_id=row.id,
            eval_loss_cp=centipawn_loss(row.eval_loss_cp),
            bad_move_san=row.bad_move_san,
            best_move_san=row.best_move_san,
            created_at=row.created_at,
        )
        for row in top_costly_blunders_rows
    ]

    # --- Openings (all-time, latest scored batch) ------------------------------
    _, white_opening_rows = list_cached_opening_scores(db, user.user_id, "white")
    _, black_opening_rows = list_cached_opening_scores(db, user.user_id, "black")
    opening_rows = [
        row
        for row in (*white_opening_rows, *black_opening_rows)
        if row.sample_size >= 3
    ]
    opening_rows.sort(key=lambda row: row.opening_score)
    strongest = [_opening_stat(row) for row in reversed(opening_rows[-3:])]
    weakest = [_opening_stat(row) for row in opening_rows[:3]]

    return StatsSummaryResponse(
        window_days=window_days,
        generated_at=now,
        games=GamesSummary(
            played=played,
            score_pct=score_pct,
            wins=wins,
            losses=losses,
            draws=draws,
            avg_moves=avg_moves,
        ),
        moves=MoveSummary(
            accuracy_pct=accuracy_pct,
            mistake_free_game_rate=mistake_free_game_rate,
            quality_distribution=quality_distribution,
        ),
        colors=ColorSplitSummary(
            white=colors["white"],
            black=colors["black"],
        ),
        training=TrainingSummary(
            retention_pct=retention_pct,
            reviewed_blunders=reviewed_blunders,
            retained_blunders=retained_blunders,
            review_pass_rate=review_pass_rate,
            reviews_total=reviews_total,
            reviews_passed=reviews_passed,
            conversions_in_window=conversions_in_window,
            mastery_threshold=MASTERY_THRESHOLD,
        ),
        library=LibrarySummary(
            blunders_total=blunders_total,
            new_blunders_in_window=new_blunders_in_window,
            avg_blunder_eval_loss_cp=avg_blunder_eval_loss_cp,
            top_costly_blunders=top_costly_blunders,
        ),
        openings=OpeningsSummary(
            strongest=strongest,
            weakest=weakest,
        ),
    )
