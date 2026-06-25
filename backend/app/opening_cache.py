from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.fen import normalize_fen
from app.game_phase import DIVIDER_VERSION
from app.models import (
    OpeningPositionEdge,
    OpeningPositionScore,
    OpeningScoreBatch,
    OpeningScoreCursor,
    UserOpeningScore,
)
from app.opening_aggregate import (
    CachedOpeningScoreRow,
    CachedPositionScoreRow,
    _batch_has_stale_branch_keys,
    _snapshot_cached_rows,
    _snapshot_position_rows,
)
from app.opening_evidence import (
    OPENING_EVIDENCE_INPUTS_VERSION,
    EdgeEvidence,
    EvidenceOverlay,
    overlay_evidence,
    raw_evidence_inputs_digest,
)
from app.opening_graph import OpeningGraph, get_opening_graph
from app.opening_quality import QUALITY_VERSION, TAU_CP, TAU_WC
from app.opening_rootcalc import (
    PositionCalcTelemetry,
    PositionScore,
    RootCalcConfig,
    RootScore,
    compute_all_scores,
    root_calc_config_fingerprint,
)
from app.opening_roots import OpeningRoots, get_opening_roots
from app.posthog_client import capture

logger = logging.getLogger(__name__)

PlayerColor = Literal["white", "black"]
_VALID_PLAYER_COLORS = {"white", "black"}

# Explicit score-model version. Bump on any change to the scoring model that is
# not already captured by graph/roots/config/quality fingerprints, to force a
# full recompute and invalidate stale snapshots.
#
# sm-v2-2: the batch now also carries opening_position_scores (the direct
# tree position read model, g-tree-score-model). Batches written before this
# version match the old fingerprint but hold zero position rows, so the fast path
# (recompute_opening_scores_if_needed) would serve them with no direct rows. The
# bump changes registry_fingerprint -> registry drift -> exactly one recompute per
# (user, color) on first read after deploy, backfilling position rows.
SCORE_MODEL_VERSION = "sm-v2-2"

# Persisted-read-model schema version. Bump when the SET of persisted batch
# read-model tables/columns changes (NOT the scoring math — that is
# SCORE_MODEL_VERSION). Folded into the registry fingerprint so a batch built
# before the change reports registry drift and recomputes once per (user, color)
# on first read, materializing the new rows.
#
# edges-v1: the batch now also carries opening_position_edges (the observed-edge
# tree read model, g-tree-fast-cache). A batch predating this version matches no
# current registry fingerprint, so the /tree path BLOCKS for a one-time bootstrap
# (see ensure_tree_cache) rather than serving a book-only tree that silently hides
# the user's observed moves until a later recompute lands.
OPENING_SCORE_CACHE_SCHEMA_VERSION = "edges-v1"

# Generous timeout for the one-time /tree bootstrap recompute: a heavy user pays
# the full overlay rebuild once, inline, so edges exist before the tree is served.
TREE_BOOTSTRAP_TIMEOUT = 30.0

# Number of latest batches to retain per (user_id, player_color). keep=2 protects
# a concurrent reader that holds the previous batch when a recompute lands; it does
# not guarantee safety against two rapid recomputes (see g-prune-score-cache plan).
OPENING_SCORE_BATCH_RETENTION = 2

# Time-decay staleness gate: wall-clock freshness decay (half-life 45d) means a
# frozen batch's confidence/coverage stop aging. Recompute at most once per interval
# to let decay keep moving without recomputing on every refresh.
OPENING_SCORE_DECAY_RECOMPUTE_INTERVAL = timedelta(days=1)


def _validate_player_color(player_color: str) -> None:
    if player_color not in _VALID_PLAYER_COLORS:
        raise ValueError(f"Unsupported player_color: {player_color}")


def opening_score_inputs_fingerprint(
    graph: OpeningGraph,
    roots: OpeningRoots,
) -> str:
    return (
        f"{graph.fingerprint}:{roots.fingerprint}:{root_calc_config_fingerprint()}"
        f":{SCORE_MODEL_VERSION}:{DIVIDER_VERSION}"
        f":{QUALITY_VERSION}:{TAU_WC!r}:{TAU_CP!r}"
        f":{OPENING_SCORE_CACHE_SCHEMA_VERSION}"
    )


def opening_score_raw_inputs_fingerprint(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> str:
    """Cheap freshness fingerprint computed from raw DB rows — no overlay build.

    ``overlay_evidence`` is a pure deterministic function of a fixed set of raw DB
    rows plus the graph/roots/config/version constants. So "did anything change?"
    can be answered by hashing the INPUTS to the derivation (cheap SQL, zero board
    work) instead of the OUTPUT (which forces the ~2.6s python-chess replay first).

    Composed from three independent change surfaces:
      - ``opening_score_inputs_fingerprint`` — graph/roots/config/scoring/divider/
        quality versions (registry/config);
      - ``OPENING_EVIDENCE_INPUTS_VERSION`` — evidence-derivation logic version,
        covering the residual ``opening_evidence`` semantics the registry does not
        plus the digest contract itself;
      - ``raw_evidence_inputs_digest`` — the ordered raw-row projection.

    If this matches the value stored on the batch, the overlay is provably
    identical and need never be built. A false-negative (digest changes when
    nothing relevant did) only causes an unnecessary, still-correct rebuild; the
    composition avoids false-positives (missing a real change) by covering all
    inputs plus the two logic versions.
    """
    _validate_player_color(player_color)
    graph = get_opening_graph()
    roots = get_opening_roots()
    registry_fp = opening_score_inputs_fingerprint(graph, roots)
    row_digest = raw_evidence_inputs_digest(db, user_id, player_color)
    return hashlib.sha256(
        f"{registry_fp}|{OPENING_EVIDENCE_INPUTS_VERSION}|{row_digest}".encode("utf-8")
    ).hexdigest()


def prune_old_opening_score_batches(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
    *,
    keep: int = OPENING_SCORE_BATCH_RETENTION,
) -> int:
    """Delete all but the newest `keep` batches for (user_id, player_color).

    Cascade (user_opening_scores.batch_id ON DELETE CASCADE) removes the snapshot
    rows. Best-effort: rolls back and swallows on failure so a failed DELETE never
    leaves the shared session in a failed-transaction state or fails the request.
    """
    _validate_player_color(player_color)
    try:
        stale_ids = [
            row.id
            for row in (
                db.query(OpeningScoreBatch.id)
                .filter(
                    OpeningScoreBatch.user_id == user_id,
                    OpeningScoreBatch.player_color == player_color,
                )
                .order_by(OpeningScoreBatch.generation.desc())
                .offset(keep)
                .all()
            )
        ]
        if not stale_ids:
            return 0
        deleted = (
            db.query(OpeningScoreBatch)
            .filter(OpeningScoreBatch.id.in_(stale_ids))
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(deleted)
    except Exception:
        db.rollback()
        logger.warning(
            "Failed to prune opening score batches for user_id=%s player_color=%s",
            user_id,
            player_color,
            exc_info=True,
        )
        return 0


def _normalize_lookup_fen(fen: str) -> str:
    """Normalize an incoming FEN to the read-model key.

    Position rows are keyed by the canonical normalized 4-field FEN. ``normalize_fen``
    accepts both 4- and 6-field inputs and additionally canonicalizes the en-passant
    field (dropping a stated EP square that has no legal capture), exactly as the
    stored keys were produced. Normalizing *every* incoming FEN — not just 6-field
    ones — is what makes raw clock-bearing FENs, transpositions, and 4-field FENs
    with a non-canonical EP square hit the same row instead of silently missing.
    """
    return normalize_fen(fen)


def _build_cached_scores(
    player_color: PlayerColor,
    graph: OpeningGraph,
    overlay: EvidenceOverlay,
    roots: OpeningRoots,
    computed_at: datetime,
) -> tuple[list[RootScore], list[PositionScore]]:
    """Build named-root rows and direct position rows from one shared traversal.

    Both row sets come from a single ``compute_all_scores`` call (one
    ``_SharedCalculator``), so named and direct metrics can never disagree.
    """
    position_telemetry = PositionCalcTelemetry()
    scores, _, position_scores = compute_all_scores(
        player_color,
        graph,
        overlay,
        roots,
        RootCalcConfig(),
        computed_at,
        include_branch_summaries=True,
        include_synthetic_root=True,
        position_telemetry=position_telemetry,
    )
    logger.info(
        "opening position-score rows computed",
        extra={
            "player_color": player_color,
            "domain_count": position_telemetry.domain_count,
            "scoreable_position_count": position_telemetry.scoreable_position_count,
            "observed_off_book_row_count": position_telemetry.observed_off_book_row_count,
            "persisted_row_count": position_telemetry.persisted_row_count,
            "metric_key_count": position_telemetry.metric_key_count,
        },
    )
    return list(scores.values()), position_scores


def get_latest_opening_score_batch(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> OpeningScoreBatch | None:
    _validate_player_color(player_color)
    return (
        db.query(OpeningScoreBatch)
        .filter(
            OpeningScoreBatch.user_id == user_id,
            OpeningScoreBatch.player_color == player_color,
        )
        .order_by(OpeningScoreBatch.generation.desc())
        .first()
    )


def reserve_opening_score_generation(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> int:
    _validate_player_color(player_color)
    dialect_name = db.bind.dialect.name if db.bind else ""

    if dialect_name == "sqlite":
        stmt = sqlite_insert(OpeningScoreCursor).values(
            user_id=user_id,
            player_color=player_color,
            latest_generation=1,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[OpeningScoreCursor.user_id, OpeningScoreCursor.player_color],
            set_={"latest_generation": OpeningScoreCursor.latest_generation + 1},
        ).returning(OpeningScoreCursor.latest_generation)
        generation = int(db.execute(stmt).scalar_one())
        db.commit()
        return generation

    if dialect_name == "postgresql":
        stmt = postgresql_insert(OpeningScoreCursor).values(
            user_id=user_id,
            player_color=player_color,
            latest_generation=1,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[OpeningScoreCursor.user_id, OpeningScoreCursor.player_color],
            set_={"latest_generation": OpeningScoreCursor.latest_generation + 1},
        ).returning(OpeningScoreCursor.latest_generation)
        generation = int(db.execute(stmt).scalar_one())
        db.commit()
        return generation

    cursor = (
        db.query(OpeningScoreCursor)
        .filter(
            OpeningScoreCursor.user_id == user_id,
            OpeningScoreCursor.player_color == player_color,
        )
        .first()
    )
    if cursor is None:
        cursor = OpeningScoreCursor(
            user_id=user_id,
            player_color=player_color,
            latest_generation=1,
        )
        db.add(cursor)
    else:
        cursor.latest_generation += 1

    db.commit()
    return cursor.latest_generation


def list_cached_opening_scores(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> tuple[OpeningScoreBatch | None, list[UserOpeningScore]]:
    batch = get_latest_opening_score_batch(db, user_id, player_color)
    if batch is None:
        return None, []
    rows = (
        db.query(UserOpeningScore)
        .filter(
            UserOpeningScore.batch_id == batch.id,
            UserOpeningScore.user_id == user_id,
            UserOpeningScore.player_color == player_color,
        )
        .order_by(
            UserOpeningScore.opening_family.asc(),
            UserOpeningScore.opening_name.asc(),
            UserOpeningScore.opening_key.asc(),
        )
        .all()
    )
    return batch, rows


def load_cached_rows(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> tuple[OpeningScoreBatch | None, list[CachedOpeningScoreRow]]:
    """Stale-while-revalidate reader for the /opening read endpoints.

    WARM (a batch exists): serve the currently-cached batch immediately and
    schedule a coalesced BACKGROUND recompute via the debounced scheduler —
    non-blocking. The g-6zhp gate (``recompute_opening_scores_if_needed``) runs
    the real, content-based freshness check on the worker thread, OFF the request
    path, and rebuilds the batch only when evidence actually changed.

    COLD (no batch yet): compute an initial batch once synchronously via the
    bounded ``refresh_now`` flush/await, then re-list.

    ``request_recompute`` on every warm read is load-bearing, not redundant: it is
    the trigger that lets the gate catch evidence changes with no write-path
    enqueue (out-of-process scripts, post-restart first read).
    """
    # Lazy import: opening_score_scheduler imports opening_cache at module load,
    # so a module-level import here would create a cycle.
    from app.opening_score_scheduler import refresh_now, request_recompute

    batch, rows = list_cached_opening_scores(db, user_id, player_color)
    if batch is None:
        refresh_now(user_id, player_color)
        batch, rows = list_cached_opening_scores(db, user_id, player_color)
    else:
        request_recompute(user_id, player_color)
    return batch, _snapshot_cached_rows(rows)


def list_position_scores(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> tuple[OpeningScoreBatch | None, list[OpeningPositionScore]]:
    """All direct position rows for the latest batch of (user_id, player_color)."""
    _validate_player_color(player_color)
    batch = get_latest_opening_score_batch(db, user_id, player_color)
    if batch is None:
        return None, []
    rows = (
        db.query(OpeningPositionScore)
        .filter(
            OpeningPositionScore.batch_id == batch.id,
            OpeningPositionScore.user_id == user_id,
            OpeningPositionScore.player_color == player_color,
        )
        .order_by(OpeningPositionScore.normalized_fen.asc())
        .all()
    )
    return batch, rows


def lookup_position_scores(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
    fens: Iterable[str],
) -> tuple[OpeningScoreBatch | None, dict[str, CachedPositionScoreRow]]:
    """Look up direct position rows for ``fens`` in the latest batch.

    Every incoming FEN is normalized to the 4-field read-model key before lookup
    (raw tree-UI FENs carry clocks), so transpositions and halfmove/fullmove
    differences resolve to the same row instead of silently missing. The returned
    map is keyed by normalized FEN and contains only FENs that have a persisted row.

    An absent entry is the read model's no-data representation, and the API layer
    decides how to render it: a normalized FEN that is in ``OpeningGraph`` but absent
    here is a static in-book no-evidence node (expected no-data); a normalized FEN
    absent from both graph and batch is outside the current connected scorer domain
    (also no-data). This repository does not consult the graph; it only resolves
    persisted rows by normalized key.
    """
    _validate_player_color(player_color)
    normalized = {_normalize_lookup_fen(fen) for fen in fens}
    if not normalized:
        return get_latest_opening_score_batch(db, user_id, player_color), {}
    batch = get_latest_opening_score_batch(db, user_id, player_color)
    if batch is None:
        return None, {}
    rows = (
        db.query(OpeningPositionScore)
        .filter(
            OpeningPositionScore.batch_id == batch.id,
            OpeningPositionScore.normalized_fen.in_(normalized),
        )
        .all()
    )
    snapshots = _snapshot_position_rows(rows)
    return batch, {snapshot.normalized_fen: snapshot for snapshot in snapshots}


def ensure_tree_cache(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
    graph: OpeningGraph,
    roots: OpeningRoots,
) -> tuple[int | None, datetime | None, str]:
    """Resolve the batch the ``/api/openings/tree`` read path will serve from, and
    fire the single stale-while-revalidate trigger for that request.

    Unlike the card-grid readers, the tree CANNOT tolerate serving a registry-stale
    batch: a batch built before ``edges-v1`` carries zero ``opening_position_edges``
    rows, so plain background revalidation would render a book-only tree and silently
    hide the user's observed moves until a later recompute lands. Because the
    read-model schema version is folded into the registry fingerprint, that case is
    exactly "the latest batch's ``registry_fingerprint`` does not match the current
    one," so on the tree path we upgrade registry/schema drift from background
    revalidate to a BLOCKING bootstrap:

      - WARM-FRESH (a batch exists and its registry fingerprint matches): the edge
        rows are present, so schedule a coalesced BACKGROUND recompute (evidence /
        decay revalidation) and serve the cached batch immediately — non-blocking.
      - COLD (no batch) or registry/schema drift (predates ``edges-v1`` ⇒ no edge
        rows): BLOCK on ``refresh_now`` so the served tree carries its observed
        edges. ``refresh_now`` is serialized through the scheduler's single writer
        thread, so the recompute never runs on this request thread.

    Returns the resolved ``(batch_id, batch_computed_at, cache_state)`` where the two
    scalars are captured BEFORE the caller's ``db.rollback()`` (which expires every
    ORM instance), so the builder never reads an ORM batch field after the rollback.
    ``cache_state`` is diagnostic for the route timing log: ``"warm_fresh"`` (served
    cached, background revalidate), ``"bootstrapped"`` (blocked on a recompute that
    left a current-registry batch — cold-with-evidence or registry/schema drift,
    edges now present), ``"book_only"`` (``refresh_now`` reached quiescence but wrote
    no batch — a user with no evidence), or ``"bootstrap_timeout"`` (``refresh_now``
    timed out and the latest batch is still missing or registry-stale, so this one
    request degrades to a book-only tree while the background recompute finishes).

    A pathologically heavy user whose bootstrap ``refresh_now`` times out is served
    the registry-stale batch for that one request (``lookup_observed_edges_for_batch``
    returns an empty map ⇒ book-only); the background recompute finishes and the next read
    is correct and fast. That rare, logged degradation is preferred over serving a
    wrong tree on every warm read.
    """
    _validate_player_color(player_color)
    # Lazy import mirrors load_cached_rows: opening_score_scheduler imports this
    # module at load, so a module-level import would create a cycle.
    from app.opening_score_scheduler import refresh_now, request_recompute

    current_registry = opening_score_inputs_fingerprint(graph, roots)
    batch = get_latest_opening_score_batch(db, user_id, player_color)
    warm_fresh = batch is not None and batch.registry_fingerprint == current_registry
    if warm_fresh:
        request_recompute(user_id, player_color)
        cache_state = "warm_fresh"
    else:
        refreshed = refresh_now(user_id, player_color, timeout=TREE_BOOTSTRAP_TIMEOUT)
        if not refreshed:
            logger.warning(
                "tree_cache_bootstrap_timeout user_id=%s player_color=%s",
                user_id,
                player_color,
            )
        batch = get_latest_opening_score_batch(db, user_id, player_color)
        # The bootstrap genuinely succeeded only if it left a batch whose registry
        # matches the current one (so it carries this read model's edge rows). A
        # timed-out refresh can leave the latest batch still registry-stale/edgeless
        # — that single request degrades to a book-only tree, so report it distinctly
        # ("bootstrap_timeout") rather than claiming a clean "bootstrapped" run. A
        # quiescent refresh that simply wrote no batch is a genuine no-evidence user
        # ("book_only").
        if batch is not None and batch.registry_fingerprint == current_registry:
            cache_state = "bootstrapped"
        elif refreshed:
            cache_state = "book_only"
        else:
            cache_state = "bootstrap_timeout"
    # Snapshot scalars BEFORE the caller rolls back and expires the ORM row.
    if batch is None:
        return None, None, cache_state
    return batch.id, batch.computed_at, cache_state


def lookup_position_scores_for_batch(
    db: Session,
    batch_id: int,
    fens: Iterable[str],
) -> dict[str, CachedPositionScoreRow]:
    """Look up direct position rows for ``fens`` within a specific batch.

    Like ``lookup_position_scores`` but takes the already-resolved ``batch_id`` (the
    tree route resolves it once via ``ensure_tree_cache``) instead of re-querying the
    latest batch, so it neither re-triggers the scheduler nor touches the ORM batch
    row after the route's ``db.rollback()``. Every incoming FEN is normalized to the
    4-field read-model key before lookup. The returned map is keyed by normalized FEN
    and contains only FENs that have a persisted row.
    """
    normalized = {_normalize_lookup_fen(fen) for fen in fens}
    if not normalized:
        return {}
    rows = (
        db.query(OpeningPositionScore)
        .filter(
            OpeningPositionScore.batch_id == batch_id,
            OpeningPositionScore.normalized_fen.in_(normalized),
        )
        .all()
    )
    snapshots = _snapshot_position_rows(rows)
    return {snapshot.normalized_fen: snapshot for snapshot in snapshots}


def _edge_evidence_from_row(row: OpeningPositionEdge) -> EdgeEvidence:
    """Reconstruct an ``EdgeEvidence`` from one persisted edge row.

    The tree never reads quality, so the reconstructed ``EdgeEvidence`` carries
    ``quality_sum=0.0, quality_count=0`` (the columns are not persisted; see
    ``OpeningPositionEdge``).
    """
    return EdgeEvidence(
        parent_fen=row.parent_fen,
        child_fen=row.child_fen,
        uci=row.uci,
        traversal_count=row.traversal_count,
        live_attempts=row.live_attempts,
        live_passes=row.live_passes,
        live_fails=row.live_fails,
        quality_sum=0.0,
        quality_count=0,
    )


def lookup_observed_edges_for_parent(
    db: Session,
    batch_id: int,
    parent_fen: str,
) -> list[EdgeEvidence]:
    """Observed edges out of ``parent_fen`` for one batch, as ``EdgeEvidence``.

    Reads via the ``idx_opening_position_edges_batch_parent`` index: ``parent_fen``
    is the normalized 4-field key the edges were stored under, matching the builder's
    ``norm_fen``, so no renormalization.
    """
    rows = (
        db.query(OpeningPositionEdge)
        .filter(
            OpeningPositionEdge.batch_id == batch_id,
            OpeningPositionEdge.parent_fen == parent_fen,
        )
        .all()
    )
    return [_edge_evidence_from_row(row) for row in rows]


def lookup_observed_edges_for_batch(
    db: Session,
    batch_id: int,
) -> dict[str, list[EdgeEvidence]]:
    """All observed edges for one batch, indexed by normalized parent FEN.

    A single ``SELECT … WHERE batch_id=?`` that the tree builder loads ONCE up front,
    replacing the previous per-parent point queries (an N+1 that scaled the endpoint's
    latency as visible-parents × DB round-trip). Total edge rows per batch are tiny
    (tens), so eager-loading is cheap and collapses the structural pass to a single
    round-trip — including the terminal-probe frontier, which the per-parent design
    could miss.

    Keys are the persisted ``parent_fen`` (normalized 4-field, matching the builder's
    ``norm_fen``). A parent with no observed edges is simply absent from the map, so
    callers should use ``.get(norm_fen, [])``.
    """
    rows = (
        db.query(OpeningPositionEdge)
        .filter(OpeningPositionEdge.batch_id == batch_id)
        .all()
    )
    edges_by_parent: dict[str, list[EdgeEvidence]] = {}
    for row in rows:
        edges_by_parent.setdefault(row.parent_fen, []).append(
            _edge_evidence_from_row(row)
        )
    return edges_by_parent


def list_opening_score_candidate_pairs(
    db: Session,
    *,
    user_id: int | None = None,
    player_color: PlayerColor | None = None,
    limit: int | None = None,
) -> list[tuple[int, str]]:
    if player_color is not None:
        _validate_player_color(player_color)
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")

    sql_parts = [
        """
        SELECT pairs.user_id, pairs.player_color
        FROM (
            SELECT DISTINCT gs.user_id AS user_id, gs.player_color AS player_color
            FROM session_moves sm
            JOIN game_sessions gs ON gs.id = sm.session_id
            WHERE sm.fen_before IS NOT NULL
              AND gs.session_mode IN ('normal', 'drill')

            UNION

            SELECT DISTINCT b.user_id AS user_id, p.active_color AS player_color
            FROM blunders b
            JOIN positions p ON p.id = b.position_id
            WHERE b.source_session_id IS NULL

            UNION

            SELECT DISTINCT b.user_id AS user_id, gs.player_color AS player_color
            FROM blunders b
            LEFT JOIN game_sessions gs ON gs.id = b.source_session_id
            WHERE gs.player_color IS NOT NULL
              AND gs.session_mode IN ('normal', 'drill')
        ) pairs
    """
    ]
    filters: list[str] = []
    params: dict[str, int | str] = {}
    if user_id is not None:
        filters.append("pairs.user_id = :user_id")
        params["user_id"] = user_id
    if player_color is not None:
        filters.append("pairs.player_color = :player_color")
        params["player_color"] = player_color
    if filters:
        sql_parts.append("        WHERE " + "\n          AND ".join(filters))
    sql_parts.append("        ORDER BY pairs.user_id ASC, pairs.player_color ASC")
    if limit is not None:
        sql_parts.append("        LIMIT :limit")
        params["limit"] = limit

    rows = db.execute(text("\n".join(sql_parts)), params).fetchall()
    return [(int(row[0]), str(row[1])) for row in rows]


def has_opening_evidence(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> bool:
    return bool(
        list_opening_score_candidate_pairs(
            db,
            user_id=user_id,
            player_color=player_color,
            limit=1,
        )
    )


def recompute_opening_scores(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
    *,
    overlay: EvidenceOverlay | None = None,
    inputs_fingerprint: str | None = None,
    computed_at: datetime | None = None,
) -> OpeningScoreBatch:
    _validate_player_color(player_color)
    if overlay is not None and inputs_fingerprint is None:
        # A prebuilt overlay reflects a specific raw-input snapshot. Deriving the
        # fingerprint here — after the overlay was built elsewhere — could store a
        # fingerprint NEWER than the scored overlay if evidence changed in between,
        # letting a later read fast-path over stale scores. Callers that pass an
        # overlay must pass its matching fingerprint. Checked before any side
        # effects (generation reservation) so misuse cannot leave dangling state.
        raise ValueError("inputs_fingerprint is required when overlay is provided")
    generation = reserve_opening_score_generation(db, user_id, player_color)
    if computed_at is None:
        computed_at = datetime.now(timezone.utc)
    graph = get_opening_graph()
    roots = get_opening_roots()
    if inputs_fingerprint is None:
        # Compute the freshness fingerprint BEFORE building the overlay so the
        # stored fingerprint can never be newer than the scored inputs. If evidence
        # changes in the gap, the overlay is at-or-newer than the fingerprint, so
        # the next read recomputes (at most one redundant pass) rather than
        # fast-pathing over stale scores.
        inputs_fingerprint = opening_score_raw_inputs_fingerprint(db, user_id, player_color)
    if overlay is None:
        overlay = overlay_evidence(db, user_id, player_color, graph)
    # Release the checked-out DB connection before the CPU-heavy scoring pass.
    # Without this, repeated requests can sit idle in transaction and exhaust
    # the pool while score computation is still running.
    db.rollback()
    scores, position_scores = _build_cached_scores(
        player_color, graph, overlay, roots, computed_at
    )

    batch = OpeningScoreBatch(
        user_id=user_id,
        player_color=player_color,
        generation=generation,
        registry_fingerprint=opening_score_inputs_fingerprint(graph, roots),
        inputs_fingerprint=inputs_fingerprint,
        computed_at=computed_at,
    )

    try:
        db.add(batch)
        db.flush()

        if scores:
            db.add_all(
                [
                    UserOpeningScore(
                        batch_id=batch.id,
                        user_id=user_id,
                        player_color=player_color,
                        opening_key=score.opening_key,
                        opening_name=score.opening_name,
                        opening_family=score.opening_family,
                        opening_score=score.opening_score,
                        confidence=score.confidence,
                        coverage=score.coverage,
                        weighted_depth=score.weighted_depth,
                        sample_size=score.sample_size,
                        game_count=score.game_count,
                        last_practiced_at=score.last_practiced_at,
                        strongest_branch_name=(
                            score.strongest_branch.opening_name if score.strongest_branch else None
                        ),
                        strongest_branch_key=(
                            score.strongest_branch.opening_key if score.strongest_branch else None
                        ),
                        strongest_branch_score=(
                            score.strongest_branch.value if score.strongest_branch else None
                        ),
                        weakest_branch_name=(
                            score.weakest_branch.opening_name if score.weakest_branch else None
                        ),
                        weakest_branch_key=(
                            score.weakest_branch.opening_key if score.weakest_branch else None
                        ),
                        weakest_branch_score=score.weakest_branch.value if score.weakest_branch else None,
                        underexposed_branch_name=(
                            score.underexposed_branch.opening_name if score.underexposed_branch else None
                        ),
                        underexposed_branch_key=(
                            score.underexposed_branch.opening_key if score.underexposed_branch else None
                        ),
                        underexposed_branch_value=(
                            score.underexposed_branch.value if score.underexposed_branch else None
                        ),
                        computed_at=computed_at,
                    )
                    for score in scores
                ]
            )

        if position_scores:
            insert_started = time.monotonic()
            db.add_all(
                [
                    OpeningPositionScore(
                        batch_id=batch.id,
                        user_id=user_id,
                        player_color=player_color,
                        normalized_fen=position.normalized_fen,
                        in_book=position.in_book,
                        has_evidence=position.has_evidence,
                        opening_score=position.opening_score,
                        confidence=position.confidence,
                        coverage=position.coverage,
                        weighted_depth=position.weighted_depth,
                        sample_size=position.sample_size,
                        game_count=position.game_count,
                        last_practiced_at=position.last_practiced_at,
                        computed_at=computed_at,
                    )
                    for position in position_scores
                ]
            )
            logger.info(
                "opening position-score rows staged",
                extra={
                    "user_id": user_id,
                    "player_color": player_color,
                    "position_row_count": len(position_scores),
                    "stage_seconds": round(time.monotonic() - insert_started, 4),
                },
            )

        if overlay.edges:
            edge_insert_started = time.monotonic()
            db.add_all(
                [
                    OpeningPositionEdge(
                        batch_id=batch.id,
                        user_id=user_id,
                        player_color=player_color,
                        parent_fen=edge.parent_fen,
                        child_fen=edge.child_fen,
                        uci=edge.uci,
                        traversal_count=edge.traversal_count,
                        live_attempts=edge.live_attempts,
                        live_passes=edge.live_passes,
                        live_fails=edge.live_fails,
                        computed_at=computed_at,
                    )
                    for edge in overlay.edges.values()
                ]
            )
            logger.info(
                "opening position-edge rows staged",
                extra={
                    "user_id": user_id,
                    "player_color": player_color,
                    "edge_row_count": len(overlay.edges),
                    "stage_seconds": round(time.monotonic() - edge_insert_started, 4),
                },
            )

        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(batch)
    # Best-effort retention pruning as its own committed statement after the new
    # batch is durable. Never fails the request (see prune helper).
    prune_old_opening_score_batches(db, user_id, player_color)
    return batch


def ensure_opening_scores(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> tuple[OpeningScoreBatch | None, list[UserOpeningScore]]:
    batch, rows = list_cached_opening_scores(db, user_id, player_color)
    if batch is not None:
        return batch, rows
    if not has_opening_evidence(db, user_id, player_color):
        return None, []
    recompute_opening_scores(db, user_id, player_color)
    return list_cached_opening_scores(db, user_id, player_color)


def _emit_opening_scores_recomputed(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
    batch: OpeningScoreBatch,
    *,
    duration_ms: float,
    reason: str,
    cache_miss: bool,
    registry_drift: bool,
    stale_branch_keys: bool,
    evidence_change: bool,
    decay_staleness: bool,
) -> None:
    """Emit the ``opening_scores_recomputed`` perf event for a real recompute.

    Best-effort: a count/analytics failure must never break the scheduler worker
    (``capture`` already swallows its own errors; the count query is guarded too).
    Runs only on the serialized recompute worker, off the request hot path.
    """
    try:
        batch_size = (
            db.query(func.count(UserOpeningScore.id))
            .filter(UserOpeningScore.batch_id == batch.id)
            .scalar()
        )
    except Exception:
        logger.debug("opening_scores_recomputed batch_size query failed", exc_info=True)
        batch_size = None
    capture(
        str(user_id),
        "opening_scores_recomputed",
        {
            "duration_ms": duration_ms,
            "reason": reason,
            "cache_miss": cache_miss,
            "registry_drift": registry_drift,
            "stale_branch_keys": stale_branch_keys,
            "evidence_change": evidence_change,
            "decay_staleness": decay_staleness,
            "batch_size": batch_size,
            "player_color": player_color,
        },
    )


def recompute_opening_scores_if_needed(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> OpeningScoreBatch | None:
    """Single recompute-decision function — the only reader-driven path that may
    write a batch, run exclusively on the scheduler's serialized worker.

    Recomputes when any of the following holds, else reuses the current batch:
      - cache miss (no batch) and the user has opening evidence,
      - the evidence ``inputs_fingerprint`` changed (drill bursts), gated by the
        time-decay interval when nothing else changed,
      - the ``registry_fingerprint`` drifted (graph/roots/config/model versions),
      - the batch has legacy/stale branch-key rows.

    Emits ``opening_scores_recomputed`` (with timing + the trigger reason) ONLY
    when an actual recompute runs — never on the fast cached-batch return.
    """
    now = datetime.now(timezone.utc)
    graph = get_opening_graph()
    registry_fingerprint = opening_score_inputs_fingerprint(graph, get_opening_roots())
    # Cheap raw-input digest first — built WITHOUT the overlay. The overlay (which
    # replays every session's board line through the Lichess divider, ~2.6s for a
    # few hundred games) is built only on the non-fast paths below.
    raw_fingerprint = opening_score_raw_inputs_fingerprint(db, user_id, player_color)

    batch, rows = list_cached_opening_scores(db, user_id, player_color)

    if batch is None:
        if not has_opening_evidence(db, user_id, player_color):
            return None
        started = time.monotonic()
        overlay = overlay_evidence(db, user_id, player_color, graph)
        result = recompute_opening_scores(
            db,
            user_id,
            player_color,
            overlay=overlay,
            inputs_fingerprint=raw_fingerprint,
            computed_at=now,
        )
        _emit_opening_scores_recomputed(
            db,
            user_id,
            player_color,
            result,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            reason="cache_miss",
            cache_miss=True,
            registry_drift=False,
            stale_branch_keys=False,
            evidence_change=False,
            decay_staleness=False,
        )
        return result

    registry_drift = batch.registry_fingerprint != registry_fingerprint
    stale_branch_keys = _batch_has_stale_branch_keys(rows)
    evidence_change = batch.inputs_fingerprint != raw_fingerprint
    decay_staleness = False

    if not registry_drift and not stale_branch_keys and not evidence_change:
        # Inputs/registry/config unchanged → overlay provably identical. Serve the
        # cached batch WITHOUT building the overlay, unless the batch is stale
        # enough that wall-clock time decay should be re-applied. Reuse
        # empty-but-valid snapshots too, else they re-append on every refresh.
        computed_at = batch.computed_at
        if computed_at.tzinfo is None:
            computed_at = computed_at.replace(tzinfo=timezone.utc)
        decay_staleness = computed_at < now - OPENING_SCORE_DECAY_RECOMPUTE_INTERVAL
        if not decay_staleness:
            return batch

    # Trigger reason in priority order (the dominant cause of THIS recompute).
    if registry_drift:
        reason = "registry_drift"
    elif stale_branch_keys:
        reason = "stale_branch_keys"
    elif evidence_change:
        reason = "evidence_change"
    else:
        reason = "decay_staleness"

    # Change / registry drift / stale branch keys / decay-staleness: build the
    # overlay and recompute.
    started = time.monotonic()
    overlay = overlay_evidence(db, user_id, player_color, graph)
    result = recompute_opening_scores(
        db,
        user_id,
        player_color,
        overlay=overlay,
        inputs_fingerprint=raw_fingerprint,
        computed_at=now,
    )
    _emit_opening_scores_recomputed(
        db,
        user_id,
        player_color,
        result,
        duration_ms=round((time.monotonic() - started) * 1000, 3),
        reason=reason,
        cache_miss=False,
        registry_drift=registry_drift,
        stale_branch_keys=stale_branch_keys,
        evidence_change=evidence_change,
        decay_staleness=decay_staleness,
    )
    return result
