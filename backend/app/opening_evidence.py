"""Overlay user evidence from DB onto the opening graph.

Maps session_moves, blunders, and blunder_reviews for a (user_id, player_color)
pair onto the opening graph, producing per-node and per-edge counters that the
downstream score calculator consumes. No score computation happens here.

For opening-score v2 this module also:

- reconstructs each session's board line and runs the exact Lichess phase
  divider (``app.game_phase``) so only moves inside the opening interval become
  evidence;
- attaches a continuous ``[0, 1]`` move quality (``app.opening_quality``)
  alongside the legacy binary pass/fail counters (kept for SRS/debug contracts);
- records each qualifying session move exactly once by stable
  ``(session_id, move_number, color)`` identity, so the legacy pass-2/pass-3
  book-exit traversal cannot double-count a transposed move; and
- tracks quality-source counts and excluded-session counts for rollout
  telemetry.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import chess
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.fen import normalize_fen
from app.game_phase import (
    ContinuityError,
    divide,
    is_opening_premove,
    reconstruct_board_sequence,
)
from app.opening_graph import OpeningGraph
from app.opening_quality import cache_row_to_mover_evals, move_quality

logger = logging.getLogger(__name__)

PASS_THRESHOLD = 50  # eval_delta < this → pass (legacy binary signal, SRS/debug)

# Evidence-derivation logic version. The cheap raw-input freshness digest
# (``raw_evidence_inputs_digest``) hashes the RAW DB rows the overlay consumes,
# not the derived overlay. A raw-row hash is blind to SEMANTIC changes in the
# derivation code that change the overlay WITHOUT changing any raw row. This
# version is folded into the digest to force a self-healing recompute on any
# such change.
#
# BUMP DISCIPLINE — bump this on ANY change to evidence-derivation semantics,
# including:
#   - PASS_THRESHOLD (feeds live_passes → child selection/weights);
#   - quality-source precedence/ordering in ``_apply_cache_fallbacks`` or
#     ``move_quality`` consumption;
#   - ``normalize_fen`` behavior or how/where it is applied;
#   - the phase filter / divider application in ``_build_move_rows`` /
#     ``is_opening_premove`` (the digest deliberately does NOT replay it);
#   - the SQL projection or filters in ``raw_evidence_inputs_digest`` below; or
#   - the set of columns the overlay consumes from any of these tables.
# (DIVIDER_VERSION, QUALITY_VERSION, TAU_WC, TAU_CP, SCORE_MODEL_VERSION already
# live in ``opening_score_inputs_fingerprint`` and remain there.)
OPENING_EVIDENCE_INPUTS_VERSION = "raw-v2"

# White-before-black ordering within a single (session, move_number).
_COLOR_RANK = {"white": 0, "black": 1}

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

@dataclass(slots=True)
class NodeEvidence:
    fen: str
    live_attempts: int = 0
    live_passes: int = 0
    live_fails: int = 0
    # Continuous quality (opening-score v2). quality_count is the number of
    # quality observations; quality_sum their total. The calculator forms local
    # mastery from these, not from the binary pass/fail counts above.
    quality_sum: float = 0.0
    quality_count: int = 0
    # Distinct game_sessions that contributed live (played) evidence at this node.
    # Counts games, not move-observations: a single game that revisits the same
    # position adds one session id. The score calculator unions these across a
    # reachable subtree to surface a true "Games" count (distinct from
    # ``quality_count``, which counts move-observations).
    session_ids: set[str] = field(default_factory=set)
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
    quality_sum: float = 0.0
    quality_count: int = 0


@dataclass(slots=True)
class PhaseSample:
    """Per-session phase-horizon telemetry captured where ``divide`` already runs.

    ``opening_interval_len`` is the divider's opening size (``middle`` when a
    middlegame boundary exists, else the full ply count). ``middle_ply`` and
    ``end_ply`` are the raw divider indices (``None`` when not reached). Purely
    diagnostic for calibration; carries no effect on scoring.
    """

    opening_interval_len: int
    middle_ply: int | None
    end_ply: int | None


@dataclass
class EvidenceOverlay:
    user_id: int
    player_color: str
    nodes: dict[str, NodeEvidence] = field(default_factory=dict)
    edges: dict[tuple[str, str], EdgeEvidence] = field(default_factory=dict)
    # Rollout telemetry: how many quality observations came from each source, and
    # how many sessions were dropped for broken board continuity.
    source_counts: Counter[str] = field(default_factory=Counter)
    excluded_sessions: int = 0
    # Per-session horizon telemetry from the Lichess divider (calibration only).
    phase_samples: list[PhaseSample] = field(default_factory=list)


@dataclass(slots=True)
class _MoveRow:
    session_id: str
    move_number: int
    color: str
    norm_before: str
    norm_after: str
    fen_before_raw: str
    move_san: str
    eval_delta: int | None
    quality: float | None
    quality_source: str | None
    session_ts: datetime | str | None

    @property
    def identity(self) -> tuple[str, int, str]:
        """Stable per-move identity used to record each session move once."""
        return (str(self.session_id), self.move_number, self.color)


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


def _record_node(overlay: EvidenceOverlay, mr: _MoveRow) -> None:
    """Record one user move's evidence at its pre-move position.

    Skips entirely when the move carries neither a binary signal nor a quality
    observation, so positions with no usable evidence are not materialised.
    """
    if mr.eval_delta is None and mr.quality is None:
        return
    node = _get_or_create_node(overlay.nodes, mr.norm_before)
    node.session_ids.add(mr.session_id)
    if mr.eval_delta is not None:
        node.live_attempts += 1
        if mr.eval_delta < PASS_THRESHOLD:
            node.live_passes += 1
        else:
            node.live_fails += 1
    if mr.quality is not None:
        node.quality_sum += mr.quality
        node.quality_count += 1
        overlay.source_counts[mr.quality_source] += 1
    ts = _parse_ts(mr.session_ts)
    if ts is not None:
        if node.last_live_at is None or ts > node.last_live_at:
            node.last_live_at = ts


def _record_edge(
    edges: dict[tuple[str, str], EdgeEvidence],
    mr: _MoveRow,
    uci: str,
    is_user_move: bool,
) -> None:
    edge = _get_or_create_edge(edges, mr.norm_before, mr.norm_after, uci)
    edge.traversal_count += 1
    if is_user_move and mr.eval_delta is not None:
        edge.live_attempts += 1
        if mr.eval_delta < PASS_THRESHOLD:
            edge.live_passes += 1
        else:
            edge.live_fails += 1
    if is_user_move and mr.quality is not None:
        edge.quality_sum += mr.quality
        edge.quality_count += 1


def _apply_move(
    overlay: EvidenceOverlay,
    mr: _MoveRow,
    uci: str,
    is_user_move: bool,
    recorded: set[tuple[str, int, str]],
) -> None:
    """Record a move's node/edge evidence at most once per session-move identity.

    The legacy book-exit traversal can reach the same physical move through both
    the pass-2 (book exit) and pass-3 (extension/transposition) routes. Guarding
    on identity here is what makes a transposed move contribute exactly once.
    """
    if mr.identity in recorded:
        return
    recorded.add(mr.identity)
    if is_user_move:
        _record_node(overlay, mr)
    _record_edge(overlay.edges, mr, uci, is_user_move)


def _build_move_rows(
    db: Session,
    user_id: int,
    player_color: str,
    overlay: EvidenceOverlay,
) -> list[_MoveRow]:
    """Load session moves, phase-tag them, and attach continuous quality.

    Groups rows by session, reconstructs each board line, runs the exact Lichess
    divider, and keeps only moves whose pre-move position is inside the opening
    interval. Sessions with broken continuity are excluded and counted.
    """
    rows = db.execute(
        text("""
            SELECT sm.session_id, sm.move_number, sm.color,
                   sm.fen_before, sm.fen_after, sm.move_san,
                   sm.eval_delta, sm.eval_cp, sm.best_move_eval_cp,
                   gs.started_at AS session_ts
            FROM session_moves sm
            JOIN game_sessions gs ON gs.id = sm.session_id
            WHERE gs.user_id = :user_id
              AND gs.player_color = :player_color
              AND sm.fen_before IS NOT NULL
              AND gs.session_mode IN ('normal', 'drill')
        """),
        {"user_id": user_id, "player_color": player_color},
    ).fetchall()

    by_session: dict[str, list] = defaultdict(list)
    for row in rows:
        by_session[str(row.session_id)].append(row)

    # First pass per session: reconstruct the line, divide it, and keep opening
    # moves. Quality is resolved from the primary session_moves evals or the
    # eval_delta fallback now; analysis_cache fallbacks are filled in afterwards.
    opening_moves: list[_MoveRow] = []
    # Candidates needing an analysis_cache lookup: index into opening_moves plus
    # the (fen_before_raw, uci, side_to_move) lookup key.
    cache_candidates: list[tuple[int, str, str, str]] = []

    for srows in by_session.values():
        srows.sort(key=lambda r: (r.move_number, _COLOR_RANK.get(r.color, 0)))
        triples = [(r.fen_before, r.fen_after, r.move_san) for r in srows]
        try:
            boards = reconstruct_board_sequence(triples)
        except ContinuityError as exc:
            overlay.excluded_sessions += 1
            logger.warning(
                "excluding session %s from opening evidence: %s",
                srows[0].session_id,
                exc,
            )
            continue

        division = divide(boards)
        overlay.phase_samples.append(
            PhaseSample(
                opening_interval_len=division.opening_size,
                middle_ply=division.middle,
                end_ply=division.end,
            )
        )
        for index, r in enumerate(srows):
            if not is_opening_premove(division, index):
                continue
            norm_before = normalize_fen(r.fen_before)
            norm_after = normalize_fen(r.fen_after)
            quality, source = move_quality(
                eval_cp=r.eval_cp,
                best_move_eval_cp=r.best_move_eval_cp,
                eval_delta=r.eval_delta,
            )
            mr = _MoveRow(
                session_id=str(r.session_id),
                move_number=r.move_number,
                color=r.color,
                norm_before=norm_before,
                norm_after=norm_after,
                fen_before_raw=r.fen_before,
                move_san=r.move_san,
                eval_delta=r.eval_delta,
                quality=quality,
                quality_source=source,
                session_ts=r.session_ts,
            )
            # A user move that fell back to eval_delta (or has no signal yet) may
            # be upgradable to a win-chance score from analysis_cache.
            needs_cache = (
                r.color == player_color
                and (r.eval_cp is None or r.best_move_eval_cp is None)
            )
            if needs_cache:
                uci = _uci_from_san(norm_before, r.move_san)
                if uci is not None:
                    stm = "w" if " w " in r.fen_before else "b"
                    cache_candidates.append((len(opening_moves), r.fen_before, uci, stm))
            opening_moves.append(mr)

    _apply_cache_fallbacks(db, opening_moves, cache_candidates)
    return opening_moves


def _apply_cache_fallbacks(
    db: Session,
    opening_moves: list[_MoveRow],
    candidates: list[tuple[int, str, str, str]],
) -> None:
    """Upgrade eval_delta-only user moves to analysis_cache win-chance quality.

    Looks up matching ``analysis_cache`` rows by ``(fen_before, move_uci)``,
    converts their white-relative evals to mover perspective, and rescores. Only
    applies when the primary session evals were absent, so it strictly improves
    on the deterministic eval_delta fallback.
    """
    if not candidates:
        return

    fen_set = sorted({fen for _, fen, _, _ in candidates})
    stmt = text("""
        SELECT fen_before, move_uci, played_eval, played_eval_mate,
               best_eval, best_eval_mate
        FROM analysis_cache
        WHERE fen_before IN :fens
    """).bindparams(bindparam("fens", expanding=True))
    cache_rows = db.execute(stmt, {"fens": fen_set}).fetchall()
    by_key = {(r.fen_before, r.move_uci): r for r in cache_rows}

    for move_index, fen_before, uci, stm in candidates:
        cache_row = by_key.get((fen_before, uci))
        if cache_row is None:
            continue
        mover_evals = cache_row_to_mover_evals(
            cache_row.played_eval,
            cache_row.played_eval_mate,
            cache_row.best_eval,
            cache_row.best_eval_mate,
            stm,
        )
        if mover_evals is None:
            continue
        mr = opening_moves[move_index]
        quality, source = move_quality(
            eval_cp=None,
            best_move_eval_cp=None,
            eval_delta=mr.eval_delta,
            cache_mover_evals=mover_evals,
        )
        mr.quality = quality
        mr.quality_source = source


def _collect_session_moves(
    db: Session,
    user_id: int,
    player_color: str,
    graph: OpeningGraph,
    overlay: EvidenceOverlay,
) -> None:
    moves = _build_move_rows(db, user_id, player_color, overlay)

    # Index opening moves by their normalized pre-move FEN for extension BFS.
    move_chains: dict[str, list[_MoveRow]] = defaultdict(list)
    for mr in moves:
        move_chains[mr.norm_before].append(mr)

    # Each session move is recorded at most once across passes 2 and 3.
    recorded: set[tuple[str, int, str]] = set()

    # Pass 2: process in-book moves and collect book-boundary exits.
    book_exits: set[str] = set()

    for mr in moves:
        if not graph.has_position(mr.norm_before):
            continue

        is_user = mr.color == player_color

        # Check if this edge exists in the book graph.
        book_uci = _resolve_edge_uci(graph, mr.norm_before, mr.norm_after)
        if book_uci is not None:
            # Normal in-book edge.
            _apply_move(overlay, mr, book_uci, is_user, recorded)
        else:
            # Book exit: parent in book, but the edge is not a book edge.
            # This includes moves to off-book positions AND non-book edges
            # that happen to land on a position known elsewhere in the graph.
            uci = _uci_from_san(mr.norm_before, mr.move_san)
            if uci is None:
                continue
            _apply_move(overlay, mr, uci, is_user, recorded)
            book_exits.add(mr.norm_after)

    # Pass 3: follow observed continuations from book-boundary exits. There is no
    # fixed user-decision cutoff: the opening phase is the sole horizon, and
    # ``move_chains`` already contains only opening-interval premoves (everything
    # at or beyond the divider's middlegame boundary was filtered out in
    # ``_build_move_rows``). A visited-FEN guard terminates transpositions and
    # cycles; ``recorded`` keeps each session move's contribution to exactly one.
    frontier = list(book_exits)
    visited: set[str] = set(book_exits)

    while frontier:
        current_fen = frontier.pop()
        for mr in move_chains.get(current_fen, []):
            is_user = mr.color == player_color
            uci = _uci_from_san(mr.norm_before, mr.move_san)
            if uci is None:
                continue
            _apply_move(overlay, mr, uci, is_user, recorded)
            if mr.norm_after not in visited:
                visited.add(mr.norm_after)
                frontier.append(mr.norm_after)


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
              AND (gs.id IS NULL OR gs.session_mode IN ('normal', 'drill'))
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
              AND (gs.id IS NULL OR gs.session_mode IN ('normal', 'drill'))
        """),
        {"user_id": user_id, "player_color": player_color},
    ).fetchall()

    for fen_raw, passed, reviewed_at in rows:
        norm = normalize_fen(fen_raw)
        if not graph.has_position(norm) and norm not in overlay.nodes:
            continue
        # Reviews contribute discounted confidence/coverage evidence only; they
        # never add a mastery (quality) observation, so quality counters are
        # untouched here.
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


def observed_off_book_fens(
    overlay: EvidenceOverlay, graph: OpeningGraph
) -> set[str]:
    """Normalized off-book FENs that appear as observed-edge endpoints.

    Explicit contract for the tree position-score read model (g-tree-score-model):
    these are the candidate observed off-book scorer positions — endpoints of the
    user's observed continuation edges (``overlay.edges``) that are not reference
    ``OpeningGraph`` positions. ``overlay.edges`` keys are already normalized
    4-field FENs (``_record_edge`` keys on the move row's ``norm_before`` /
    ``norm_after``), so no renormalization is needed.

    This is a superset of what actually gets scored: the calculator admits only the
    subset reachable from book seeds via observed edges (its domain enumeration over
    ``_structural_children``). Disconnected off-book endpoints — e.g. a manually
    seeded blunder with no observed path into the book — are deliberately not
    seeded into the scorer, matching the off-book seed semantics in the design.
    """
    result: set[str] = set()
    for parent_fen, child_fen in overlay.edges:
        for endpoint in (parent_fen, child_fen):
            if not graph.has_position(endpoint):
                result.add(endpoint)
    return result


def _digest_ts(val: datetime | str | None) -> str:
    """Canonicalise a timestamp for the digest, dialect-agnostically.

    SQLite returns timestamps as strings and Postgres as datetimes; normalise
    both through ``_parse_ts`` so the same stored instant hashes identically.
    """
    ts = _parse_ts(val)
    return ts.isoformat() if ts is not None else ""


def raw_evidence_inputs_digest(
    db: Session,
    user_id: int,
    player_color: str,
) -> str:
    """Cheap freshness digest over the RAW DB rows ``overlay_evidence`` consumes.

    Hashes a canonical, order-independent projection of exactly the raw rows the
    overlay reads — and nothing derived. No python-chess, no board reconstruction,
    no divider, no overlay build. Same raw inputs → identical digest → provably
    identical overlay → identical scores, so a matching digest lets the cache
    serve a batch without paying the per-session board-replay cost.

    Scoping is INTENTIONALLY broad where it diverges from the overlay (e.g. all
    session moves are hashed, not just opening-interval premoves; the
    analysis_cache subset is keyed by fen only, not (fen, uci)): a broader digest
    can only cause an unnecessary (still-correct) rebuild, never a missed change.
    Semantic-only changes that leave every raw row unchanged are covered by
    ``OPENING_EVIDENCE_INPUTS_VERSION``, folded in by the caller.
    """
    lines: list[str] = []

    # 1. Session moves (mirrors _build_move_rows). Same SELECT, but we hash the
    #    scalar columns and stop instead of replaying boards.
    session_rows = db.execute(
        text("""
            SELECT sm.session_id, sm.move_number, sm.color,
                   sm.fen_before, sm.fen_after, sm.move_san,
                   sm.eval_delta, sm.eval_cp, sm.best_move_eval_cp,
                   gs.started_at AS session_ts
            FROM session_moves sm
            JOIN game_sessions gs ON gs.id = sm.session_id
            WHERE gs.user_id = :user_id
              AND gs.player_color = :player_color
              AND sm.fen_before IS NOT NULL
              AND gs.session_mode IN ('normal', 'drill')
        """),
        {"user_id": user_id, "player_color": player_color},
    ).fetchall()
    for r in session_rows:
        lines.append(
            "SM|"
            + "|".join(
                (
                    str(r.session_id),
                    str(r.move_number),
                    str(r.color),
                    str(r.fen_before),
                    str(r.fen_after),
                    str(r.move_san),
                    str(r.eval_delta),
                    str(r.eval_cp),
                    str(r.best_move_eval_cp),
                    _digest_ts(r.session_ts),
                )
            )
        )

    # 2. analysis_cache fallback subset (mirrors _apply_cache_fallbacks). Bounded
    #    to fen_before values that appear in THIS user's player-color session
    #    moves lacking a primary eval — the only rows the overlay can consult.
    cache_rows = db.execute(
        text("""
            SELECT ac.fen_before, ac.move_uci, ac.played_eval, ac.played_eval_mate,
                   ac.best_eval, ac.best_eval_mate
            FROM analysis_cache ac
            WHERE ac.fen_before IN (
                SELECT sm.fen_before
                FROM session_moves sm
                JOIN game_sessions gs ON gs.id = sm.session_id
                WHERE gs.user_id = :user_id
                  AND gs.player_color = :player_color
                  AND sm.color = :player_color
                  AND sm.fen_before IS NOT NULL
                  AND (sm.eval_cp IS NULL OR sm.best_move_eval_cp IS NULL)
                  AND gs.session_mode IN ('normal', 'drill')
            )
        """),
        {"user_id": user_id, "player_color": player_color},
    ).fetchall()
    for r in cache_rows:
        lines.append(
            "AC|"
            + "|".join(
                (
                    str(r.fen_before),
                    str(r.move_uci),
                    str(r.played_eval),
                    str(r.played_eval_mate),
                    str(r.best_eval),
                    str(r.best_eval_mate),
                )
            )
        )

    # 3. Ghost targets (mirrors _collect_ghost_targets).
    ghost_rows = db.execute(
        text("""
            SELECT p.fen_raw
            FROM blunders b
            JOIN positions p ON p.id = b.position_id
            LEFT JOIN game_sessions gs ON gs.id = b.source_session_id
            WHERE b.user_id = :user_id
              AND (gs.player_color = :player_color
                   OR (b.source_session_id IS NULL AND p.active_color = :player_color))
              AND (gs.id IS NULL OR gs.session_mode IN ('normal', 'drill'))
        """),
        {"user_id": user_id, "player_color": player_color},
    ).fetchall()
    for (fen_raw,) in ghost_rows:
        lines.append("GT|" + str(fen_raw))

    # 4. Blunder reviews (mirrors _collect_reviews).
    review_rows = db.execute(
        text("""
            SELECT p.fen_raw, br.passed, br.reviewed_at
            FROM blunder_reviews br
            JOIN blunders b ON b.id = br.blunder_id
            JOIN positions p ON p.id = b.position_id
            LEFT JOIN game_sessions gs ON gs.id = b.source_session_id
            WHERE b.user_id = :user_id
              AND (gs.player_color = :player_color
                   OR (b.source_session_id IS NULL AND p.active_color = :player_color))
              AND (gs.id IS NULL OR gs.session_mode IN ('normal', 'drill'))
        """),
        {"user_id": user_id, "player_color": player_color},
    ).fetchall()
    for fen_raw, passed, reviewed_at in review_rows:
        lines.append(
            "BR|" + "|".join((str(fen_raw), str(bool(passed)), _digest_ts(reviewed_at)))
        )

    # Order-independent: sort the projected lines before hashing so the digest is
    # invariant to row return order. The version is folded in by the caller.
    lines.sort()
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
