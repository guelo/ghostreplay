import hashlib
import logging
import math
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.accuracy import expected_total_moves_from_pgn
from app.db import get_db
from app.drill_steering import route_map_for_target, route_preserving_moves
from app.fen import fen_hash, active_color
from app.models import GameSession, Position, RatingHistory, decode_uci_line
from app.opening_baseline_scheduler import enqueue_baseline_snapshot
from app.opening_graph import get_opening_graph
from app.opening_score_delta import (
    OpeningScoreDeltaItem,
    compute_opening_score_delta,
)
from app.posthog_client import capture
from app.glicko import CHESSCOM_INITIAL_RATING, LICHESS_INITIAL_RATING
from app.rating import DEFAULT_RATING, RESULT_SCORES
from app.rating_scores import compute_rating_tracks, latest_rating_order, rating_score, scores_for_row
from app.security import TokenPayload, get_current_user
from app.session_contracts import DRILL_SESSION_MODE, VISIBLE_DRILL_STATE, utcnow
from app.srs_math import (
    OPPORTUNITY_POWER,
    calculate_opportunity_overdue,
    calculate_urgency,
    compute_p_reach,
)
from app.srs_opportunity import (
    P_REACH_FLOOR,
    P_REACH_MIN_SAMPLE,
    SEVERITY_NORMALIZER_CP,
    detect_opening_family,
    ghost_eligible,
    load_opportunity_counters,
    load_review_counters,
    opening_weight,
)

router = APIRouter(prefix="/api/game", tags=["game"])
logger = logging.getLogger(__name__)

STEERING_RADIUS = 5
DISTANCE_DECAY_RATE = 0.35
TOP_K = 5
FIRST_MOVE_SECONDARY_WEIGHT = 0.15
SELECTION_WEIGHT_POWER = 0.5
REPEAT_HISTORY_SCAN_LIMIT = 200
REPEAT_PENALTY_LOOKBACK = 3
REPEAT_PENALTY_FACTORS = (0.35, 0.60, 0.80)
SLOW_GHOST_SEARCH_LOG_MS = 250
SLOW_NEXT_OPPONENT_MOVE_LOG_MS = 1000
T = TypeVar("T")


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


@dataclass(frozen=True)
class GhostMoveCandidate:
    first_move: str
    blunder_id: int
    depth: int
    eval_loss_cp: int
    pass_streak: int
    last_reviewed_at: datetime | None
    created_at: datetime | None
    opportunities_since_review: int = 0
    opportunities_30d: int = 0
    reached_30d: int = 0
    has_opportunity_events: bool = False
    opening_family: str | None = None

    def score(self, now: datetime, current_opening_family: str | None = None) -> float:
        urgency = calculate_urgency(
            pass_streak=self.pass_streak,
            last_reviewed_at=self.last_reviewed_at,
            created_at=self.created_at,
            now=now,
        )
        if self.has_opportunity_events:
            urgency = calculate_opportunity_overdue(
                opportunities_since_review=self.opportunities_since_review,
                pass_streak=self.pass_streak,
            )
        severity = math.log1p(max(float(self.eval_loss_cp), 0.0) / SEVERITY_NORMALIZER_CP)
        distance_weight = math.exp(-DISTANCE_DECAY_RATE * self.depth)
        reach_weight = 1.0
        if self.has_opportunity_events:
            reach_weight = compute_p_reach(self.reached_30d, self.opportunities_30d) ** OPPORTUNITY_POWER
        return (
            urgency
            * severity
            * distance_weight
            * reach_weight
            * opening_weight(self.opening_family, current_opening_family)
        )


@dataclass(frozen=True)
class FirstMoveGroup:
    first_move: str
    candidates: tuple[tuple[GhostMoveCandidate, float], ...]
    aggregate_score: float
    penalty_factor: float = 1.0

    @property
    def penalized_score(self) -> float:
        return self.aggregate_score * self.penalty_factor


def _stable_seed(user_id: int, fen: str, session_id: uuid.UUID) -> int:
    """Deterministic seed stable across Python restarts.

    Uses fen_hash (normalized position identity) so equivalent FENs that
    differ only in halfmove/fullmove counters produce the same seed.
    """
    raw = f"{user_id}|{fen_hash(fen)}|{session_id}".encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], byteorder="big")


def _candidate_sort_key(scored_candidate: tuple[GhostMoveCandidate, float]) -> tuple[float, int, int, int, str]:
    candidate, score = scored_candidate
    return (
        -score,
        candidate.depth,
        -candidate.eval_loss_cp,
        -candidate.blunder_id,
        candidate.first_move,
    )


def _dedupe_path_candidates(
    scored: list[tuple[GhostMoveCandidate, float]],
) -> list[tuple[GhostMoveCandidate, float]]:
    best_by_key: dict[tuple[str, int], tuple[GhostMoveCandidate, float]] = {}
    for candidate, score in scored:
        key = (candidate.first_move, candidate.blunder_id)
        existing = best_by_key.get(key)
        current = (candidate, score)
        if existing is None or _candidate_sort_key(current) < _candidate_sort_key(existing):
            best_by_key[key] = current
    return sorted(best_by_key.values(), key=_candidate_sort_key)


def _aggregate_first_move_score(candidate_scores: list[float]) -> float:
    if not candidate_scores:
        return 0.0
    sorted_scores = sorted(candidate_scores, reverse=True)
    return sorted_scores[0] + FIRST_MOVE_SECONDARY_WEIGHT * sum(sorted_scores[1:])


def _group_candidates_by_first_move(
    scored: list[tuple[GhostMoveCandidate, float]],
    repeat_penalties: dict[str, float] | None = None,
) -> list[FirstMoveGroup]:
    grouped: dict[str, list[tuple[GhostMoveCandidate, float]]] = {}
    for candidate, score in scored:
        grouped.setdefault(candidate.first_move, []).append((candidate, score))

    groups: list[FirstMoveGroup] = []
    for first_move, candidates in grouped.items():
        stable_candidates = tuple(sorted(candidates, key=_candidate_sort_key))
        aggregate_score = _aggregate_first_move_score([score for _, score in stable_candidates])
        groups.append(
            FirstMoveGroup(
                first_move=first_move,
                candidates=stable_candidates,
                aggregate_score=aggregate_score,
                penalty_factor=(repeat_penalties or {}).get(first_move, 1.0),
            )
        )
    return sorted(groups, key=lambda group: (-group.penalized_score, group.first_move))


def _flatten_selection_weight(score: float) -> float:
    return max(score, 0.0) ** SELECTION_WEIGHT_POWER


def _weighted_choice(items: list[T], weights: list[float], rng: random.Random) -> T:
    if not items:
        raise ValueError("Cannot choose from an empty list")
    if any(weight > 0 for weight in weights):
        return rng.choices(items, weights=weights, k=1)[0]
    return items[0]


def _same_fen_recent_ghost_moves(db: Session, user_id: int, fen: str) -> list[str]:
    current_hash = fen_hash(fen)
    rows = db.execute(
        text("""
            SELECT sm.fen_before, sm.move_san
            FROM session_moves sm
            JOIN game_sessions gs ON gs.id = sm.session_id
            WHERE gs.user_id = :user_id
              AND sm.decision_source = 'ghost_path'
              AND sm.fen_before IS NOT NULL
              AND (gs.session_mode = 'normal' OR sm.target_blunder_id IS NOT NULL)
            ORDER BY gs.started_at DESC, sm.id DESC
            LIMIT :limit
        """),
        {"user_id": user_id, "limit": REPEAT_HISTORY_SCAN_LIMIT},
    ).fetchall()

    matches: list[str] = []
    for fen_before, move_san in rows:
        try:
            if fen_hash(fen_before) != current_hash:
                continue
        except ValueError:
            continue
        matches.append(move_san)
        if len(matches) >= REPEAT_PENALTY_LOOKBACK:
            break
    return matches


def _repeat_penalties(recent_same_fen_moves: list[str]) -> dict[str, float]:
    penalties: dict[str, float] = {}
    for move_san, factor in zip(recent_same_fen_moves, REPEAT_PENALTY_FACTORS):
        penalties[move_san] = penalties.get(move_san, 1.0) * factor
    return penalties


def _isoformat_optional(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def find_ghost_move(
    db: Session,
    user_id: int,
    fen: str,
    player_color: str,
    *,
    session_id: uuid.UUID | None = None,
    _rng_seed: int | None = None,
) -> tuple[str | None, int | None, datetime | None, datetime | None]:
    """
    Find a move that steers toward a position where the user previously blundered.

    Uses recursive path traversal to search up to 5 moves downstream for reachable
    blunders that match the player's color.

    Args:
        db: Database session
        user_id: User ID to scope blunder lookup
        fen: Current board position FEN
        player_color: Player color from game session ('white' or 'black')

    Returns:
        Tuple of (move_san, target_blunder_id, last_reviewed_at, created_at) if ghost path exists,
        else (None, None, None, None)
    """
    search_started = time.perf_counter()
    current_fen_hash = fen_hash(fen)
    position_lookup_ms = 0.0
    cte_ms = 0.0
    opening_family_ms = 0.0
    opportunity_ms = 0.0
    repeat_history_ms = 0.0

    def _log_slow(
        outcome: str,
        *,
        position_id: int | None = None,
        candidate_count: int = 0,
        scored_count: int = 0,
        group_count: int = 0,
    ) -> None:
        total_ms = _elapsed_ms(search_started)
        if total_ms < SLOW_GHOST_SEARCH_LOG_MS:
            return
        logger.info(
            "ghost_move_search slow outcome=%s user_id=%s fen_hash=%s "
            "player_color=%s total_ms=%.3f position_lookup_ms=%.3f "
            "cte_ms=%.3f opening_family_ms=%.3f opportunity_ms=%.3f "
            "repeat_history_ms=%.3f position_id=%s candidate_count=%d "
            "scored_count=%d group_count=%d",
            outcome,
            user_id,
            current_fen_hash,
            player_color,
            total_ms,
            position_lookup_ms,
            cte_ms,
            opening_family_ms,
            opportunity_ms,
            repeat_history_ms,
            position_id,
            candidate_count,
            scored_count,
            group_count,
        )

    # Look up current position by FEN hash
    position_lookup_started = time.perf_counter()
    current_position = (
        db.query(Position)
        .filter(
            Position.user_id == user_id,
            Position.fen_hash == current_fen_hash,
        )
        .first()
    )
    position_lookup_ms = _elapsed_ms(position_lookup_started)

    if not current_position:
        _log_slow("no_position")
        return (None, None, None, None)

    # Recursive CTE to find candidate blunders up to the steering radius.
    # Returns the first move in each path and candidate metadata for scoring.
    # Uses string-based path for cycle detection
    cte_query = text("""
        WITH RECURSIVE reachable(position_id, depth, path, first_move) AS (
            -- Base case: current position (depth 0, no first_move yet)
            SELECT
                CAST(:start_position_id AS BIGINT),
                0,
                ',' || :start_position_id || ',',
                CAST(NULL AS TEXT)

            UNION ALL

            -- Recursive case: follow moves up to configured steering radius
            SELECT
                m.to_position_id,
                r.depth + 1,
                r.path || m.to_position_id || ',',
                COALESCE(r.first_move, m.move_san)
            FROM reachable r
            JOIN moves m ON m.from_position_id = r.position_id
            WHERE r.depth < :steering_radius
              AND r.path NOT LIKE '%,' || CAST(m.to_position_id AS TEXT) || ',%'
        )
        SELECT
            r.first_move,
            b.id AS blunder_id,
            r.depth,
            b.eval_loss_cp,
            b.pass_streak,
            b.last_reviewed_at,
            b.created_at,
            b.opening_family
        FROM reachable r
        JOIN positions p ON p.id = r.position_id
        JOIN blunders b ON b.position_id = r.position_id
        WHERE b.user_id = :user_id
          AND p.active_color = :player_color
          AND r.first_move IS NOT NULL
    """)

    cte_started = time.perf_counter()
    candidate_rows = db.execute(
        cte_query,
        {
            "start_position_id": current_position.id,
            "user_id": user_id,
            "player_color": player_color,
            "steering_radius": STEERING_RADIUS,
        },
    ).fetchall()
    cte_ms = _elapsed_ms(cte_started)

    if not candidate_rows:
        _log_slow("no_candidates", position_id=current_position.id)
        return (None, None, None, None)

    now = datetime.now(timezone.utc)
    if any(row[7] for row in candidate_rows):
        opening_family_started = time.perf_counter()
        current_opening_family = detect_opening_family(fen)
        opening_family_ms = _elapsed_ms(opening_family_started)
    else:
        current_opening_family = None
    opportunity_started = time.perf_counter()
    # Exclude the in-progress game session: the game we are steering toward the
    # blunder in must not count as a missed opportunity against that blunder, or
    # an early ancestor touch flips it to "exactly due, not overdue" and kills
    # steering for the rest of the game (priority drops from time-based to
    # opportunities_since_review/expected = 1/1.0 = 1.0, failing the > 1.0 gate).
    opportunity_counters = load_opportunity_counters(
        db,
        [row[1] for row in candidate_rows],
        now=now,
        exclude_session_id=session_id,
    )
    opportunity_ms = _elapsed_ms(opportunity_started)
    scored: list[tuple[GhostMoveCandidate, float]] = []

    for row in candidate_rows:
        counters = opportunity_counters.get(row[1])
        candidate = GhostMoveCandidate(
            first_move=row[0],
            blunder_id=row[1],
            depth=row[2],
            eval_loss_cp=row[3],
            pass_streak=row[4],
            last_reviewed_at=row[5],
            created_at=row[6],
            opportunities_since_review=counters.opportunities_since_review if counters else 0,
            opportunities_30d=counters.opportunities_30d if counters else 0,
            reached_30d=counters.reached_30d if counters else 0,
            has_opportunity_events=bool(counters and counters.event_count > 0),
            opening_family=row[7],
        )
        if not ghost_eligible(
            counters=counters,
            pass_streak=candidate.pass_streak,
            last_reviewed_at=candidate.last_reviewed_at,
            created_at=candidate.created_at,
            now=now,
        ):
            continue
        scored.append((candidate, candidate.score(now, current_opening_family)))

    if not scored:
        _log_slow(
            "no_eligible_candidates",
            position_id=current_position.id,
            candidate_count=len(candidate_rows),
        )
        return (None, None, None, None)

    # A depth-1 candidate reaches the review position immediately after the
    # ghost move. Prefer those over multi-ply steering routes, which still
    # depend on the player following the stored line before a review can happen.
    replay_ready = [item for item in scored if item[0].depth == 1]
    selection_pool = replay_ready or scored

    deduped = _dedupe_path_candidates(selection_pool)
    repeat_history_started = time.perf_counter()
    recent_same_fen_moves = _same_fen_recent_ghost_moves(db, user_id, fen)
    repeat_history_ms = _elapsed_ms(repeat_history_started)
    repeat_penalties = _repeat_penalties(recent_same_fen_moves)
    groups = _group_candidates_by_first_move(deduped, repeat_penalties)
    top_first_moves = groups[:TOP_K]

    if _rng_seed is not None:
        seed = _rng_seed
    elif session_id is not None:
        seed = _stable_seed(user_id, fen, session_id)
    else:
        seed = 0
    rng = random.Random(seed)

    chosen_group = _weighted_choice(
        top_first_moves,
        [_flatten_selection_weight(group.penalized_score) for group in top_first_moves],
        rng,
    )
    chosen_candidate, _ = _weighted_choice(
        list(chosen_group.candidates),
        [_flatten_selection_weight(score) for _, score in chosen_group.candidates],
        rng,
    )

    _log_slow(
        "selected",
        position_id=current_position.id,
        candidate_count=len(candidate_rows),
        scored_count=len(scored),
        group_count=len(groups),
    )
    return (chosen_candidate.first_move, chosen_candidate.blunder_id, chosen_candidate.last_reviewed_at, chosen_candidate.created_at)


class GameResult(str, Enum):
    """Possible game results."""
    CHECKMATE_WIN = "checkmate_win"
    CHECKMATE_LOSS = "checkmate_loss"
    RESIGN = "resign"
    DRAW = "draw"
    ABANDON = "abandon"


class PlayerColor(str, Enum):
    """Player color selection."""
    WHITE = "white"
    BLACK = "black"


class GameStartRequest(BaseModel):
    engine_elo: int = Field(..., description="Engine ELO rating")
    player_color: PlayerColor = Field(
        PlayerColor.WHITE,
        description="Player color (white|black)",
    )


class GameStartResponse(BaseModel):
    session_id: uuid.UUID
    engine_elo: int
    player_color: PlayerColor


class GameEndRequest(BaseModel):
    session_id: uuid.UUID = Field(..., description="Game session ID")
    result: GameResult = Field(..., description="Game result")
    pgn: str = Field(..., description="PGN of the game")
    is_rated: bool = Field(True, description="Whether this game counts for rating")


class RatingChange(BaseModel):
    rating_before: int
    rating_after: int
    is_provisional: bool


class RatingScore(BaseModel):
    rating: int
    is_provisional: bool
    rd: float | None = None
    volatility: float | None = None


class RatingScores(BaseModel):
    elo: RatingScore
    chesscom: RatingScore | None = None
    lichess: RatingScore | None = None


class GameEndResponse(BaseModel):
    session_id: uuid.UUID
    result: str
    ended_at: datetime
    rating: RatingChange | None = None
    scores: RatingScores | None = None
    score_changes: RatingScores | None = None
    scores_after: RatingScores | None = None
    # Per-played-opening score deltas (before -> after) vs the session baseline,
    # broadest -> deepest. Independent of rating gating, so present for unrated /
    # practice-continuation games too. None when no opening was crossed or the
    # delta could not be computed.
    opening_score_changes: list[OpeningScoreDeltaItem] | None = None


class MoveDetails(BaseModel):
    """Move representation with both UCI and SAN formats."""
    uci: str = Field(..., description="Move in UCI notation (e.g., 'e2e4')")
    san: str = Field(..., description="Move in SAN notation (e.g., 'e4')")


class DecisionSource(str, Enum):
    """Source of the opponent move decision."""
    GHOST_PATH = "ghost_path"
    BACKEND_ENGINE = "backend_engine"


class OpponentMoveMode(str, Enum):
    """Opponent move response mode."""
    GHOST = "ghost"
    ENGINE = "engine"


class NextOpponentMoveRequest(BaseModel):
    """Request for next opponent move."""
    session_id: uuid.UUID = Field(..., description="Game session ID")
    fen: str = Field(..., description="Current board position FEN")
    moves: list[str] = Field(default_factory=list, description="UCI move history from game start")


class TargetBlunderSrs(BaseModel):
    """SRS metadata for the blunder being targeted by a ghost move."""
    last_reviewed_at: str | None = Field(None, description="ISO timestamp of last review")
    created_at: str | None = Field(None, description="ISO timestamp of when the blunder was first recorded")
    pass_count: int = Field(0, description="Total times passed")
    fail_count: int = Field(0, description="Total times failed")
    pass_streak: int = Field(0, description="Current consecutive pass streak")
    opportunities_since_review: int = Field(0, description="Opportunity events since latest review")
    opportunities_30d: int = Field(0, description="Opportunity events in the last 30 days")
    reached_30d: int = Field(0, description="Exact blunder reaches in the last 30 days")
    p_reach: float = Field(0.5, description="Smoothed 30-day reach probability")


class DrillRouteMetadata(BaseModel):
    status: str
    target_fen: str
    resulting_fen: str
    plies_to_target: int


class NextOpponentMoveResponse(BaseModel):
    """Response for next opponent move (unified ghost + engine endpoint)."""
    mode: OpponentMoveMode = Field(
        ...,
        description="ghost = steering toward blunder, engine = backend inference",
    )
    move: MoveDetails = Field(
        ...,
        description="Next opponent move in both UCI and SAN formats",
    )
    target_blunder_id: int | None = Field(
        None,
        description="ID of the blunder being targeted (ghost mode only)",
    )
    target_blunder_srs: TargetBlunderSrs | None = Field(
        None,
        description="SRS info for the targeted blunder (ghost mode only)",
    )
    target_fen: str | None = Field(
        None,
        description="FEN of the blunder position the ghost is steering toward (ghost mode only)",
    )
    decision_source: DecisionSource = Field(
        ...,
        description="Backend decision branch used to produce the move",
    )
    drill_route: DrillRouteMetadata | None = None


@router.post("/start", response_model=GameStartResponse, status_code=201)
def start_game(
    request: GameStartRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> GameStartResponse:
    """
    Create a new game session with the specified engine ELO.

    Returns the session_id to be used for subsequent game operations.
    """
    session = GameSession(
        id=uuid.uuid4(),
        user_id=user.user_id,
        started_at=datetime.now(timezone.utc),
        status="active",
        engine_elo=request.engine_elo,
        blunder_recorded=False,
        player_color=request.player_color.value,
        session_mode="normal",
        opening_score_baseline=None,
    )

    db.add(session)
    db.commit()
    db.refresh(session)

    # Capture the opening-score baseline OFF the request thread (g-mxeo): proving
    # the cached batch fresh costs an O(all-evidence) digest. The worker fills
    # ``opening_score_baseline`` shortly after, only when the pre-session batch is
    # provably fresh and dated strictly before ``started_at``; otherwise it stays
    # NULL and the end-of-game delta degrades to "no delta". Best-effort: an
    # enqueue failure must not regress /start from 201.
    enqueue_baseline_snapshot(session.id, user.user_id, request.player_color.value)

    capture(
        str(user.user_id),
        "game_started",
        {
            "engine_elo": session.engine_elo,
            "player_color": session.player_color,
            "is_rated": session.is_rated,
        },
    )

    return GameStartResponse(
        session_id=session.id,
        engine_elo=session.engine_elo,
        player_color=request.player_color,
    )


@router.post("/end", response_model=GameEndResponse)
def end_game(
    request: GameEndRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> GameEndResponse:
    """
    End a game session by setting its status to 'ended', recording the result,
    and setting the ended_at timestamp.

    Validates that the session exists, belongs to the user, and is currently active.
    """
    # Fetch the session
    session = db.query(GameSession).filter(GameSession.id == request.session_id).first()

    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")

    # Verify ownership
    if session.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to end this game")

    # Verify session is active
    if session.status != "active":
        raise HTTPException(
            status_code=400,
            detail=f"Game session is already {session.status}"
        )
    if session.session_mode == DRILL_SESSION_MODE and session.drill_state != VISIBLE_DRILL_STATE:
        raise HTTPException(
            status_code=400,
            detail="Use the drill abandon or continue endpoint before ending this drill",
        )

    # Update session
    session.status = "ended"
    session.result = request.result.value
    session.ended_at = utcnow()
    session.pgn = request.pgn
    effective_is_rated = session.is_rated if session.session_mode == DRILL_SESSION_MODE else request.is_rated
    session.is_rated = effective_is_rated

    # Compute rating change for rated results
    rating_change = None
    if effective_is_rated and request.result.value in RESULT_SCORES:
        latest = (
            db.query(RatingHistory)
            .filter(RatingHistory.user_id == user.user_id)
            .order_by(*latest_rating_order())
            .first()
        )
        current_rating = latest.rating if latest else DEFAULT_RATING
        games_played = latest.games_played if latest else 0

        new_rating, is_provisional, chesscom, lichess = compute_rating_tracks(
            latest, session.engine_elo, request.result.value
        )

        rating_row = RatingHistory(
            user_id=user.user_id,
            game_session_id=session.id,
            rating=new_rating,
            is_provisional=is_provisional,
            games_played=games_played + 1,
            chesscom_rating=chesscom.rating if chesscom else None,
            chesscom_rd=chesscom.rd if chesscom else None,
            lichess_rating=lichess.rating if lichess else None,
            lichess_rd=lichess.rd if lichess else None,
            lichess_volatility=lichess.volatility if lichess else None,
            recorded_at=session.ended_at,
        )
        db.add(rating_row)

        rating_change = RatingChange(
            rating_before=current_rating,
            rating_after=new_rating,
            is_provisional=is_provisional,
        )
        scores_before = scores_for_row(latest)
        scores_after = scores_for_row(rating_row)
        score_changes = {
            "elo": rating_score(new_rating - current_rating, is_provisional),
            "chesscom": None,
            "lichess": None,
        }
        if chesscom:
            chesscom_before = (
                scores_before["chesscom"]["rating"]
                if scores_before["chesscom"] is not None
                else round(CHESSCOM_INITIAL_RATING)
            )
            score_changes["chesscom"] = rating_score(
                round(chesscom.rating) - chesscom_before,
                is_provisional,
                chesscom.rd,
            )
        if lichess:
            lichess_before = (
                scores_before["lichess"]["rating"]
                if scores_before["lichess"] is not None
                else round(LICHESS_INITIAL_RATING)
            )
            score_changes["lichess"] = rating_score(
                round(lichess.rating) - lichess_before,
                is_provisional,
                lichess.rd,
                lichess.volatility,
            )
    else:
        scores_after = None
        score_changes = None

    db.commit()
    db.refresh(session)

    # Recompute opening scores and diff the played chain against the session
    # baseline. Best-effort and supplementary to the rating change — never raises.
    # Skipped for ABANDON: those end calls fire during "new game/drill" cleanup
    # and the response is discarded, so computing the delta would only add the
    # synchronous refresh_now latency for a banner that never renders.
    opening_score_changes = (
        None
        if request.result == GameResult.ABANDON
        else compute_opening_score_delta(db, session) or None
    )

    capture(
        str(user.user_id),
        "game_ended",
        {
            "result": session.result,
            "is_rated": effective_is_rated,
            "rating_before": rating_change.rating_before if rating_change else None,
            "rating_after": rating_change.rating_after if rating_change else None,
            "rating_delta": (
                rating_change.rating_after - rating_change.rating_before
                if rating_change
                else None
            ),
            "ply_count": expected_total_moves_from_pgn(session.pgn),
        },
    )

    return GameEndResponse(
        session_id=session.id,
        result=session.result,
        ended_at=session.ended_at,
        rating=rating_change,
        scores=scores_after,
        score_changes=score_changes,
        scores_after=scores_after,
        opening_score_changes=opening_score_changes,
    )


@router.post("/next-opponent-move", response_model=NextOpponentMoveResponse)
def get_next_opponent_move(
    request: NextOpponentMoveRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> NextOpponentMoveResponse:
    """
    Get the next opponent move using unified ghost-first pipeline with backend engine fallback.

    This endpoint replaces the split orchestration (ghost endpoint + local engine fallback)
    with a single decision-maker that returns exactly one opponent move.

    Flow:
    1. Validate session ownership and FEN input
    2. Attempt ghost-path traversal (look for due blunders within steering radius)
    3. If ghost path exists, return ghost move
    4. Otherwise, fall back to backend engine inference (Maia)
    """
    request_started = time.perf_counter()
    ghost_search_ms = 0.0
    engine_ms = 0.0

    def _log_slow(mode: OpponentMoveMode, decision_source: DecisionSource, has_target_blunder: bool) -> None:
        total_ms = _elapsed_ms(request_started)
        if total_ms < SLOW_NEXT_OPPONENT_MOVE_LOG_MS:
            return
        logger.info(
            "next_opponent_move slow mode=%s decision_source=%s user_id=%s "
            "session_id=%s total_ms=%.3f ghost_search_ms=%.3f "
            "engine_ms=%.3f moves_count=%d has_target_blunder=%s",
            mode.value,
            decision_source.value,
            user.user_id,
            request.session_id,
            total_ms,
            ghost_search_ms,
            engine_ms,
            len(request.moves),
            has_target_blunder,
        )

    def _emit_served(mode: OpponentMoveMode, has_target_blunder: bool) -> None:
        capture(
            str(user.user_id),
            "opponent_move_served",
            {"decision_source": mode.value, "has_target_blunder": has_target_blunder},
        )

    # Fetch and validate session
    session = db.query(GameSession).filter(GameSession.id == request.session_id).first()

    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")

    if session.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this game")
    is_active_drill = (
        session.session_mode == DRILL_SESSION_MODE
        and session.drill_state in {"active", "root_reached"}
    )
    is_active_preroot_drill = (
        session.session_mode == DRILL_SESSION_MODE and session.drill_state == "active"
    )
    if (
        session.session_mode == DRILL_SESSION_MODE
        and session.drill_state != VISIBLE_DRILL_STATE
        and not is_active_drill
    ):
        raise HTTPException(status_code=400, detail="Opponent moves are unavailable for this drill state")

    # Validate FEN and check it's the opponent's turn
    try:
        position_color = active_color(request.fen)
    except (IndexError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid FEN: {e}")

    if position_color == session.player_color:
        raise HTTPException(
            status_code=400,
            detail="Cannot get opponent move when it's the player's turn",
        )

    if is_active_preroot_drill:
        if not session.drill_opening_key:
            raise HTTPException(status_code=400, detail="Drill is missing an opening root")
        graph = get_opening_graph()
        route_map = route_map_for_target(
            graph, session.drill_opening_key, decode_uci_line(session.drill_line)
        )
        if not route_map.plies_by_fen:
            raise HTTPException(status_code=400, detail="Drill route is unavailable")
        if route_map.is_target(request.fen):
            session.drill_state = "root_reached"
            db.commit()
            raise HTTPException(status_code=400, detail="Drill root already reached")
        suggestions = route_preserving_moves(graph, route_map, request.fen)
        if not suggestions:
            raise HTTPException(status_code=400, detail="Current drill position is off route")

        move = suggestions[0]
        if move.resulting_fen == route_map.target_fen:
            session.drill_state = "root_reached"
            db.commit()

        _emit_served(OpponentMoveMode.GHOST, False)
        _log_slow(OpponentMoveMode.GHOST, DecisionSource.GHOST_PATH, False)
        return NextOpponentMoveResponse(
            mode=OpponentMoveMode.GHOST,
            move=MoveDetails(uci=move.uci, san=move.san),
            target_blunder_id=None,
            target_blunder_srs=None,
            target_fen=route_map.target_fen,
            decision_source=DecisionSource.GHOST_PATH,
            drill_route=DrillRouteMetadata(
                status="root_reached" if move.resulting_fen == route_map.target_fen else "on_route",
                target_fen=route_map.target_fen,
                resulting_fen=move.resulting_fen,
                plies_to_target=move.plies_to_target,
            ),
        )

    # Step 1: Ghost-first path traversal
    # Use shared ghost path traversal logic to find moves toward due blunders
    ghost_search_started = time.perf_counter()
    move_san, target_blunder_id, blunder_last_reviewed, blunder_created_at = find_ghost_move(
        db=db,
        user_id=user.user_id,
        fen=request.fen,
        player_color=session.player_color,
        session_id=request.session_id,
    )
    ghost_search_ms = _elapsed_ms(ghost_search_started)

    # If ghost path exists, convert SAN to both UCI and SAN formats
    if move_san is not None:
        import chess
        try:
            board = chess.Board(request.fen)
            # Parse SAN to get the move object
            move = board.parse_san(move_san)

            # Fetch SRS review counts for the targeted blunder (shared loader)
            review_counters = load_review_counters(
                db, [target_blunder_id] if target_blunder_id else []
            )
            review_counter = review_counters.get(target_blunder_id) if target_blunder_id else None

            blunder_row = db.execute(
                text("""
                    SELECT b.pass_streak, p.fen_raw
                    FROM blunders b
                    JOIN positions p ON p.id = b.position_id
                    WHERE b.id = :id
                """),
                {"id": target_blunder_id},
            ).fetchone()
            opportunity_counters = load_opportunity_counters(db, [target_blunder_id] if target_blunder_id else [])
            counters = opportunity_counters.get(target_blunder_id) if target_blunder_id else None

            target_srs = TargetBlunderSrs(
                last_reviewed_at=_isoformat_optional(blunder_last_reviewed),
                created_at=_isoformat_optional(blunder_created_at),
                pass_count=review_counter.pass_count if review_counter else 0,
                fail_count=review_counter.fail_count if review_counter else 0,
                pass_streak=blunder_row[0] if blunder_row else 0,
                opportunities_since_review=counters.opportunities_since_review if counters else 0,
                opportunities_30d=counters.opportunities_30d if counters else 0,
                reached_30d=counters.reached_30d if counters else 0,
                p_reach=round(counters.p_reach, 4) if counters else 0.5,
            )

            target_fen = blunder_row[1] if blunder_row else None

            _emit_served(OpponentMoveMode.GHOST, target_blunder_id is not None)
            _log_slow(
                OpponentMoveMode.GHOST,
                DecisionSource.GHOST_PATH,
                target_blunder_id is not None,
            )
            return NextOpponentMoveResponse(
                mode=OpponentMoveMode.GHOST,
                move=MoveDetails(
                    uci=move.uci(),
                    san=move_san,
                ),
                target_blunder_id=target_blunder_id,
                target_blunder_srs=target_srs,
                target_fen=target_fen,
                decision_source=DecisionSource.GHOST_PATH,
            )
        except (ValueError, chess.IllegalMoveError, chess.InvalidMoveError) as e:
            # If SAN parsing fails, log and fall through to engine fallback
            # This should not happen in normal operation but provides resilience
            logger.warning(
                "Failed to parse ghost SAN move %r for request session_id=%s: %s",
                move_san,
                request.session_id,
                e,
            )

    # Step 2: Backend engine fallback — remote Maia3 API
    try:
        from app.maia3_client import Maia3Error
        from app.opponent_move_controller import choose_move

        # The remote Maia call can stall on network/DNS outside the database.
        # Safe: this fallback path has no pending writes. Release the read
        # transaction/connection before waiting on that external dependency.
        engine_elo = session.engine_elo
        db.rollback()

        engine_started = time.perf_counter()
        controller_move = choose_move(
            fen=request.fen,
            target_elo=engine_elo,
            moves=request.moves,
        )
        engine_ms = _elapsed_ms(engine_started)

        _emit_served(OpponentMoveMode.ENGINE, False)
        _log_slow(OpponentMoveMode.ENGINE, DecisionSource.BACKEND_ENGINE, False)
        return NextOpponentMoveResponse(
            mode=OpponentMoveMode.ENGINE,
            move=MoveDetails(
                uci=controller_move.uci,
                san=controller_move.san,
            ),
            target_blunder_id=None,
            decision_source=DecisionSource.BACKEND_ENGINE,
        )

    except Maia3Error as e:
        if "engine_started" in locals():
            engine_ms = _elapsed_ms(engine_started)
        _log_slow(OpponentMoveMode.ENGINE, DecisionSource.BACKEND_ENGINE, False)
        raise HTTPException(
            status_code=503,
            detail=f"Maia3 API unavailable: {e}",
        )
    except ValueError as e:
        if "engine_started" in locals():
            engine_ms = _elapsed_ms(engine_started)
        _log_slow(OpponentMoveMode.ENGINE, DecisionSource.BACKEND_ENGINE, False)
        raise HTTPException(status_code=400, detail=f"Invalid input: {e}")
