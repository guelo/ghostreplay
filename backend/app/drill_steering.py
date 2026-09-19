from __future__ import annotations

from dataclasses import dataclass

import chess

from app.fen import normalize_fen
from app.game_phase import is_middlegame_position
from app.opening_densify import RoutingView
from app.opening_limits import MAX_OPENING_LINE_PLY as MAX_DRILL_LINE_PLY
from app.opening_transposition_artifact import coverage_structural_edge_is_eligible


@dataclass(frozen=True)
class DrillRouteMove:
    uci: str
    san: str
    resulting_fen: str
    plies_to_target: int


@dataclass(frozen=True)
class DrillStructuralMove:
    """A score-relevant opponent continuation after the drill root."""

    uci: str
    san: str
    resulting_fen: str


@dataclass(frozen=True)
class DrillRouteMap:
    target_fen: str
    plies_by_fen: dict[str, int]
    # None  → book BFS map (transposition-tolerant; routing reads the view).
    # dict  → strict played-line map (off-book target). Keyed by normalized FEN
    #         to the single on-route next move; routing ignores the view entirely.
    forward_moves: dict[str, list[DrillRouteMove]] | None = None
    # Request-local additions for prefer_line; never written into the graph/cache.
    supplemental_children: dict[str, dict[str, str]] | None = None
    preferred_ucis: dict[str, str] | None = None

    def plies_to_target(self, fen: str) -> int | None:
        return self.plies_by_fen.get(normalize_fen(fen))

    def is_on_route(self, fen: str) -> bool:
        return self.plies_to_target(fen) is not None

    def is_target(self, fen: str) -> bool:
        return normalize_fen(fen) == self.target_fen


# Keyed by (graph fingerprint, overlay fingerprint, target). The overlay
# fingerprint is not redundant: the transposition artifact can change without
# graph.fingerprint changing, and a stale map would then route over dead edges.
_ROUTE_CACHE: dict[tuple[str, str, str], DrillRouteMap] = {}


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


def get_drill_route_map(routing: RoutingView, target_fen: str) -> DrillRouteMap:
    normalized_target = normalize_fen(target_fen)
    cache_key = (
        routing.graph_fingerprint,
        routing.overlay_fingerprint,
        normalized_target,
    )
    cached = _ROUTE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if not routing.has_position(normalized_target):
        route_map = DrillRouteMap(target_fen=normalized_target, plies_by_fen={})
        _ROUTE_CACHE[cache_key] = route_map
        return route_map

    plies_by_fen = {normalized_target: 0}
    queue = [normalized_target]
    for fen in queue:
        current_distance = plies_by_fen[fen]
        # Routing parents, not graph parents: a densified edge is exactly how a
        # position only reachable by a transposed move order reads as on-route.
        for parent_node, _uci in routing.routing_parents(fen):
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
    match route-check FENs. Lines are short (≤ MAX_DRILL_LINE_PLY) and built fresh, so
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


def validate_drill_line(line: list[str] | None, target_fen: str) -> DrillRouteMap:
    """Validate a saved preference or strict line before using its position keys."""
    if not line:
        raise ValueError("Drill line is required")
    if len(line) > MAX_DRILL_LINE_PLY:
        raise ValueError("Drill line is too long")
    board = chess.Board()
    seen = {normalize_fen(board.fen())}
    for uci in line:
        if not isinstance(uci, str):
            raise ValueError("Invalid move in drill line")
        try:
            move = chess.Move.from_uci(uci)
        except ValueError as exc:
            raise ValueError(f"Invalid move in drill line: {uci}") from exc
        if move not in board.legal_moves:
            raise ValueError(f"Illegal move in drill line: {uci}")
        board.push(move)
        fen = normalize_fen(board.fen())
        if fen in seen:
            raise ValueError("Drill line revisits a position")
        seen.add(fen)
    try:
        target = normalize_fen(target_fen)
    except (IndexError, ValueError) as exc:
        raise ValueError("Invalid target position") from exc
    if normalize_fen(board.fen()) != target:
        raise ValueError("Drill line does not reach the target position")
    return build_line_route_map(line)


def _preferred_route_map(
    routing: RoutingView, target_fen: str, line: list[str] | None,
) -> DrillRouteMap:
    saved = validate_drill_line(line, target_fen)
    children: dict[str, dict[str, str]] = {}
    parents: dict[str, list[str]] = {}
    preferred: dict[str, str] = {}
    for parent, moves in (saved.forward_moves or {}).items():
        for move in moves:
            children.setdefault(parent, {})[move.uci] = move.resulting_fen
            parents.setdefault(move.resulting_fen, []).append(parent)
            preferred[parent] = move.uci

    distances = {saved.target_fen: 0}
    queue = [saved.target_fen]
    for fen in queue:
        combined_parents = list(parents.get(fen, ()))
        if routing.has_position(fen):
            combined_parents.extend(node.fen for node, _ in routing.routing_parents(fen))
        for parent in combined_parents:
            if parent not in distances:
                distances[parent] = distances[fen] + 1
                queue.append(parent)
    return DrillRouteMap(
        target_fen=saved.target_fen, plies_by_fen=distances,
        supplemental_children=children, preferred_ucis=preferred,
    )


def route_map_for_target(
    routing: RoutingView,
    target_fen: str,
    drill_line: list[str] | None,
    route_mode: str = "auto",
) -> DrillRouteMap:
    """Build the accepted-route context shared by route-check and opponent play.

    Auto keeps book BFS for in-graph targets and a strict line off graph.
    Prefer_line combines saved edges with the graph, retaining alternate routes
    and indexing a separate position-based opponent preference.
    """
    if route_mode == "prefer_line":
        return _preferred_route_map(routing, target_fen, drill_line)
    if route_mode != "auto":
        raise ValueError("Unknown drill route mode")
    normalized_target = normalize_fen(target_fen)
    if routing.has_position(normalized_target):
        return get_drill_route_map(routing, normalized_target)
    if not drill_line:
        # Caller raises 400 on the resulting empty plies_by_fen.
        return DrillRouteMap(target_fen=normalized_target, plies_by_fen={})
    return build_line_route_map(drill_line)


def route_preserving_moves(
    routing: RoutingView,
    route_map: DrillRouteMap,
    fen: str,
) -> list[DrillRouteMove]:
    if route_map.forward_moves is not None:
        # Strict line map: the single on-route continuation (no graph node).
        return list(route_map.forward_moves.get(normalize_fen(fen), []))
    normalized_fen = normalize_fen(fen)
    current_distance = route_map.plies_by_fen.get(normalized_fen)
    if current_distance is None:
        return []

    children = dict(routing.routing_children(normalized_fen)) if routing.has_position(normalized_fen) else {}
    children.update((route_map.supplemental_children or {}).get(normalized_fen, {}))
    moves: list[DrillRouteMove] = []
    for uci, child_fen in children.items():
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


def opponent_route_move(
    routing: RoutingView, route_map: DrillRouteMap, fen: str,
) -> DrillRouteMove | None:
    moves = route_preserving_moves(routing, route_map, fen)
    preferred = (route_map.preferred_ucis or {}).get(normalize_fen(fen))
    return next((move for move in moves if move.uci == preferred), moves[0] if moves else None)


def post_root_structural_moves(
    routing: RoutingView,
    fen: str,
) -> list[DrillStructuralMove]:
    """Return the preferred score-relevant opponent tier at ``fen``.

    Base reference children have priority and use the quality scorer's exact
    child-only middlegame boundary. Routing-overlay-only children are the fallback
    tier and use Coverage's stricter parent-and-child boundary. Only routing edges
    are considered; malformed, illegal, or topology-inconsistent edges are skipped.
    """

    normalized_fen = normalize_fen(fen)
    node = routing.get_node(normalized_fen)
    if node is None:
        return []

    reference_moves: list[DrillStructuralMove] = []
    overlay_moves: list[DrillStructuralMove] = []
    for uci, child_fen in sorted(routing.routing_children(normalized_fen).items()):
        try:
            resulting_fen = _resulting_fen(normalized_fen, uci)
            if resulting_fen != normalize_fen(child_fen):
                continue
            san = _san_for_uci(normalized_fen, uci)
            move = DrillStructuralMove(
                uci=uci,
                san=san,
                resulting_fen=resulting_fen,
            )
            if uci in node.children:
                if not is_middlegame_position(resulting_fen):
                    reference_moves.append(move)
            elif coverage_structural_edge_is_eligible(
                normalized_fen,
                resulting_fen,
                is_middlegame=is_middlegame_position,
            ):
                overlay_moves.append(move)
        except (IndexError, ValueError):
            continue

    return reference_moves or overlay_moves


def route_move_for_uci(
    routing: RoutingView,
    route_map: DrillRouteMap,
    fen: str,
    uci: str,
) -> DrillRouteMove | None:
    """Describe `uci` as a route move, or None if it does not preserve the route.

    This never accepts or rejects a move: the off-route branch in drills.py has
    already decided the session failed by the time it calls here, and only uses
    the result to label played_move_san. It reads routing children purely so it
    cannot disagree with route_preserving_moves about which edges exist.
    """
    return next((move for move in route_preserving_moves(routing, route_map, fen) if move.uci == uci), None)


def safe_san_for_uci(fen: str, uci: str) -> str | None:
    """Return SAN for uci from fen, or None if the move is illegal or the FEN is invalid."""
    try:
        return _san_for_uci(fen, uci)
    except ValueError:
        return None


def apply_uci_normalized(fen: str, uci: str) -> str:
    return _resulting_fen(fen, uci)


def replay_history_fen(moves: list[str]) -> str | None:
    """Normalized FEN reached by replaying ``moves`` from the initial position.

    ``None`` when any entry is not a legal move there — including a malformed string, a
    non-string, or a null move.

    This is what makes a move history EVIDENCE rather than an assertion. The opponent
    endpoint records ``ply_before = len(request.moves)`` and drill root confirmation
    treats that number as authoritative, so without a replay a client could pair a
    genuine on-route FEN with a truncated history, be served the real route move, and
    then confirm a boundary several plies too low — readmitting exactly the scripted
    prefix the boundary exists to exclude.

    Every drill starts from the standard position, which is why replaying from
    ``chess.Board()`` is a complete proof for the drill branch that uses it.
    """
    board = chess.Board()
    for uci in moves:
        if not isinstance(uci, str):
            return None
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            return None
        if move not in board.legal_moves:
            return None
        board.push(move)
    return normalize_fen(board.fen())


def _reset_drill_route_cache_for_testing() -> None:
    _ROUTE_CACHE.clear()
