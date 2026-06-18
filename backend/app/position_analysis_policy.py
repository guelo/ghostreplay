"""Pure position-grain primitives for the canonical-winner backfill (Phase 2).

This module is intentionally DB-free: it decides, given the eligible
``analysis_cache`` candidates for a single ``normalized_fen``, which one is the
canonical winner and whether the group disagrees. The orchestration that reads
rows, groups them, and writes ``position_analysis`` / ``position_analysis_conflicts``
lives in :mod:`app.position_analysis_backfill`.

Winner selection is **group-then-pick**: never an ``INSERT ... ON CONFLICT``
ordering trick. A candidate is promoted over the preference-earlier winner ONLY
when it is *strictly stronger* under the guarded
:func:`app.analysis_profiles.compare_search_strength`; equal-strength /
incomparable candidates resolve by a deterministic preference order
(authoritative-profile priority, then higher ``cache_id``). Raw
``search_limit_value`` is never a sort key outside the guarded comparator.

The conflict trigger is best-move / mate-winner disagreement only (see
:func:`position_signature`). PV-continuation and CP-only ``best_eval`` differences
are captured in the audit axes when such a conflict fires, but are NOT themselves
triggers — ``position-complete-v1`` only requires the PV to start with the best
move, so those differences do not change the canonical decision.

Phase 3 will EXTEND this module with the live write decision
(``decide_position_analysis_replacement``); the primitives here are shared.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.analysis_profiles import (
    AUTHORITATIVE_PROFILE_PRIORITY,
    IDENTITY_FIELDS,
    Profile,
    StrengthComparison,
    compare_search_strength,
    get_profile,
)
from app.evidence_contracts import legacy_v2_satisfies_position

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


def _effectively_authoritative(row: dict) -> bool:
    """Profile is authoritative+active AND every IDENTITY_FIELDS column matches.

    Same gate as ``api/analysis.py:_is_authoritative`` /
    ``analysis_cache_repo._identity_verified``, expressed over a row dict so this
    module stays DB-free. Excludes browser-game / JeffML (non-authoritative) and
    any row whose stored identity does not back up its claimed profile.
    """
    profile = get_profile(row.get("analysis_profile_id"))
    if profile is None or not profile.authoritative or not profile.active:
        return False
    return all(row.get(f) == getattr(profile, f) for f in IDENTITY_FIELDS)


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
