from __future__ import annotations

from dataclasses import dataclass

import chess

from app.fen import normalize_fen
from app.opening_graph import OpeningGraph


@dataclass(frozen=True)
class DrillRouteMove:
    uci: str
    san: str
    resulting_fen: str
    plies_to_target: int


@dataclass(frozen=True)
class DrillRouteMap:
    target_fen: str
    plies_by_fen: dict[str, int]
    # None  → book BFS map (transposition-tolerant; routing reads `graph`).
    # dict  → strict played-line map (off-book target). Keyed by normalized FEN
    #         to the single on-route next move; routing ignores `graph` entirely.
    forward_moves: dict[str, list[DrillRouteMove]] | None = None

    def plies_to_target(self, fen: str) -> int | None:
        return self.plies_by_fen.get(normalize_fen(fen))

    def is_on_route(self, fen: str) -> bool:
        return self.plies_to_target(fen) is not None

    def is_target(self, fen: str) -> bool:
        return normalize_fen(fen) == self.target_fen


_ROUTE_CACHE: dict[tuple[str, str], DrillRouteMap] = {}


def _board_for_fen(fen: str) -> chess.Board:
    if len(fen.split()) == 4:
        return chess.Board(f"{fen} 0 1")
    return chess.Board(fen)


def _san_for_uci(fen: str, uci: str) -> str:
    board = _board_for_fen(fen)
    move = chess.Move.from_uci(uci)
    if move not in board.legal_moves:
        raise ValueError(f"Route move {uci} is illegal for FEN {fen}")
    return board.san(move)


def _resulting_fen(fen: str, uci: str) -> str:
    board = _board_for_fen(fen)
    move = chess.Move.from_uci(uci)
    if move not in board.legal_moves:
        raise ValueError(f"Route move {uci} is illegal for FEN {fen}")
    board.push(move)
    return normalize_fen(board.fen())


def get_drill_route_map(graph: OpeningGraph, target_fen: str) -> DrillRouteMap:
    normalized_target = normalize_fen(target_fen)
    cache_key = (graph.fingerprint, normalized_target)
    cached = _ROUTE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if not graph.has_position(normalized_target):
        route_map = DrillRouteMap(target_fen=normalized_target, plies_by_fen={})
        _ROUTE_CACHE[cache_key] = route_map
        return route_map

    plies_by_fen = {normalized_target: 0}
    queue = [normalized_target]
    for fen in queue:
        current_distance = plies_by_fen[fen]
        for parent_node, _uci in graph.get_parents(fen):
            if parent_node.fen in plies_by_fen:
                continue
            plies_by_fen[parent_node.fen] = current_distance + 1
            queue.append(parent_node.fen)

    route_map = DrillRouteMap(
        target_fen=normalized_target,
        plies_by_fen=plies_by_fen,
    )
    _ROUTE_CACHE[cache_key] = route_map
    return route_map


def build_line_route_map(line_ucis: list[str]) -> DrillRouteMap:
    """Strict route map for an exact played line (off-book targets).

    Unlike the BFS book map, "on route" means *following this exact line*: each
    position maps to the single next move that continues it, and success is
    reaching the line's final position. Positions are normalized so the keys
    match route-check FENs. Lines are short (≤ MAX_TREE_PLY) and built fresh, so
    these maps are NOT cached.

    Duplicate-position policy: a position is keyed only on its first occurrence,
    keeping the route deterministic from the top if a line revisits a square.
    """
    board = chess.Board()
    fens = [normalize_fen(board.fen())]
    sans: list[str] = []
    for uci in line_ucis:
        move = chess.Move.from_uci(uci)
        sans.append(board.san(move))
        board.push(move)
        fens.append(normalize_fen(board.fen()))

    n = len(line_ucis)
    target_fen = fens[n]
    plies_by_fen: dict[str, int] = {}
    forward_moves: dict[str, list[DrillRouteMove]] = {}
    for i in range(n):
        fen_i = fens[i]
        if fen_i in plies_by_fen:
            continue  # first-occurrence policy keeps the line deterministic
        plies_by_fen[fen_i] = n - i
        forward_moves[fen_i] = [
            DrillRouteMove(
                uci=line_ucis[i],
                san=sans[i],
                resulting_fen=fens[i + 1],
                plies_to_target=n - (i + 1),
            )
        ]
    # The target itself is on route at distance 0 with no forward move.
    if target_fen not in plies_by_fen:
        plies_by_fen[target_fen] = 0
        forward_moves[target_fen] = []
    return DrillRouteMap(
        target_fen=target_fen,
        plies_by_fen=plies_by_fen,
        forward_moves=forward_moves,
    )


def route_map_for_target(
    graph: OpeningGraph,
    target_fen: str,
    drill_line: list[str] | None,
) -> DrillRouteMap:
    """Pick the route strategy for a target. Shared by route-check + opponent
    steering so the two never diverge. In-book targets keep the transposition-
    tolerant book BFS; off-book targets use the strict played line."""
    normalized_target = normalize_fen(target_fen)
    if graph.has_position(normalized_target):
        return get_drill_route_map(graph, normalized_target)
    if not drill_line:
        # Caller raises 400 on the resulting empty plies_by_fen.
        return DrillRouteMap(target_fen=normalized_target, plies_by_fen={})
    return build_line_route_map(drill_line)


def route_preserving_moves(
    graph: OpeningGraph,
    route_map: DrillRouteMap,
    fen: str,
) -> list[DrillRouteMove]:
    if route_map.forward_moves is not None:
        # Strict line map: the single on-route continuation (no graph node).
        return list(route_map.forward_moves.get(normalize_fen(fen), []))
    normalized_fen = normalize_fen(fen)
    current_distance = route_map.plies_by_fen.get(normalized_fen)
    node = graph.get_node(normalized_fen)
    if current_distance is None or node is None:
        return []

    moves: list[DrillRouteMove] = []
    for uci, child_fen in node.children.items():
        child_distance = route_map.plies_by_fen.get(child_fen)
        if child_distance is None:
            continue
        try:
            san = _san_for_uci(normalized_fen, uci)
        except ValueError:
            continue
        moves.append(
            DrillRouteMove(
                uci=uci,
                san=san,
                resulting_fen=child_fen,
                plies_to_target=child_distance,
            )
        )

    return sorted(moves, key=lambda move: (move.plies_to_target, move.uci))


def route_move_for_uci(
    graph: OpeningGraph,
    route_map: DrillRouteMap,
    fen: str,
    uci: str,
) -> DrillRouteMove | None:
    if route_map.forward_moves is not None:
        # Strict line map: only the exact next line move matches (no graph node).
        for move in route_map.forward_moves.get(normalize_fen(fen), []):
            if move.uci == uci:
                return move
        return None
    normalized_fen = normalize_fen(fen)
    node = graph.get_node(normalized_fen)
    if node is None:
        return None
    child_fen = node.children.get(uci)
    if child_fen is None:
        return None
    plies = route_map.plies_by_fen.get(child_fen)
    if plies is None:
        return None
    try:
        san = _san_for_uci(normalized_fen, uci)
    except ValueError:
        return None
    return DrillRouteMove(
        uci=uci,
        san=san,
        resulting_fen=child_fen,
        plies_to_target=plies,
    )


def safe_san_for_uci(fen: str, uci: str) -> str | None:
    """Return SAN for uci from fen, or None if the move is illegal or the FEN is invalid."""
    try:
        return _san_for_uci(fen, uci)
    except ValueError:
        return None


def apply_uci_normalized(fen: str, uci: str) -> str:
    return _resulting_fen(fen, uci)


def _reset_drill_route_cache_for_testing() -> None:
    _ROUTE_CACHE.clear()
