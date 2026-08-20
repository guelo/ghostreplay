from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from enum import Enum

import chess
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.openings import MAX_TREE_PLY
from app.db import get_db
from app.drill_steering import (
    DrillRouteMap,
    DrillRouteMove,
    apply_uci_normalized,
    replay_history_fen,
    route_map_for_target,
    route_move_for_uci,
    route_preserving_moves,
    safe_san_for_uci,
)
from app.fen import active_color, fen_hash, normalize_fen
from app.models import GameSession, OpponentDecision, decode_uci_line, encode_uci_line
from app.opening_baseline_scheduler import (
    TerminalKind,
    enqueue_baseline_snapshot,
    terminal_baseline_observation,
)
from app.opening_boundary import (
    claim_opening_boundary_shadow_terminal,
    emit_opening_boundary_shadow_terminal,
)
from app.opening_cache import bump_evidence_seq
from app.opening_densify import routing_view
from app.opening_evidence import session_is_evidence_eligible
from app.opening_graph import get_opening_graph
from app.opening_roots import derive_family, get_opening_roots
from app.opening_score_delta import (
    OpeningScoreDeltaItem,
    capture_baseline_watermark,
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
from app.terminal_row_reconcile import (
    OUTCOME_LINE_UNACKNOWLEDGED,
    ReconcileResult,
    reconcile_terminal_move_rows,
    suppress_unacknowledged_move_line,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/drills", tags=["drills"])

# Every drill starts from the standard position, so a root reached on ply 1 is the
# player's own first move — the one arrival with nothing before it to anchor against.
START_POSITION_FEN = normalize_fen(chess.Board().fen())


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
    line_revision: int | None = Field(None, ge=0)
    discard_move_evidence: bool = False


class DrillTerminalLineRequest(BaseModel):
    line_revision: int | None = Field(None, ge=0)
    discard_move_evidence: bool = False


class DrillRouteCheckRequest(BaseModel):
    current_fen: str = Field(..., min_length=1)
    previous_fen: str | None = Field(None, min_length=1)
    played_uci: str | None = Field(None, min_length=4, max_length=5)
    # Plies played to reach current_fen. Required on every check; at the root it is a
    # boundary claim, while away from the root it is ordinary route metadata.
    current_ply: int = Field(..., ge=0)
    # The opponent_decisions row whose served move the client just applied. Required to
    # confirm a root the OPPONENT moved into, rejected on one the player moved into —
    # which of the two applies is derived from the target, never from this field.
    decision_id: uuid.UUID | None = None


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
    # The session's committed evidence boundary, echoed so a client can tell a
    # confirmed root from one that only transitioned. NULL on every pre-root response,
    # and on a root whose boundary could not be proven.
    drill_root_reached_ply: int | None = None


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
    move_line_revision: int
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
    current_fen: str, route_map: DrillRouteMap, root_ply: int | None
) -> DrillRouteCheckResponse:
    return DrillRouteCheckResponse(
        status="root_reached",
        current_fen=current_fen,
        target_fen=route_map.target_fen,
        suggestions=[],
        drill_root_reached_ply=root_ply,
    )


def _refreshed_route_guard(
    session: GameSession,
    current_fen: str,
    route_map: DrillRouteMap,
    *,
    boundary_pending: bool = False,
) -> DrillRouteCheckResponse | None:
    """Re-derive a route-check mutating branch from refreshed, locked state.

    Called only after ``_get_drill_for_update`` re-reads the session under the NKU
    lock, so ``session`` reflects any transition a concurrent request committed
    since the unlocked entry snapshot. The FEN geometry the caller already computed
    is immutable, so re-deriving the branch is purely a re-check of drill state:

    * still active pre-root -> ``None``; the caller performs its intended write;
    * already root-reached -> the root-reached response, sent WITHOUT writing;
    * failed/abandoned/converted/non-active -> the existing entry 400.

    ``boundary_pending`` narrows the root-reached case by exactly one situation: a
    VALIDATED boundary claim for a row that carries no boundary yet must proceed to
    stamp it. Serving a route move is no longer a transition and the observed-root
    fallback in /api/game/next-opponent-move stamps state and boundary together, so
    the remaining producers of state-without-boundary are soft-declined
    confirmations (a well-formed but unprovable claim transitions without stamping;
    a later provable claim may still stamp) and rows written before this endpoint
    existed.
    """
    if session.status != "active":
        raise HTTPException(status_code=400, detail="Drill session is not active")
    if session.drill_state in {"failed", "abandoned", "converted"}:
        raise HTTPException(
            status_code=400,
            detail="Drill route cannot be checked from its current state",
        )
    if session.drill_state == "root_reached":
        if boundary_pending and session.drill_root_reached_ply is None:
            return None
        return _root_reached_response(
            current_fen, route_map, session.drill_root_reached_ply
        )
    return None


def _confirmed_root_ply(
    db: Session,
    session: GameSession,
    request: DrillRouteCheckRequest,
    route_map: DrillRouteMap,
    current_fen: str,
    previous_fen: str | None,
) -> int | None:
    """Prove the ply at which this drill reached its opening root, or refuse to.

    Returns the PROVEN ply, or ``None`` when the claim is well formed but unprovable —
    the caller then transitions drill state without stamping a boundary. Raises 422 when
    the claim CONTRADICTS server-side evidence.

    Which proof is owed is derived, never inferred from the request. A FEN's active
    colour fixes who moved into it, so the route target alone decides whether the root
    is reached by the player or by the opponent; a client cannot select the weaker path
    by omitting ``decision_id``.
    """
    current_ply = request.current_ply
    if current_ply < 1:
        raise HTTPException(
            status_code=422, detail="current_ply does not match the drill's move order"
        )
    # The target's own side-to-move fixes the parity of ANY ply that reaches it: white
    # to move means an even number of plies has been played. That is a property of the
    # position, so it holds whichever side moved in, and it is checked without trusting
    # a single recorded number.
    if current_ply % 2 != (0 if active_color(route_map.target_fen) == "white" else 1):
        raise HTTPException(
            status_code=422, detail="current_ply does not match the drill's move order"
        )
    root_mover = "black" if active_color(route_map.target_fen) == "white" else "white"
    if root_mover == session.player_color:
        return _confirmed_player_root_ply(
            db, session, request, current_fen, previous_fen, current_ply
        )
    return _confirmed_opponent_root_ply(db, session, request, current_fen, current_ply)


def _decision_ply_is_proven(decision: OpponentDecision) -> bool:
    """Is this decision's ``ply_before`` backed by its own recorded history?

    ``ply_before`` is ``len(request.moves)`` at serve time. The serve path replays that
    history and requires it to reproduce the request FEN before recording — but only in
    the pre-root drill branch, and only for rows written since g-root-confirm-api.
    Re-proving it here closes both gaps, using only columns the row already carries:
    replay the stored history, require it to hash to the stored request FEN, and require
    its length to be the recorded ply.

    The gap that is NOT merely legacy: once a drill is root-reached the endpoint serves
    from the ghost/engine path, which validates no history, and the ghost can steer back
    through the route target by repetition. Such a decision has ``resulting_fen`` equal
    to the target, so it reaches this validator — with a ply nothing checked. Re-proving
    is what stops it from stamping a boundary far below the real root.

    A row that fails is not a client contradiction — it is a record whose ply is not
    evidence — so callers decline to stamp rather than rejecting the confirmation.
    """
    try:
        history = json.loads(decision.uci_history)
    except (TypeError, ValueError):
        return False
    if not isinstance(history, list) or len(history) != decision.ply_before:
        return False
    replayed = replay_history_fen(history)
    return replayed is not None and fen_hash(replayed) == decision.request_fen_hash


def _confirmed_opponent_root_ply(
    db: Session,
    session: GameSession,
    request: DrillRouteCheckRequest,
    current_fen: str,
    current_ply: int,
) -> int | None:
    """The opponent moved into the root, so the server itself served that move.

    Nothing here is client-asserted: ``ply_before`` and ``resulting_fen`` are both read
    off the recorded decision, so the client's numbers are only ever compared against
    them — and ``ply_before`` is itself only evidence once its own history replays
    (``_decision_ply_is_proven``).
    """
    if request.decision_id is None:
        raise HTTPException(
            status_code=422,
            detail="decision_id is required to confirm an opponent-reached drill root",
        )
    decision = db.execute(
        select(OpponentDecision).where(
            OpponentDecision.decision_id == request.decision_id,
            OpponentDecision.session_id == session.id,
        )
    ).scalar_one_or_none()
    if decision is None:
        # Same answer for unknown and foreign: the id is an unguessable uuid4, and a
        # distinguishing response would confirm existence across sessions.
        raise HTTPException(
            status_code=422, detail="Unknown opponent decision for this drill session"
        )
    # Geometry decides, NOT the recorded ``reaches_drill_root`` flag: that column is
    # extracted from the response's status string, which the cutover renames to
    # root_pending. The caller already proved current_fen IS the route target, so a
    # decision whose resulting_fen is current_fen reaches the root by construction.
    # A stale id from a reverted branch fails here rather than stamping.
    if decision.resulting_fen != current_fen:
        raise HTTPException(
            status_code=422, detail="The confirmed decision does not reach the drill root"
        )
    if not _decision_ply_is_proven(decision):
        # The decision exists and reaches the root, but its recorded ply is not backed
        # by a history that reproduces the position it was served from — a row from
        # before the serve-time replay existed. Transition without a boundary rather
        # than writing a ply nothing proves.
        logger.warning(
            "drill root confirmation left unstamped: decision %s has an unproven "
            "ply_before session_id=%s",
            decision.decision_id,
            session.id,
        )
        return None
    if current_ply != decision.ply_before + 1:
        raise HTTPException(
            status_code=422, detail="current_ply does not match the served decision"
        )
    return current_ply


def _confirmed_player_root_ply(
    db: Session,
    session: GameSession,
    request: DrillRouteCheckRequest,
    current_fen: str,
    previous_fen: str | None,
    current_ply: int,
) -> int | None:
    """The player moved into the root, so no decision records the arrival itself.

    ``session_moves`` uploads are asynchronous, so at confirmation time the server holds
    no record of the player's move. Every remaining proof is therefore required: the
    played move is replayed to check it produces the observed position (the caller has
    already checked the ply's parity against the target), and the ply itself is ANCHORED
    to the decision log — the position the player moved FROM is the resulting position
    of the opponent decision this server served two plies earlier, and that decision's
    own ply must be proven in turn.

    The anchor is what keeps ``current_ply`` from being a bare client assertion, and the
    asymmetry matters: a too-low boundary readmits the scripted pre-root plies this whole
    boundary exists to exclude, while a too-high one only discards the claimant's own
    evidence.
    """
    if request.decision_id is not None:
        raise HTTPException(
            status_code=422,
            detail="decision_id is not valid for a player-reached drill root",
        )
    if previous_fen is None or request.played_uci is None:
        raise HTTPException(
            status_code=422,
            detail="previous_fen and played_uci are required to confirm the drill root",
        )
    try:
        played_fen = apply_uci_normalized(previous_fen, request.played_uci)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="played_uci is not legal in previous_fen"
        ) from exc
    if played_fen != current_fen:
        raise HTTPException(
            status_code=422, detail="played_uci does not produce the confirmed position"
        )

    if current_ply == 1:
        # The player moved first: nothing precedes it, so the start position IS the
        # anchor and the ply is fully proven.
        if previous_fen != START_POSITION_FEN:
            raise HTTPException(
                status_code=422,
                detail="A ply-1 drill root must be reached from the start position",
            )
        return 1

    # Any decision matching BOTH the position moved from and the ply two before proves
    # this arrival. Several rows can match — a transposition reaches the same position
    # at the same ply by a different history — and each carries its own proof, so the
    # anchor holds if ANY of them is itself proven.
    candidates = (
        db.execute(
            select(OpponentDecision).where(
                OpponentDecision.session_id == session.id,
                OpponentDecision.ply_before == current_ply - 2,
                OpponentDecision.resulting_fen == previous_fen,
            )
        )
        .scalars()
        .all()
    )
    if not any(_decision_ply_is_proven(decision) for decision in candidates):
        # Well formed but unprovable — a drill in flight across the deploy, or history
        # predating the decision log. NOT a contradiction, so it must not fail the
        # confirmation: a NULL boundary already means "contributes no reach evidence",
        # while rejecting would strand a live drill for a claim we merely cannot check.
        logger.warning(
            "drill root confirmation left unstamped: no decision anchors ply %d "
            "session_id=%s",
            current_ply,
            session.id,
        )
        return None
    return current_ply


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
        move_line_revision=session.move_line_revision,
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

    baseline_watermark = capture_baseline_watermark(
        db, user.user_id, request.player_color.value
    )
    watermark_values = baseline_watermark or (None, None, None)
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
        baseline_watermark_seq=watermark_values[0],
        baseline_watermark_epoch=watermark_values[1],
        baseline_watermark_fingerprint=watermark_values[2],
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    # Capture the opening-score baseline OFF the request thread (g-mxeo) — mirrors
    # the game-start path. The worker fills it only when a batch is proven to
    # represent the durable start watermark; otherwise it stays NULL. Best-effort.
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
    baseline_observation = terminal_baseline_observation(
        session,
        TerminalKind.DRILL_ACCURACY_FAIL,
    )
    # Accuracy-fail flips SESSION_EVIDENCE_ELIGIBLE_SQL false->true without any
    # timestamp write, so the opening-evidence counter carries the change (g-jact).
    was_evidence_eligible = session_is_evidence_eligible(session)
    line_fence = suppress_unacknowledged_move_line(
        db,
        session,
        line_revision=request.line_revision,
        discard_move_evidence=request.discard_move_evidence,
    )
    session.drill_state = "failed"
    session.drill_terminal_reason = "accuracy"
    shadow_terminal_at = utcnow()
    shadow_terminal_claimed = claim_opening_boundary_shadow_terminal(
        session,
        terminal_at=shadow_terminal_at,
    )
    db.flush()
    if session_is_evidence_eligible(session) != was_evidence_eligible or (
        was_evidence_eligible and line_fence.deleted_rows > 0
    ):
        bump_evidence_seq(db, user.user_id, session.player_color)
    db.commit()
    db.refresh(session)
    fail_properties = {"reason": session.drill_terminal_reason}
    fail_properties.update(baseline_observation)
    capture(str(user.user_id), "drill_failed", fail_properties)
    if shadow_terminal_claimed:
        emit_opening_boundary_shadow_terminal(
            session,
            terminal_trigger="accuracy_fail",
            terminal_at=shadow_terminal_at,
        )
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

    at_root = route_map.is_target(current_fen)
    if request.decision_id is not None and not at_root:
        # A confirmation for a position that is not the root. The server SERVED that
        # move, so this is the confirmation failing — never the drill: refuse here
        # rather than letting it fall through to the off-route failure branch.
        raise HTTPException(
            status_code=422, detail="Confirmed position is not the drill root"
        )
    # A boundary CLAIM is current_ply AT THE ROOT. Everywhere else current_ply is
    # ordinary metadata — the per-player-move call site sends it on every check.
    # Validate BEFORE anything else touches the row: the claim reads only immutable
    # inputs (the route target, player_color, decision rows), so a false one fails fast
    # and never acquires the lock, and a claim that proves nothing cannot turn a
    # snapshot branch into a locking one.
    confirmed_ply = (
        _confirmed_root_ply(db, session, request, route_map, current_fen, previous_fen)
        if at_root
        else None
    )

    if session.drill_state == "root_reached" and not (
        confirmed_ply is not None and session.drill_root_reached_ply is None
    ):
        # Entry snapshot: this response writes nothing and may reflect state that
        # changes concurrently. Do NOT lock — snapshot semantics are intentional. The
        # one case that must NOT stop here is a PROVEN boundary for a row that has
        # none: state can reach root_reached without a boundary (the observed-root
        # fallback stamps both, but soft-declined confirmations and pre-boundary rows
        # do not), so confirmation has to be able to stamp a boundary onto an
        # already-root_reached row. Reading the boundary unlocked is safe because it is
        # write-once.
        return _root_reached_response(
            current_fen, route_map, session.drill_root_reached_ply
        )

    if at_root:
        # Mutating branch. The unlocked snapshot told us only the geometry; lock
        # and refresh the row, then re-derive the branch before writing so a
        # concurrent terminal transition survives instead of being overwritten.
        session = _get_drill_for_update(db, session_id)
        guard = _refreshed_route_guard(
            session,
            current_fen,
            route_map,
            boundary_pending=confirmed_ply is not None,
        )
        if guard is not None:
            return guard
        if session.drill_state != "root_reached":
            session.drill_state = "root_reached"
        # Read the boundary BEFORE the commit expires the row: echoing it afterwards
        # would cost a third game_sessions SELECT on every root-reaching check.
        root_ply = session.drill_root_reached_ply
        if confirmed_ply is not None and root_ply is None:
            # Write-once, and in the SAME transaction as any state transition it
            # accompanies. The invariant is ONE-WAY: a non-NULL boundary always implies
            # root_reached, but root_reached does NOT imply a boundary — legacy sessions
            # and soft-declined confirmations leave it NULL permanently. A concurrent
            # confirmation that won the lock first has already stamped, so this one
            # converges on ITS ply instead of restamping.
            session.drill_root_reached_ply = confirmed_ply
            root_ply = confirmed_ply
        db.commit()
        return _root_reached_response(current_fen, route_map, root_ply)

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
    line_revision: int | None = Field(None, ge=0)
    discard_move_evidence: bool = False


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
    baseline_observation = terminal_baseline_observation(
        session,
        TerminalKind.DRILL_NATURAL_END,
    )
    # status->'ended' flips SESSION_EVIDENCE_ELIGIBLE_SQL false->true (g-jact).
    was_evidence_eligible = session_is_evidence_eligible(session)
    line_fence = suppress_unacknowledged_move_line(
        db,
        session,
        line_revision=request.line_revision,
        discard_move_evidence=request.discard_move_evidence,
    )
    session.drill_state = "failed"
    session.drill_terminal_reason = "natural_end"
    session.status = "ended"
    session.result = request.result
    session.ended_at = utcnow()
    if request.pgn:
        session.pgn = request.pgn
    # Natural drill termination is the second live terminal writer. It must
    # restore a lost/sparse final upload before the status transition exposes
    # the session to opening evidence, just like /api/game/end does.
    reconcile_result = (
        reconcile_terminal_move_rows(db, session, allow_sparse=True)
        if line_fence.acknowledged
        else ReconcileResult(
            outcome=OUTCOME_LINE_UNACKNOWLEDGED,
            expected_plies=None,
            stored_rows=0,
            derived_rows=0,
        )
    )
    session.terminal_line_reconciled = line_fence.acknowledged
    if reconcile_result.derived_rows:
        # Match /api/game/end's durable audit marker. Once the missing rows are
        # present, the row grid alone cannot show that terminal reconciliation
        # repaired a lost final upload.
        session.derived_tail_rows = reconcile_result.derived_rows
    shadow_terminal_claimed = bool(
        session.ended_at is not None
        and claim_opening_boundary_shadow_terminal(
            session,
            terminal_at=session.ended_at,
        )
    )
    db.flush()
    if session_is_evidence_eligible(session) != was_evidence_eligible or (
        was_evidence_eligible and line_fence.deleted_rows > 0
    ):
        bump_evidence_seq(db, user.user_id, session.player_color)
    db.commit()
    db.refresh(session)
    natural_end_properties = {
        "result": request.result,
        "row_reconcile_outcome": reconcile_result.outcome,
        "derived_tail_rows": reconcile_result.derived_rows,
    }
    natural_end_properties.update(baseline_observation)
    capture(str(user.user_id), "drill_natural_end", natural_end_properties)
    if shadow_terminal_claimed and session.ended_at is not None:
        emit_opening_boundary_shadow_terminal(
            session,
            terminal_trigger="drill_natural_end",
            terminal_at=session.ended_at,
        )
    return _contract(session, opening_score_changes=compute_opening_score_delta(db, session) or None)


@router.post("/{session_id}/abandon", response_model=DrillSessionContract)
def abandon_drill(
    session_id: uuid.UUID,
    request: DrillTerminalLineRequest | None = None,
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
        line_fence = suppress_unacknowledged_move_line(
            db,
            session,
            line_revision=request.line_revision if request is not None else None,
            discard_move_evidence=(
                request.discard_move_evidence if request is not None else False
            ),
        )
        if session.drill_state != "failed":
            session.drill_state = "abandoned"
        session.status = "ended"
        session.result = "drill_abandon"
        session.ended_at = utcnow()
        session.is_rated = False
        db.flush()
        if session_is_evidence_eligible(session) != was_evidence_eligible or (
            was_evidence_eligible and line_fence.deleted_rows > 0
        ):
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
