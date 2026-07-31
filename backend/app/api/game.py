import hashlib
import json
import logging
import math
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import NamedTuple, TypeVar

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.accuracy import recompute_session_accuracy
from app.centipawn_loss import centipawn_loss
from app.db import get_db
from app.drill_steering import (
    replay_history_fen,
    route_map_for_target,
    route_preserving_moves,
)
from app.fen import fen_hash, active_color, normalize_fen
from app.models import (
    GameSession,
    OpponentDecision,
    Position,
    RatingHistory,
    User,
    decode_uci_line,
)
from app.opening_baseline_scheduler import enqueue_baseline_snapshot
from app.opening_cache import bump_evidence_seq
from app.opening_densify import routing_view
from app.opening_evidence import session_is_evidence_eligible
from app.opening_graph import get_opening_graph
from app.opening_score_delta import (
    OpeningScoreDeltaItem,
    compute_opening_score_delta,
)
from app.posthog_client import capture
from app.row_locks import for_no_key_update
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
    SEVERITY_NORMALIZER_CP,
    OpportunityCounters,
    detect_opening_family,
    ghost_eligible,
    load_opportunity_counters,
    load_review_counters,
    opening_weight,
)
from app.terminal_row_reconcile import reconcile_terminal_move_rows

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
    # Targeted-session reach rate (g-targeted-reach-rate): the p_reach source.
    # The broad opportunities_30d / reached_30d above stay for urgency and the
    # SRS surfaces; they are no longer the reach denominator.
    targeted_30d: int = 0
    targeted_reached_30d: int = 0
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
        # Severity saturates at the decisive-mistake ceiling (g-no51): every
        # blunder losing >=1000cp is one "decisive mistake" of severity, so a mate
        # pseudo-cp (~10000) cannot dominate scheduling. Only the SEVERITY factor is
        # flattened — urgency, distance, reach, and opening weight still differentiate.
        severity = math.log1p(float(centipawn_loss(self.eval_loss_cp)) / SEVERITY_NORMALIZER_CP)
        distance_weight = math.exp(-DISTANCE_DECAY_RATE * self.depth)
        # Ungated: zero targeted samples give the Laplace 0.5 prior, so a
        # never-steered target no longer outranks every measured one at 1.0.
        reach_weight = (
            compute_p_reach(self.targeted_reached_30d, self.targeted_30d) ** OPPORTUNITY_POWER
        )
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
        # Normalized (g-no51) so mate encoding magnitude cannot re-order two
        # equal-severity candidates; falls through to -blunder_id then first_move.
        -centipawn_loss(candidate.eval_loss_cp),
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


class GhostSelection(NamedTuple):
    """What the ghost search chose, and the evidence it chose it on.

    ``counters`` is carried out rather than re-read by the caller on purpose. Under
    READ COMMITTED a second load_opportunity_counters call is a second snapshot: it
    takes a later clock (drifting the 30-day cutoff) and can observe decisions other
    sessions committed in between. The response payload is frozen at serve time and
    replayed verbatim, so any drift is stored permanently as a snapshot that
    contradicts the score it is supposed to explain. Returning the scored object is
    the only version with no window at all.
    """

    move_san: str | None
    blunder_id: int | None
    last_reviewed_at: datetime | None
    created_at: datetime | None
    counters: OpportunityCounters | None


NO_GHOST = GhostSelection(None, None, None, None, None)


def find_ghost_move(
    db: Session,
    user_id: int,
    fen: str,
    player_color: str,
    *,
    session_id: uuid.UUID | None = None,
    _rng_seed: int | None = None,
) -> GhostSelection:
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
        A GhostSelection. All fields are None when no ghost path exists.
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
        return NO_GHOST

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
        return NO_GHOST

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
        user_id=user_id,
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
            targeted_30d=counters.targeted_30d if counters else 0,
            targeted_reached_30d=counters.targeted_reached_30d if counters else 0,
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
        return NO_GHOST

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
    return GhostSelection(
        chosen_candidate.first_move,
        chosen_candidate.blunder_id,
        chosen_candidate.last_reviewed_at,
        chosen_candidate.created_at,
        opportunity_counters.get(chosen_candidate.blunder_id),
    )


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
    """SRS metadata for the blunder being targeted by a ghost move.

    The opportunity counters are not a fresh read: they are the very
    OpportunityCounters find_ghost_move scored, carried out on GhostSelection. That
    read excludes the serving session, so the decision this payload ships with — and
    every earlier steer in the same game — is outside the counts by construction.
    A payload is frozen at serve time and replayed verbatim, so anything less exact
    (a re-read, even a correctly scoped one) stores a snapshot that can contradict
    the score it is supposed to explain.
    """
    last_reviewed_at: str | None = Field(None, description="ISO timestamp of last review")
    created_at: str | None = Field(None, description="ISO timestamp of when the blunder was first recorded")
    pass_count: int = Field(0, description="Total times passed")
    fail_count: int = Field(0, description="Total times failed")
    pass_streak: int = Field(0, description="Current consecutive pass streak")
    opportunities_since_review: int = Field(0, description="Opportunity events since latest review")
    opportunities_30d: int = Field(0, description="Opportunity events in the last 30 days")
    reached_30d: int = Field(0, description="Exact blunder reaches in the last 30 days")
    targeted_30d: int = Field(
        0,
        description=(
            "Sessions this blunder was steered at in the last 30 days, excluding this one"
        ),
    )
    targeted_reached_30d: int = Field(
        0, description="Those targeted sessions that reached the blunder"
    )
    p_reach: float = Field(
        0.5,
        description=(
            "Smoothed 30-day reach probability over targeted sessions, as this "
            "target was scored"
        ),
    )


class DrillRouteMetadata(BaseModel):
    status: str
    target_fen: str
    resulting_fen: str
    plies_to_target: int
    reaches_root: bool = False


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
    decision_id: uuid.UUID | None = Field(
        None,
        description=(
            "Opaque id of the opponent_decisions row that recorded this decision. "
            "Optional at the model level ONLY so the pre-stamp intermediate object is "
            "constructible — every SERVED response carries one: a fresh decision gets "
            "it from _record_decision before serializing, and a replay reads it out of "
            "a stored payload that always had it. Root confirmation sends it back."
        ),
    )


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
    session = for_no_key_update(
        db.query(GameSession).filter(GameSession.id == request.session_id)
    ).first()

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

    # Update session. The opening-evidence counter bumps ONLY when this write
    # flips SESSION_EVIDENCE_ELIGIBLE_SQL's truth value (g-jact): ending a session
    # makes its already-uploaded moves digest-visible in one transition.
    was_evidence_eligible = session_is_evidence_eligible(session)
    session.status = "ended"
    session.result = request.result.value
    session.ended_at = utcnow()
    session.pgn = request.pgn
    effective_is_rated = session.is_rated if session.session_mode == DRILL_SESSION_MODE else request.is_rated
    session.is_rated = effective_is_rated
    # Terminal row reconcile (g-short-move-rows): the PGN above is persisted
    # verbatim while move rows travel in separate /moves transactions, so a
    # final upload that never committed would otherwise end the session with
    # fewer stored rows than its own PGN describes. Derive every verified
    # missing coordinate — a tail or an interior hole left by resolved-only
    # incremental uploads — from the PGN inside THIS transaction (evals NULL; a
    # late /moves upsert overwrites derived rows with the client's richer
    # record). The historical backfill keeps the stricter prefix-only default;
    # sparse completion is safe here because this session is still active and
    # locked for terminal finalization. The flush is load-bearing: autoflush is
    # off, and recompute's scoped SELECT below must see the staged rows.
    row_reconcile = reconcile_terminal_move_rows(
        db, session, allow_sparse=True
    )
    if row_reconcile.derived_rows:
        # Durable marker: after derivation the row grid alone can't distinguish
        # a reconciled session from ordinary unresolved uploads, and the
        # post-commit capture() below is fire-and-forget. No receipt is written.
        # Compatibility name from g-short-move-rows: the count now includes
        # PGN-derived interior coordinates as well as a derived tail.
        session.derived_tail_rows = row_reconcile.derived_rows
        db.flush()
    # Cached-accuracy recompute (g-accuracy-hooks) runs HERE — after the terminal
    # mutation above and before the users lock below — so its dirty accuracy
    # assignment drains in the same pre-cursor flush() as the terminal/rating
    # writes, and precedes the users lock to shorten the rating serialization
    # window. It reads committed moves plus the dirty in-memory status/PGN and
    # does not depend on ended_at/rating state; the population guard makes it a
    # no-op for ended failed/abandoned drills (which never reach this handler).
    # The reconcile's expected-ply verdict rides along so the SAME PGN is never
    # parsed a second time — and a size-refused PGN (expected_plies None) is
    # parsed zero times: accuracy fails closed on the propagated refusal.
    recompute_session_accuracy(
        db, session, expected_total_moves=row_reconcile.expected_plies
    )
    # Compute rating change for rated results
    rating_change = None
    if effective_is_rated and request.result.value in RESULT_SCORES:
        # Serialize this user's rated game-end chain (g-rating-serial). Lock the
        # users row FOR NO KEY UPDATE BEFORE reading the durable rating head so
        # concurrent rated ends for the same user — even across distinct sessions —
        # cannot both read the same head and insert the same games_played. The
        # loser blocks on this lock until the winner commits, then reads the
        # winner's row and chains games_played += 1 off it. A missing users row is
        # an invariant violation (a valid token always has a backing users row):
        # fail closed with 500 and persist no rating rather than orphan a row.
        if for_no_key_update(
            db.query(User.id).filter(User.id == user.user_id)
        ).one_or_none() is None:
            raise HTTPException(status_code=500, detail="User record not found")
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

    db.flush()
    if session_is_evidence_eligible(session) != was_evidence_eligible:
        bump_evidence_seq(db, user.user_id, session.player_color)
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
            # The reconcile's parse of the same PGN — no reparse here, and None
            # when the size ceiling refused to parse at all.
            "ply_count": row_reconcile.expected_plies,
            # g-short-move-rows: the reconcile verdict. Best-effort only —
            # capture() may drop; the durable recurrence record is the
            # game_sessions.derived_tail_rows column stamped above.
            "row_reconcile_outcome": row_reconcile.outcome,
            "stored_move_rows": row_reconcile.stored_rows,
            "derived_tail_rows": row_reconcile.derived_rows,
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


# Version prefix on the fingerprint input so the definition can change later without
# silently colliding with rows written under the old one.
DECISION_FINGERPRINT_SCHEME = "v1"


def _encode_uci_history(moves: list[str]) -> str:
    """Store the history as canonical JSON, NOT ``encode_uci_line``'s space-join.

    The repo's space-joined form (``drill_line``) is lossy over what this endpoint
    accepts: ``NextOpponentMoveRequest.moves`` is ``list[str]`` with no per-element
    validation, so ``["e2e4 e7e5", "g1f3"]`` and ``["e2e4", "e7e5 g1f3"]`` would store
    identical text — and, having the same length, ``ply_before`` would not tell them
    apart either. The column is documented as the FULL UCI history, so it stores the
    array the client actually sent. JSON-as-string follows the repo convention for
    structured Text columns and round-trips through ``json.loads``.

    Compact separators pin one canonical rendering; ``[]`` is a real empty history,
    never NULL.
    """
    return json.dumps(moves, separators=(",", ":"))


def _decision_fingerprint(normalized_fen: str, moves: list[str]) -> str:
    """Replay key over the whole request INPUT, not just the position.

    History is included because Maia consumes ``NextOpponentMoveRequest.moves``, and
    two transpositions can share a FEN and a ply while being different request inputs.
    It is also what makes a post-revert branch — same session, same ply, truncated
    history — a NEW decision rather than a conflict against the pre-revert row.

    The encoding MUST be injective, because a collision lets one request replay a
    different request's decision. Every field is therefore netstring-framed
    (``<len>:<field>``), which is self-delimiting: a decoder reads digits up to ``:``
    and then exactly that many characters, so no field's CONTENT can be mistaken for
    a separator and no field boundary can shift.

    Delimiter-joining was tried first and is not sufficient:

    * ``" ".join(moves)`` is DEMONSTRABLY not injective. ``NextOpponentMoveRequest.moves``
      is ``list[str]`` with no per-element validation, so ``["e2e4 e7e5"]`` and
      ``["e2e4", "e7e5"]`` — two distinct JSON arrays, forwarded verbatim to Maia as
      different inputs — joined to the same string. So did ``[""]`` and ``[]``. This
      is a real replay hazard and the reason for the change.
    * ``"\\n"`` was an unsound field separator, though no exploit is known.
      ``normalize_fen`` splits on ``" "`` while ``chess.Board`` splits on ANY
      whitespace, so a newline-carrying FEN parses and survives normalization — the
      FEN field is not newline-free. A boundary SHIFT additionally needs one
      normalized output to be a ``"\\n"``-terminated prefix of another, which
      ``normalize_fen`` currently prevents by unconditionally overwriting ``parts[3]``
      with ``"-"`` or a square name. Framing is defensive here: it stops that
      non-obvious invariant from being load-bearing.

    Per-element UCI validation would also close the first hole, but it would narrow
    what the endpoint accepts — Maia owns that contract today. Framing closes it
    without changing the accepted input surface.
    """
    payload = "".join(
        f"{len(field)}:{field}"
        for field in (DECISION_FINGERPRINT_SCHEME, normalized_fen, *moves)
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _replay_decision(
    db: Session, session_id: uuid.UUID, request_fingerprint: str
) -> NextOpponentMoveResponse | None:
    """Return the stored response for an already-recorded request, else None.

    Replay, NOT recompute: the payload is deserialized verbatim, with no business
    logic and no re-query of mutable state. That is the whole point of storing the
    serialized response — ``target_blunder_srs`` snapshots counters that move between
    the original request and its retry, so a reconstruction would answer a retry with
    a different response than the one first served.
    """
    # Typed columns rather than raw SQL: the UUID storage form differs between
    # Postgres (native uuid) and the SQLite test dialect, and a hand-bound string
    # would silently never match on one of them.
    payload = db.execute(
        select(OpponentDecision.response_payload).where(
            OpponentDecision.session_id == session_id,
            OpponentDecision.request_fingerprint == request_fingerprint,
        )
    ).scalar()
    if payload is None:
        return None
    return NextOpponentMoveResponse.model_validate_json(payload)


def _record_decision(
    db: Session,
    *,
    session_id: uuid.UUID,
    request_fingerprint: str,
    request_fen_hash: str,
    uci_history: str | None,
    ply_before: int,
    response: NextOpponentMoveResponse,
    resulting_fen: str | None,
) -> tuple[NextOpponentMoveResponse, bool]:
    """Persist a freshly computed decision and return the response to actually serve.

    Returns ``(response, was_replayed)``. ``was_replayed`` is True when this caller
    LOST an insert race and is serving the winner's stored payload instead of its own
    computed move.

    Concurrency is arbitrated by ``uq_opponent_decisions_session_fingerprint``, not by
    a lock: the normal ghost/engine path holds none (the pre-root branch rolls the
    drill lock back before falling through), so two concurrent identical requests can
    both miss the replay lookup and both compute — and ``find_ghost_move`` is
    randomized, so their moves need not agree.

    The loser MUST discard its own move. Serving it would serve a move that no stored
    decision records, and root confirmation would then fail its ``resulting_fen``
    check against the winner's row. Returning the winner's payload also returns the
    winner's ``decision_id``, so the id a client later confirms always names a
    committed row.

    This function commits. A response served from an uncommitted decision is one the
    retry cannot replay. A write failure propagates rather than being swallowed: the
    endpoint fails closed instead of serving a move it did not record, because an
    unrecorded served target silently drops a FAILED steer from the p_reach
    denominator.
    """
    decision_id = uuid.uuid4()
    # Allocate BEFORE serializing so response_payload carries the decision_id of the
    # row it is stored in. A database-default id is unknown until after the INSERT,
    # forcing either a post-insert payload rewrite or a self-contradicting payload.
    stamped = response.model_copy(update={"decision_id": decision_id})

    values = {
        "decision_id": decision_id,
        "session_id": session_id,
        "request_fingerprint": request_fingerprint,
        "request_fen_hash": request_fen_hash,
        "uci_history": uci_history,
        "ply_before": ply_before,
        "served_at": datetime.now(timezone.utc),
        "response_payload": stamped.model_dump_json(),
        "target_blunder_id": stamped.target_blunder_id,
        "resulting_fen": resulting_fen,
        # Read off the response, per the envelope rule — never recomputed here.
        # Reads the explicit boolean, NOT the status string: the served status is
        # `root_pending` (the client must confirm before anything is root-reached),
        # so a status comparison here would silently record FALSE for exactly the
        # decisions confirmation is about. Pinned by
        # test_reaches_drill_root_agrees_with_geometry.
        "reaches_drill_root": (
            stamped.drill_route is not None and stamped.drill_route.reaches_root
        ),
    }

    dialect_name = db.bind.dialect.name if db.bind else ""
    if dialect_name in ("sqlite", "postgresql"):
        insert = sqlite_insert if dialect_name == "sqlite" else postgresql_insert
        stmt = (
            insert(OpponentDecision)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    OpponentDecision.session_id,
                    OpponentDecision.request_fingerprint,
                ]
            )
            .returning(OpponentDecision.decision_id)
        )
        won = db.execute(stmt).first() is not None
    else:
        # Generic-dialect fallback: a plain insert, with the constraint violation as
        # the loss signal. The savepoint keeps the surrounding transaction usable
        # after the rollback.
        try:
            with db.begin_nested():
                db.execute(OpponentDecision.__table__.insert().values(**values))
            won = True
        except IntegrityError:
            won = False

    if not won:
        # Lost. Commit to release whatever this transaction still holds — the route
        # branch's drill row lock — before reading the winner's row. The route branch
        # no longer writes drill state here, so there is nothing of ours to clobber.
        db.commit()
        winner = _replay_decision(db, session_id, request_fingerprint)
        if winner is None:
            # Unreachable on Postgres: ON CONFLICT DO NOTHING blocks on a speculative
            # insert and only reports a conflict once the winner COMMITTED (an aborted
            # winner lets our insert proceed). Fail closed rather than serve a move
            # that no decision records.
            logger.error(
                "opponent decision insert lost but no winner row is visible "
                "session_id=%s fingerprint=%s",
                session_id,
                request_fingerprint,
            )
            raise HTTPException(
                status_code=503,
                detail="Opponent decision could not be recorded; retry the request",
            )
        return winner, True

    db.commit()
    return stamped, False


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

    def _emit_served(
        mode: OpponentMoveMode, has_target_blunder: bool, *, replayed: bool = False
    ) -> None:
        capture(
            str(user.user_id),
            "opponent_move_served",
            {
                "decision_source": mode.value,
                "has_target_blunder": has_target_blunder,
                # Additive property: existing dashboards keep working, and served
                # counts can now exclude the retry replays that fold three requests
                # into one decision.
                "replayed": replayed,
            },
        )

    def _serve(
        served: NextOpponentMoveResponse, replayed: bool
    ) -> NextOpponentMoveResponse:
        has_target = served.target_blunder_id is not None
        _emit_served(served.mode, has_target, replayed=replayed)
        _log_slow(served.mode, served.decision_source, has_target)
        return served

    # Fetch and validate session
    session = db.query(GameSession).filter(GameSession.id == request.session_id).first()

    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")

    if session.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this game")
    # player_color and engine_elo are fixed at session creation. Copy them now so
    # the normal ghost/engine path never re-reads the row — a pre-root drill lock
    # (below) is released via rollback before that path runs, and these copies keep
    # the fall-through independent of the row's post-rollback expired state.
    player_color = session.player_color
    engine_elo = session.engine_elo
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

    if position_color == player_color:
        raise HTTPException(
            status_code=400,
            detail="Cannot get opponent move when it's the player's turn",
        )

    try:
        normalized_request_fen = normalize_fen(request.fen)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid FEN: {e}")
    request_fingerprint = _decision_fingerprint(normalized_request_fen, request.moves)

    # Replay before dispatching to any decision branch. getNextOpponentMove sets
    # retries: 2, so one decision can arrive as three requests; replaying makes the
    # endpoint idempotent at the source instead of deduplicating downstream.
    #
    # Placement is load-bearing on both sides. AFTER the drill-state guard, so the
    # terminal states keep answering 400 exactly as before. BEFORE the pre-root drill
    # branch, so a retry is answered byte-identically — same move, same decision_id,
    # same target_blunder_srs snapshot — instead of recomputing. The retry must serve
    # the move the stored decision records, because that row is what a later root
    # confirmation validates the applied position against.
    replayed = _replay_decision(db, request.session_id, request_fingerprint)
    if replayed is not None:
        return _serve(replayed, True)

    if is_active_preroot_drill:
        # The entry snapshot said active pre-root — a branch that mutates drill
        # state. Lock and refresh the row immediately, then re-derive from current
        # state: a concurrent request may have converted the drill or reached root
        # since the unlocked read.
        session = for_no_key_update(
            db.query(GameSession).filter(GameSession.id == request.session_id)
        ).first()
        if session is None:
            raise HTTPException(status_code=404, detail="Game session not found")

        if session.drill_state in {"failed", "abandoned"}:
            # A terminal transition raced ahead: the existing entry 400.
            raise HTTPException(status_code=400, detail="Opponent moves are unavailable for this drill state")

        if session.drill_state != "active":
            # converted or root_reached: the normal ghost/engine path applies. Its
            # required immutable values (player_color, engine_elo) were copied at
            # entry, so release the lock here and fall through WITHOUT returning —
            # no row lock may survive into ghost or engine computation.
            db.rollback()
        else:
            if not session.drill_opening_key:
                raise HTTPException(status_code=400, detail="Drill is missing an opening root")
            # PROVE the history before recording its length. ply_before is
            # len(request.moves), and drill root confirmation treats that number as
            # authoritative evidence for the session's evidence boundary — so an
            # unverified history is a client-controlled boundary. A legitimate on-route
            # FEN paired with a truncated history would otherwise be served the real
            # route move and then confirm a boundary several plies too low, readmitting
            # the scripted prefix the boundary exists to exclude.
            #
            # Scoped to the pre-root drill branch on purpose. NOT because confirmation
            # only ever reads rows written here — it reads any decision in the session,
            # including ghost/engine rows written after the root, which can carry
            # resulting_fen == the target by repetition (see
            # test_post_root_ghost_decision_cannot_stamp_a_low_boundary). The scoping is
            # safe because confirmation re-proves every row it reads against that row's
            # own uci_history/request_fen_hash and declines to stamp what it cannot prove;
            # this check only makes the pre-root rows provable in the first place, where
            # the proof is complete (every drill starts from the standard position) and
            # the ghost/engine path deliberately forwards `moves` to Maia verbatim with
            # no per-element validation.
            if replay_history_fen(request.moves) != normalized_request_fen:
                raise HTTPException(
                    status_code=400,
                    detail="Move history does not reproduce the requested position",
                )
            routing = routing_view(get_opening_graph())
            route_map = route_map_for_target(
                routing, session.drill_opening_key, decode_uci_line(session.drill_line)
            )
            if not route_map.plies_by_fen:
                raise HTTPException(status_code=400, detail="Drill route is unavailable")
            if route_map.is_target(request.fen):
                # The request position IS the root: client-OBSERVED, not merely
                # served, so this branch does transition — and stamps the boundary
                # write-once. len(request.moves) is proven here: replay_history_fen
                # above already required the history to reproduce this FEN, and this
                # FEN is the target. We hold the row lock. Under the client-side
                # confirmation barrier this should be unreachable for current
                # clients; it remains the fallback for legacy and lost-confirmation
                # arrivals.
                session.drill_state = "root_reached"
                if session.drill_root_reached_ply is None:
                    session.drill_root_reached_ply = len(request.moves)
                db.commit()
                raise HTTPException(status_code=400, detail="Drill root already reached")
            suggestions = route_preserving_moves(routing, route_map, request.fen)
            if not suggestions:
                raise HTTPException(status_code=400, detail="Current drill position is off route")

            move = suggestions[0]
            # Serving is NOT a transition. A root-reaching route move is served as
            # `root_pending` and mutates no drill state; the client applies it and
            # then confirms the resulting position via /api/drills/{id}/route-check,
            # which writes drill_state and the evidence boundary together. A boundary
            # derived from a serve is a boundary a lost response can fabricate.
            reaches_root = move.resulting_fen == route_map.target_fen

            route_response = NextOpponentMoveResponse(
                mode=OpponentMoveMode.GHOST,
                move=MoveDetails(uci=move.uci, san=move.san),
                target_blunder_id=None,
                target_blunder_srs=None,
                target_fen=route_map.target_fen,
                decision_source=DecisionSource.GHOST_PATH,
                drill_route=DrillRouteMetadata(
                    status="root_pending" if reaches_root else "on_route",
                    target_fen=route_map.target_fen,
                    resulting_fen=move.resulting_fen,
                    plies_to_target=move.plies_to_target,
                    reaches_root=reaches_root,
                ),
            )
            # _record_decision's commit is this branch's transaction sink and lock
            # release, exactly as the bare db.commit() it replaces. The branch writes
            # no drill state of its own any more, but the decision row it commits is
            # the evidence a later confirmation validates against, so it must still
            # land before the response is served — and the lock must not outlive it
            # (the route response does no ghost/engine work).
            served, was_replayed = _record_decision(
                db,
                session_id=request.session_id,
                request_fingerprint=request_fingerprint,
                request_fen_hash=fen_hash(request.fen),
                uci_history=_encode_uci_history(request.moves),
                ply_before=len(request.moves),
                response=route_response,
                resulting_fen=move.resulting_fen,
            )
            return _serve(served, was_replayed)

    # Step 1: Ghost-first path traversal
    # Use shared ghost path traversal logic to find moves toward due blunders
    ghost_search_started = time.perf_counter()
    (
        move_san,
        target_blunder_id,
        blunder_last_reviewed,
        blunder_created_at,
        ghost_counters,
    ) = find_ghost_move(
        db=db,
        user_id=user.user_id,
        fen=request.fen,
        player_color=player_color,
        session_id=request.session_id,
    )
    ghost_search_ms = _elapsed_ms(ghost_search_started)

    # If ghost path exists, convert SAN to both UCI and SAN formats
    ghost_response: NextOpponentMoveResponse | None = None
    ghost_resulting_fen: str | None = None
    if move_san is not None:
        import chess
        try:
            board = chess.Board(request.fen)
            # Parse SAN to get the move object
            move = board.parse_san(move_san)
            # parse_san already proved the move legal here, so pushing it onto a copy
            # is a free, exact resulting FEN.
            resulting_board = board.copy(stack=False)
            resulting_board.push(move)

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
            # THE object find_ghost_move scored, carried out of the search rather
            # than re-queried here. A second read would be a second READ COMMITTED
            # snapshot — later clock, so a drifted 30-day cutoff, and visibility of
            # decisions other sessions committed in between — and this payload is
            # frozen and replayed verbatim, so any drift would be stored forever as
            # a snapshot contradicting the score it explains.
            counters = ghost_counters if target_blunder_id else None

            target_srs = TargetBlunderSrs(
                last_reviewed_at=_isoformat_optional(blunder_last_reviewed),
                created_at=_isoformat_optional(blunder_created_at),
                pass_count=review_counter.pass_count if review_counter else 0,
                fail_count=review_counter.fail_count if review_counter else 0,
                pass_streak=blunder_row[0] if blunder_row else 0,
                opportunities_since_review=counters.opportunities_since_review if counters else 0,
                opportunities_30d=counters.opportunities_30d if counters else 0,
                reached_30d=counters.reached_30d if counters else 0,
                targeted_30d=counters.targeted_30d if counters else 0,
                targeted_reached_30d=counters.targeted_reached_30d if counters else 0,
                p_reach=round(counters.p_reach, 4) if counters else 0.5,
            )

            target_fen = blunder_row[1] if blunder_row else None

            ghost_response = NextOpponentMoveResponse(
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
            ghost_resulting_fen = resulting_board.fen()
        except (ValueError, chess.IllegalMoveError, chess.InvalidMoveError) as e:
            # If SAN parsing fails, log and fall through to engine fallback
            # This should not happen in normal operation but provides resilience
            logger.warning(
                "Failed to parse ghost SAN move %r for request session_id=%s: %s",
                move_san,
                request.session_id,
                e,
            )

    if ghost_response is not None:
        # Recorded OUTSIDE the except above on purpose: in there, a serialization or
        # database error would be swallowed as "SAN parsing failed" and fall through
        # to the engine, serving a move no decision records.
        served, was_replayed = _record_decision(
            db,
            session_id=request.session_id,
            request_fingerprint=request_fingerprint,
            request_fen_hash=fen_hash(request.fen),
            uci_history=_encode_uci_history(request.moves),
            ply_before=len(request.moves),
            response=ghost_response,
            resulting_fen=ghost_resulting_fen,
        )
        return _serve(served, was_replayed)

    # Step 2: Backend engine fallback — remote Maia3 API
    try:
        from app.maia3_client import Maia3Error
        from app.opponent_move_controller import choose_move

        # The remote Maia call can stall on network/DNS outside the database.
        # Safe: this fallback path has no pending writes. Release the read
        # transaction/connection before waiting on that external dependency.
        # engine_elo was copied at entry (immutable), so no row read is needed here.
        db.rollback()

        engine_started = time.perf_counter()
        controller_move = choose_move(
            fen=request.fen,
            target_elo=engine_elo,
            moves=request.moves,
        )
        engine_ms = _elapsed_ms(engine_started)

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

    # Recorded OUTSIDE the try above on purpose: in there, a database error would be
    # caught by `except ValueError` or shadowed by the Maia mapping and answered as
    # 400/503 with a move already chosen. Out here it propagates, so the endpoint
    # fails closed rather than serving a move no decision records.
    import chess

    engine_resulting_fen: str | None = None
    try:
        engine_board = chess.Board(request.fen)
        engine_board.push_uci(controller_move.uci)
        engine_resulting_fen = engine_board.fen()
    except (ValueError, chess.IllegalMoveError, chess.InvalidMoveError) as e:
        # Nothing validates the controller's move today, and this record must not
        # start rejecting engine responses. Engine decisions are never root decisions,
        # so no consumer reads this NULL.
        logger.warning(
            "Failed to apply engine move %r for request session_id=%s: %s",
            controller_move.uci,
            request.session_id,
            e,
        )

    served, was_replayed = _record_decision(
        db,
        session_id=request.session_id,
        request_fingerprint=request_fingerprint,
        request_fen_hash=fen_hash(request.fen),
        uci_history=_encode_uci_history(request.moves),
        ply_before=len(request.moves),
        response=NextOpponentMoveResponse(
            mode=OpponentMoveMode.ENGINE,
            move=MoveDetails(
                uci=controller_move.uci,
                san=controller_move.san,
            ),
            target_blunder_id=None,
            decision_source=DecisionSource.BACKEND_ENGINE,
        ),
        resulting_fen=engine_resulting_fen,
    )
    return _serve(served, was_replayed)
