from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

import chess
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.openings import MAX_TREE_PLY
from app.db import get_db
from app.drill_steering import (
    DrillRouteMap,
    DrillRouteMove,
    route_map_for_target,
    route_move_for_uci,
    route_preserving_moves,
    safe_san_for_uci,
)
from app.fen import normalize_fen
from app.models import GameSession, decode_uci_line, encode_uci_line
from app.opening_baseline_scheduler import enqueue_baseline_snapshot
from app.opening_cache import bump_evidence_seq
from app.opening_densify import routing_view
from app.opening_evidence import session_is_evidence_eligible
from app.opening_graph import get_opening_graph
from app.opening_roots import derive_family, get_opening_roots
from app.opening_score_delta import (
    OpeningScoreDeltaItem,
    compute_opening_score_delta,
)
from app.posthog_client import capture
from app.row_locks import for_no_key_update
from app.security import TokenPayload, get_current_user
from app.session_contracts import (
    DRILL_SESSION_MODE,
    resegment_session_moves,
    utcnow,
)

router = APIRouter(prefix="/api/drills", tags=["drills"])


class PlayerColor(str, Enum):
    WHITE = "white"
    BLACK = "black"


class DrillStrictness(str, Enum):
    LENIENT = "lenient"
    STANDARD = "standard"
    STRICT = "strict"


class DrillStartRequest(BaseModel):
    # opening_key is a registered root key OR an ad-hoc target FEN. For ad-hoc
    # card drills, `line` carries the full UCI line from the start position to
    # that target; the backend validates it by replay and persists it.
    opening_key: str = Field(..., min_length=1)
    player_color: PlayerColor
    engine_elo: int
    strictness: DrillStrictness
    strictness_cp: int | None = Field(None, ge=0, le=50)
    line: list[str] | None = None


class DrillContinueRequest(BaseModel):
    current_ply: int = Field(..., ge=0)


class DrillFailRequest(BaseModel):
    terminal_reason: str


class DrillRouteCheckRequest(BaseModel):
    current_fen: str = Field(..., min_length=1)
    previous_fen: str | None = Field(None, min_length=1)
    played_uci: str | None = Field(None, min_length=4, max_length=5)


class DrillRouteSuggestion(BaseModel):
    uci: str
    san: str
    resulting_fen: str
    plies_to_target: int


class DrillRouteFailure(BaseModel):
    played_move_uci: str | None = None
    played_move_san: str | None = None
    correction_fen: str
    reason: str | None = None


class DrillRouteCheckResponse(BaseModel):
    status: str
    current_fen: str
    target_fen: str
    suggestions: list[DrillRouteSuggestion]
    failure: DrillRouteFailure | None = None


class DrillSessionContract(BaseModel):
    session_id: uuid.UUID
    mode: str
    drill_state: str
    opening_key: str
    opening_name: str
    opening_family: str
    eco: str | None
    depth: int
    player_color: str
    engine_elo: int
    strictness: str
    strictness_cp: int | None = None
    is_rated: bool
    rated_start_ply: int | None
    normal_started_at: datetime | None
    converted_at: datetime | None
    terminal_reason: str | None = None
    # Played-opening score deltas (before -> after) vs the drill baseline, set
    # only by the terminal drill endpoints (natural-end, accuracy fail). None for
    # start/get/continue/abandon, which don't recompute.
    opening_score_changes: list[OpeningScoreDeltaItem] | None = None


def _get_drill_or_404(db: Session, session_id: uuid.UUID) -> GameSession:
    session = db.query(GameSession).filter(GameSession.id == session_id).first()
    if session is None:
        raise HTTPException(status_code=404, detail="Drill session not found")
    return session


def _get_drill_for_update(db: Session, session_id: uuid.UUID) -> GameSession:
    session = for_no_key_update(
        db.query(GameSession).filter(GameSession.id == session_id)
    ).first()
    if session is None:
        raise HTTPException(status_code=404, detail="Drill session not found")
    return session


def _ensure_owner(session: GameSession, user: TokenPayload) -> None:
    if session.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this drill")


def _suggestion(move: DrillRouteMove) -> DrillRouteSuggestion:
    return DrillRouteSuggestion(
        uci=move.uci,
        san=move.san,
        resulting_fen=move.resulting_fen,
        plies_to_target=move.plies_to_target,
    )


def _root_reached_response(
    current_fen: str, route_map: DrillRouteMap
) -> DrillRouteCheckResponse:
    return DrillRouteCheckResponse(
        status="root_reached",
        current_fen=current_fen,
        target_fen=route_map.target_fen,
        suggestions=[],
    )


def _refreshed_route_guard(
    session: GameSession, current_fen: str, route_map: DrillRouteMap
) -> DrillRouteCheckResponse | None:
    """Re-derive a route-check mutating branch from refreshed, locked state.

    Called only after ``_get_drill_for_update`` re-reads the session under the NKU
    lock, so ``session`` reflects any transition a concurrent request committed
    since the unlocked entry snapshot. The FEN geometry the caller already computed
    is immutable, so re-deriving the branch is purely a re-check of drill state:

    * still active pre-root -> ``None``; the caller performs its intended write;
    * already root-reached -> the root-reached response, sent WITHOUT writing;
    * failed/abandoned/converted/non-active -> the existing entry 400.
    """
    if session.status != "active":
        raise HTTPException(status_code=400, detail="Drill session is not active")
    if session.drill_state in {"failed", "abandoned", "converted"}:
        raise HTTPException(
            status_code=400,
            detail="Drill route cannot be checked from its current state",
        )
    if session.drill_state == "root_reached":
        return _root_reached_response(current_fen, route_map)
    return None


def _contract(
    session: GameSession,
    opening_score_changes: list[OpeningScoreDeltaItem] | None = None,
) -> DrillSessionContract:
    if session.session_mode != DRILL_SESSION_MODE or not session.drill_opening_key:
        raise HTTPException(status_code=400, detail="Session is not a drill")

    root = get_opening_roots().get_root(session.drill_opening_key)
    if root is not None:
        opening_key = root.opening_key
        opening_name = root.opening_name
        opening_family = root.opening_family
        eco = root.eco
        depth = root.depth
    else:
        # Ad-hoc card drill: every such session carries its played line, so the
        # metadata is synthesized to match the card — deepest named book node
        # along the line (the same inheritance /openings uses), depth = line
        # length. No graph-node-only fallback: an ad-hoc session always has a
        # line, and a line is what makes the inherited name resolvable.
        line = decode_uci_line(session.drill_line)
        if not line:
            raise HTTPException(status_code=404, detail="Unknown opening root")
        graph = get_opening_graph()
        board = chess.Board()
        name: str | None = None
        eco = None
        node = graph.get_node(normalize_fen(board.fen()))
        if node is not None and node.name is not None:
            name, eco = node.name, node.eco
        for uci in line:
            board.push(chess.Move.from_uci(uci))
            node = graph.get_node(normalize_fen(board.fen()))
            if node is not None and node.name is not None:
                name, eco = node.name, node.eco
        # Set the fallback name BEFORE deriving family so derive_family is never
        # called on None.
        opening_name = name or "Custom line"
        opening_family = derive_family(opening_name)
        opening_key = session.drill_opening_key  # already-normalized target FEN
        depth = len(line)

    return DrillSessionContract(
        session_id=session.id,
        mode=session.session_mode,
        drill_state=session.drill_state or "active",
        opening_key=opening_key,
        opening_name=opening_name,
        opening_family=opening_family,
        eco=eco,
        depth=depth,
        player_color=session.player_color,
        engine_elo=session.engine_elo,
        strictness=session.drill_strictness or "standard",
        strictness_cp=session.drill_strictness_cp,
        is_rated=session.is_rated,
        rated_start_ply=session.rated_start_ply,
        normal_started_at=session.normal_started_at,
        converted_at=session.converted_at,
        terminal_reason=session.drill_terminal_reason,
        opening_score_changes=opening_score_changes,
    )


@router.post("/start", response_model=DrillSessionContract, status_code=201)
def start_drill(
    request: DrillStartRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> DrillSessionContract:
    root = get_opening_roots().get_root(request.opening_key)
    if root is not None:
        # Registered-root drill keeps legacy behavior: the target is the root
        # key and the line (if any) is ignored — routing uses the book BFS.
        drill_opening_key = request.opening_key
        drill_line: str | None = None
    else:
        # Ad-hoc card drill: a full UCI line to the target FEN is required and
        # validated by replay (legality + reaches the claimed position). Every
        # failure maps to a controlled 4xx, never a 500.
        if not request.line:
            raise HTTPException(status_code=404, detail="Unknown opening root")
        if len(request.line) > MAX_TREE_PLY:
            raise HTTPException(status_code=422, detail="Drill line is too long")
        board = chess.Board()
        final_fen = normalize_fen(board.fen())
        seen_fens = {final_fen}
        for uci in request.line:
            try:
                move = chess.Move.from_uci(uci)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422, detail=f"Invalid move in drill line: {uci}"
                ) from exc
            if move not in board.legal_moves:
                raise HTTPException(
                    status_code=422, detail=f"Illegal move in drill line: {uci}"
                )
            board.push(move)
            final_fen = normalize_fen(board.fen())
            # The strict line route map keys by normalized FEN and is_target is
            # FEN-only, so a line that revisits a position is ambiguous (it could
            # report the target reached early or suggest the wrong continuation).
            # Reject it outright — real opening lines never transpose onto
            # themselves; keeping the map unambiguous keeps routing strict.
            if final_fen in seen_fens:
                raise HTTPException(
                    status_code=422, detail="Drill line revisits a position"
                )
            seen_fens.add(final_fen)
        try:
            normalized_target = normalize_fen(request.opening_key)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid target position") from exc
        if final_fen != normalized_target:
            raise HTTPException(
                status_code=422, detail="Drill line does not reach the target position"
            )
        drill_opening_key = normalized_target
        drill_line = encode_uci_line(request.line)

    session = GameSession(
        id=uuid.uuid4(),
        user_id=user.user_id,
        started_at=utcnow(),
        status="active",
        engine_elo=request.engine_elo,
        blunder_recorded=False,
        is_rated=False,
        player_color=request.player_color.value,
        session_mode=DRILL_SESSION_MODE,
        drill_state="active",
        drill_opening_key=drill_opening_key,
        drill_line=drill_line,
        drill_strictness=request.strictness.value,
        drill_strictness_cp=request.strictness_cp,
        opening_score_baseline=None,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    # Capture the opening-score baseline OFF the request thread (g-mxeo) — mirrors
    # the game-start path. The worker fills it only when the pre-session batch is
    # provably fresh and dated strictly before ``started_at``; otherwise it stays
    # NULL and the end-of-drill delta degrades to "no delta". Best-effort.
    enqueue_baseline_snapshot(session.id, user.user_id, request.player_color.value)
    contract = _contract(session)
    capture(
        str(user.user_id),
        "drill_started",
        {
            "opening_key": contract.opening_key,
            "family": contract.opening_family,
            "eco": contract.eco,
            "player_color": contract.player_color,
            "engine_elo": contract.engine_elo,
            "strictness": contract.strictness,
        },
    )
    return contract


@router.get("/{session_id}", response_model=DrillSessionContract)
def get_drill(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> DrillSessionContract:
    session = _get_drill_or_404(db, session_id)
    _ensure_owner(session, user)
    return _contract(session)


@router.post("/{session_id}/fail", response_model=DrillSessionContract)
def fail_drill(
    session_id: uuid.UUID,
    request: DrillFailRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> DrillSessionContract:
    session = _get_drill_for_update(db, session_id)
    _ensure_owner(session, user)
    if session.session_mode != DRILL_SESSION_MODE:
        raise HTTPException(status_code=400, detail="Session is not a drill")
    if request.terminal_reason != "accuracy":
        raise HTTPException(status_code=422, detail="terminal_reason must be accuracy")
    if session.status != "active" or session.drill_state != "root_reached":
        raise HTTPException(status_code=400, detail="Drill cannot be failed from its current state")
    # Accuracy-fail flips SESSION_EVIDENCE_ELIGIBLE_SQL false->true without any
    # timestamp write, so the opening-evidence counter carries the change (g-jact).
    was_evidence_eligible = session_is_evidence_eligible(session)
    session.drill_state = "failed"
    session.drill_terminal_reason = "accuracy"
    db.flush()
    if session_is_evidence_eligible(session) != was_evidence_eligible:
        bump_evidence_seq(db, user.user_id, session.player_color)
    db.commit()
    db.refresh(session)
    capture(str(user.user_id), "drill_failed", {"reason": session.drill_terminal_reason})
    # The opening root was reached before the accuracy slip, so the played chain
    # carries a meaningful delta to surface in the stopped-drill banner.
    return _contract(session, opening_score_changes=compute_opening_score_delta(db, session) or None)


@router.post("/{session_id}/continue", response_model=DrillSessionContract)
def continue_drill(
    session_id: uuid.UUID,
    request: DrillContinueRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> DrillSessionContract:
    session = _get_drill_for_update(db, session_id)
    _ensure_owner(session, user)
    if session.session_mode != DRILL_SESSION_MODE:
        raise HTTPException(status_code=400, detail="Session is not a drill")
    if session.status != "active":
        raise HTTPException(status_code=400, detail="Drill session is not active")
    if session.drill_state == "converted":
        if session.rated_start_ply == request.current_ply:
            return _contract(session)
        raise HTTPException(status_code=409, detail="Drill already converted with a different rated_start_ply")
    if session.drill_state not in ("root_reached", "failed"):
        raise HTTPException(status_code=400, detail="Drill must be at root or stopped before continuing")

    now = utcnow()
    # Converting an ELIGIBLE accuracy-failed drill (true->false flip) REMOVES its
    # moves from the opening-evidence set — a digest-visible change the counter
    # must carry (g-jact). A root_reached->converted transition stays ineligible
    # on both sides -> no bump. (``segment``, which resegment rewrites, is not
    # read by the digest — the eligibility flip is the whole signal.)
    was_evidence_eligible = session_is_evidence_eligible(session)
    session.drill_state = "converted"
    session.is_rated = True
    session.normal_started_at = now
    session.converted_at = now
    session.rated_start_ply = request.current_ply
    resegment_session_moves(db, session)
    db.flush()
    if session_is_evidence_eligible(session) != was_evidence_eligible:
        bump_evidence_seq(db, user.user_id, session.player_color)
    db.commit()
    db.refresh(session)
    capture(str(user.user_id), "drill_continued", {})
    return _contract(session)


@router.post("/{session_id}/route-check", response_model=DrillRouteCheckResponse)
def check_drill_route(
    session_id: uuid.UUID,
    request: DrillRouteCheckRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> DrillRouteCheckResponse:
    session = _get_drill_or_404(db, session_id)
    _ensure_owner(session, user)
    if session.session_mode != DRILL_SESSION_MODE or not session.drill_opening_key:
        raise HTTPException(status_code=400, detail="Session is not a drill")
    if session.status != "active":
        raise HTTPException(status_code=400, detail="Drill session is not active")
    if session.drill_state in {"failed", "abandoned", "converted"}:
        raise HTTPException(status_code=400, detail="Drill route cannot be checked from its current state")
    if (request.previous_fen is None) != (request.played_uci is None):
        raise HTTPException(status_code=400, detail="previous_fen and played_uci must be provided together")

    routing = routing_view(get_opening_graph())
    route_map = route_map_for_target(
        routing, session.drill_opening_key, decode_uci_line(session.drill_line)
    )
    if not route_map.plies_by_fen:
        raise HTTPException(status_code=400, detail="Drill route is unavailable")

    try:
        current_fen = normalize_fen(request.current_fen)
        previous_fen = normalize_fen(request.previous_fen) if request.previous_fen else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid FEN: {exc}") from exc

    if session.drill_state == "root_reached":
        # Entry snapshot: this response writes nothing and may reflect state that
        # changes concurrently. Do NOT lock — snapshot semantics are intentional.
        return _root_reached_response(current_fen, route_map)

    if route_map.is_target(current_fen):
        # Mutating branch. The unlocked snapshot told us only the geometry; lock
        # and refresh the row, then re-derive the branch before writing so a
        # concurrent terminal transition survives instead of being overwritten.
        session = _get_drill_for_update(db, session_id)
        guard = _refreshed_route_guard(session, current_fen, route_map)
        if guard is not None:
            return guard
        session.drill_state = "root_reached"
        db.commit()
        return _root_reached_response(current_fen, route_map)

    if route_map.is_on_route(current_fen):
        # Snapshot: writes nothing and may reflect concurrently changing state.
        return DrillRouteCheckResponse(
            status="on_route",
            current_fen=current_fen,
            target_fen=route_map.target_fen,
            suggestions=[
                _suggestion(move)
                for move in route_preserving_moves(routing, route_map, current_fen)
            ],
        )

    if previous_fen is None or request.played_uci is None:
        raise HTTPException(
            status_code=400,
            detail="previous_fen and played_uci are required when the current position is off route",
        )

    played_move = route_move_for_uci(routing, route_map, previous_fen, request.played_uci)
    suggestions = route_preserving_moves(routing, route_map, previous_fen)
    # Mutating branch. Lock, refresh, and re-derive before recording the failure so
    # a root-reached or terminal transition committed concurrently is not clobbered.
    session = _get_drill_for_update(db, session_id)
    guard = _refreshed_route_guard(session, current_fen, route_map)
    if guard is not None:
        return guard
    session.drill_state = "failed"
    session.drill_terminal_reason = "off_route"
    db.commit()
    # A route-check off-route transition is a drill failure too (the spec defines
    # failed = off_route | accuracy); emit it here so analytics don't undercount
    # by only seeing the /fail accuracy path.
    capture(str(user.user_id), "drill_failed", {"reason": "off_route"})
    # reason="off_route" even if the move also exceeds the centipawn threshold —
    # leaving the route is the primary signal; staying on route is the first correction.
    #
    # No opening-score delta here: route-check is a speculative per-move endpoint
    # (the "is this terminal?" answer only arrives in this response), so the
    # frontend cannot apply the full-history upload barrier the other terminal
    # paths use before the backend reads session_moves — the just-played off-route
    # move may not be persisted yet, yielding a stale/short chain. Off-route also
    # means the drill failed BEFORE reaching the target opening, so the delta is
    # the least meaningful here. The accuracy-fail and natural-end paths (clean
    # terminal calls the frontend barriers) carry the delta instead.
    return DrillRouteCheckResponse(
        status="failed",
        current_fen=current_fen,
        target_fen=route_map.target_fen,
        suggestions=[_suggestion(move) for move in suggestions],
        failure=DrillRouteFailure(
            reason="off_route",
            played_move_uci=request.played_uci,
            played_move_san=played_move.san if played_move is not None else safe_san_for_uci(previous_fen, request.played_uci),
            correction_fen=previous_fen,
        ),
    )


class DrillNaturalEndRequest(BaseModel):
    result: str
    pgn: str | None = None


@router.post("/{session_id}/natural-end", response_model=DrillSessionContract)
def natural_end_drill(
    session_id: uuid.UUID,
    request: DrillNaturalEndRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> DrillSessionContract:
    session = _get_drill_for_update(db, session_id)
    _ensure_owner(session, user)
    if session.session_mode != DRILL_SESSION_MODE:
        raise HTTPException(status_code=400, detail="Session is not a drill")
    if session.drill_state not in ("active", "root_reached"):
        raise HTTPException(status_code=400, detail="Drill cannot end naturally from its current state")
    if request.result not in ("checkmate_win", "checkmate_loss", "draw"):
        raise HTTPException(status_code=400, detail="Invalid natural-end result")
    # status->'ended' flips SESSION_EVIDENCE_ELIGIBLE_SQL false->true (g-jact).
    was_evidence_eligible = session_is_evidence_eligible(session)
    session.drill_state = "failed"
    session.drill_terminal_reason = "natural_end"
    session.status = "ended"
    session.result = request.result
    session.ended_at = utcnow()
    if request.pgn:
        session.pgn = request.pgn
    db.flush()
    if session_is_evidence_eligible(session) != was_evidence_eligible:
        bump_evidence_seq(db, user.user_id, session.player_color)
    db.commit()
    db.refresh(session)
    capture(str(user.user_id), "drill_natural_end", {"result": request.result})
    return _contract(session, opening_score_changes=compute_opening_score_delta(db, session) or None)


@router.post("/{session_id}/abandon", response_model=DrillSessionContract)
def abandon_drill(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> DrillSessionContract:
    session = _get_drill_for_update(db, session_id)
    _ensure_owner(session, user)
    if session.session_mode != DRILL_SESSION_MODE:
        raise HTTPException(status_code=400, detail="Session is not a drill")
    if session.drill_state == "converted":
        raise HTTPException(status_code=400, detail="Use /api/game/end to abandon a converted drill")
    if session.drill_state not in {"active", "root_reached", "failed", "abandoned"}:
        raise HTTPException(status_code=400, detail="Drill cannot be abandoned from its current state")
    # Already-ended drills (e.g. natural-end finished the session) are no-ops:
    # don't overwrite session.result or ended_at.
    if session.drill_state != "abandoned" and session.status != "ended":
        # status->'ended' flips eligibility false->true for active/root_reached/
        # off_route drills; an accuracy-failed drill was already eligible (no
        # flip, no bump) (g-jact).
        was_evidence_eligible = session_is_evidence_eligible(session)
        # A drill that already FAILED keeps its outcome: 'abandoned' means "quit with
        # no terminal outcome", and overwriting it erased ~94% of real failures
        # (g-drill-failed-overwrite). The session still ENDS here — status/result/
        # ended_at are the LIFECYCLE record, drill_state + drill_terminal_reason are
        # the OUTCOME record.
        #
        # failed + status='ended' is a row shape natural-end already produces, so the
        # DB CHECKs are proven — but the two remain distinguishable by ``result``:
        # natural-end writes checkmate_win|checkmate_loss|draw, this path writes
        # 'drill_abandon'. That difference is what lets the g-drill-failed-backfill
        # predicate target only rows this endpoint clobbered.
        if session.drill_state != "failed":
            session.drill_state = "abandoned"
        session.status = "ended"
        session.result = "drill_abandon"
        session.ended_at = utcnow()
        session.is_rated = False
        db.flush()
        if session_is_evidence_eligible(session) != was_evidence_eligible:
            bump_evidence_seq(db, user.user_id, session.player_color)
        db.commit()
        db.refresh(session)
        # terminal_reason separates "quit after a failure" from "quit mid-drill"
        # (NULL) now that drill_state no longer records the quit.
        capture(
            str(user.user_id),
            "drill_abandoned",
            {"terminal_reason": session.drill_terminal_reason},
        )
    return _contract(session)
