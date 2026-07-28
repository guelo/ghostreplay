from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal

from sqlalchemy import func, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.fen import normalize_fen
from app.game_phase import DIVIDER_VERSION
from app.models import (
    EvidenceEpoch,
    OpeningPositionEdge,
    OpeningPositionScore,
    OpeningScoreBatch,
    OpeningScoreBatchSharedScope,
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
    FRESHNESS_CONTRACT_VERSION,
    OPENING_EVIDENCE_INPUTS_VERSION,
    SESSION_EVIDENCE_ELIGIBLE_SQL,
    EdgeEvidence,
    EvidenceOverlay,
    overlay_evidence,
    raw_evidence_inputs_digest,
    raw_evidence_inputs_snapshot,
    shared_scope_digest,
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
# full recompute and invalidate stale snapshots. Session-baseline compatibility
# independently stamps root_calc_config_fingerprint(), so a config-only retune can
# keep this version without allowing cross-config delta subtraction.
#
# sm-v2-4: user-turn reported rows additionally fold their whole pre-fold
# quality by sqrt(coverage); opponent-turn rows retain the recursive coverage
# gate without a second report-time fold.
#
# sm-v2-3: readiness fold calibration (lcb_z=1.0, coverage_fold="gate",
# coverage_live_threshold=1) shifts the public score semantics from posterior
# mean mastery toward earned real-game readiness.
#
# sm-v2-2: the batch now also carries opening_position_scores (the direct
# tree position read model, g-tree-score-model). Batches written before this
# version match the old fingerprint but hold zero position rows, so the fast path
# (recompute_opening_scores_if_needed) would serve them with no direct rows. The
# bump changes registry_fingerprint -> registry drift -> exactly one recompute per
# (user, color) on first read after deploy, backfilling position rows.
SCORE_MODEL_VERSION = "sm-v2-4"

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


def _utcnow() -> datetime:
    """Small seam around ``datetime.now(timezone.utc)`` so tests can control
    timestamp ordering (g-mxeo).

    ``recompute_opening_scores`` samples the batch's ``computed_at`` through this
    AFTER the fingerprint + overlay evidence reads, making ``computed_at`` an UPPER
    BOUND on the evidence reflected in the batch. The opening-baseline date guard
    (``opening_score_delta._capture_baseline_json``) relies on that invariant: a
    batch with ``computed_at < session.started_at`` cannot contain any of the
    session's evidence, so it is safe to persist as the pre-session baseline.
    """
    return datetime.now(timezone.utc)


def opening_score_inputs_fingerprint(
    graph: OpeningGraph,
    roots: OpeningRoots,
) -> str:
    """Registry fingerprint — every VERSION/semantic surface, all O(1).

    ``OPENING_EVIDENCE_INPUTS_VERSION`` and ``FRESHNESS_CONTRACT_VERSION`` are
    folded in here (not into the raw fingerprint) so the cheap freshness check's
    first, O(1) registry comparison catches evidence-derivation semantic bumps
    and cheap-signal contract changes on already-stamped batches (g-jact).
    """
    return (
        f"{graph.fingerprint}:{roots.fingerprint}:{root_calc_config_fingerprint()}"
        f":{SCORE_MODEL_VERSION}:{DIVIDER_VERSION}"
        f":{QUALITY_VERSION}:{TAU_WC!r}:{TAU_CP!r}"
        f":{OPENING_SCORE_CACHE_SCHEMA_VERSION}"
        f":{OPENING_EVIDENCE_INPUTS_VERSION}:{FRESHNESS_CONTRACT_VERSION}"
    )


def evidence_derivation_fingerprint() -> str:
    """Overlay-derivation compatibility stamp for the frozen-cohort artifact (g-p4ih).

    The frozen overlay's fields are DERIVED, not raw DB rows: ``quality_sum`` embeds
    per-observation ``exp(-loss/tau)`` under ``TAU_WC`` / ``TAU_CP`` / ``QUALITY_VERSION``;
    pass/fail counts embed the collector semantics versioned by
    ``OPENING_EVIDENCE_INPUTS_VERSION``; node/edge membership embeds the phase filter
    under ``DIVIDER_VERSION``. Each of these can change the overlay WITHOUT changing
    graph/roots, so a frozen-cohort load guard that checked only graph/roots would
    happily score an artifact the current pipeline could no longer derive from the same
    raw rows.

    Composed over EXACTLY these five derivation surfaces — deliberately NOT
    ``SCORE_MODEL_VERSION`` / root-calc config (scoring-side; the artifact stays reusable
    across model bumps), NOT graph/roots (separate header fields), and NOT the
    cache-schema / freshness-contract versions (cache-side; they do not change overlay
    content). Same composition style as ``opening_score_inputs_fingerprint``.
    """
    return (
        f"{DIVIDER_VERSION}:{QUALITY_VERSION}:{TAU_WC!r}:{TAU_CP!r}"
        f":{OPENING_EVIDENCE_INPUTS_VERSION}"
    )


def _compose_raw_fingerprint(registry_fp: str, row_digest: str) -> str:
    """Single composition rule for ``inputs_fingerprint`` (build + verify sides).

    No explicit version fold: ``registry_fp`` already carries
    ``OPENING_EVIDENCE_INPUTS_VERSION`` / ``FRESHNESS_CONTRACT_VERSION``
    transitively (see ``opening_score_inputs_fingerprint``).
    """
    return hashlib.sha256(f"{registry_fp}|{row_digest}".encode("utf-8")).hexdigest()


def opening_score_raw_inputs_fingerprint(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> str:
    """Full freshness fingerprint computed from raw DB rows — no overlay build.

    ``overlay_evidence`` is a pure deterministic function of a fixed set of raw DB
    rows plus the graph/roots/config/version constants. So "did anything change?"
    can be answered by hashing the INPUTS to the derivation (cheap SQL, zero board
    work) instead of the OUTPUT (which forces the ~2.6s python-chess replay first).

    Composed from two surfaces via ``_compose_raw_fingerprint``:
      - ``opening_score_inputs_fingerprint`` — graph/roots/config/scoring/divider/
        quality/evidence-derivation/freshness-contract versions;
      - ``raw_evidence_inputs_digest`` — the ordered raw-row projection.

    O(evidence volume): it full-scans + hashes every session_moves row and
    IN-queries the shared caches. Since g-jact it is NOT on any warm freshness
    path — the cheap partitioned signal (``_is_batch_fresh``) answers there —
    and is computed only on the REBUILD branch (via
    ``capture_freshness_snapshot``) as the stored source-of-truth digest.
    """
    _validate_player_color(player_color)
    graph = get_opening_graph()
    roots = get_opening_roots()
    registry_fp = opening_score_inputs_fingerprint(graph, roots)
    row_digest = raw_evidence_inputs_digest(db, user_id, player_color)
    return _compose_raw_fingerprint(registry_fp, row_digest)


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


def bump_evidence_seq(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> None:
    """Advance the per-(user,color) evidence counter — IN THE CALLER'S TXN.

    Called wherever a PER-USER evidence surface changes in a digest-visible way
    (g-jact): an eligible session_moves upload, an eligibility truth-value flip
    (end / accuracy-fail / abandon / convert), a new ghost-target blunder, a new
    blunder review. Executes the upsert but does NOT commit — the caller's commit
    makes the bump atomic with the evidence write it accounts for, and a rolled
    back write rolls back its bump.

    The increment is an atomic in-DB column expression (``evidence_seq + 1``
    under the ON-CONFLICT row lock), NEVER a Python read-modify-write: two
    concurrent read-add-write bumps would collapse into one advance, and a batch
    build sampling between their commits would then match forever — the exact
    false-positive this signal forbids. The DO-UPDATE ``set_`` touches ONLY
    ``evidence_seq`` so it can never clobber ``latest_generation``, which
    ``reserve_opening_score_generation`` owns on the same composite-PK row (and
    vice versa — its ``set_`` must stay single-column too).
    """
    _validate_player_color(player_color)
    dialect_name = db.bind.dialect.name if db.bind else ""

    if dialect_name == "sqlite":
        stmt = sqlite_insert(OpeningScoreCursor).values(
            user_id=user_id,
            player_color=player_color,
            evidence_seq=1,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[OpeningScoreCursor.user_id, OpeningScoreCursor.player_color],
            set_={"evidence_seq": OpeningScoreCursor.evidence_seq + 1},
        )
        db.execute(stmt)
        return

    if dialect_name == "postgresql":
        stmt = postgresql_insert(OpeningScoreCursor).values(
            user_id=user_id,
            player_color=player_color,
            evidence_seq=1,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[OpeningScoreCursor.user_id, OpeningScoreCursor.player_color],
            set_={"evidence_seq": OpeningScoreCursor.evidence_seq + 1},
        )
        db.execute(stmt)
        return

    # Generic fallback (unused dialects in practice) — mirrors reserve's last
    # branch; acceptable there since those dialects aren't run concurrently.
    cursor = (
        db.query(OpeningScoreCursor)
        .filter(
            OpeningScoreCursor.user_id == user_id,
            OpeningScoreCursor.player_color == player_color,
        )
        .first()
    )
    if cursor is None:
        db.add(
            OpeningScoreCursor(
                user_id=user_id,
                player_color=player_color,
                evidence_seq=1,
            )
        )
    else:
        cursor.evidence_seq += 1


def current_evidence_seq(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> int:
    """Live per-(user,color) evidence counter; 0 when no cursor row exists yet
    (bumps upsert the row, so no row ⇔ no bump has ever fired)."""
    value = (
        db.query(OpeningScoreCursor.evidence_seq)
        .filter(
            OpeningScoreCursor.user_id == user_id,
            OpeningScoreCursor.player_color == player_color,
        )
        .scalar()
    )
    return int(value) if value is not None else 0


def current_cache_epoch(db: Session) -> int | None:
    """Live global shared-cache epoch; None when the singleton row is missing
    (pre-migration / mis-seeded DB — freshness is then never provable, which is
    the safe degradation: every check rebuilds)."""
    value = db.query(EvidenceEpoch.value).filter(EvidenceEpoch.id == 1).scalar()
    return int(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class FreshnessSnapshot:
    """Everything ``recompute_opening_scores`` stamps on a batch for the cheap
    freshness check (g-jact), captured as ONE bundle so a caller can never pair
    a newer signal with an older overlay (the hazard the old
    overlay-requires-fingerprint guard existed to prevent).

    ``evidence_seq`` / ``cache_epoch`` are sampled BEFORE the evidence read they
    describe, so each stamped value is a LOWER BOUND on the evidence in the
    batch: a write landing during/after the read advances the live counter above
    the stamp → the next check sees a mismatch → harmless rebuild, never a false
    accept. (The OPPOSITE of ``computed_at``, which is sampled after the read as
    an evidence upper bound for the g-mxeo date guard.)

    ``cache_epoch`` is None when the ``evidence_epoch`` singleton was MISSING at
    build time, and MUST be stamped as NULL (never coerced to 0): shared writes
    during a missing-singleton window fire triggers that silently no-op, so no
    live epoch value can vouch for them. A 0-stamp would alias with a later
    re-seeded ``(1, 0)`` row and fast-accept over those invisible writes — the
    exact false positive this signal forbids. A NULL stamp keeps the batch
    permanently unprovable (``_cheap_evidence_fresh`` treats it as unstamped →
    always rebuild), which is the safe degradation.
    """

    inputs_fingerprint: str
    evidence_seq: int
    cache_epoch: int | None
    shared_raw_fens: tuple[str, ...]
    shared_norm_fens: tuple[str, ...]
    scoped_shared_digest: str


def capture_freshness_snapshot(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> FreshnessSnapshot:
    """One evidence read → the full freshness bundle for a batch build.

    O(evidence): pays the full raw-input digest. Only the REBUILD branch calls
    this; warm freshness verdicts go through ``_is_batch_fresh`` and never do.
    """
    _validate_player_color(player_color)
    registry_fp = opening_score_inputs_fingerprint(get_opening_graph(), get_opening_roots())
    # Counters BEFORE the evidence read (lower-bound discipline — see
    # FreshnessSnapshot). A missing epoch singleton stamps NULL — never 0: the
    # triggers no-op while the row is missing, so a 0-stamp would alias with a
    # later re-seeded (1, 0) row and fast-accept over shared writes the epoch
    # never saw (see the FreshnessSnapshot docstring). NULL keeps the batch
    # unstamped → never provably fresh → always rebuild until the singleton
    # exists and a rebuild re-stamps a real epoch.
    evidence_seq = current_evidence_seq(db, user_id, player_color)
    cache_epoch = current_cache_epoch(db)
    snapshot = raw_evidence_inputs_snapshot(db, user_id, player_color)
    return FreshnessSnapshot(
        inputs_fingerprint=_compose_raw_fingerprint(registry_fp, snapshot.digest),
        evidence_seq=evidence_seq,
        cache_epoch=cache_epoch,
        shared_raw_fens=snapshot.shared_raw_fens,
        shared_norm_fens=snapshot.shared_norm_fens,
        scoped_shared_digest=snapshot.scoped_shared_digest,
    )


def _load_batch_shared_scope(db: Session, batch_id: int) -> tuple[list[str], list[str]]:
    """The (raw_fens, norm_fens) shared scope stored for one batch."""
    rows = (
        db.query(OpeningScoreBatchSharedScope.fen, OpeningScoreBatchSharedScope.kind)
        .filter(OpeningScoreBatchSharedScope.batch_id == batch_id)
        .all()
    )
    raw_fens = [fen for fen, kind in rows if kind == "raw"]
    norm_fens = [fen for fen, kind in rows if kind == "norm"]
    return raw_fens, norm_fens


def _best_effort_rearm(db: Session, batch_id: int, epoch: int) -> None:
    """Catch a scoped-fresh batch's ``cache_epoch`` up to ``epoch`` so the next
    check is O(1) again — a PURE OPTIMIZATION, never required for correctness.

    This is a WRITE reached from logically-read paths (the delta poll GET, the
    baseline worker), so it runs in a short INDEPENDENT session on the caller's
    bind — never the caller's transaction — and swallows every error (sqlite
    ``database is locked`` contention included): a failed re-arm only costs one
    extra scoped re-check next time. Concurrent re-arms racing on one batch are
    safe: each stamps an epoch sampled BEFORE its own scoped read, so even a
    stale writer's stamp is a valid lower bound (worst case one extra re-check,
    never a false accept).
    """
    try:
        rearm_session = Session(bind=db.get_bind())
        try:
            rearm_session.execute(
                update(OpeningScoreBatch)
                .where(OpeningScoreBatch.id == batch_id)
                .values(cache_epoch=epoch)
            )
            rearm_session.commit()
        finally:
            rearm_session.close()
    except Exception:
        logger.debug(
            "opening batch re-arm failed (best-effort) batch_id=%s", batch_id,
            exc_info=True,
        )


def _cheap_evidence_fresh(db: Session, batch: OpeningScoreBatch) -> bool:
    """Partitioned cheap evidence-freshness check for one batch (g-jact).

    Covers the EVIDENCE surfaces only — callers own the registry-fingerprint and
    stale-branch-key checks. Check order:

    1. unstamped signal (NULL signal columns, or a NULL ``inputs_fingerprint``)
       → False. Covers pre-migration batches (which also fail the registry
       check upstream via the raw-v5 fold), genuinely corrupt/partial batches,
       AND batches deliberately stamped with a NULL ``cache_epoch`` because the
       ``evidence_epoch`` singleton was missing at build time (see
       ``FreshnessSnapshot`` — a 0-stamp there would alias with a re-seeded
       row). Treat as stale and let a rebuild re-stamp (no oracle-reseed path).
    2. epoch singleton missing → False (cannot prove).
    3. per-user ``evidence_seq`` mismatch → False. Straight to rebuild, NOT the
       scoped path: a per-user change can add/remove candidate FENs, so the
       stored scope is no longer valid (matches today's any-per-user-change →
       full rebuild).
    4. epoch match → True. The O(1) fast accept: two integer reads total.
    5. epoch drift → re-hash the shared lines over the STORED scope. Still skips
       the session_moves scan and the python-chess normalize loop (the digest's
       dominant costs). Scoped match → True + best-effort re-arm; else False.

    Soundness: True requires seq match (⇒ no per-user change ⇒ scope unchanged)
    AND (epoch match ⇒ no shared write anywhere, OR scoped match ⇒ no shared
    change at this batch's positions) ⇒ every ``raw_evidence_inputs_digest``
    input unchanged ⇒ overlay identical ⇒ scores identical.
    """
    if (
        batch.inputs_fingerprint is None
        or batch.evidence_seq is None
        or batch.cache_epoch is None
        or batch.scoped_shared_digest is None
    ):
        return False
    # Sample the live epoch BEFORE the scoped read below so a re-arm stamp is a
    # lower bound on the evidence that read saw.
    epoch = current_cache_epoch(db)
    if epoch is None:
        return False
    if current_evidence_seq(db, batch.user_id, batch.player_color) != batch.evidence_seq:
        return False
    if epoch == batch.cache_epoch:
        return True
    raw_fens, norm_fens = _load_batch_shared_scope(db, batch.id)
    if shared_scope_digest(db, raw_fens, norm_fens) == batch.scoped_shared_digest:
        _best_effort_rearm(db, batch.id, epoch)
        return True
    return False


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


def load_cached_rows_nonblocking(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> tuple[OpeningScoreBatch | None, list[CachedOpeningScoreRow], bool]:
    """Non-blocking sibling of ``load_cached_rows`` for latency-sensitive readers.

    Returns ``(batch, rows, scores_pending)``. ``scores_pending`` is True ONLY
    when a batch is genuinely still coming; callers surface it as a loading
    state, so a False here must mean "this is the final answer".

    WARM: identical to ``load_cached_rows`` — serve the cached batch and call
    ``request_recompute`` UNCONDITIONALLY. That unconditional warm enqueue is
    load-bearing for the same reason documented there (it is the only trigger
    catching evidence changes with no write-path enqueue). Not pending.

    COLD **with evidence**: never calls ``refresh_now``; returns no batch
    immediately so the caller can respond with unscored data instead of blocking
    up to the ``refresh_now`` timeout. The enqueue is guarded on
    ``is_recompute_scheduled`` first, mirroring ``ensure_opening_scores``:
    ``request_recompute`` pushes the debounced deadline out to
    ``now + quiet_window``, so an UNGUARDED enqueue from a polling reader would
    repeatedly postpone the very compute it is waiting on (bounded only by
    ``first_seen + max_wait``). Re-enqueueing when nothing is scheduled also
    retries work lost to a worker fault/restart. Pending.

    COLD **with NO evidence**: NOT pending, and no enqueue. The worker itself
    bails out without creating a batch when ``has_opening_evidence`` is false
    (see ``recompute_opening_scores_if_needed``), so no amount of waiting or
    re-scheduling can ever produce one. Reporting this as pending would pin a
    first-time user — whose only games are still in progress, and so are not yet
    eligible evidence — behind a permanent loading state while their client
    re-scheduled no-op recomputes. Mirrors ``ensure_opening_scores``, which
    likewise reports this case as settled ("warm") rather than building.
    """
    # Lazy import: opening_score_scheduler imports opening_cache at module load,
    # so a module-level import here would create a cycle.
    from app.opening_score_scheduler import is_recompute_scheduled, request_recompute

    batch, rows = list_cached_opening_scores(db, user_id, player_color)
    if batch is None:
        if not has_opening_evidence(db, user_id, player_color):
            return None, [], False
        if not is_recompute_scheduled(user_id, player_color):
            request_recompute(user_id, player_color)
        return None, [], True
    request_recompute(user_id, player_color)
    return batch, _snapshot_cached_rows(rows), False


def _is_batch_fresh(
    db: Session,
    batch: OpeningScoreBatch,
    rows: list[UserOpeningScore],
) -> bool:
    """Freshness predicate for an ALREADY-FETCHED batch + its rows — CHEAP (g-jact).

    Single source of truth for "would ``recompute_opening_scores_if_needed`` serve
    this batch UNCHANGED": the registry fingerprint matches (graph/roots/config/
    model AND evidence-derivation/freshness-contract versions, all O(1)), there
    are no stale branch-key rows, and the partitioned cheap evidence signal holds
    (``_cheap_evidence_fresh``: per-user ``evidence_seq`` + global ``cache_epoch``
    + scoped shared digest on epoch drift). (Time-decay staleness is intentionally
    ignored — it perturbs scores by a small wall-clock amount, not by un-folded
    evidence, so gating on it would gut baseline coverage for no correctness
    benefit.)

    Cost: O(1) when the shared cache is quiescent (two integer reads + the
    branch-key check); the scoped shared digest over the batch's STORED scope when
    a shared write happened somewhere (still no session_moves scan and no
    python-chess normalize loop). The O(evidence) ``raw_evidence_inputs_digest``
    is NEVER computed here — it survives only on the rebuild branch and as the
    differential-test reference.

    False-negatives are allowed (harmless rebuild); NO false-positives (see
    ``_cheap_evidence_fresh``'s soundness note — a stale batch is never served
    as fresh).

    ``batch`` carries its own ``user_id`` / ``player_color``, so callers need not
    re-thread them. Mirrors the fast-path conditions in
    ``recompute_opening_scores_if_needed``; if that gate's freshness predicate
    changes, update this helper to match.
    """
    registry_fingerprint = opening_score_inputs_fingerprint(
        get_opening_graph(), get_opening_roots()
    )
    if batch.registry_fingerprint != registry_fingerprint:
        return False
    if _batch_has_stale_branch_keys(rows):
        return False
    return _cheap_evidence_fresh(db, batch)


def proven_fresh_opening_scores(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
) -> tuple[OpeningScoreBatch | None, list[UserOpeningScore], bool]:
    """Non-blocking freshness verdict for the latest cached batch — NEVER touches
    the scheduler (no ``refresh_now``, no ``request_recompute``, no enqueue, no wait).

    Returns ``(batch, rows, is_fresh)``. ``is_fresh`` is True only when a batch
    exists AND ``_is_batch_fresh`` holds (see it for the predicate).

    Cost: one batch+rows read plus the cheap partitioned freshness check —
    O(1) counter reads when the shared cache is quiescent, the scoped shared
    digest otherwise; never the O(evidence) raw digest and never the ~2.6s
    python-chess overlay. Built for the session-start hot path, which must
    capture a confident baseline only when the cache is PROVABLY current and
    otherwise degrade to NULL without blocking.
    """
    batch, rows = list_cached_opening_scores(db, user_id, player_color)
    if batch is None:
        return None, [], False
    return batch, rows, _is_batch_fresh(db, batch, rows)


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

    Unlike the non-tree score readers, the tree CANNOT tolerate serving a registry-stale
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
    the registry-stale batch for that one request (the observed-edge prefetch finds no
    matching rows ⇒ book-only); the background recompute finishes and the next read
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
    elif batch is None and not has_opening_evidence(db, user_id, player_color):
        # A user with no opening evidence has no observed moves to hide, so a
        # book-only tree is the correct AND complete result — there is nothing to
        # bootstrap. Short-circuit to "book_only" WITHOUT refresh_now: a blocking
        # refresh_now here would enqueue an immediate recompute and AWAIT the single
        # serialized scheduler worker, so even though the no-evidence recompute is
        # itself a no-op, this read could still sit up to TREE_BOOTSTRAP_TIMEOUT
        # behind another user's in-flight recompute. This mirrors
        # resolve_tree_cache_state's no-evidence "warm" and keeps that path fast.
        cache_state = "book_only"
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


def resolve_tree_cache_state(
    db: Session,
    user_id: int,
    player_color: PlayerColor,
    graph: OpeningGraph,
    roots: OpeningRoots,
) -> str:
    """Cheap, non-blocking cache-state probe for the ``/api/openings/tree/status``
    poll — the read-side signal that lets the UI show an explicit one-time
    "Setting up your opening tree…" state instead of a silent ~22s spinner.

    Returns ``"warm"`` | ``"building"`` | ``"cold"`` from ONE indexed batch lookup
    (and, only when there is no batch, one ``limit=1`` evidence existence check). It
    NEVER builds the evidence overlay and NEVER calls ``refresh_now``, so it cannot
    trigger the blocking bootstrap that ``ensure_tree_cache`` performs on the read
    path:

      - ``"warm"`` — a current-registry batch already exists (``/tree`` serves it
        and only background-revalidates), OR there is no batch and the user has no
        opening evidence at all. The latter's tree is correctly book-only and
        ``/tree`` is fast for them (``recompute_opening_scores_if_needed`` writes no
        batch for a no-evidence user, so a poll would otherwise never flip to warm);
        reporting warm lets the UI load ``/tree`` directly.
      - ``"cold"`` / ``"building"`` — no current-registry batch yet but a recompute
        WILL produce one (cold-with-evidence, or a registry/schema-stale batch that
        ``recompute_opening_scores_if_needed`` always rebuilds). Fire the BACKGROUND
        scheduler trigger (``request_recompute``, never ``refresh_now``) so the
        bootstrap runs off the request thread, and report ``"cold"`` when this poll
        just kicked it off or ``"building"`` when work was already scheduled. Firing
        only when nothing is scheduled avoids redundant coalesced re-enqueues, and a
        failed/lost recompute is simply re-fired by the next poll.
    """
    _validate_player_color(player_color)
    # Lazy import mirrors ensure_tree_cache: opening_score_scheduler imports this
    # module at load, so a module-level import would create a cycle.
    from app.opening_score_scheduler import is_recompute_scheduled, request_recompute

    current_registry = opening_score_inputs_fingerprint(graph, roots)
    batch = get_latest_opening_score_batch(db, user_id, player_color)
    if batch is not None and batch.registry_fingerprint == current_registry:
        return "warm"
    if batch is None and not has_opening_evidence(db, user_id, player_color):
        return "warm"
    already_scheduled = is_recompute_scheduled(user_id, player_color)
    if not already_scheduled:
        request_recompute(user_id, player_color)
    return "building" if already_scheduled else "cold"


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


# Max parents per ``parent_fen IN (...)`` chunk. The visible node set is small by
# construction (~tens), so a wave is normally a single chunk; this only splits the
# rare pathological wave to stay under SQLite's ~999-bound-parameter cap (tests run on
# SQLite; Postgres is unaffected). Exported so the tree builder can count the actual
# number of chunked SELECTs a wave issues (each chunk is one DB round-trip).
OBSERVED_EDGE_PARENT_CHUNK_SIZE = 900


def _chunked(items: list[str], size: int) -> Iterable[list[str]]:
    """Yield ``items`` in lists of at most ``size`` (defensive SQLite param cap)."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def observed_edge_parent_chunk_count(n_parents: int) -> int:
    """Number of chunked SELECTs ``lookup_observed_edges_for_parents`` issues for
    ``n_parents`` distinct parents (one DB round-trip per chunk). Zero for an empty
    set; the tree builder uses this to count actual queries, not waves."""
    if n_parents <= 0:
        return 0
    return -(-n_parents // OBSERVED_EDGE_PARENT_CHUNK_SIZE)


def lookup_observed_edges_for_parents(
    db: Session,
    batch_id: int,
    parent_fens: Iterable[str],
) -> dict[str, list[EdgeEvidence]]:
    """Observed edges for a SPECIFIC set of parents in one batch, indexed by
    normalized parent FEN.

    Bounded by the visible node set (line ∪ frontier) the tree builder will actually
    visit, so it fetches ~tens of rows instead of the whole batch (g-0qe6 Option B,
    superseding the whole-batch eager load that pulled a high-history user's ENTIRE
    edge history across the remote-DB RTT). Reads via the
    ``idx_opening_position_edges_batch_parent`` index using ``parent_fen IN (...)``.

    Keys are the persisted ``parent_fen`` (normalized 4-field, matching the builder's
    ``norm_fen``). A requested parent with no observed edges is simply absent from the
    map, so callers should use ``.get(norm_fen, [])``.
    """
    fens = {f for f in parent_fens}
    if not fens:
        return {}
    edges_by_parent: dict[str, list[EdgeEvidence]] = {}
    # The set is small by construction (visible nodes), so this is usually a single
    # chunk; chunk defensively for SQLite's ~999-bound-parameter cap (see
    # OBSERVED_EDGE_PARENT_CHUNK_SIZE; tests run on SQLite, Postgres is unaffected).
    for chunk in _chunked(sorted(fens), OBSERVED_EDGE_PARENT_CHUNK_SIZE):
        rows = (
            db.query(OpeningPositionEdge)
            .filter(
                OpeningPositionEdge.batch_id == batch_id,
                OpeningPositionEdge.parent_fen.in_(chunk),
            )
            .all()
        )
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

    # Session-scoped arms mirror the overlay/digest eligibility gate so that
    # "has evidence" ⇔ "the overlay would produce at least one row". Manual
    # blunders (no source session) are always eligible, matching the
    # ``gs.id IS NULL`` branch in the overlay/digest.
    sql_parts = [
        f"""
        SELECT pairs.user_id, pairs.player_color
        FROM (
            SELECT DISTINCT gs.user_id AS user_id, gs.player_color AS player_color
            FROM session_moves sm
            JOIN game_sessions gs ON gs.id = sm.session_id
            WHERE sm.fen_before IS NOT NULL
              AND gs.session_mode IN ('normal', 'drill')
              AND {SESSION_EVIDENCE_ELIGIBLE_SQL}

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
              AND {SESSION_EVIDENCE_ELIGIBLE_SQL}
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
    freshness: FreshnessSnapshot | None = None,
    computed_at: datetime | None = None,
) -> OpeningScoreBatch:
    _validate_player_color(player_color)
    if overlay is not None and freshness is None:
        # A prebuilt overlay reflects a specific raw-input snapshot. Deriving the
        # freshness bundle here — after the overlay was built elsewhere — could
        # stamp a signal NEWER than the scored overlay if evidence changed in
        # between, letting a later read fast-path over stale scores. Callers that
        # pass an overlay must pass the matching FreshnessSnapshot (sampled before
        # their overlay evidence read). Checked before any side effects
        # (generation reservation) so misuse cannot leave dangling state.
        raise ValueError("freshness snapshot is required when overlay is provided")
    generation = reserve_opening_score_generation(db, user_id, player_color)
    graph = get_opening_graph()
    roots = get_opening_roots()
    if freshness is None:
        # Capture the freshness bundle BEFORE building the overlay so the stored
        # fingerprint/signal can never be newer than the scored inputs. If
        # evidence changes in the gap, the overlay is at-or-newer than the stamp,
        # so the next read recomputes (at most one redundant pass) rather than
        # fast-pathing over stale scores.
        freshness = capture_freshness_snapshot(db, user_id, player_color)
    if overlay is None:
        overlay = overlay_evidence(db, user_id, player_color, graph)
    # Release the checked-out DB connection before the CPU-heavy scoring pass.
    # Without this, repeated requests can sit idle in transaction and exhaust
    # the pool while score computation is still running.
    db.rollback()
    # Sample computed_at AFTER the fingerprint + overlay reads (or the prebuilt
    # overlay the caller passed) so it is an UPPER BOUND on the evidence reflected
    # in this batch, not a lower bound (g-mxeo). The opening-baseline date guard
    # depends on this: a batch dated strictly before a session's start then cannot
    # possibly contain that session's evidence. A caller that passes an explicit
    # computed_at (tests / controlled ordering) keeps its value.
    if computed_at is None:
        computed_at = _utcnow()
    scores, position_scores = _build_cached_scores(
        player_color, graph, overlay, roots, computed_at
    )

    batch = OpeningScoreBatch(
        user_id=user_id,
        player_color=player_color,
        generation=generation,
        registry_fingerprint=opening_score_inputs_fingerprint(graph, roots),
        inputs_fingerprint=freshness.inputs_fingerprint,
        evidence_seq=freshness.evidence_seq,
        cache_epoch=freshness.cache_epoch,
        scoped_shared_digest=freshness.scoped_shared_digest,
        computed_at=computed_at,
    )

    try:
        db.add(batch)
        db.flush()

        # Persist the batch's shared-FEN scope so an epoch drift can be resolved
        # by re-hashing only these positions (see _cheap_evidence_fresh).
        scope_rows = [
            OpeningScoreBatchSharedScope(batch_id=batch.id, fen=fen, kind="raw")
            for fen in freshness.shared_raw_fens
        ]
        scope_rows.extend(
            OpeningScoreBatchSharedScope(batch_id=batch.id, fen=fen, kind="norm")
            for fen in freshness.shared_norm_fens
        )
        if scope_rows:
            db.add_all(scope_rows)

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
      - the cheap evidence signal reports a change (``_cheap_evidence_fresh``:
        per-user seq / shared epoch / scoped shared digest — g-jact), gated by
        the time-decay interval when nothing else changed,
      - the ``registry_fingerprint`` drifted (graph/roots/config/model/evidence
        versions),
      - the batch has legacy/stale branch-key rows.

    The O(evidence) raw-input digest is computed ONLY on the rebuild branches
    (inside ``capture_freshness_snapshot``, as the stored source-of-truth
    ``inputs_fingerprint``) — never on the fast cached-batch return.

    Emits ``opening_scores_recomputed`` (with timing + the trigger reason) ONLY
    when an actual recompute runs — never on the fast cached-batch return.
    """
    now = datetime.now(timezone.utc)
    graph = get_opening_graph()
    registry_fingerprint = opening_score_inputs_fingerprint(graph, get_opening_roots())

    batch, rows = list_cached_opening_scores(db, user_id, player_color)

    if batch is None:
        if not has_opening_evidence(db, user_id, player_color):
            return None
        started = time.monotonic()
        # Freshness bundle BEFORE the overlay evidence read (lower-bound
        # discipline — see FreshnessSnapshot); this is where the full digest runs.
        freshness = capture_freshness_snapshot(db, user_id, player_color)
        overlay = overlay_evidence(db, user_id, player_color, graph)
        # Do NOT pass computed_at=now: the writer samples it AFTER the freshness
        # + overlay reads above so it stays an upper bound on the batch's evidence
        # (g-mxeo). ``now`` is kept only for the decay-staleness gate below.
        result = recompute_opening_scores(
            db,
            user_id,
            player_color,
            overlay=overlay,
            freshness=freshness,
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
    # Cheap partitioned signal in place of the old unconditional raw-digest
    # fast-gate (g-jact). Skipped when registry/branch-key drift already forces
    # a rebuild: under shared-cache churn the check's scoped-digest tier is real
    # work, and its verdict would be moot. The analytics flag then reports False
    # — "not the trigger", matching the reason priority — rather than "checked
    # and changed".
    evidence_change = (
        not registry_drift
        and not stale_branch_keys
        and not _cheap_evidence_fresh(db, batch)
    )
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
    # overlay and recompute. The full raw digest runs HERE (rebuild only), inside
    # capture_freshness_snapshot, sampled before the overlay evidence read.
    started = time.monotonic()
    freshness = capture_freshness_snapshot(db, user_id, player_color)
    overlay = overlay_evidence(db, user_id, player_color, graph)
    # As in the cache-miss branch: let the writer sample computed_at after these
    # reads so it remains an evidence-read upper bound (g-mxeo). ``now`` above is
    # for the decay-staleness gate only.
    result = recompute_opening_scores(
        db,
        user_id,
        player_color,
        overlay=overlay,
        freshness=freshness,
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
