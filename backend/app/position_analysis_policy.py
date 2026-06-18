"""Pure position-grain primitives: backfill winner selection (Phase 2) and the
live write decision (Phase 3).

This module is intentionally DB-free: all decision functions operate on plain
dicts or dataclasses so they can be unit-tested without a database session.

Phase 2 — backfill winner selection
------------------------------------
Given the eligible ``analysis_cache`` candidates for a single ``normalized_fen``,
select exactly one canonical winner.  Winner selection is **group-then-pick**:
never an ``INSERT ... ON CONFLICT`` ordering trick. A candidate is promoted over
the preference-earlier winner ONLY when it is *strictly stronger* under the guarded
:func:`app.analysis_profiles.compare_search_strength`; equal-strength /
incomparable candidates resolve by a deterministic preference order
(authoritative-profile priority, then higher ``cache_id``).

The conflict trigger is best-move / mate-winner disagreement only (see
:func:`position_signature`). PV-continuation and CP-only ``best_eval`` differences
are captured in the audit axes when such a conflict fires, but are NOT themselves
triggers — ``position-complete-v1`` only requires the PV to start with the best
move, so those differences do not change the canonical decision.

Phase 3 — live write decision
------------------------------
:func:`decide_position_analysis_replacement` governs every native live write to
``position_analysis``.  The structural invariant: non-authoritative (browser-game)
rows are rejected BEFORE any strength/dominance comparison.  This check runs even
for new-key inserts so a browser write can never create a position truth row.

The only accepted position-grain write contract is ``position-complete-v1``.
``resolver-complete-v2`` is the legacy *read/projection* contract for migration; it
is never written to ``position_analysis`` natively.  The DB-level orchestration
(read-decide-write) lives in :mod:`app.position_analysis_repo`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum

from app.analysis_profiles import (
    AUTHORITATIVE_PROFILE_PRIORITY,
    Profile,
    StrengthComparison,
    compare_search_strength,
    get_profile,
)
# Single definition of the dict-based authority gate lives in analysis_trust (the
# neutral bottom-of-graph module shared with the read-time consumers). Re-exported
# here so existing importers of ``position_analysis_policy._effectively_authoritative``
# keep working.
from app.analysis_trust import _effectively_authoritative
from app.evidence_contracts import (
    POSITION_COMPLETE,
    is_strict_successor,
    is_superset_or_successor,
    legacy_v2_satisfies_position,
)

# Provenance / quality columns copied verbatim from the winning analysis_cache row
# onto position_analysis (mirrors AnalysisCache; excludes evidence_contract_id and
# source, which the backfill sets to the position grain's own values).
POSITION_METADATA_FIELDS = (
    "analysis_profile_id",
    "engine_name",
    "engine_version",
    "engine_build",
    "network_id",
    "search_limit_type",
    "search_limit_value",
    "threads",
    "hash_mb",
    "multipv",
    "eval_file_id",
    "eval_file_small_id",
    "analyzer_protocol_version",
    "profile_manifest_digest",
)

# Position-grain evidence facts (properties of the position, not the played move).
POSITION_FACT_FIELDS = (
    "best_move_uci",
    "best_move_san",
    "best_line_uci",
    "best_eval",
    "best_eval_mate",
)


@dataclass
class PositionCandidate:
    """One eligible ``analysis_cache`` row projected to the position grain."""

    cache_id: int
    normalized_fen: str
    fen: str  # representative full FEN of this run (provenance/sampling only)
    profile_id: str | None
    contract_id: str | None
    source: str | None
    best_move_uci: str | None
    best_move_san: str | None
    best_line_uci: str | None  # space-joined storage form
    best_eval: int | None
    best_eval_mate: int | None
    # Provenance columns (POSITION_METADATA_FIELDS) to stamp onto the winner row.
    metadata: dict = field(default_factory=dict)


@dataclass
class WinnerSelection:
    """Outcome of :func:`select_position_winner` for one normalized_fen group."""

    winner: PositionCandidate
    is_conflict: bool
    policy_reason: str
    # Candidates in deterministic preference order (winner is not necessarily
    # first: a strictly-stronger but preference-later candidate may have won).
    ordered: tuple[PositionCandidate, ...]


def is_eligible_position_candidate(row: dict) -> bool:
    """True when a cache-row dict may become a canonical position candidate.

    Eligible iff BOTH (a) effectively authoritative (full canonical identity) and
    (b) legacy-v2 position-complete (declared resolver-complete-v2 whose
    position-grain projection passes ``_validate_position_complete``). The g-ul4p
    browser sibling fails (a); v1 / legacy rows fail (b).
    """
    return _effectively_authoritative(row) and legacy_v2_satisfies_position(row)


def _mate_winner(candidate: PositionCandidate) -> int | None:
    """Conflict-relevant mate axis: ``None`` for a CP eval (no forced mate), else
    the sign of the mate count (+1 white mates, -1 black mates).

    Deliberately encodes only *who* mates / mate-vs-cp, NOT the mate distance: two
    runs that both force mate for the same side at different depths agree on the
    winner. Distance differences surface in the audit columns, not the signature.
    """
    mate = candidate.best_eval_mate
    if mate is None:
        return None
    return (mate > 0) - (mate < 0)


def position_signature(candidate: PositionCandidate) -> tuple[str | None, int | None]:
    """The (best_move_uci, mate_winner) axes that define a position disagreement.

    PV-continuation and CP-only ``best_eval`` differences are intentionally absent
    (decision #3): they are not conflict triggers.
    """
    return (candidate.best_move_uci, _mate_winner(candidate))


def _preference_key(candidate: PositionCandidate) -> tuple[int, int]:
    """Deterministic tiebreak order: authoritative-profile priority, then higher
    cache_id. NO raw search_limit_value — depth ranking lives only in the guarded
    comparator."""
    try:
        priority = AUTHORITATIVE_PROFILE_PRIORITY.index(candidate.profile_id)
    except ValueError:
        priority = len(AUTHORITATIVE_PROFILE_PRIORITY)
    return (priority, -candidate.cache_id)


def _profile_of(candidate: PositionCandidate) -> Profile | None:
    return get_profile(candidate.profile_id)


def _strictly_stronger(a: PositionCandidate, b: PositionCandidate) -> bool:
    """True when ``a``'s run is strictly stronger than ``b``'s under the guarded
    comparator (EQUAL / WEAKER / INCOMPARABLE all return False)."""
    pa, pb = _profile_of(a), _profile_of(b)
    if pa is None or pb is None:
        return False
    return compare_search_strength(pa, pb) == StrengthComparison.A_STRONGER


def select_position_winner(candidates: list[PositionCandidate]) -> WinnerSelection:
    """Pick exactly one canonical winner for one normalized_fen group.

    1. Order candidates by the deterministic preference key.
    2. Fold to a winner, promoting ONLY to a strictly-stronger candidate; equal /
       incomparable candidates keep the preference-earlier winner.
    3. ``is_conflict`` iff more than one distinct :func:`position_signature`.
    4. ``policy_reason``: ``selected_dominant`` when the winner is strictly
       stronger than every disagreeing candidate (strength resolved it), else
       ``conflict_best_known_kept`` (equal-strength deterministic tiebreak).
    """
    if not candidates:
        raise ValueError("select_position_winner requires at least one candidate")

    ordered = sorted(candidates, key=_preference_key)
    winner = ordered[0]
    for nxt in ordered[1:]:
        if _strictly_stronger(nxt, winner):
            winner = nxt

    winner_sig = position_signature(winner)
    disagreeing = [
        c
        for c in candidates
        if c.cache_id != winner.cache_id and position_signature(c) != winner_sig
    ]
    is_conflict = bool(disagreeing)
    if not is_conflict:
        policy_reason = "selected_dominant"  # uncontested; not persisted
    elif all(_strictly_stronger(winner, c) for c in disagreeing):
        policy_reason = "selected_dominant"
    else:
        policy_reason = "conflict_best_known_kept"

    return WinnerSelection(
        winner=winner,
        is_conflict=is_conflict,
        policy_reason=policy_reason,
        ordered=tuple(ordered),
    )


def _distinct_or_none(values: list) -> list | None:
    """Distinct values in first-seen order, or ``None`` when the axis agreed."""
    seen: list = []
    for v in values:
        if v not in seen:
            seen.append(v)
    return seen if len(seen) > 1 else None


def position_conflict_axes(candidates: list[PositionCandidate]) -> dict:
    """Per-axis disagreement detail for the append-only conflict audit row.

    Candidates are ordered by ``cache_id`` so every derived list (and therefore
    the dedupe signature) is deterministic. Each per-axis value is ``None`` when
    that axis agreed; ``best_eval`` / PV differences are recorded here for audit
    but are not themselves what fired the conflict.
    """
    ordered = sorted(candidates, key=lambda c: c.cache_id)
    return {
        "candidate_cache_ids": [c.cache_id for c in ordered],
        "candidate_summaries": [
            {
                "cache_id": c.cache_id,
                "source": c.source,
                "profile": c.profile_id,
                "contract": c.contract_id,
                "best_move_uci": c.best_move_uci,
                "best_line_uci": c.best_line_uci,
                "best_eval": c.best_eval,
                "best_eval_mate": c.best_eval_mate,
            }
            for c in ordered
        ],
        "best_move_disagreement": _distinct_or_none([c.best_move_uci for c in ordered]),
        "pv_disagreement": _distinct_or_none([c.best_line_uci for c in ordered]),
        "best_eval_disagreement": _distinct_or_none([c.best_eval for c in ordered]),
        "best_eval_mate_disagreement": _distinct_or_none(
            [c.best_eval_mate for c in ordered]
        ),
    }


def conflict_signature(axes: dict, policy_reason: str) -> str:
    """Deterministic content signature for append-only conflict dedupe.

    Covers ``candidate_cache_ids`` + the four per-axis columns + ``policy_reason``
    (NOT the human-readable summaries). A rerun over the same candidate set yields
    the same signature, so the identical conflict is skipped instead of appended.
    """
    payload = {
        "candidate_cache_ids": axes.get("candidate_cache_ids"),
        "best_move_disagreement": axes.get("best_move_disagreement"),
        "pv_disagreement": axes.get("pv_disagreement"),
        "best_eval_disagreement": axes.get("best_eval_disagreement"),
        "best_eval_mate_disagreement": axes.get("best_eval_mate_disagreement"),
        "policy_reason": policy_reason,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Phase 3 — live write decision for position_analysis
# ---------------------------------------------------------------------------

# Only position-complete-v1 is accepted as the write contract for native
# position_analysis rows. resolver-complete-v2 is the legacy *read/projection*
# contract for existing analysis_cache rows during the migration window; it is
# never written natively to position_analysis.
_ALLOWED_POSITION_WRITE_CONTRACTS = frozenset({POSITION_COMPLETE})


class PositionDecision(str, Enum):
    INSERT = "insert"
    REPLACE = "replace"
    MERGE = "merge"
    KEEP = "keep"


class PositionReason(str, Enum):
    NEW_KEY = "new_key"
    INVALID_INCOMING_KEEP = "invalid_incoming_keep"
    NON_AUTHORITATIVE_KEEP = "non_authoritative_keep"
    DOMINATES_REPLACE = "dominates_replace"
    LEGACY_REPLACED_BY_AUTH = "legacy_replaced_by_auth"
    SAME_PROFILE_IDEMPOTENT = "same_profile_idempotent"
    SAME_PROFILE_SUPERSET_MERGE = "same_profile_superset_merge"
    SAME_PROFILE_CONTRACT_UPGRADE = "same_profile_contract_upgrade"
    MERGE_CONFLICT_KEEP = "merge_conflict_keep"
    INCOMPATIBLE_KEEP = "incompatible_keep"
    INCOMING_LESS_COMPLETE_KEEP = "incoming_less_complete_keep"


@dataclass(frozen=True)
class PositionRow:
    """Minimal projection used for the position_analysis write decision.

    Mirrors :class:`app.analysis_cache_policy.CacheRow` but is scoped to the
    position grain: only :data:`POSITION_FACT_FIELDS` participate in
    ``populated_fields`` and ``values``.
    """

    analysis_profile_id: str | None
    evidence_contract_id: str | None
    identity_verified: bool
    contract_satisfied: bool
    populated_fields: frozenset[str]  # over POSITION_FACT_FIELDS
    values: dict  # position fact field values for overlap/agree checks

    def effective_profile_id(self) -> str | None:
        if self.identity_verified:
            return self.analysis_profile_id
        return None

    def is_effectively_authoritative(self) -> bool:
        if not self.identity_verified:
            return False
        profile = get_profile(self.analysis_profile_id)
        return bool(profile and profile.authoritative and profile.active)


def position_populated_fields_of(data: dict) -> frozenset[str]:
    """Position fact fields that are non-null in ``data``."""
    return frozenset(f for f in POSITION_FACT_FIELDS if data.get(f) is not None)


def _position_fields_agree(existing: PositionRow, incoming: PositionRow) -> bool:
    """Every overlapping non-null position fact field must agree."""
    for f in existing.populated_fields & incoming.populated_fields:
        if existing.values.get(f) != incoming.values.get(f):
            return False
    return True


def incoming_position_is_valid(incoming: PositionRow) -> bool:
    """Validity gate applied before any insert/replace/merge.

    Rejects rows that:
    * do not declare ``position-complete-v1`` (the only write contract);
    * fail that contract's semantic validation;
    * claim an ``analysis_profile_id`` they cannot identity-verify.

    Note: this gate does NOT enforce authoritative status.  The subsequent
    :func:`decide_position_analysis_replacement` authority gate handles that
    separately so rejections carry the right reason code.
    """
    if incoming.evidence_contract_id not in _ALLOWED_POSITION_WRITE_CONTRACTS:
        return False
    if not incoming.contract_satisfied:
        return False
    if incoming.analysis_profile_id is not None and not incoming.identity_verified:
        return False
    return True


def decide_position_analysis_replacement(
    existing: PositionRow | None,
    incoming: PositionRow,
) -> tuple[PositionDecision, PositionReason]:
    """Decide what to do with ``incoming`` given ``existing`` (or None for a new key).

    Rules mirror :func:`app.analysis_cache_policy.decide_analysis_cache_replacement`
    with one critical addition: non-authoritative writes are rejected structurally
    before any dominance / strength comparison, even for new-key inserts.  This
    ensures a browser-game row can never become position truth.

    The ``existing`` argument is ``None`` on a missing key.
    """
    if not incoming_position_is_valid(incoming):
        return PositionDecision.KEEP, PositionReason.INVALID_INCOMING_KEEP

    # Structural browser rejection: non-authoritative writes never become position
    # truth, even for new keys.  This check precedes dominance comparison so it
    # cannot be short-circuited by the g-mk1d strength-aware browser hierarchy
    # (which lives in analysis_cache, not position_analysis).
    if not incoming.is_effectively_authoritative():
        return PositionDecision.KEEP, PositionReason.NON_AUTHORITATIVE_KEEP

    if existing is None:
        return PositionDecision.INSERT, PositionReason.NEW_KEY

    existing_eff = existing.effective_profile_id()
    incoming_eff = incoming.effective_profile_id()

    # Rule: same effective (verified) profile.
    if (
        existing_eff is not None
        and incoming_eff is not None
        and existing_eff == incoming_eff
    ):
        if not (existing.is_effectively_authoritative() and incoming.is_effectively_authoritative()):
            return PositionDecision.KEEP, PositionReason.SAME_PROFILE_IDEMPOTENT
        if not is_superset_or_successor(
            incoming.evidence_contract_id, existing.evidence_contract_id
        ):
            return PositionDecision.KEEP, PositionReason.SAME_PROFILE_IDEMPOTENT
        if not _position_fields_agree(existing, incoming):
            return PositionDecision.KEEP, PositionReason.MERGE_CONFLICT_KEEP
        if incoming.populated_fields - existing.populated_fields:
            return PositionDecision.MERGE, PositionReason.SAME_PROFILE_SUPERSET_MERGE
        if is_strict_successor(
            incoming.evidence_contract_id, existing.evidence_contract_id
        ):
            return PositionDecision.MERGE, PositionReason.SAME_PROFILE_CONTRACT_UPGRADE
        return PositionDecision.KEEP, PositionReason.SAME_PROFILE_IDEMPOTENT

    # Incoming is authoritative and on a different profile from existing.

    # Existing is effectively legacy (NULL profile or unverified).
    if existing_eff is None:
        if incoming.populated_fields >= existing.populated_fields:
            return PositionDecision.REPLACE, PositionReason.LEGACY_REPLACED_BY_AUTH
        return PositionDecision.KEEP, PositionReason.INCOMING_LESS_COMPLETE_KEEP

    # Different verified profiles.
    incoming_profile = get_profile(incoming.analysis_profile_id)
    has_dominates = bool(incoming_profile and existing_eff in incoming_profile.dominates)
    if not has_dominates:
        return PositionDecision.KEEP, PositionReason.INCOMPATIBLE_KEEP
    contract_ok = is_superset_or_successor(
        incoming.evidence_contract_id, existing.evidence_contract_id
    )
    superset_ok = incoming.populated_fields >= existing.populated_fields
    if contract_ok and superset_ok:
        return PositionDecision.REPLACE, PositionReason.DOMINATES_REPLACE
    return PositionDecision.KEEP, PositionReason.INCOMING_LESS_COMPLETE_KEEP
