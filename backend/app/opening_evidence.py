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

from app.analysis_profiles import (
    IDENTITY_FIELDS,
    StrengthComparison,
    compare_search_strength,
    get_profile,
)
from app.analysis_trust import cache_row_as_move_dict, move_trust_flags
from app.fen import normalize_fen
from app.game_phase import (
    ContinuityError,
    divide,
    is_opening_premove,
    reconstruct_board_sequence,
)
from app.models import AnalysisCache
from app.opening_graph import OpeningGraph
from app.opening_quality import cache_row_to_mover_evals, move_quality
from app.position_analysis_repo import resolve_trusted_positions

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
OPENING_EVIDENCE_INPUTS_VERSION = "raw-v4"

# Session-eligibility predicate applied to EVERY session-scoped evidence
# selection (aliased ``gs``). The overlay and the freshness digest must apply
# it identically — the digest is the freshness proxy for the overlay's inputs.
#
# Only terminal sessions feed opening evidence. session_moves are upserted
# per-move during live play, so an in-progress session would flip the digest
# on every move and force a full overlay rebuild per recompute trigger
# (g-dmd1: continuous >90% CPU recompute loop during gameplay).
#
# The one terminal-while-active state is an accuracy-failed drill:
# POST /api/drills/{id}/fail computes an opening-score delta WITHOUT ending
# the session (``status`` stays 'active' so /continue can convert it into a
# rated game), so its quiescent played chain must count. A /continue flips
# ``drill_state`` to 'converted' → excluded again until the true session end.
# Off-route-failed drills compute no delta and their final move may not be
# durable yet, so they wait for the session to properly end.
SESSION_EVIDENCE_ELIGIBLE_SQL = (
    "(gs.status = 'ended' OR (gs.drill_state = 'failed'"
    " AND gs.drill_terminal_reason = 'accuracy'))"
)

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
        text(f"""
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
              AND {SESSION_EVIDENCE_ELIGIBLE_SQL}
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
    """Upgrade eval_delta-only user moves to trusted win-chance quality.

    Pairs a TRUSTED position best eval with the MOVE-trusted played eval, then
    rescores — never reading the move row's own (possibly duplicated/untrusted)
    ``best_eval`` as position truth (the duplicated-best-move bug from the parent
    epic). Mirrors the cross-grain pairing in
    ``app.api.analysis._position_eval_loss_cp``:

    * **Position best** from :func:`resolve_trusted_positions` — the
      ``position_analysis`` storage winner or the strongest trusted legacy
      ``resolver-complete-v2`` ``analysis_cache`` row at the normalized FEN.
    * **Played eval** from the exact ``(fen_before, move_uci)`` ``analysis_cache``
      row, gated by ``move_trust_flags``.
    * **Equal search strength** — both profiles must be ``StrengthComparison.EQUAL``
      before their win-chances are differenced; subtracting evals across
      different-strength runs is invalid even through the saturating win-chance
      curve.

    Only applies when the primary session evals were absent, so it strictly
    improves on the deterministic eval_delta fallback; on any failed guard the
    move keeps its eval_delta quality.
    """
    if not candidates:
        return

    fen_set = sorted({fen for _, fen, _, _ in candidates})
    rows = db.query(AnalysisCache).filter(AnalysisCache.fen_before.in_(fen_set)).all()
    by_key = {(r.fen_before, r.move_uci): r for r in rows}

    # Candidate fens already normalized successfully upstream (_build_move_rows
    # calls normalize_fen unguarded before a candidate is appended), so this is safe.
    norm_by_fen = {fen: normalize_fen(fen) for fen in fen_set}
    trusted = resolve_trusted_positions(db, set(norm_by_fen.values()))

    for move_index, fen_before, uci, stm in candidates:
        row = by_key.get((fen_before, uci))
        if row is None:
            continue
        tp = trusted.get(norm_by_fen[fen_before])
        if tp is None:
            continue
        if not move_trust_flags(cache_row_as_move_dict(row))[2]:
            continue
        pp = get_profile(tp.analysis_profile_id)
        mp = get_profile(row.analysis_profile_id)
        if pp is None or mp is None:
            continue
        if compare_search_strength(pp, mp) is not StrengthComparison.EQUAL:
            continue
        mover_evals = cache_row_to_mover_evals(
            row.played_eval,
            row.played_eval_mate,
            tp.best_eval,
            tp.best_eval_mate,
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
    # Manually-seeded blunders (gs.id IS NULL) are always eligible; a blunder
    # recorded mid-game in a live session waits for that session to terminate
    # (blunders are written at detection time, so they would otherwise flip the
    # digest during play).
    rows = db.execute(
        text(f"""
            SELECT p.fen_raw
            FROM blunders b
            JOIN positions p ON p.id = b.position_id
            LEFT JOIN game_sessions gs ON gs.id = b.source_session_id
            WHERE b.user_id = :user_id
              AND (gs.player_color = :player_color
                   OR (b.source_session_id IS NULL AND p.active_color = :player_color))
              AND (gs.id IS NULL OR (gs.session_mode IN ('normal', 'drill')
                   AND {SESSION_EVIDENCE_ELIGIBLE_SQL}))
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
    # Deliberately NOT gated on SESSION_EVIDENCE_ELIGIBLE_SQL: ``gs`` here is
    # the blunder's ORIGINATING game session, which is long ended by review
    # time; a new review row flips the digest via ``reviewed_at`` regardless.
    # If a gate is ever added, apply it to BOTH this query and digest
    # section 4 together.
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
    session moves are hashed, not just opening-interval premoves; the candidate
    set is every eligible-session player-color move lacking a primary eval, not
    just opening premoves): a broader digest can only cause an unnecessary
    (still-correct) rebuild, never a missed change. Semantic-only changes that
    leave every raw row unchanged are covered by
    ``OPENING_EVIDENCE_INPUTS_VERSION``, folded in by the caller.

    The cache fallback now consumes TWO grains (a trusted position best paired
    with a move-trusted played eval), so the digest tracks both: the exact
    ``(fen_before, move_uci)`` move rows plus their trust columns, AND the trusted
    position sources (``position_analysis`` storage winner and the legacy
    ``analysis_cache`` rows that feed ``_legacy_position_sort_key``) at the
    candidate NORMALIZED FENs. The normalized keys are derived with
    ``normalize_fen`` in Python — exactly as the runtime consumer keys
    ``resolve_trusted_positions`` — NOT from the nullable stored
    ``normalized_fen_before`` column, so digest key == runtime key by construction.

    MAINTENANCE (g-mxeo): the SESSION-SCOPED sources below — session_moves (SM|),
    ghost-target blunders via ``source_session_id`` (GT|), and blunder_reviews
    (BR|) — are ALSO enumerated by the opening-baseline persist guard in
    ``opening_score_delta.run_baseline_snapshot_job`` (its NOT EXISTS clauses),
    which is that guard's airtight, clock-independent correctness check. If you add
    a new source here that a single session can contribute (i.e. scoped to a
    session_id / source_session_id), add a matching NOT EXISTS clause there too, or
    a session feeding only the new source could receive a wrongly-attributed
    baseline.
    """
    lines: list[str] = []

    # 1. Session moves (mirrors _build_move_rows). Same SELECT, but we hash the
    #    scalar columns and stop instead of replaying boards.
    session_rows = db.execute(
        text(f"""
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
              AND {SESSION_EVIDENCE_ELIGIBLE_SQL}
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

    # 2. Trusted-source fallback subset (mirrors _apply_cache_fallbacks /
    #    resolve_trusted_positions). The cache fallback now pairs a TRUSTED
    #    position best eval with a MOVE-trusted played eval, so the digest tracks
    #    BOTH grains and their trust columns — not just the raw best_eval/
    #    played_eval it used to hash.
    #
    #    Candidate fen_before set: this user's player-color session moves lacking a
    #    primary eval (the only positions a fallback can consult). Normalized keys
    #    are derived with normalize_fen IN PYTHON — exactly as the runtime keys
    #    resolve_trusted_positions — NOT from the nullable stored
    #    normalized_fen_before column (which may be NULL/stale on a still
    #    move-trusted row), so digest key == runtime key by construction.
    candidate_rows = db.execute(
        text(f"""
            SELECT DISTINCT sm.fen_before
            FROM session_moves sm
            JOIN game_sessions gs ON gs.id = sm.session_id
            WHERE gs.user_id = :user_id
              AND gs.player_color = :player_color
              AND sm.color = :player_color
              AND sm.fen_before IS NOT NULL
              AND (sm.eval_cp IS NULL OR sm.best_move_eval_cp IS NULL)
              AND gs.session_mode IN ('normal', 'drill')
              AND {SESSION_EVIDENCE_ELIGIBLE_SQL}
        """),
        {"user_id": user_id, "player_color": player_color},
    ).fetchall()
    candidate_fens = [fen for (fen,) in candidate_rows]
    norm_keys: set[str] = set()
    for fen in candidate_fens:
        try:
            norm_keys.add(normalize_fen(fen))
        except Exception:
            # A fen that fails to normalize never produces a consumed candidate at
            # runtime (the overlay normalizes it unguarded upstream), so skipping
            # it here cannot miss a change.
            continue

    ident_cols = ", ".join(IDENTITY_FIELDS)

    def _ident(r) -> str:
        return "|".join(str(getattr(r, f, None)) for f in IDENTITY_FIELDS)

    # 2a. Move grain: the exact (fen_before, move_uci) analysis_cache rows the
    #     fallback reads for a played eval, plus the columns the MOVE-trust gate
    #     reads (a move-trust flip must change the digest): played eval, the
    #     ``classification`` the move-complete contract requires, and the
    #     profile/contract/IDENTITY trust columns. best_eval is no longer consumed
    #     as truth from this row but is harmless to keep hashing.
    if candidate_fens:
        move_stmt = text(f"""
            SELECT fen_before, move_uci, played_eval, played_eval_mate,
                   best_eval, best_eval_mate, classification,
                   analysis_profile_id, evidence_contract_id, {ident_cols}
            FROM analysis_cache
            WHERE fen_before IN :fens
        """).bindparams(bindparam("fens", expanding=True))
        for r in db.execute(move_stmt, {"fens": candidate_fens}).fetchall():
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
                        str(r.classification),
                        str(r.analysis_profile_id),
                        str(r.evidence_contract_id),
                        _ident(r),
                    )
                )
            )

    # 2b. Position grain at the candidate NORMALIZED FENs (the two tiers
    #     resolve_trusted_positions ranks).
    if norm_keys:
        norm_list = sorted(norm_keys)
        # (i) position_analysis storage winner (tier 1).
        pa_stmt = text(f"""
            SELECT normalized_fen, best_move_uci, best_line_uci,
                   best_eval, best_eval_mate,
                   analysis_profile_id, evidence_contract_id, {ident_cols}
            FROM position_analysis
            WHERE normalized_fen IN :norms
        """).bindparams(bindparam("norms", expanding=True))
        for r in db.execute(pa_stmt, {"norms": norm_list}).fetchall():
            lines.append(
                "PA|"
                + "|".join(
                    (
                        str(r.normalized_fen),
                        str(r.best_move_uci),
                        str(r.best_line_uci),
                        str(r.best_eval),
                        str(r.best_eval_mate),
                        str(r.analysis_profile_id),
                        str(r.evidence_contract_id),
                        _ident(r),
                    )
                )
            )
        # (ii) Legacy trusted fallback (tier 2): the analysis_cache rows at each
        #      candidate normalized_fen_before that feed _legacy_position_sort_key.
        #      Hash the FULL sort-key input set (id, source, move_uci,
        #      best_move_uci, best_eval, best_eval_mate) so the SELECTED legacy
        #      winner cannot change without flipping the digest, PLUS the columns
        #      the position-trust FILTER reads before ranking (best_line_uci, the
        #      profile/contract/IDENTITY trust columns) so a trust flip that adds or
        #      drops a candidate also flips the digest. ``normalized_fen_before`` is
        #      hashed too: ``resolve_trusted_positions`` GROUPS by it
        #      (position_analysis_repo._legacy_position_sort_key callsite), so a row
        #      that moves between two candidate norms stays in this IN-set but is
        #      reassigned to a different position — without the column hashed the
        #      digest would miss that change and serve a stale overlay.
        legacy_stmt = text(f"""
            SELECT id, source, move_uci, normalized_fen_before,
                   best_move_uci, best_line_uci, best_eval, best_eval_mate,
                   analysis_profile_id, evidence_contract_id, {ident_cols}
            FROM analysis_cache
            WHERE normalized_fen_before IN :norms
        """).bindparams(bindparam("norms", expanding=True))
        for r in db.execute(legacy_stmt, {"norms": norm_list}).fetchall():
            lines.append(
                "ACP|"
                + "|".join(
                    (
                        str(r.id),
                        str(r.source),
                        str(r.move_uci),
                        str(r.normalized_fen_before),
                        str(r.best_move_uci),
                        str(r.best_line_uci),
                        str(r.best_eval),
                        str(r.best_eval_mate),
                        str(r.analysis_profile_id),
                        str(r.evidence_contract_id),
                        _ident(r),
                    )
                )
            )

    # 3. Ghost targets (mirrors _collect_ghost_targets).
    ghost_rows = db.execute(
        text(f"""
            SELECT p.fen_raw
            FROM blunders b
            JOIN positions p ON p.id = b.position_id
            LEFT JOIN game_sessions gs ON gs.id = b.source_session_id
            WHERE b.user_id = :user_id
              AND (gs.player_color = :player_color
                   OR (b.source_session_id IS NULL AND p.active_color = :player_color))
              AND (gs.id IS NULL OR (gs.session_mode IN ('normal', 'drill')
                   AND {SESSION_EVIDENCE_ELIGIBLE_SQL}))
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
