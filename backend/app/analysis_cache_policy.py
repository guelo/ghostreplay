"""Pure decision function governing analysis_cache replacement.

Every cache writer routes through :func:`decide_analysis_cache_replacement` so
the quality-aware replacement policy lives in exactly one tested place rather
than being duplicated across SQL conflict clauses.

The decision separates two axes:
  * profile identity / authority — from :mod:`app.analysis_profiles`
  * evidence completeness — ``populated_fields`` vs. the row's selected contract
    (:mod:`app.evidence_contracts`)

Ordering signals are authority + explicit ``dominates`` edges + completeness
masks only — never raw numeric depth.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.analysis_profiles import get_profile
from app.evidence_contracts import (
    contract_satisfied,
    is_strict_successor,
    is_superset_or_successor,
)

# Evidence fields whose presence/agreement participate in completeness + merge.
EVIDENCE_FIELDS = (
    "best_move_uci",
    "best_move_san",
    "best_line_uci",
    "played_eval",
    "played_eval_mate",
    "best_eval",
    "best_eval_mate",
    "eval_delta",
    "classification",
)


class Decision(str, Enum):
    INSERT = "insert"
    REPLACE = "replace"
    MERGE = "merge"
    KEEP = "keep"


class Reason(str, Enum):
    NEW_KEY = "new_key"
    INVALID_INCOMING_KEEP = "invalid_incoming_keep"
    DOMINATES_REPLACE = "dominates_replace"
    NON_AUTHORITATIVE_KEEP = "non_authoritative_keep"
    LEGACY_KEEP_NON_AUTH = "legacy_keep_non_auth"
    LEGACY_REPLACED_BY_AUTH = "legacy_replaced_by_auth"
    SAME_PROFILE_IDEMPOTENT = "same_profile_idempotent"
    SAME_PROFILE_SUPERSET_MERGE = "same_profile_superset_merge"
    SAME_PROFILE_CONTRACT_UPGRADE = "same_profile_contract_upgrade"
    MERGE_CONFLICT_KEEP = "merge_conflict_keep"
    INCOMPATIBLE_KEEP = "incompatible_keep"
    INCOMING_LESS_COMPLETE_KEEP = "incoming_less_complete_keep"
    DUPLICATE_CONFLICT = "duplicate_conflict"


@dataclass(frozen=True)
class CacheRow:
    """Minimal projection used for the replacement decision."""

    analysis_profile_id: str | None  # None => legacy/unidentified
    evidence_contract_id: str | None
    identity_verified: bool  # persisted metadata MATCHES the registered profile
    contract_satisfied: bool  # contract-specific semantic validation passed
    populated_fields: frozenset[str]  # non-null evidence fields
    # Raw evidence values for overlapping-field agreement checks during MERGE.
    values: dict

    def effective_profile_id(self) -> str | None:
        """Profile id only when identity-verified; otherwise effective legacy."""
        if self.identity_verified:
            return self.analysis_profile_id
        return None

    def is_effectively_authoritative(self) -> bool:
        if not self.identity_verified:
            return False
        profile = get_profile(self.analysis_profile_id)
        # A retired (inactive) profile keeps its rows identity-verified for
        # dominance, but they no longer count as trusted/authoritative hits.
        return bool(profile and profile.authoritative and profile.active)


def _fields_agree(existing: CacheRow, incoming: CacheRow) -> bool:
    """Every overlapping non-null evidence field must agree."""
    overlap = existing.populated_fields & incoming.populated_fields
    for f in overlap:
        if existing.values.get(f) != incoming.values.get(f):
            return False
    return True


def incoming_is_valid(incoming: CacheRow) -> bool:
    """Validity gate applied before any insert/replace/merge.

    A row is valid only when it satisfies its declared evidence contract AND it
    does not make a profile claim it cannot back up: a row carrying an
    ``analysis_profile_id`` must identity-verify against the registry. Profile-less
    legacy rows (``analysis_profile_id is None``) are allowed; a row claiming a
    profile (canonical or otherwise) with mismatched/unknown engine metadata is
    rejected rather than persisted as a contradictory NEW_KEY.
    """
    if not incoming.contract_satisfied:
        return False
    if incoming.analysis_profile_id is not None and not incoming.identity_verified:
        return False
    return True


def decide_analysis_cache_replacement(
    existing: CacheRow | None,
    incoming: CacheRow,
) -> tuple[Decision, Reason]:
    # An incoming row that fails its contract or makes an unverifiable profile
    # claim is never stored.
    if not incoming_is_valid(incoming):
        return Decision.KEEP, Reason.INVALID_INCOMING_KEEP

    # Rule 1: missing key.
    if existing is None:
        return Decision.INSERT, Reason.NEW_KEY

    existing_eff = existing.effective_profile_id()
    incoming_eff = incoming.effective_profile_id()
    incoming_auth = incoming.is_effectively_authoritative()

    # Rule 2: same effective (verified) profile.
    if (
        existing_eff is not None
        and incoming_eff is not None
        and existing_eff == incoming_eff
    ):
        # Only authoritative profiles may MERGE; others are idempotent (first wins).
        if not (existing.is_effectively_authoritative() and incoming_auth):
            return Decision.KEEP, Reason.SAME_PROFILE_IDEMPOTENT
        # MERGE requires a compatible, strictly-newer-or-equal superset contract.
        if not is_superset_or_successor(
            incoming.evidence_contract_id, existing.evidence_contract_id
        ):
            return Decision.KEEP, Reason.SAME_PROFILE_IDEMPOTENT
        if not _fields_agree(existing, incoming):
            return Decision.KEEP, Reason.MERGE_CONFLICT_KEEP
        # A merge is worthwhile when incoming either contributes a field existing
        # lacks, OR carries a strictly-newer contract (e.g. v1 -> v2) that the
        # stored row should advertise even though no evidence field changes.
        if incoming.populated_fields - existing.populated_fields:
            return Decision.MERGE, Reason.SAME_PROFILE_SUPERSET_MERGE
        if is_strict_successor(
            incoming.evidence_contract_id, existing.evidence_contract_id
        ):
            return Decision.MERGE, Reason.SAME_PROFILE_CONTRACT_UPGRADE
        return Decision.KEEP, Reason.SAME_PROFILE_IDEMPOTENT

    # Rule 3: incoming is non-authoritative and not same-profile → never replaces.
    if not incoming_auth:
        if existing_eff is None:
            return Decision.KEEP, Reason.LEGACY_KEEP_NON_AUTH
        return Decision.KEEP, Reason.NON_AUTHORITATIVE_KEEP

    # From here, incoming is authoritative and differs from existing's profile.

    # Rule 4: existing is effectively legacy (NULL profile or unverified).
    if existing_eff is None:
        legacy_fields = existing.populated_fields
        if incoming.populated_fields >= legacy_fields:
            return Decision.REPLACE, Reason.LEGACY_REPLACED_BY_AUTH
        return Decision.KEEP, Reason.INCOMING_LESS_COMPLETE_KEEP

    # Rule 5: different family, incoming authoritative.
    incoming_profile = get_profile(incoming.analysis_profile_id)
    has_dominates = bool(
        incoming_profile and existing_eff in incoming_profile.dominates
    )
    if not has_dominates:
        return Decision.KEEP, Reason.INCOMPATIBLE_KEEP
    contract_ok = is_superset_or_successor(
        incoming.evidence_contract_id, existing.evidence_contract_id
    )
    superset_ok = incoming.populated_fields >= existing.populated_fields
    if contract_ok and superset_ok:
        return Decision.REPLACE, Reason.DOMINATES_REPLACE
    return Decision.KEEP, Reason.INCOMING_LESS_COMPLETE_KEEP


def populated_fields_of(data: dict) -> frozenset[str]:
    """Evidence fields that are non-null in ``data``."""
    return frozenset(f for f in EVIDENCE_FIELDS if data.get(f) is not None)
