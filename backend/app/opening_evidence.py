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
import json
import logging
import threading
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Callable, Iterable

import chess
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

import app.game_phase as game_phase
from app.analysis_profiles import IDENTITY_FIELDS
from app.analysis_submissions import (
    associated_user_ids_by_row,
    viewer_associated_ids,
)
from app.analysis_trust import describe_move_row
from app.centipawn_loss import centipawn_loss
from app.evidence_coherence import (
    MoveGrain,
    PositionGrain,
    resolve_coherent_evidence_tuple,
)
from app.evidence_policy import Capability
from app.fen import normalize_fen
from app.game_phase import (
    ContinuityError,
    divide,
    is_opening_premove,
    reconstruct_board_sequence,
)
from app.models import AnalysisCache, decode_uci_line
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
#
# raw-v5 (g-jact): folded into ``opening_score_inputs_fingerprint`` (the
# registry fingerprint) instead of the raw fingerprint, so the O(1) registry
# comparison covers evidence-derivation semantics; this bump also forces every
# pre-freshness-signal batch to rebuild and stamp the new signal columns.
# raw-v6 (g-no51): the continuous opening-quality read now normalizes the
# session_moves eval_delta through ``centipawn_loss`` (0..1000 cap) before feeding
# ``move_quality``, so a historical raw >1000 row yields a different quality_sum;
# the raw-row digest is blind to this derivation-code change, so the version bump
# is required to reject pre-bump batches as stale and recompute under the cap.
# raw-v7 (g-v21l): THREE independent selection changes land together, all of which
# leave every pre-existing raw database row byte-identical —
#   1. granting OPENING_EVIDENCE to browser-analysis-multipv-v2 changes WHICH
#      identity-valid rows ``_apply_cache_fallbacks`` selects;
#   2. submitter (association) scoping changes which of those rows a GIVEN USER may
#      read;
#   3. the coherent-tuple requirement changes which PAIRS upgrade — equal-strength
#      sibling rows whose facts disagree no longer do.
# Old batches must therefore fail the registry/input fingerprint and self-heal.
# This one-time bump is explicitly NOT a substitute for hashing the association set
# in ``_shared_evidence_lines`` below: a version bump cannot invalidate anything
# that changes AFTER it lands, and associations keep mutating.
OPENING_EVIDENCE_INPUTS_VERSION = "raw-v7"

# Cheap-freshness-signal contract version (g-jact). Bump whenever the CHEAP
# signal's semantics change — the shared-scope definition captured on a batch,
# the scoped-digest line formatting/composition, or the meaning of
# evidence_seq / cache_epoch — so already-stamped batches cannot be accepted
# under a different contract. Folded into ``opening_score_inputs_fingerprint``
# alongside OPENING_EVIDENCE_INPUTS_VERSION.
# fresh-v2 (g-speed-score-run): operational whole-graph batches capture the
# overlay's exact shared scope under counter/row-identity guards and intentionally
# omit the now-optional full raw ``inputs_fingerprint``.
FRESHNESS_CONTRACT_VERSION = "fresh-v2"

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


def session_is_evidence_eligible(session) -> bool:
    """Python mirror of ``SESSION_EVIDENCE_ELIGIBLE_SQL`` for a GameSession row.

    Used by the evidence_seq bump sites (g-jact): a session_moves upload bumps
    only when the session's rows are digest-visible, and an eligibility
    TRANSITION bumps only when this predicate's truth value flips (e.g.
    ``active -> root_reached`` stays False -> no bump, no recompute churn).

    NOT the same gate as ``_should_run_session_move_evidence`` in
    ``app.api.session`` — that permits live/active sessions (side effects run
    per-move); this one is the digest/overlay eligibility.
    """
    return session.status == "ended" or (
        session.drill_state == "failed"
        and session.drill_terminal_reason == "accuracy"
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


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class ReplayCacheStats:
    """Operational replay-cache work performed while building one overlay.

    These counters deliberately describe storage/replay mechanics only. They are
    not part of the semantic evidence product and must not be used by scoring.
    ``build_count`` can exceed one when the operational freshness capture
    discards an overlay and merges its work into the fallback overlay.
    """

    build_count: int = 0
    probed_sessions: int = 0
    l1_hits: int = 0
    l2_hits: int = 0
    raw_derivations: int = 0
    persisted_upserts: int = 0
    l2_read_failed: bool = False
    l2_write_failed: bool = False

    def merged(self, other: "ReplayCacheStats") -> "ReplayCacheStats":
        return ReplayCacheStats(
            build_count=self.build_count + other.build_count,
            probed_sessions=self.probed_sessions + other.probed_sessions,
            l1_hits=self.l1_hits + other.l1_hits,
            l2_hits=self.l2_hits + other.l2_hits,
            raw_derivations=self.raw_derivations + other.raw_derivations,
            persisted_upserts=self.persisted_upserts + other.persisted_upserts,
            l2_read_failed=self.l2_read_failed or other.l2_read_failed,
            l2_write_failed=self.l2_write_failed or other.l2_write_failed,
        )


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
    # Exact shared-table dependency scope consumed by ``_apply_cache_fallbacks``.
    # Both the terminal-delta publisher and fresh-v2 operational batch writer
    # hash this narrow scope. Row ids let either publisher enforce that the move
    # rows hashed after the build are exactly those whose viewer associations
    # influenced this overlay. Explicit/full snapshots retain their separate
    # deliberately broad scope.
    shared_scope: "OverlaySharedScope" = field(
        default_factory=lambda: OverlaySharedScope()
    )
    # Process/storage diagnostics only; intentionally excluded from semantic
    # overlay equivalence checks.
    replay_cache_stats: ReplayCacheStats = field(
        default_factory=ReplayCacheStats
    )


@dataclass(frozen=True, slots=True)
class OverlaySharedScope:
    raw_fens: tuple[str, ...] = ()
    norm_fens: tuple[str, ...] = ()
    move_row_ids: tuple[int, ...] = ()


@dataclass(slots=True)
class _MoveRow:
    session_id: str
    move_number: int
    color: str
    norm_before: str
    norm_after: str
    fen_before_raw: str
    move_san: str
    # ``move_san`` resolved to UCI once during the cached board REPLAY, never
    # re-derived per build — see ``_CachedMove.uci``.
    uci: str
    eval_delta: int | None
    quality: float | None
    quality_source: str | None
    session_ts: datetime | str | None

    @property
    def identity(self) -> tuple[str, int, str]:
        """Stable per-move identity used to record each session move once."""
        return (str(self.session_id), self.move_number, self.color)


# ---------------------------------------------------------------------------
# Per-session evidence REPLAY cache (g-25mp).
#
# The expensive part of ``_build_move_rows`` is the per-session board REPLAY:
# ``reconstruct_board_sequence`` + Lichess ``divide`` + opening-premove
# extraction. That work is a PURE deterministic function of one session's own
# ``session_moves`` rows plus DIVIDER_VERSION / OPENING_EVIDENCE_INPUTS_VERSION —
# it is graph-independent and analysis_cache-independent (those are consumed
# later, in passes 2/3 and ``_apply_cache_fallbacks``, over the merged rows).
# So we memoize ONLY the replay product per session_id in a bounded in-process
# L1 and a durable database L2; a rebuild then replays only sessions missing or
# invalid in both tiers. Previously-excluded broken sessions use the same path.
#
# NOT cached: move quality (recomputed cheaply on copy-out, so a
# QUALITY_VERSION/TAU_WC/TAU_CP bump needs no invalidation) and the
# analysis_cache fallbacks (re-applied to the merged rows each build).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CachedMove:
    """A frozen opening-interval premove — the RAW replay product for one ply.

    Carries only what a fresh mutable ``_MoveRow`` needs to be rebuilt on
    copy-out, plus the raw ``eval_cp``/``best_move_eval_cp`` the copy-out
    consults to recompute quality and decide the analysis_cache fallback. It
    deliberately has NO ``quality``/``quality_source`` so the cached value can
    never be poisoned by ``_apply_cache_fallbacks`` (which mutates only the
    fresh copies).
    """

    session_id: str
    move_number: int
    color: str
    norm_before: str
    norm_after: str
    fen_before_raw: str
    move_san: str
    # The played move's UCI, parsed ONCE here during the replay that already
    # proved this SAN legal from ``fen_before`` (g-overlay-evidence-reuse).
    #
    # Before this was cached, every build re-derived it via
    # ``_uci_from_san(norm_before, move_san)`` at three call sites — which
    # rebuilds a python-chess ``Board`` from a FEN each time. Profiled on a
    # heavy user (18.9k premoves) that was 16.3k Board constructions and ~51%
    # of the WHOLE warm overlay build; the SAN parse itself was only ~12% of
    # that. Board construction, not parsing, was the cost.
    #
    # Parsed on the RAW ``fen_before`` board rather than the normalized 4-field
    # FEN the old helper used. The two cannot disagree: ``normalize_fen`` copies
    # fields 1-3 verbatim and only CLEARS the en-passant square when
    # ``has_legal_en_passant()`` is false — i.e. it can drop an already-illegal
    # move but never a legal one, so both boards have the same legal move set and
    # therefore resolve any SAN (including disambiguation) identically. Halfmove/
    # fullmove clocks do not affect move generation. Verified over 51,174 prod
    # plies: zero divergence. ``test_cached_uci_matches_normalized_fen_parse``
    # pins the tricky cases (en passant available / present-but-illegal,
    # castling, promotion, ambiguous SAN).
    uci: str
    eval_delta: int | None
    eval_cp: int | None
    best_move_eval_cp: int | None
    session_ts: datetime | str | None


@dataclass(frozen=True, slots=True)
class _CachedSession:
    """The memoized replay product for one session.

    ``excluded`` sessions (ContinuityError during reconstruction) carry an empty
    ``moves`` tuple, no ``phase_sample``, and the warning message so it is logged
    once per content rather than once per rebuild.
    """

    moves: tuple[_CachedMove, ...]
    phase_sample: PhaseSample | None
    excluded: bool
    exclusion_msg: str


# Serialization changes have their own guard: changing the replay semantics uses
# OPENING_EVIDENCE_INPUTS_VERSION, while changing only this JSON envelope bumps
# this version and leaves score/cache freshness versions untouched.
SESSION_REPLAY_PAYLOAD_VERSION = 1
_SESSION_REPLAY_READ_CHUNK_SIZE = 500  # below SQLite's conservative bind limit

_SESSION_PAYLOAD_KEYS = {
    "excluded",
    "exclusion_msg",
    "moves",
    "phase_sample",
    "session_ts",
}
_PHASE_PAYLOAD_KEYS = {"opening_interval_len", "middle_ply", "end_ply"}
_MOVE_PAYLOAD_KEYS = {
    "move_number",
    "color",
    "norm_before",
    "norm_after",
    "fen_before_raw",
    "move_san",
    "uci",
    "eval_delta",
    "eval_cp",
    "best_move_eval_cp",
}


def _require_exact_keys(value, expected: set[str], label: str) -> dict:
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{label} keys differ: missing={sorted(expected - actual)!r} "
            f"unknown={sorted(actual - expected)!r}"
        )
    return value


def _require_int(value, label: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return value


def _require_nullable_int(value, label: str, *, minimum: int | None = None) -> int | None:
    if value is None:
        return None
    return _require_int(value, label, minimum=minimum)


def _require_str(value, label: str, *, nonempty: bool = False) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a string")
    if nonempty and not value:
        raise ValueError(f"{label} must not be empty")
    return value


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _encode_cached_session(value: _CachedSession, session_ts) -> str:
    """Serialize only the raw replay product as canonical, compact JSON."""
    canonical_ts = _digest_ts(session_ts)
    if not canonical_ts:
        raise ValueError("cached session requires a valid session timestamp")
    phase = None
    if value.phase_sample is not None:
        phase = {
            "opening_interval_len": value.phase_sample.opening_interval_len,
            "middle_ply": value.phase_sample.middle_ply,
            "end_ply": value.phase_sample.end_ply,
        }
    payload = {
        "excluded": value.excluded,
        "exclusion_msg": value.exclusion_msg,
        "moves": [
            {
                "move_number": move.move_number,
                "color": move.color,
                "norm_before": move.norm_before,
                "norm_after": move.norm_after,
                "fen_before_raw": move.fen_before_raw,
                "move_san": move.move_san,
                "uci": move.uci,
                "eval_delta": move.eval_delta,
                "eval_cp": move.eval_cp,
                "best_move_eval_cp": move.best_move_eval_cp,
            }
            for move in value.moves
        ],
        "phase_sample": phase,
        "session_ts": canonical_ts,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _decode_cached_session(
    session_id: str,
    payload: str,
    move_count: int,
) -> _CachedSession:
    """Strictly hydrate an untrusted persisted replay payload.

    No missing or unknown field receives a default. Any shape/type/invariant
    failure makes the row a cache miss; authoritative raw rows remain the source
    of truth.
    """
    _require_int(move_count, "move_count", minimum=0)
    _require_str(payload, "payload")
    try:
        root = json.loads(payload, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("payload is not strict JSON") from exc
    root = _require_exact_keys(root, _SESSION_PAYLOAD_KEYS, "payload")

    excluded = root["excluded"]
    if type(excluded) is not bool:
        raise ValueError("payload.excluded must be a boolean")
    exclusion_msg = _require_str(root["exclusion_msg"], "payload.exclusion_msg")
    session_ts_raw = _require_str(
        root["session_ts"], "payload.session_ts", nonempty=True
    )
    session_ts = _parse_ts(session_ts_raw)
    if session_ts is None or session_ts.isoformat() != session_ts_raw:
        raise ValueError("payload.session_ts is not canonical ISO-8601")

    phase_raw = root["phase_sample"]
    phase_sample: PhaseSample | None
    if phase_raw is None:
        phase_sample = None
    else:
        phase_raw = _require_exact_keys(
            phase_raw, _PHASE_PAYLOAD_KEYS, "payload.phase_sample"
        )
        phase_sample = PhaseSample(
            opening_interval_len=_require_int(
                phase_raw["opening_interval_len"],
                "payload.phase_sample.opening_interval_len",
                minimum=0,
            ),
            middle_ply=_require_nullable_int(
                phase_raw["middle_ply"],
                "payload.phase_sample.middle_ply",
                minimum=0,
            ),
            end_ply=_require_nullable_int(
                phase_raw["end_ply"],
                "payload.phase_sample.end_ply",
                minimum=0,
            ),
        )

    moves_raw = root["moves"]
    if type(moves_raw) is not list:
        raise ValueError("payload.moves must be an array")
    if len(moves_raw) != move_count:
        raise ValueError("payload move count disagrees with stored move_count")
    moves: list[_CachedMove] = []
    for index, move_raw in enumerate(moves_raw):
        label = f"payload.moves[{index}]"
        move_raw = _require_exact_keys(move_raw, _MOVE_PAYLOAD_KEYS, label)
        color = _require_str(move_raw["color"], f"{label}.color")
        if color not in _COLOR_RANK:
            raise ValueError(f"{label}.color is invalid")
        moves.append(
            _CachedMove(
                session_id=session_id,
                move_number=_require_int(
                    move_raw["move_number"], f"{label}.move_number", minimum=1
                ),
                color=color,
                norm_before=_require_str(
                    move_raw["norm_before"], f"{label}.norm_before", nonempty=True
                ),
                norm_after=_require_str(
                    move_raw["norm_after"], f"{label}.norm_after", nonempty=True
                ),
                fen_before_raw=_require_str(
                    move_raw["fen_before_raw"],
                    f"{label}.fen_before_raw",
                    nonempty=True,
                ),
                move_san=_require_str(
                    move_raw["move_san"], f"{label}.move_san", nonempty=True
                ),
                uci=_require_str(move_raw["uci"], f"{label}.uci", nonempty=True),
                eval_delta=_require_nullable_int(
                    move_raw["eval_delta"], f"{label}.eval_delta"
                ),
                eval_cp=_require_nullable_int(
                    move_raw["eval_cp"], f"{label}.eval_cp"
                ),
                best_move_eval_cp=_require_nullable_int(
                    move_raw["best_move_eval_cp"], f"{label}.best_move_eval_cp"
                ),
                session_ts=session_ts,
            )
        )

    if excluded:
        if moves or phase_sample is not None:
            raise ValueError("excluded payload must have no moves or phase sample")
        if not exclusion_msg:
            raise ValueError("excluded payload must carry an exclusion message")
    else:
        if phase_sample is None:
            raise ValueError("included payload must carry a phase sample")
        if exclusion_msg:
            raise ValueError("included payload must have an empty exclusion message")

    return _CachedSession(
        moves=tuple(moves),
        phase_sample=phase_sample,
        excluded=excluded,
        exclusion_msg=exclusion_msg,
    )


# Bound PRIMARILY by total cached ``_CachedMove`` rows, not session count —
# memory is driven by the FEN-ish string payload each row carries. Measured
# per-row cost (frozen slots dataclass deep-walked with sys.getsizeof over its
# string/datetime payloads on a representative row) is ~744 B; the 120k cap
# below therefore targets a ~90 MB ceiling. Only opening-interval premoves are
# cached (a fraction of a session's plies), so a heavy user's whole working set
# is a few thousand rows — comfortably (many ×) below this cap, which is the
# CONTRACT the "replay only the new session" claim depends on: a per-user
# working set larger than the budget evicts mid-build and repeatedly hydrates
# from L2 (or replays raw rows when L2 is unavailable). This remains correct but
# is slower; evictions are logged and warned. A coarse session-count backstop
# guards against pathological many-tiny-session sets.
_SESSION_CACHE_MAX_ROWS = 120_000
_SESSION_CACHE_MAX_SESSIONS = 100_000
_WARNED_EXCLUSIONS_MAX = 10_000

# session_id -> (content_hash, _CachedSession). LRU: MRU at the end.
_SESSION_EVIDENCE_CACHE: "OrderedDict[str, tuple[str, _CachedSession]]" = OrderedDict()
# (session_id, content_hash) already-warned set, bounded independently so an
# excluded session warns once per content even after its value entry is evicted.
_WARNED_EXCLUSIONS: "OrderedDict[tuple[str, str], None]" = OrderedDict()
# One lock guards ALL of the structures above and the counters below.
_SESSION_EVIDENCE_LOCK = threading.Lock()
_session_cache_rows = 0  # running total of cached _CachedMove across the LRU
_session_cache_evictions = 0  # cumulative rows evicted (observability / tests)


def _sm_line(r) -> str:
    """Canonical per-row ``SM|`` projection for the freshness digest (section 1).

    MAINTENANCE (g-overlay-evidence-reuse): this projection and the per-session
    REPLAY-cache digest below (``_SESSION_DIGEST_COLUMNS``) must cover the same
    ``session_moves`` column set. They used to share this function outright, so
    "same raw rows → same line" held by construction; the cache digest is now
    computed by the DATABASE (see ``_probe_sql`` for why), so the
    two are separate formatters over one column list. The by-construction link is
    replaced by a behavioral guard: ``test_every_consumed_column_busts_the_replay_cache``
    mutates each column in turn and asserts the cache re-derives, and the
    freshness-digest tests cover this side. Adding a column the overlay consumes
    means adding it in BOTH places.

    The stored format is deliberately unchanged: it feeds the optional
    ``inputs_fingerprint`` audit/release identity and raw-mutation tests.
    Ordinary scheduler batches use the partitioned freshness signal instead, so
    a semantic edit here must also follow the version/counter discipline
    documented at ``OPENING_EVIDENCE_INPUTS_VERSION`` above.
    """
    return "SM|" + "|".join(
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


# ---------------------------------------------------------------------------
# Per-session REPLAY-cache digest (g-overlay-evidence-reuse).
#
# WHY THE DATABASE COMPUTES IT. Validating the replay cache used to mean
# SELECTing every eligible historical row (two FENs each) and hashing all of
# them in python, every build — just to conclude that nothing changed. Profiled
# on a heavy user (52.5k rows / 1118 sessions) that was ~176 ms of row transfer
# plus ~317 ms of python hashing to reach a 1118/1118 hit rate: 33% of the warm
# build spent proving the cache was already correct. Grouping the same
# projection in SQL costs ~62 ms for the whole user and transfers one FIXED-SIZE
# row per session (see THE DB-SIDE FOLD below), and the full rows are then
# fetched ONLY for sessions that missed (~0 ms for the single new session of a
# drill finalize).
#
# TWO FORMATTERS, ONE MEANING. ``_SESSION_DIGEST_BODY_SQL`` and
# ``_session_digest_body`` must produce byte-identical output for the same rows,
# or the probe key would never match the stored key and EVERY build would
# re-replay from scratch. That failure is silent and slow rather than wrong, so
# it gets an explicit test: ``test_warm_rebuild_replays_nothing`` asserts zero
# ``_derive_session`` calls on a warm rebuild. Formatting is chosen so the two
# agree by construction:
#   - every field goes through coalesce/``_digest_value`` to the same NULL
#     sentinel, so NULL and '' cannot collide AND a NULL can never make the whole
#     concatenation NULL (which ``string_agg`` would silently DROP, hiding a
#     changed row);
#   - ``count(*)`` is folded in as a second backstop against a dropped row;
#   - integers render identically under ``CAST(x AS TEXT)`` and ``str()``;
#   - the timestamp is formatted in PYTHON on both paths (``_digest_ts``), never
#     in SQL, because timestamp text differs sharply between dialects;
#   - ordering uses an EXPLICIT color rank rather than ``ORDER BY sm.color``, so
#     the digest cannot depend on database collation;
#   - the separators are safe because no consumed field can contain them: ints,
#     'white'/'black', the FEN charset and the SAN charset admit no '|', '~' or
#     newline. (``_sm_line`` already relies on the same property.)
#
# THE DB-SIDE FOLD. The aggregate concatenates every consumed field of every row,
# so returning it verbatim would put the whole evidence payload — two FENs per
# ply — back on the wire. That would cut round-trips and per-row python without
# cutting BYTES, leaving the warm figure dependent on link bandwidth. Measured on
# the 1118-session user: the raw aggregate is 6473 body bytes per session, 7.24 MB
# per warm build — near-free over the loopback socket the timings were taken on,
# and not free over a real network. So the server folds each body before returning
# it: 32 body bytes per session, 35.8 KB across the build, a 202x reduction on the
# term that used to dominate. (Those figures count the BODY column only; each row
# also carries its session id, row count and timestamp, plus protocol overhead —
# all of which were already O(sessions). The point of the fold is that the one
# term that was O(evidence bytes) no longer is.)
#
# ``string_agg(... ORDER BY)``, ``CAST(x AS TEXT)`` and ``coalesce`` are common
# to Postgres 18 and SQLite 3.51, but a hash function is NOT: SQLite has no
# built-in md5. So the fold is the module's one dialect fork — md5 on
# PostgreSQL, identity on everything else — and python applies the MATCHING
# fold to the body it built from the rows it fetched. Since only PostgreSQL can
# prove the md5 pair agrees, that proof lives in
# ``test_opening_evidence_digest_pg.py``; the raw-body formatters are still
# proven byte-equal there too, on the unfolded aggregate.
# ---------------------------------------------------------------------------

_DIGEST_NULL = "~"  # NULL sentinel; cannot occur in any consumed field
_DIGEST_FIELD_SEP = "|"
_DIGEST_ROW_SEP = "\n"

# The ``session_moves`` columns the per-session replay product depends on —
# everything ``_derive_session`` reads plus everything ``_CachedMove`` carries.
# Must stay in sync with ``_sm_line`` (see its MAINTENANCE note). ``session_id``
# is absent because the digest is already per-session, and ``gs.started_at`` is
# folded in separately (it is a game_sessions column, constant per session).
_SESSION_DIGEST_COLUMNS = (
    "move_number",
    "color",
    "fen_before",
    "fen_after",
    "move_san",
    "eval_delta",
    "eval_cp",
    "best_move_eval_cp",
)

_SESSION_DIGEST_BODY_SQL = f" || '{_DIGEST_FIELD_SEP}' || ".join(
    f"coalesce(CAST(sm.{col} AS TEXT), '{_DIGEST_NULL}')"
    for col in _SESSION_DIGEST_COLUMNS
)

# Explicit color rank, mirroring ``_COLOR_RANK`` — NOT ``ORDER BY sm.color``,
# which would make the digest depend on the database's collation.
_SESSION_DIGEST_ORDER_SQL = (
    "sm.move_number,"
    " CASE sm.color WHEN 'white' THEN 0 WHEN 'black' THEN 1 ELSE 2 END,"
    " sm.id"
)


# The aggregated body, before folding. Shared by the probe and by the PG test's
# byte-equality assertion against ``_session_digest_body``.
_SESSION_DIGEST_AGG_SQL = (
    f"string_agg({_SESSION_DIGEST_BODY_SQL},"
    f" '{_DIGEST_ROW_SEP}' ORDER BY {_SESSION_DIGEST_ORDER_SQL})"
)

# (SQL template, python equivalent) per SQLAlchemy dialect name — see THE
# DB-SIDE FOLD above. md5 here is a non-cryptographic content fold, hence
# ``usedforsecurity=False`` (which also keeps it working under a FIPS build,
# where an unflagged hashlib.md5 raises).
_BODY_FOLDS: dict[str, tuple[str, Callable[[str], str]]] = {
    "postgresql": (
        "md5({body})",
        lambda body: hashlib.md5(
            body.encode("utf-8"), usedforsecurity=False
        ).hexdigest(),
    ),
}
# Dialects without a built-in hash return the body itself; python matches.
_IDENTITY_FOLD: tuple[str, Callable[[str], str]] = ("{body}", lambda body: body)


def _body_fold(dialect: str) -> tuple[str, Callable[[str], str]]:
    """The (SQL template, python fn) fold pair for a dialect. The two MUST agree:
    disagreement is silent and slow, not wrong — every build re-replays."""
    return _BODY_FOLDS.get(dialect, _IDENTITY_FOLD)


@lru_cache(maxsize=8)
def _probe_sql(dialect: str) -> str:
    """STEP 1 of ``_build_move_rows``: one fixed-size row per eligible session.

    Cached because it is pure in ``dialect`` and runs on every overlay build.
    """
    body = _body_fold(dialect)[0].format(body=_SESSION_DIGEST_AGG_SQL)
    return f"""
    SELECT sm.session_id AS sid,
           count(*) AS row_count,
           {body} AS body,
           max(gs.started_at) AS session_ts
    FROM session_moves sm
    JOIN game_sessions gs ON gs.id = sm.session_id
    WHERE gs.user_id = :user_id
      AND gs.player_color = :player_color
      AND sm.fen_before IS NOT NULL
      AND gs.session_mode IN ('normal', 'drill')
      AND {SESSION_EVIDENCE_ELIGIBLE_SQL}
    GROUP BY sm.session_id
"""

# STEP 3: the full rows, scoped to the sessions that missed the replay cache.
# Filters MUST match the probe's exactly or a session could be probed under one
# predicate and replayed under another. ``sm.id`` is selected because
# ``_digest_row_sort_key`` orders on it.
_SESSION_ROWS_SQL = f"""
    SELECT sm.id, sm.session_id, sm.move_number, sm.color,
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
      AND sm.session_id IN :sids
"""


def _digest_value(val) -> str:
    """One field's digest text — python side of the SQL ``coalesce(CAST(...))``."""
    return _DIGEST_NULL if val is None else str(val)


def _digest_row_sort_key(r) -> tuple[int, int, int]:
    """Python mirror of ``_SESSION_DIGEST_ORDER_SQL`` (note the ``2`` default,
    matching that expression's ``ELSE 2`` — deliberately NOT the replay sort's
    ``_COLOR_RANK.get(color, 0)``)."""
    return (r.move_number, _COLOR_RANK.get(r.color, 2), r.id)


def _session_digest_body(srows) -> str:
    """Python mirror of ``_SESSION_DIGEST_AGG_SQL`` — the UNFOLDED body.

    Used only for rows this build actually FETCHED, to key what it replayed. The
    caller must apply the dialect's ``_body_fold`` to the result, because the
    probe returns a folded body.
    """
    return _DIGEST_ROW_SEP.join(
        _DIGEST_FIELD_SEP.join(
            _digest_value(getattr(r, col)) for col in _SESSION_DIGEST_COLUMNS
        )
        for r in sorted(srows, key=_digest_row_sort_key)
    )


def _session_digest(row_count: int, body: str | None, session_ts) -> str:
    """Content hash over one session's rows + the two REPLAY version tags.

    Folds ``game_phase.DIVIDER_VERSION`` (read as a LIVE module attribute so a
    monkeypatch is observed) and ``OPENING_EVIDENCE_INPUTS_VERSION`` in, so any
    version bump misses every entry and forces full re-derivation. QUALITY /
    TAU constants are deliberately absent — quality is recomputed on copy-out.
    """
    payload = f"{row_count}\x00{body or ''}\x00{_digest_ts(session_ts)}"
    payload += f"\n\x00DIVIDER={game_phase.DIVIDER_VERSION}"
    payload += f"\n\x00INPUTS={OPENING_EVIDENCE_INPUTS_VERSION}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _session_replay_l2_select_sql(dialect: str) -> str:
    """Return an L2 lookup that preserves each dialect's primary-key index.

    PostgreSQL must cast the parameter array, never the UUID column: applying a
    function to ``session_id`` prevents the UUID primary-key btree from serving
    this lookup. SQLite stores the test-schema key as TEXT and retains the
    expanding ``IN`` form.
    """
    predicate = (
        "session_id = ANY(CAST(:sids AS UUID[]))"
        if dialect == "postgresql"
        else "CAST(session_id AS TEXT) IN :sids"
    )
    return f"""
        SELECT CAST(session_id AS TEXT) AS session_id,
               content_hash, divider_version, inputs_version, payload_version,
               move_count, payload
        FROM opening_session_replay_cache
        WHERE {predicate}
    """


@dataclass(frozen=True, slots=True)
class _PersistedSession:
    session_id: str
    content_hash: str
    session_ts: datetime | str
    value: _CachedSession


def _independent_engine(db: Session):
    """Return the caller bind's engine without importing the global SessionLocal."""
    bind = db.get_bind()
    return getattr(bind, "engine", bind)


def _read_persisted_session_rows(
    executor,
    session_ids: list[str],
    dialect: str,
):
    rows = []
    statement = text(_session_replay_l2_select_sql(dialect))
    if dialect != "postgresql":
        statement = statement.bindparams(bindparam("sids", expanding=True))
    for start in range(0, len(session_ids), _SESSION_REPLAY_READ_CHUNK_SIZE):
        chunk = session_ids[start : start + _SESSION_REPLAY_READ_CHUNK_SIZE]
        rows.extend(executor.execute(statement, {"sids": chunk}).fetchall())
    return rows


def _load_persisted_sessions(
    db: Session,
    expected_hashes: dict[str, str],
) -> tuple[dict[str, _CachedSession], bool]:
    """Best-effort L2 read for L1 misses.

    PostgreSQL uses a short independent connection so the caller's authoritative
    evidence transaction is never extended by cache I/O. SQLite uses the caller
    transaction: the test suite's StaticPool represents one logical DBAPI
    connection, where opening a second Session would not be independent and a
    commit could accidentally commit/rollback caller state.
    """
    if not expected_hashes:
        return {}, False
    dialect = db.get_bind().dialect.name
    try:
        if dialect == "sqlite":
            # Isolate a genuine adapter/statement error without rolling back the
            # authoritative caller transaction. Using the caller connection
            # retains StaticPool's one-DBAPI-connection contract, while the
            # savepoint leaves later quality/shared-fallback reads usable.
            connection = db.connection()
            with connection.begin_nested():
                rows = _read_persisted_session_rows(
                    connection,
                    list(expected_hashes),
                    dialect,
                )
        else:
            with _independent_engine(db).connect() as connection:
                rows = _read_persisted_session_rows(
                    connection,
                    list(expected_hashes),
                    dialect,
                )
    except Exception:
        logger.debug("opening-session replay L2 read failed", exc_info=True)
        return {}, True

    hydrated: dict[str, _CachedSession] = {}
    malformed = 0
    for row in rows:
        values = row._mapping
        session_id = str(values["session_id"])
        expected_hash = expected_hashes.get(session_id)
        if expected_hash is None:
            continue
        if (
            values["content_hash"] != expected_hash
            or values["divider_version"] != game_phase.DIVIDER_VERSION
            or values["inputs_version"] != OPENING_EVIDENCE_INPUTS_VERSION
            or values["payload_version"] != SESSION_REPLAY_PAYLOAD_VERSION
        ):
            continue
        try:
            hydrated[session_id] = _decode_cached_session(
                session_id,
                values["payload"],
                values["move_count"],
            )
        except Exception:
            # Persisted bytes are untrusted cache input. Bound diagnostics to one
            # aggregate warning per build instead of logging one historical UUID
            # and payload error per row.
            malformed += 1
    if malformed:
        logger.warning(
            "ignored %d malformed opening-session replay cache row(s); "
            "authoritative rows will repair them",
            malformed,
        )
    return hydrated, False


def _session_replay_l2_upsert_sql(dialect: str) -> str:
    session_id_value = (
        "CAST(:session_id AS UUID)" if dialect == "postgresql" else ":session_id"
    )
    statement_clock = (
        "statement_timestamp()" if dialect == "postgresql" else "CURRENT_TIMESTAMP"
    )
    return f"""
        INSERT INTO opening_session_replay_cache (
            session_id, content_hash, divider_version, inputs_version,
            payload_version, move_count, payload
        ) VALUES (
            {session_id_value}, :content_hash, :divider_version, :inputs_version,
            :payload_version, :move_count, :payload
        )
        ON CONFLICT (session_id) DO UPDATE SET
            content_hash = excluded.content_hash,
            divider_version = excluded.divider_version,
            inputs_version = excluded.inputs_version,
            payload_version = excluded.payload_version,
            move_count = excluded.move_count,
            payload = excluded.payload,
            updated_at = {statement_clock}
    """


def _upsert_persisted_sessions(
    db: Session,
    sessions: list[_PersistedSession],
) -> tuple[int, bool]:
    """Best-effort batched L2 upsert; never changes the scoring verdict."""
    if not sessions:
        return 0, False
    dialect = db.get_bind().dialect.name
    try:
        # PostgreSQL row locks are acquired in this order during ON CONFLICT.
        # Sorting makes concurrent overlapping rebuilds use one deterministic
        # lock order instead of inheriting an unspecified raw-row order.
        sessions = sorted(sessions, key=lambda entry: entry.session_id)
        params = [
            {
                "session_id": entry.session_id,
                "content_hash": entry.content_hash,
                "divider_version": game_phase.DIVIDER_VERSION,
                "inputs_version": OPENING_EVIDENCE_INPUTS_VERSION,
                "payload_version": SESSION_REPLAY_PAYLOAD_VERSION,
                "move_count": len(entry.value.moves),
                "payload": _encode_cached_session(entry.value, entry.session_ts),
            }
            for entry in sessions
        ]
        statement = text(_session_replay_l2_upsert_sql(dialect))
        if dialect == "sqlite":
            connection = db.connection()
            with connection.begin_nested():
                connection.execute(statement, params)
        else:
            # The independent transaction is the feature: recompute_opening_scores
            # rolls back its evidence transaction before CPU scoring, so writing
            # through that caller Session would discard every cache fill.
            with _independent_engine(db).begin() as connection:
                connection.execute(statement, params)
        return len(sessions), False
    except Exception:
        # Includes pool timeout/connection acquisition and the session-delete FK
        # race. L1 and the current overlay stay fully usable.
        logger.debug("opening-session replay L2 write failed", exc_info=True)
        return 0, True


def _session_cache_get(session_id: str, content_hash: str) -> _CachedSession | None:
    """Return the cached value iff present AND its content hash matches."""
    with _SESSION_EVIDENCE_LOCK:
        entry = _SESSION_EVIDENCE_CACHE.get(session_id)
        if entry is None or entry[0] != content_hash:
            return None
        _SESSION_EVIDENCE_CACHE.move_to_end(session_id)
        return entry[1]


def _session_cache_put(session_id: str, content_hash: str, value: _CachedSession) -> int:
    """LRU insert; evict whole LRU-first sessions until under the row budget.

    Returns the number of rows THIS call evicted, so the caller can accumulate a
    precise build-local total rather than diffing the shared cumulative counter
    (which other threads mutate concurrently — overlay_evidence runs on both
    request threads and the scheduler daemon).
    """
    global _session_cache_rows, _session_cache_evictions
    with _SESSION_EVIDENCE_LOCK:
        existing = _SESSION_EVIDENCE_CACHE.pop(session_id, None)
        if existing is not None:
            _session_cache_rows -= len(existing[1].moves)
        _SESSION_EVIDENCE_CACHE[session_id] = (content_hash, value)  # inserted MRU
        _session_cache_rows += len(value.moves)
        evicted_here = 0
        while _SESSION_EVIDENCE_CACHE and (
            _session_cache_rows > _SESSION_CACHE_MAX_ROWS
            or len(_SESSION_EVIDENCE_CACHE) > _SESSION_CACHE_MAX_SESSIONS
        ):
            _, (_, evicted_val) = _SESSION_EVIDENCE_CACHE.popitem(last=False)
            _session_cache_rows -= len(evicted_val.moves)
            _session_cache_evictions += len(evicted_val.moves)
            evicted_here += len(evicted_val.moves)
        return evicted_here


def _mark_exclusion_warned_if_new(session_id: str, content_hash: str) -> bool:
    """Atomic check-and-mark: return True only to the FIRST caller for a given
    ``(session_id, content_hash)``, so concurrent misses on the same broken
    session log the exclusion exactly once. The caller logs outside the lock."""
    key = (session_id, content_hash)
    with _SESSION_EVIDENCE_LOCK:
        if key in _WARNED_EXCLUSIONS:
            _WARNED_EXCLUSIONS.move_to_end(key)
            return False
        _WARNED_EXCLUSIONS[key] = None
        # Under sustained pressure on THIS set (more distinct broken contents
        # than the cap) the guarantee degrades to "once per retained key" —
        # acceptable, warnings are diagnostic.
        while len(_WARNED_EXCLUSIONS) > _WARNED_EXCLUSIONS_MAX:
            _WARNED_EXCLUSIONS.popitem(last=False)
        return True


def reset_session_evidence_cache() -> None:
    """Clear the value LRU, the warned-exclusion set, and all counters.

    Test hook (conftest autouse fixture) so cross-test state cannot leak and the
    instrumented replay counts stay deterministic.
    """
    global _session_cache_rows, _session_cache_evictions
    with _SESSION_EVIDENCE_LOCK:
        _SESSION_EVIDENCE_CACHE.clear()
        _WARNED_EXCLUSIONS.clear()
        _session_cache_rows = 0
        _session_cache_evictions = 0


def session_evidence_cache_session_count() -> int:
    with _SESSION_EVIDENCE_LOCK:
        return len(_SESSION_EVIDENCE_CACHE)


def session_evidence_cache_row_count() -> int:
    with _SESSION_EVIDENCE_LOCK:
        return _session_cache_rows


def session_evidence_cache_eviction_count() -> int:
    with _SESSION_EVIDENCE_LOCK:
        return _session_cache_evictions


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
    """Resolve a SAN to UCI on a board built from a NORMALIZED 4-field FEN.

    No longer on any build path — ``_CachedMove.uci`` carries this value from the
    replay (g-overlay-evidence-reuse). Retained deliberately as the PARITY ORACLE
    the equivalence test compares the cached uci against, so the argument in
    ``_CachedMove.uci`` for why the raw-FEN and normalized-FEN parses agree stays
    executable rather than only written down.
    """
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


def _derive_session(srows) -> _CachedSession:
    """Pure per-session board REPLAY — the memoized expensive work (g-25mp).

    Reconstructs the line, runs the Lichess divider, and extracts the
    opening-interval premoves as frozen ``_CachedMove`` rows. Depends ONLY on
    this session's own sorted rows (graph- and analysis_cache-independent).
    Returns an excluded verdict on ``ContinuityError``. Does NO logging and NO
    overlay mutation — the caller owns those so they happen once per rebuild
    regardless of cache hit/miss, and the warning fires only on a miss.
    """
    triples = [(r.fen_before, r.fen_after, r.move_san) for r in srows]
    try:
        boards = reconstruct_board_sequence(triples)
    except ContinuityError as exc:
        return _CachedSession(
            moves=(), phase_sample=None, excluded=True, exclusion_msg=str(exc)
        )

    division = divide(boards)
    phase_sample = PhaseSample(
        opening_interval_len=division.opening_size,
        middle_ply=division.middle,
        end_ply=division.end,
    )
    moves: list[_CachedMove] = []
    for index, r in enumerate(srows):
        if not is_opening_premove(division, index):
            continue
        # ``boards[index]`` is this ply's PRE-move board, already built and
        # already proven to accept this SAN by the reconstruction above (it
        # raises ContinuityError otherwise), so parse_san cannot fail here and
        # costs no new Board construction. reconstruct_board_sequence pushes only
        # onto a throwaway copy, so the returned boards are unmutated.
        moves.append(
            _CachedMove(
                session_id=str(r.session_id),
                move_number=r.move_number,
                color=r.color,
                norm_before=normalize_fen(r.fen_before),
                norm_after=normalize_fen(r.fen_after),
                fen_before_raw=r.fen_before,
                move_san=r.move_san,
                uci=boards[index].parse_san(r.move_san).uci(),
                eval_delta=r.eval_delta,
                eval_cp=r.eval_cp,
                best_move_eval_cp=r.best_move_eval_cp,
                session_ts=r.session_ts,
            )
        )
    return _CachedSession(
        moves=tuple(moves),
        phase_sample=phase_sample,
        excluded=False,
        exclusion_msg="",
    )


def _build_move_rows(
    db: Session,
    user_id: int,
    player_color: str,
    overlay: EvidenceOverlay,
) -> list[_MoveRow]:
    """Load session moves, phase-tag them, and attach continuous quality.

    Five steps, so an unchanged session is resolved from memory or durable replay
    storage without fetching authoritative move rows:

    1. PROBE — one grouped statement returns a content digest per eligible
       session (``_probe_sql(dialect)``).
    2. L1 RESOLVE — hit the in-process replay cache on that digest. A fully warm
       build stops here having transferred no replay payload or raw rows.
    3. L2 RESOLVE — hydrate remaining sessions from strict persisted JSON.
    4. FETCH + REPLAY — only for sessions that missed both tiers, keyed on a digest of the
       rows actually fetched (see the torn-read note inline).
    5. COPY OUT — a fresh mutable ``_MoveRow`` per cached premove, in sorted
       session order, with quality recomputed live so a QUALITY/TAU version bump
       honours automatically with no cache invalidation and
       ``_apply_cache_fallbacks`` can never poison the frozen cached value.

    Sessions with broken continuity are excluded and counted.
    """
    # STEP 1 — probe. One grouped statement per (user, color) yields one
    # fixed-size row per eligible session carrying its folded content digest,
    # instead of transferring every historical row to hash it here.
    dialect = db.get_bind().dialect.name
    fold = _body_fold(dialect)[1]
    probe_rows = db.execute(
        text(_probe_sql(dialect)),
        {"user_id": user_id, "player_color": player_color},
    ).fetchall()
    if not probe_rows:
        overlay.replay_cache_stats = ReplayCacheStats(build_count=1)
        return []

    # STEP 2 — resolve against the process-local L1. Warm builds end here: every
    # session hits and not one persisted payload or raw row is fetched.
    derived: dict[str, _CachedSession] = {}
    expected_hashes: dict[str, str] = {}
    l1_missed: list[str] = []
    for pr in probe_rows:
        sid = str(pr.sid)
        content_hash = _session_digest(pr.row_count, pr.body, pr.session_ts)
        expected_hashes[sid] = content_hash
        cached = _session_cache_get(sid, content_hash)
        if cached is None:
            l1_missed.append(sid)
        else:
            derived[sid] = cached

    # Build-local eviction tally (rows this rebuild evicted). NOT a diff of the
    # shared cumulative counter: other threads mutate it concurrently, which
    # would over/under-count and misattribute this user's warning.
    evicted_rows = 0

    # STEP 3 — hydrate L1 misses from the database-backed L2. Version/hash
    # mismatches are ordinary misses; malformed rows and adapter failures can
    # never alter the overlay and fall through to authoritative replay.
    persisted, l2_read_failed = _load_persisted_sessions(
        db,
        {sid: expected_hashes[sid] for sid in l1_missed},
    )
    for sid in l1_missed:
        cached = persisted.get(sid)
        if cached is None:
            continue
        content_hash = expected_hashes[sid]
        if cached.excluded and _mark_exclusion_warned_if_new(sid, content_hash):
            logger.warning(
                "excluding session %s from opening evidence: %s",
                sid,
                cached.exclusion_msg,
            )
        evicted_rows += _session_cache_put(sid, content_hash, cached)
        derived[sid] = cached

    raw_missed = [sid for sid in l1_missed if sid not in persisted]
    to_persist: list[_PersistedSession] = []

    # STEP 4 — fetch and replay ONLY the sessions that missed both tiers.
    if raw_missed:
        rows = db.execute(
            text(_SESSION_ROWS_SQL).bindparams(bindparam("sids", expanding=True)),
            {
                "user_id": user_id,
                "player_color": player_color,
                "sids": raw_missed,
            },
        ).fetchall()

        by_session: dict[str, list] = defaultdict(list)
        for row in rows:
            by_session[str(row.session_id)].append(row)

        for sid, srows in by_session.items():
            srows.sort(key=lambda r: (r.move_number, _COLOR_RANK.get(r.color, 0)))
            # Key the entry on a digest of the rows THIS build actually fetched,
            # never on the probe's digest. The probe and this fetch are separate
            # statements, so under READ COMMITTED the session can change in the
            # gap; storing probe-key → fetched-value would leave an entry whose
            # key describes rows that were never replayed, and a later probe
            # returning that key would serve it. Re-deriving here makes key and
            # value come from ONE snapshot by construction. Every row of a
            # session shares its game_sessions join row, so srows[0].session_ts
            # is exactly the probe's max(gs.started_at). ``fold`` is the same
            # dialect's fold the probe applied server-side.
            content_hash = _session_digest(
                len(srows), fold(_session_digest_body(srows)), srows[0].session_ts
            )
            cached = _derive_session(srows)  # REPLAY happens here only (miss)
            if cached.excluded and _mark_exclusion_warned_if_new(sid, content_hash):
                logger.warning(
                    "excluding session %s from opening evidence: %s",
                    sid,
                    cached.exclusion_msg,
                )
            evicted_rows += _session_cache_put(sid, content_hash, cached)
            derived[sid] = cached
            to_persist.append(
                _PersistedSession(
                    session_id=sid,
                    content_hash=content_hash,
                    session_ts=srows[0].session_ts,
                    value=cached,
                )
            )

        # A session the probe saw but that this fetch did not return went
        # ineligible (or lost its rows) in the gap above. It is simply absent from
        # ``derived`` and contributes nothing — the same outcome as if the probe
        # had run after the change. Safe under the freshness lower-bound
        # discipline in opening_cache.py: the signal is sampled BEFORE this read,
        # so an at-or-newer overlay re-verifies on the next pass.

    persisted_upserts, l2_write_failed = _upsert_persisted_sessions(db, to_persist)

    opening_moves: list[_MoveRow] = []
    # Candidates needing an analysis_cache lookup: index into opening_moves plus
    # the (fen_before_raw, uci, side_to_move) lookup key.
    cache_candidates: list[tuple[int, str, str, str]] = []

    # STEP 5 — copy out in a DETERMINISTIC session order. Neither statement above
    # orders by session, so dict insertion order follows DB row order and is not
    # stable across runs. sorted() makes phase_samples order and cache_candidates
    # indices identical between an incremental and a from-scratch build.
    for sid in sorted(derived):
        cached = derived[sid]
        if cached.excluded:
            overlay.excluded_sessions += 1
            continue

        overlay.phase_samples.append(cached.phase_sample)  # frozen → safe to share
        # COPY-OUT: build a fresh MUTABLE _MoveRow per frozen _CachedMove, with
        # quality recomputed live so a QUALITY/TAU bump needs no invalidation.
        for cm in cached.moves:
            quality, source = move_quality(
                eval_cp=cm.eval_cp,
                best_move_eval_cp=cm.best_move_eval_cp,
                # Normalize the continuous-quality eval_delta read through the shared
                # cap (g-no51): historical raw >1000 rows produce a different
                # quality_sum than the capped value. The _MoveRow below keeps the raw
                # eval_delta (its binary PASS_THRESHOLD read is cap-independent).
                eval_delta=centipawn_loss(cm.eval_delta),
            )
            mr = _MoveRow(
                session_id=cm.session_id,
                move_number=cm.move_number,
                color=cm.color,
                norm_before=cm.norm_before,
                norm_after=cm.norm_after,
                fen_before_raw=cm.fen_before_raw,
                move_san=cm.move_san,
                uci=cm.uci,
                eval_delta=cm.eval_delta,
                quality=quality,
                quality_source=source,
                session_ts=cm.session_ts,
            )
            # A user move that fell back to eval_delta (or has no signal yet) may
            # be upgradable to a win-chance score from analysis_cache.
            needs_cache = (
                cm.color == player_color
                and (cm.eval_cp is None or cm.best_move_eval_cp is None)
            )
            if needs_cache:
                # ``cm.uci`` replaces a per-build _uci_from_san re-parse. The old
                # code skipped the candidate when that returned None; the cached
                # uci is always present (the replay proved the SAN legal), so this
                # branch no longer has an unreachable skip.
                stm = "w" if " w " in cm.fen_before_raw else "b"
                cache_candidates.append(
                    (len(opening_moves), cm.fen_before_raw, cm.uci, stm)
                )
            opening_moves.append(mr)

    if evicted_rows > 0:
        # Silent thrash reads as "incremental working" while it repeatedly
        # transfers entries from L2 (or replays on L2 failure); surface it so an
        # undersized budget is diagnosable. The count is build-local (summed
        # from each _session_cache_put), so it is precise for this user even
        # under concurrent rebuilds on other threads.
        logger.warning(
            "session-evidence cache evicted %d rows during build for "
            "user=%s color=%s — _SESSION_CACHE_MAX_ROWS may be undersized",
            evicted_rows,
            user_id,
            player_color,
        )

    overlay.shared_scope = _apply_cache_fallbacks(
        db, user_id, opening_moves, cache_candidates
    )
    overlay.replay_cache_stats = ReplayCacheStats(
        build_count=1,
        probed_sessions=len(probe_rows),
        l1_hits=len(probe_rows) - len(l1_missed),
        l2_hits=len(persisted),
        raw_derivations=len(to_persist),
        persisted_upserts=persisted_upserts,
        l2_read_failed=l2_read_failed,
        l2_write_failed=l2_write_failed,
    )
    return opening_moves


def _apply_cache_fallbacks(
    db: Session,
    user_id: int,
    opening_moves: list[_MoveRow],
    candidates: list[tuple[int, str, str, str]],
) -> OverlaySharedScope:
    """Upgrade eval_delta-only user moves to trusted win-chance quality.

    Pairs a position best eval with the played eval of the SAME analysis through
    the SHARED coherent-tuple resolver, then rescores — never reading the move
    row's own (possibly duplicated/untrusted) ``best_eval`` as position truth (the
    duplicated-best-move bug from the parent epic).

    * **Capability** — OPENING_EVIDENCE for BOTH grains, with ``user_id`` as the
      viewer, so a non-canonical row serves only the user who independently
      submitted it (g-v21l).
    * **Position best** from :func:`resolve_trusted_positions` — the
      ``position_analysis`` storage winner or the strongest holding
      ``analysis_cache`` row at the normalized FEN.
    * **Pairing** through :func:`resolve_coherent_evidence_tuple`, the single place
      any consumer may combine a position grain with a move grain. This REPLACES
      the former bare ``compare_search_strength(...) is EQUAL`` check, which had no
      factual-coherence requirement at all: equal-profile sibling rows whose facts
      disagreed used to upgrade opening quality even though the atomic reuse path
      rejects the same combination. The rule that the move row's own ``best_eval``
      is never read as position truth is preserved and STRENGTHENED — the helper's
      overlap-agreement requirement now REJECTS the disagreement instead of
      silently discarding it.

    Only applies when the primary session evals were absent, so it strictly
    improves on the deterministic eval_delta fallback; on any failed guard the
    move keeps its eval_delta quality, exactly as before.
    """
    if not candidates:
        return OverlaySharedScope()

    fen_set = sorted({fen for _, fen, _, _ in candidates})
    rows = db.query(AnalysisCache).filter(AnalysisCache.fen_before.in_(fen_set)).all()
    by_key = {(r.fen_before, r.move_uci): r for r in rows}

    # Candidate fens already normalized successfully upstream (_build_move_rows
    # calls normalize_fen unguarded before a candidate is appended), so this is safe.
    norm_by_fen = {fen: normalize_fen(fen) for fen in fen_set}
    trusted = resolve_trusted_positions(
        db, set(norm_by_fen.values()), Capability.OPENING_EVIDENCE, user_id
    )
    associated_ids = viewer_associated_ids(db, user_id, [r.id for r in rows])
    scope = OverlaySharedScope(
        raw_fens=tuple(fen_set),
        norm_fens=tuple(sorted(set(norm_by_fen.values()))),
        move_row_ids=tuple(sorted(int(row.id) for row in rows)),
    )

    for move_index, fen_before, uci, stm in candidates:
        row = by_key.get((fen_before, uci))
        if row is None:
            continue
        tp = trusted.get(norm_by_fen[fen_before])
        if tp is None:
            continue
        move_evidence = describe_move_row(
            row, viewer_associated=row.id in associated_ids
        )
        coherent = resolve_coherent_evidence_tuple(
            fen_before,
            PositionGrain(
                evidence=tp.evidence,
                best_move_uci=tp.best_move_uci,
                best_move_san=tp.best_move_san,
                best_line_uci=tp.best_line_uci,
                best_eval=tp.best_eval,
                best_eval_mate=tp.best_eval_mate,
            ),
            MoveGrain(
                evidence=move_evidence,
                move_uci=row.move_uci,
                played_eval=row.played_eval,
                played_eval_mate=row.played_eval_mate,
                eval_delta=row.eval_delta,
                classification=row.classification,
                best_move_uci=row.best_move_uci,
                best_line_uci=decode_uci_line(row.best_line_uci),
                best_eval=row.best_eval,
                best_eval_mate=row.best_eval_mate,
            ),
            Capability.OPENING_EVIDENCE,
            user_id,
        )
        if coherent is None:
            continue
        mover_evals = cache_row_to_mover_evals(
            coherent.played_eval,
            coherent.played_eval_mate,
            coherent.best_eval,
            coherent.best_eval_mate,
            stm,
        )
        if mover_evals is None:
            continue
        mr = opening_moves[move_index]
        quality, source = move_quality(
            eval_cp=None,
            best_move_eval_cp=None,
            # Normalized for defense-in-depth (g-no51); this branch's eval_delta arg
            # is effectively dead — cache_mover_evals takes precedence in move_quality.
            eval_delta=centipawn_loss(mr.eval_delta),
            cache_mover_evals=mover_evals,
        )
        mr.quality = quality
        mr.quality_source = source
    return scope


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
            _apply_move(overlay, mr, mr.uci, is_user, recorded)
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
            _apply_move(overlay, mr, mr.uci, is_user, recorded)
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


@dataclass(frozen=True, slots=True)
class EvidenceInputsSnapshot:
    """One evidence read's digest + the shared-FEN scope it consumed (g-jact).

    ``digest`` is the full raw-input digest (identical to what
    ``raw_evidence_inputs_digest`` returns). ``shared_raw_fens`` /
    ``shared_norm_fens`` are the digest's OWN broad candidate scope — every
    eligible player-color move lacking a primary eval (raw ``fen_before`` set)
    and its normalized keys — NOT the overlay's narrower fallback candidates.
    ``scoped_shared_digest`` hashes ONLY the shared lines (``AC|``/``PA|``/
    ``ACP|``) over that scope, so a later ``shared_scope_digest`` over the
    STORED scope equals it by construction whenever no shared row at those
    positions changed.
    """

    digest: str
    shared_raw_fens: tuple[str, ...]
    shared_norm_fens: tuple[str, ...]
    scoped_shared_digest: str


def _hash_digest_lines(lines: list[str]) -> str:
    """Order-independent digest over projected lines: sort, join, sha256.

    The single hashing convention for BOTH the full digest and the scoped
    shared digest — the scoped == full-slice guarantee depends on the two
    consumers formatting and hashing lines identically.
    """
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


def _per_user_evidence_lines(
    db: Session,
    user_id: int,
    player_color: str,
) -> tuple[list[str], list[str], list[str]]:
    """Per-user digest lines (``SM|``/``GT|``/``BR|``) plus the shared-FEN scope.

    Returns ``(lines, candidate_fens, norm_list)`` where the candidate sets are
    the SHARED-lookup scope derived from the per-user rows: this user's
    player-color session moves lacking a primary eval (raw ``fen_before``) and
    their python-side ``normalize_fen`` keys — exactly the keys the runtime
    consumer ``resolve_trusted_positions`` uses, never the nullable stored
    ``normalized_fen_before`` column. Both lists are sorted for deterministic
    persistence/comparison.
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
        # Canonical per-row projection for THIS digest only. The replay cache's
        # per-session digest is a separate formatter over the same column set —
        # see the MAINTENANCE note on ``_sm_line`` for what keeps them aligned
        # now that they no longer share this function.
        lines.append(_sm_line(r))

    # 2. Candidate fen_before set for the shared trusted-source lookups: this
    #    user's player-color session moves lacking a primary eval (the only
    #    positions a fallback can consult).
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
    candidate_fens = sorted(fen for (fen,) in candidate_rows)
    norm_keys: set[str] = set()
    for fen in candidate_fens:
        try:
            norm_keys.add(normalize_fen(fen))
        except Exception:
            # A fen that fails to normalize never produces a consumed candidate at
            # runtime (the overlay normalizes it unguarded upstream), so skipping
            # it here cannot miss a change.
            continue

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

    return lines, candidate_fens, sorted(norm_keys)


def _shared_evidence_lines(
    db: Session,
    candidate_fens: list[str],
    norm_list: list[str],
    *,
    move_row_ids_out: set[int] | None = None,
) -> list[str]:
    """Shared digest lines (``AC|``/``PA|``/``ACP|``) over a SUPPLIED FEN scope.

    The single source of the shared-line SQL and formatting. The full digest
    calls it with its freshly-derived broad candidate scope; operational capture
    and ``shared_scope_digest`` call it with the overlay's exact dependency
    scope. For any supplied scope, build-time and re-check digests are identical
    by construction.

    Tracks BOTH grains the cache fallback consumes (a trusted position best
    paired with a move-trusted played eval): the exact ``(fen_before, move_uci)``
    move rows plus their trust columns, AND the trusted position sources
    (``position_analysis`` storage winner and the legacy ``analysis_cache`` rows
    that feed ``_legacy_position_sort_key``) at the candidate NORMALIZED FENs.

    ONGOING ELIGIBILITY (g-v21l): submitter associations are an input to the
    OPENING_EVIDENCE trust filter, so both ``analysis_cache`` projections — the
    ``AC|`` move-grain line and the ``ACP|`` legacy position-tier line — hash each
    row's associated user ids as a sorted, deterministically formatted list. Without
    it an association-only mutation (the claim rule granting a second submitter
    access while every evidence column stays byte-identical) would advance
    ``evidence_epoch`` via the shared-table trigger, fall to ``_cheap_evidence_fresh``
    step 5, re-hash the stored scope, STILL match, and re-arm a batch computed when
    its user could not read evidence they can now read.

    The FULL association set is hashed, not just the requesting user's membership:
    this function must stay USER-INDEPENDENT so a stored scope has one canonical
    digest regardless of whose batch or publication carries it. The cost is that
    one user's claim invalidates other users' batches at the same positions; that
    is accepted, because association writes are rare next to evidence writes and
    the alternative makes scoped re-checks viewer-dependent.

    ``PA|`` deliberately does NOT carry it: ``position_analysis`` is canonical-only
    storage that browser evidence is structurally excluded from, and canonical rows
    carry no associations.
    """
    lines: list[str] = []
    ident_cols = ", ".join(IDENTITY_FIELDS)

    def _ident(r) -> str:
        return "|".join(str(getattr(r, f, None)) for f in IDENTITY_FIELDS)

    def _subs(row_id, submitters: dict[int, tuple[int, ...]]) -> str:
        return ",".join(str(u) for u in submitters.get(int(row_id), ()))

    # 2a. Move grain: the exact (fen_before, move_uci) analysis_cache rows the
    #     fallback reads for a played eval, plus the columns the MOVE-trust gate
    #     reads (a move-trust flip must change the digest): played eval, the
    #     ``classification`` the move-complete contract requires, and the
    #     profile/contract/IDENTITY trust columns.
    #
    #     COHERENCE INPUTS (g-v21l): this projection must hash every column
    #     ``resolve_coherent_evidence_tuple`` reads off the MOVE row, because each
    #     one can flip the pair between accepted and refused with no other row
    #     changing. That is a strictly larger set than "the facts the overlay
    #     consumes", and it is why the four fields below are hashed even though
    #     three of them are never read as truth from this row:
    #       * ``eval_delta``  — check 4 (finite CP data) and the move-only branch's
    #         recomputed-delta equality, and an argument to the classification
    #         validator;
    #       * ``best_move_uci`` — selects the COMBINED vs move-only branch, and is
    #         then required to equal the position winner's best move;
    #       * ``best_line_uci``/``best_eval``/``best_eval_mate`` — the remaining
    #         ``_combined_facts_match`` equalities.
    #     ``best_line_uci`` is hashed in its STORED encoding rather than decoded:
    #     the comparison is on the decoded value, and equal encodings decode equal,
    #     so the digest can only over-invalidate (an extra recompute), never
    #     under-invalidate. Hashing the pre-image is the safe direction.
    if candidate_fens:
        move_stmt = text(f"""
            SELECT id, fen_before, move_uci, played_eval, played_eval_mate,
                   best_move_uci, best_line_uci, best_eval, best_eval_mate,
                   eval_delta, classification,
                   analysis_profile_id, evidence_contract_id, {ident_cols}
            FROM analysis_cache
            WHERE fen_before IN :fens
        """).bindparams(bindparam("fens", expanding=True))
        move_rows = db.execute(move_stmt, {"fens": list(candidate_fens)}).fetchall()
        if move_row_ids_out is not None:
            move_row_ids_out.update(int(row.id) for row in move_rows)
        # Bulk projection join, inside this function's own scoped query — NOT
        # through the resolved-evidence descriptor (which carries only one viewer's
        # membership) and NOT inside the four-SELECT lookup ceiling, which this
        # path does not share.
        move_subs = associated_user_ids_by_row(db, [r.id for r in move_rows])
        for r in move_rows:
            lines.append(
                "AC|"
                + "|".join(
                    (
                        str(r.fen_before),
                        str(r.move_uci),
                        str(r.played_eval),
                        str(r.played_eval_mate),
                        str(r.best_move_uci),
                        str(r.best_line_uci),
                        str(r.best_eval),
                        str(r.best_eval_mate),
                        str(r.eval_delta),
                        str(r.classification),
                        str(r.analysis_profile_id),
                        str(r.evidence_contract_id),
                        _ident(r),
                        _subs(r.id, move_subs),
                    )
                )
            )

    # 2b. Position grain at the candidate NORMALIZED FENs (the two tiers
    #     resolve_trusted_positions ranks).
    if norm_list:
        # (i) position_analysis storage winner (tier 1).
        pa_stmt = text(f"""
            SELECT normalized_fen, best_move_uci, best_line_uci,
                   best_eval, best_eval_mate,
                   analysis_profile_id, evidence_contract_id, {ident_cols}
            FROM position_analysis
            WHERE normalized_fen IN :norms
        """).bindparams(bindparam("norms", expanding=True))
        for r in db.execute(pa_stmt, {"norms": list(norm_list)}).fetchall():
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
        legacy_rows = db.execute(legacy_stmt, {"norms": list(norm_list)}).fetchall()
        legacy_subs = associated_user_ids_by_row(db, [r.id for r in legacy_rows])
        for r in legacy_rows:
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
                        _subs(r.id, legacy_subs),
                    )
                )
            )

    return lines


@dataclass(frozen=True, slots=True)
class SharedScopeSnapshot:
    """Digest plus the raw move-row identities selected for one shared scope."""

    digest: str
    move_row_ids: tuple[int, ...]


def shared_scope_snapshot(
    db: Session,
    raw_fens: Iterable[str],
    norm_fens: Iterable[str],
) -> SharedScopeSnapshot:
    """Hash a supplied shared scope and expose the move-row identity set.

    The identity set is used by the scoped terminal-delta build and fresh-v2
    operational whole-batch build to enforce that the ``analysis_cache`` rows
    hashed here are exactly those passed to ``viewer_associated_ids`` while the
    overlay was built. Later batch freshness checks consume
    :func:`shared_scope_digest` below.
    """
    move_row_ids: set[int] = set()
    lines = _shared_evidence_lines(
        db,
        sorted(raw_fens),
        sorted(norm_fens),
        move_row_ids_out=move_row_ids,
    )
    return SharedScopeSnapshot(
        digest=_hash_digest_lines(lines),
        move_row_ids=tuple(sorted(move_row_ids)),
    )


def shared_scope_digest(
    db: Session,
    raw_fens: Iterable[str],
    norm_fens: Iterable[str],
) -> str:
    """Scoped shared-evidence digest over a STORED batch scope (g-jact).

    Re-hashes ONLY the shared lines (``AC|``/``PA|``/``ACP|``) at the supplied
    raw/normalized candidate FENs — no session_moves scan, no python-chess
    normalize loop. Equals the ``scoped_shared_digest`` stamped at build time
    iff no shared row at this batch's positions changed, because both sides use
    ``_shared_evidence_lines`` + ``_hash_digest_lines`` verbatim.
    """
    return shared_scope_snapshot(db, raw_fens, norm_fens).digest


def raw_evidence_inputs_snapshot(
    db: Session,
    user_id: int,
    player_color: str,
) -> EvidenceInputsSnapshot:
    """Cheap freshness digest over the RAW DB rows ``overlay_evidence`` consumes,
    plus the shared-FEN scope + scoped shared digest for the batch stamp (g-jact).

    Hashes a canonical, order-independent projection of exactly the raw rows the
    overlay reads — and nothing derived. No python-chess board work, no board
    reconstruction, no divider, no overlay build. Same raw inputs → identical
    digest → provably identical overlay → identical scores, so a matching digest
    lets the cache serve a batch without paying the per-session board-replay cost.

    Scoping is INTENTIONALLY broad where it diverges from the overlay (e.g. all
    session moves are hashed, not just opening-interval premoves; the candidate
    set is every eligible-session player-color move lacking a primary eval, not
    just opening premoves). Explicit/direct snapshots and conservative fallbacks
    persist this same broad scope, so their scoped re-check remains a slice of
    their full digest. Fresh-v2 operational batches deliberately do not use this
    scope: they persist the overlay's exact shared dependency set after
    row-identity and counter-stability checks. A shared write at a broad-only FEN
    can then change this full oracle digest while correctly leaving the
    operational batch fresh because it cannot change that overlay or its scores.
    Semantic-only changes that leave every raw row unchanged are covered by
    ``OPENING_EVIDENCE_INPUTS_VERSION`` / ``FRESHNESS_CONTRACT_VERSION``, folded
    into the registry fingerprint (``opening_score_inputs_fingerprint``).

    MAINTENANCE (g-mxeo): the SESSION-SCOPED sources — session_moves (SM|),
    ghost-target blunders via ``source_session_id`` (GT|), and blunder_reviews
    (BR|), built in ``_per_user_evidence_lines`` — are ALSO enumerated by the
    opening-baseline persist guard in
    ``opening_score_delta.run_baseline_snapshot_job`` (its NOT EXISTS clauses),
    which is that guard's airtight, clock-independent correctness check. If you add
    a new source there that a single session can contribute (i.e. scoped to a
    session_id / source_session_id), add a matching NOT EXISTS clause there too, or
    a session feeding only the new source could receive a wrongly-attributed
    baseline. MAINTENANCE (g-jact): a new PER-USER source also needs a
    ``bump_evidence_seq`` choke-point; a new SHARED table needs evidence_epoch
    triggers.
    """
    per_user_lines, candidate_fens, norm_list = _per_user_evidence_lines(
        db, user_id, player_color
    )
    shared_lines = _shared_evidence_lines(db, candidate_fens, norm_list)
    return EvidenceInputsSnapshot(
        digest=_hash_digest_lines(per_user_lines + shared_lines),
        shared_raw_fens=tuple(candidate_fens),
        shared_norm_fens=tuple(norm_list),
        scoped_shared_digest=_hash_digest_lines(shared_lines),
    )


def raw_evidence_inputs_digest(
    db: Session,
    user_id: int,
    player_color: str,
) -> str:
    """Full raw-input digest — see ``raw_evidence_inputs_snapshot``.

    Retained as the slow-path content identity: explicit/direct snapshots, release
    calibration, and conservative operational fallbacks store it (composed into
    ``inputs_fingerprint``), while tests use it to prove raw mutations are
    non-vacuous and to pin the intentional broad-vs-exact-scope distinction.
    Fresh-v2 acceptance is compared against derived overlay/score semantics, not
    this broader digest. Ordinary scheduler rebuilds and warm verdicts skip it.
    """
    return raw_evidence_inputs_snapshot(db, user_id, player_color).digest
