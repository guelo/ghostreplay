"""Overlay user evidence from DB onto the opening graph.

Maps session_moves, blunders, and blunder_reviews for a (user_id, player_color)
pair onto the opening graph, producing per-node and per-edge counters that the
downstream score calculator consumes. No score computation happens here.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import NamedTuple

import chess
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.fen import normalize_fen
from app.opening_graph import OpeningGraph

logger = logging.getLogger(__name__)

PASS_THRESHOLD = 50  # eval_delta < this → pass

# SQLite returns timestamps as strings; Postgres returns datetime objects.


def _parse_ts(val: datetime | str | None) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(val)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

# Maximum user decisions to follow beyond the last book node.
BOOK_EXIT_EXTENSION = 2


@dataclass(slots=True)
class NodeEvidence:
    fen: str
    live_attempts: int = 0
    live_passes: int = 0
    live_fails: int = 0
    last_live_at: datetime | None = None
    review_attempts: int = 0
    review_passes: int = 0
    review_fails: int = 0
    last_review_at: datetime | None = None
    is_ghost_target: bool = False


@dataclass(slots=True)
class EdgeEvidence:
    parent_fen: str
    child_fen: str
    uci: str
    traversal_count: int = 0
    live_attempts: int = 0
    live_passes: int = 0
    live_fails: int = 0


@dataclass
class EvidenceOverlay:
    user_id: int
    player_color: str
    nodes: dict[str, NodeEvidence] = field(default_factory=dict)
    edges: dict[tuple[str, str], EdgeEvidence] = field(default_factory=dict)


class _MoveRow(NamedTuple):
    norm_before: str
    norm_after: str
    color: str
    eval_delta: int | None
    move_san: str
    session_ts: datetime | None


def _get_or_create_node(nodes: dict[str, NodeEvidence], fen: str) -> NodeEvidence:
    node = nodes.get(fen)
    if node is None:
        node = NodeEvidence(fen=fen)
        nodes[fen] = node
    return node


def _get_or_create_edge(
    edges: dict[tuple[str, str], EdgeEvidence],
    parent_fen: str,
    child_fen: str,
    uci: str,
) -> EdgeEvidence:
    key = (parent_fen, child_fen)
    edge = edges.get(key)
    if edge is None:
        edge = EdgeEvidence(parent_fen=parent_fen, child_fen=child_fen, uci=uci)
        edges[key] = edge
    return edge


def _resolve_edge_uci(graph: OpeningGraph, parent_fen: str, child_fen: str) -> str | None:
    node = graph.get_node(parent_fen)
    if node is None:
        return None
    for uci, target_fen in node.children.items():
        if target_fen == child_fen:
            return uci
    return None


def _uci_from_san(fen_4field: str, move_san: str) -> str | None:
    try:
        board = chess.Board(fen_4field + " 0 1")
        move = board.parse_san(move_san)
        return move.uci()
    except (ValueError, chess.IllegalMoveError, chess.InvalidMoveError, chess.AmbiguousMoveError):
        return None


def _record_edge(
    edges: dict[tuple[str, str], EdgeEvidence],
    parent_fen: str,
    child_fen: str,
    uci: str,
    is_user_move: bool,
    eval_delta: int | None,
) -> None:
    edge = _get_or_create_edge(edges, parent_fen, child_fen, uci)
    edge.traversal_count += 1
    if is_user_move and eval_delta is not None:
        edge.live_attempts += 1
        if eval_delta < PASS_THRESHOLD:
            edge.live_passes += 1
        else:
            edge.live_fails += 1


def _record_node(
    nodes: dict[str, NodeEvidence],
    fen: str,
    eval_delta: int | None,
    session_ts: datetime | str | None,
) -> None:
    if eval_delta is None:
        return
    node = _get_or_create_node(nodes, fen)
    node.live_attempts += 1
    if eval_delta < PASS_THRESHOLD:
        node.live_passes += 1
    else:
        node.live_fails += 1
    ts = _parse_ts(session_ts)
    if ts is not None:
        if node.last_live_at is None or ts > node.last_live_at:
            node.last_live_at = ts


def _collect_session_moves(
    db: Session,
    user_id: int,
    player_color: str,
    graph: OpeningGraph,
    overlay: EvidenceOverlay,
) -> None:
    rows = db.execute(
        text("""
            SELECT sm.fen_before, sm.fen_after, sm.color, sm.eval_delta, sm.move_san,
                   COALESCE(gs.ended_at, gs.started_at) AS session_ts
            FROM session_moves sm
            JOIN game_sessions gs ON gs.id = sm.session_id
            WHERE gs.user_id = :user_id
              AND gs.player_color = :player_color
              AND sm.fen_before IS NOT NULL
        """),
        {"user_id": user_id, "player_color": player_color},
    ).fetchall()

    # Pass 1: normalize FENs and build move_chains for extension traversal.
    moves: list[_MoveRow] = []
    move_chains: dict[str, list[_MoveRow]] = defaultdict(list)

    for row in rows:
        fen_before_raw, fen_after_raw, color, eval_delta, move_san, session_ts = row
        norm_before = normalize_fen(fen_before_raw)
        norm_after = normalize_fen(fen_after_raw)
        mr = _MoveRow(norm_before, norm_after, color, eval_delta, move_san, session_ts)
        moves.append(mr)
        move_chains[norm_before].append(mr)

    # Pass 2: process in-book moves and collect book-boundary exits.
    # Each exit is (child_fen, user_decisions_already_consumed).
    book_exits: dict[str, int] = {}

    for mr in moves:
        in_book_before = graph.has_position(mr.norm_before)

        if not in_book_before:
            continue

        is_user = mr.color == player_color

        # Check if this edge exists in the book graph.
        book_uci = _resolve_edge_uci(graph, mr.norm_before, mr.norm_after)
        is_book_edge = book_uci is not None

        if is_book_edge:
            # Normal in-book edge.
            if is_user:
                _record_node(overlay.nodes, mr.norm_before, mr.eval_delta, mr.session_ts)
            _record_edge(overlay.edges, mr.norm_before, mr.norm_after, book_uci, is_user, mr.eval_delta)
        else:
            # Book exit: parent in book, but the edge is not a book edge.
            # This includes moves to off-book positions AND non-book edges
            # that happen to land on a position known elsewhere in the graph.
            uci = _uci_from_san(mr.norm_before, mr.move_san)
            if uci is None:
                continue
            if is_user:
                _record_node(overlay.nodes, mr.norm_before, mr.eval_delta, mr.session_ts)
            _record_edge(overlay.edges, mr.norm_before, mr.norm_after, uci, is_user, mr.eval_delta)
            # Track the exit. A user exit consumes 1 decision; opponent exit consumes 0.
            exit_depth = 1 if is_user else 0
            # Keep the minimum depth if we've seen this child from multiple exits.
            if mr.norm_after not in book_exits or exit_depth < book_exits[mr.norm_after]:
                book_exits[mr.norm_after] = exit_depth

    # Pass 3: follow extension chains from book-boundary exits.
    # BFS up to BOOK_EXIT_EXTENSION user decisions deep.
    # Track the best (minimum) depth seen per FEN so a shallower transposition
    # can re-enqueue a position already visited at a deeper depth.
    frontier = list(book_exits.items())
    best_depth: dict[str, int] = dict(book_exits)

    while frontier:
        current_fen, depth = frontier.pop()
        # Skip if we've already processed this FEN at a shallower depth.
        if best_depth.get(current_fen, depth + 1) < depth:
            continue
        for mr in move_chains.get(current_fen, []):
            is_user = mr.color == player_color
            next_depth = depth + (1 if is_user else 0)
            if is_user and next_depth > BOOK_EXIT_EXTENSION:
                continue

            # Record evidence for this extension move.
            uci = _uci_from_san(mr.norm_before, mr.move_san)
            if uci is None:
                continue
            if is_user:
                _record_node(overlay.nodes, mr.norm_before, mr.eval_delta, mr.session_ts)
            _record_edge(overlay.edges, mr.norm_before, mr.norm_after, uci, is_user, mr.eval_delta)

            if next_depth < BOOK_EXIT_EXTENSION:
                prev = best_depth.get(mr.norm_after)
                if prev is None or next_depth < prev:
                    best_depth[mr.norm_after] = next_depth
                    frontier.append((mr.norm_after, next_depth))


def _collect_ghost_targets(
    db: Session,
    user_id: int,
    player_color: str,
    graph: OpeningGraph,
    overlay: EvidenceOverlay,
) -> None:
    rows = db.execute(
        text("""
            SELECT p.fen_raw
            FROM blunders b
            JOIN positions p ON p.id = b.position_id
            LEFT JOIN game_sessions gs ON gs.id = b.source_session_id
            WHERE b.user_id = :user_id
              AND (gs.player_color = :player_color
                   OR (b.source_session_id IS NULL AND p.active_color = :player_color))
        """),
        {"user_id": user_id, "player_color": player_color},
    ).fetchall()

    for (fen_raw,) in rows:
        norm = normalize_fen(fen_raw)
        if graph.has_position(norm) or norm in overlay.nodes:
            node = _get_or_create_node(overlay.nodes, norm)
            node.is_ghost_target = True


def _collect_reviews(
    db: Session,
    user_id: int,
    player_color: str,
    graph: OpeningGraph,
    overlay: EvidenceOverlay,
) -> None:
    rows = db.execute(
        text("""
            SELECT p.fen_raw, br.passed, br.reviewed_at
            FROM blunder_reviews br
            JOIN blunders b ON b.id = br.blunder_id
            JOIN positions p ON p.id = b.position_id
            LEFT JOIN game_sessions gs ON gs.id = b.source_session_id
            WHERE b.user_id = :user_id
              AND (gs.player_color = :player_color
                   OR (b.source_session_id IS NULL AND p.active_color = :player_color))
        """),
        {"user_id": user_id, "player_color": player_color},
    ).fetchall()

    for fen_raw, passed, reviewed_at in rows:
        norm = normalize_fen(fen_raw)
        if not graph.has_position(norm) and norm not in overlay.nodes:
            continue
        node = _get_or_create_node(overlay.nodes, norm)
        node.review_attempts += 1
        if passed:
            node.review_passes += 1
        else:
            node.review_fails += 1
        ts = _parse_ts(reviewed_at)
        if ts is not None:
            if node.last_review_at is None or ts > node.last_review_at:
                node.last_review_at = ts


def overlay_evidence(
    db: Session,
    user_id: int,
    player_color: str,
    graph: OpeningGraph,
) -> EvidenceOverlay:
    """Build an evidence overlay for one (user, color) pair on the opening graph."""
    overlay = EvidenceOverlay(user_id=user_id, player_color=player_color)
    _collect_session_moves(db, user_id, player_color, graph, overlay)
    _collect_ghost_targets(db, user_id, player_color, graph, overlay)
    _collect_reviews(db, user_id, player_color, graph, overlay)
    return overlay
