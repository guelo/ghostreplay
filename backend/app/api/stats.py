from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Blunder, GameSession, Move, Position, SessionMove
from app.security import TokenPayload, get_current_user

router = APIRouter(prefix="/api/stats", tags=["stats"])


class GameRecord(BaseModel):
    wins: int
    losses: int
    draws: int
    resigns: int
    abandons: int


class GamesSummary(BaseModel):
    played: int
    completed: int
    active: int
    record: GameRecord
    avg_duration_seconds: int
    avg_moves: float


class ColorSummary(BaseModel):
    games: int
    completed: int
    wins: int
    losses: int
    draws: int
    avg_cpl: float
    blunders_per_100_moves: float


class ColorSplitSummary(BaseModel):
    white: ColorSummary
    black: ColorSummary


class MoveQualityDistribution(BaseModel):
    best: float
    excellent: float
    good: float
    inaccuracy: float
    mistake: float
    blunder: float


class MoveSummary(BaseModel):
    player_moves: int
    avg_cpl: float
    mistakes_per_100_moves: float
    blunders_per_100_moves: float
    quality_distribution: MoveQualityDistribution


class TopCostlyBlunder(BaseModel):
    blunder_id: int
    eval_loss_cp: int
    bad_move_san: str
    best_move_san: str
    created_at: datetime


class LibrarySummary(BaseModel):
    blunders_total: int
    positions_total: int
    edges_total: int
    new_blunders_in_window: int
    avg_blunder_eval_loss_cp: int
    top_costly_blunders: list[TopCostlyBlunder]


class DataCompletenessSummary(BaseModel):
    sessions_with_uploaded_moves_pct: float
    notes: list[str]


class StatsSummaryResponse(BaseModel):
    window_days: int
    generated_at: datetime
    games: GamesSummary
    colors: ColorSplitSummary
    moves: MoveSummary
    library: LibrarySummary
    data_completeness: DataCompletenessSummary


def _round1(value: float) -> float:
    return round(value, 1)


def _safe_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return _round1((numerator * 100.0) / denominator)


def _base_color_summary() -> ColorSummary:
    return ColorSummary(
        games=0,
        completed=0,
        wins=0,
        losses=0,
        draws=0,
        avg_cpl=0.0,
        blunders_per_100_moves=0.0,
    )


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

    session_query = db.query(GameSession).filter(GameSession.user_id == user.user_id)
    if cutoff is not None:
        session_query = session_query.filter(GameSession.started_at >= cutoff)
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
    completed = 0
    active = 0
    wins = 0
    losses = 0
    draws = 0
    resigns = 0
    abandons = 0
    ended_duration_seconds: list[float] = []
    total_moves_across_sessions = 0

    per_color_games = {
        "white": {"games": 0, "completed": 0, "wins": 0, "losses": 0, "draws": 0},
        "black": {"games": 0, "completed": 0, "wins": 0, "losses": 0, "draws": 0},
    }

    for session in sessions:
        player_color = "black" if session.player_color == "black" else "white"
        per_color_games[player_color]["games"] += 1

        total_moves_across_sessions += move_count_by_session.get(session.id, 0)

        if session.status == "ended":
            completed += 1
            per_color_games[player_color]["completed"] += 1
            if session.ended_at is not None:
                ended_duration_seconds.append(
                    (session.ended_at - session.started_at).total_seconds()
                )
        elif session.status == "active":
            active += 1

        if session.result == "checkmate_win":
            wins += 1
            per_color_games[player_color]["wins"] += 1
        elif session.result == "checkmate_loss":
            losses += 1
            per_color_games[player_color]["losses"] += 1
        elif session.result == "draw":
            draws += 1
            per_color_games[player_color]["draws"] += 1
        elif session.result == "resign":
            resigns += 1
        elif session.result == "abandon":
            abandons += 1

    avg_duration_seconds = (
        int(round(sum(ended_duration_seconds) / len(ended_duration_seconds)))
        if ended_duration_seconds
        else 0
    )
    avg_moves = (
        _round1(total_moves_across_sessions / played)
        if played > 0
        else 0.0
    )

    player_move_rows: list[tuple[str, str | None, int | None]] = []
    if session_ids:
        player_move_rows = (
            db.query(GameSession.player_color, SessionMove.classification, SessionMove.eval_delta)
            .join(SessionMove, SessionMove.session_id == GameSession.id)
            .filter(
                GameSession.id.in_(session_ids),
                SessionMove.color == GameSession.player_color,
            )
            .all()
        )

    player_moves = len(player_move_rows)
    cpl_values: list[int] = []
    mistake_count = 0
    blunder_count = 0
    quality_counts = {
        "best": 0,
        "excellent": 0,
        "good": 0,
        "inaccuracy": 0,
        "mistake": 0,
        "blunder": 0,
    }
    by_color_move_totals = {"white": 0, "black": 0}
    by_color_cpl_values: dict[str, list[int]] = {"white": [], "black": []}
    by_color_blunders = {"white": 0, "black": 0}

    for player_color, classification, eval_delta in player_move_rows:
        color_key = "black" if player_color == "black" else "white"
        by_color_move_totals[color_key] += 1

        if eval_delta is not None:
            normalized = max(eval_delta, 0)
            cpl_values.append(normalized)
            by_color_cpl_values[color_key].append(normalized)

        if classification == "mistake":
            mistake_count += 1
        if classification == "blunder":
            blunder_count += 1
            by_color_blunders[color_key] += 1
        if classification in quality_counts:
            quality_counts[classification] += 1

    classified_move_total = sum(quality_counts.values())
    avg_cpl = _round1(sum(cpl_values) / len(cpl_values)) if cpl_values else 0.0

    colors = {
        "white": _base_color_summary(),
        "black": _base_color_summary(),
    }
    for color in ("white", "black"):
        color_cpl_values = by_color_cpl_values[color]
        colors[color] = ColorSummary(
            games=per_color_games[color]["games"],
            completed=per_color_games[color]["completed"],
            wins=per_color_games[color]["wins"],
            losses=per_color_games[color]["losses"],
            draws=per_color_games[color]["draws"],
            avg_cpl=(
                _round1(sum(color_cpl_values) / len(color_cpl_values))
                if color_cpl_values
                else 0.0
            ),
            blunders_per_100_moves=_safe_rate(
                by_color_blunders[color], by_color_move_totals[color]
            ),
        )

    quality_distribution = MoveQualityDistribution(
        best=_safe_rate(quality_counts["best"], classified_move_total),
        excellent=_safe_rate(quality_counts["excellent"], classified_move_total),
        good=_safe_rate(quality_counts["good"], classified_move_total),
        inaccuracy=_safe_rate(quality_counts["inaccuracy"], classified_move_total),
        mistake=_safe_rate(quality_counts["mistake"], classified_move_total),
        blunder=_safe_rate(quality_counts["blunder"], classified_move_total),
    )

    blunders_total = (
        db.query(func.count(Blunder.id))
        .filter(Blunder.user_id == user.user_id)
        .scalar()
    ) or 0
    positions_total = (
        db.query(func.count(Position.id))
        .filter(Position.user_id == user.user_id)
        .scalar()
    ) or 0
    edges_total = (
        db.query(func.count())
        .select_from(Move)
        .join(Position, Position.id == Move.from_position_id)
        .filter(Position.user_id == user.user_id)
        .scalar()
    ) or 0

    window_blunders_query = db.query(Blunder).filter(Blunder.user_id == user.user_id)
    if cutoff is not None:
        window_blunders_query = window_blunders_query.filter(Blunder.created_at >= cutoff)
    new_blunders_in_window = window_blunders_query.count()

    avg_blunder_eval_loss_cp_raw = (
        db.query(func.avg(Blunder.eval_loss_cp))
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
        .order_by(Blunder.eval_loss_cp.desc(), Blunder.created_at.desc())
        .limit(5)
        .all()
    )
    top_costly_blunders = [
        TopCostlyBlunder(
            blunder_id=row.id,
            eval_loss_cp=row.eval_loss_cp,
            bad_move_san=row.bad_move_san,
            best_move_san=row.best_move_san,
            created_at=row.created_at,
        )
        for row in top_costly_blunders_rows
    ]

    sessions_with_uploaded_moves = sum(
        1 for session_id in session_ids if move_count_by_session.get(session_id, 0) > 0
    )
    sessions_with_uploaded_moves_pct = _safe_rate(
        sessions_with_uploaded_moves, played
    )

    return StatsSummaryResponse(
        window_days=window_days,
        generated_at=now,
        games=GamesSummary(
            played=played,
            completed=completed,
            active=active,
            record=GameRecord(
                wins=wins,
                losses=losses,
                draws=draws,
                resigns=resigns,
                abandons=abandons,
            ),
            avg_duration_seconds=avg_duration_seconds,
            avg_moves=avg_moves,
        ),
        colors=ColorSplitSummary(
            white=colors["white"],
            black=colors["black"],
        ),
        moves=MoveSummary(
            player_moves=player_moves,
            avg_cpl=avg_cpl,
            mistakes_per_100_moves=_safe_rate(mistake_count, player_moves),
            blunders_per_100_moves=_safe_rate(blunder_count, player_moves),
            quality_distribution=quality_distribution,
        ),
        library=LibrarySummary(
            blunders_total=int(blunders_total),
            positions_total=int(positions_total),
            edges_total=int(edges_total),
            new_blunders_in_window=new_blunders_in_window,
            avg_blunder_eval_loss_cp=avg_blunder_eval_loss_cp,
            top_costly_blunders=top_costly_blunders,
        ),
        data_completeness=DataCompletenessSummary(
            sessions_with_uploaded_moves_pct=sessions_with_uploaded_moves_pct,
            notes=[
                "Per-move metrics use player moves only.",
                "SRS review stats are excluded until review outcomes are persisted.",
            ],
        ),
    )
