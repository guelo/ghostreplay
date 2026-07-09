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

from app.analysis_profiles import BROWSER_PROFILE_ID, IDENTITY_FIELDS, get_profile
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

# Optional engine mate annotations. They are NOT a measure of evidence richness:
# every registered contract's ``required_fields`` excludes them, and a stronger
# search that resolves a shallow "mate" claim into a finite CP score is BETTER
# evidence, not less complete. So Rule 5 dominance strips them before its
# completeness (superset) comparison — a dominant CP-only row must not be blocked
# with INCOMING_LESS_COMPLETE_KEEP by a weaker row that merely stored a raw mate
# count. The exclusion is symmetric and global to Rule 5 (it also lets a CP-only
# canonical write replace a browser row that stored mate counts). Rule 2 same-
# profile merge is deliberately UNCHANGED: there mate fields are genuinely
# additive and still participate in agreement + superset-contribution checks.
OPTIONAL_MATE_FIELDS = frozenset({"played_eval_mate", "best_eval_mate"})


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
    # The batch writer could neither write nor resolve this key: under a
    # persistent concurrent deleter it vanished at every lock and lost every
    # ON CONFLICT re-insert past the recovery-pass budget. NOT an accepted write
    # (the incoming row is not stored) — callers should treat it as a failure.
    RECOVERY_ABORTED_KEEP = "recovery_aborted_keep"


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

    def is_replacement_eligible(self) -> bool:
        """May this row participate in dominance replacement / same-profile merge?

        Split from :meth:`is_effectively_authoritative` (g-cache-stronger-evals): a
        NON-authoritative but replacement-eligible profile (browser-analysis) can
        replace a weaker COMPATIBLE profile via an explicit ``dominates`` edge, but
        it is still not canonical — it never becomes a trusted /lookup hit and never
        reclaims legacy/unidentified rows (those keep true-authority gates). Canonical
        profiles are replacement-eligible too (defaulted from ``authoritative``), so
        their behavior is unchanged. Requires an active identity-verified profile.
        """
        if not self.identity_verified:
            return False
        profile = get_profile(self.analysis_profile_id)
        return bool(profile and profile.replacement_eligible and profile.active)


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
    incoming_repl = incoming.is_replacement_eligible()

    # Rule 2: same effective (verified) profile.
    if (
        existing_eff is not None
        and incoming_eff is not None
        and existing_eff == incoming_eff
    ):
        # Only replacement-eligible profiles may MERGE; others are idempotent (first
        # wins). Canonical stays eligible (defaulted from authoritative); browser-
        # analysis is now eligible and merges; browser-game stays first-wins.
        if not (existing.is_replacement_eligible() and incoming_repl):
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
        # stored row should advertise even though no evidence field changes. Mate
        # fields are NOT stripped here: for same-profile merge they are genuinely
        # additive evidence a later write can contribute.
        if incoming.populated_fields - existing.populated_fields:
            return Decision.MERGE, Reason.SAME_PROFILE_SUPERSET_MERGE
        if is_strict_successor(
            incoming.evidence_contract_id, existing.evidence_contract_id
        ):
            return Decision.MERGE, Reason.SAME_PROFILE_CONTRACT_UPGRADE
        return Decision.KEEP, Reason.SAME_PROFILE_IDEMPOTENT

    # Rule 3: incoming is not replacement-eligible and not same-profile → never
    # replaces. This is the eligibility gate: a non-eligible incoming row (e.g.
    # browser-game) keeps the existing row and never reaches Rule 5.
    if not incoming_repl:
        if existing_eff is None:
            return Decision.KEEP, Reason.LEGACY_KEEP_NON_AUTH
        return Decision.KEEP, Reason.NON_AUTHORITATIVE_KEEP

    # From here, incoming is replacement-eligible (authoritative canonical OR a
    # non-authoritative but eligible browser profile) and differs from existing.

    # Rule 4: existing is effectively legacy (NULL profile or unverified).
    # Legacy reclamation still requires TRUE authority, independent of the Rule 3
    # loosening above: a replacement-eligible but non-authoritative incoming row
    # (browser-analysis) must NOT reclaim legacy/unidentified evidence.
    if existing_eff is None:
        if not incoming_auth:
            return Decision.KEEP, Reason.LEGACY_KEEP_NON_AUTH
        legacy_fields = existing.populated_fields
        if incoming.populated_fields >= legacy_fields:
            return Decision.REPLACE, Reason.LEGACY_REPLACED_BY_AUTH
        return Decision.KEEP, Reason.INCOMING_LESS_COMPLETE_KEEP

    # Rule 5: different family, incoming replacement-eligible.
    incoming_profile = get_profile(incoming.analysis_profile_id)
    has_dominates = bool(
        incoming_profile and existing_eff in incoming_profile.dominates
    )
    if not has_dominates:
        return Decision.KEEP, Reason.INCOMPATIBLE_KEEP
    contract_ok = is_superset_or_successor(
        incoming.evidence_contract_id, existing.evidence_contract_id
    )
    # Mate annotations never veto dominance: strip them symmetrically before the
    # completeness comparison so a stronger CP-only row replaces a weaker row that
    # merely stored raw mate counts. contract_ok still guarantees no contract-
    # required field is dropped.
    superset_ok = (incoming.populated_fields - OPTIONAL_MATE_FIELDS) >= (
        existing.populated_fields - OPTIONAL_MATE_FIELDS
    )
    if contract_ok and superset_ok:
        return Decision.REPLACE, Reason.DOMINATES_REPLACE
    return Decision.KEEP, Reason.INCOMING_LESS_COMPLETE_KEEP


def populated_fields_of(data: dict) -> frozenset[str]:
    """Evidence fields that are non-null in ``data``."""
    return frozenset(f for f in EVIDENCE_FIELDS if data.get(f) is not None)


def _identity_verified(data: dict) -> bool:
    """True when stored identity metadata matches the claimed profile."""
    profile = get_profile(data.get("analysis_profile_id"))
    if profile is None:
        return False
    return all(data.get(f) == getattr(profile, f) for f in IDENTITY_FIELDS)


def project_cache_row(data: dict) -> CacheRow:
    """Project a raw cache-row ``dict`` into the minimal :class:`CacheRow`.

    The SINGLE projector for the replacement decision and for read-time display
    gating (:func:`display_upgrade_eligible`). ``analysis_cache_repo`` re-imports
    this so there is exactly one projection path (do NOT duplicate it there).
    """
    contract_id = data.get("evidence_contract_id")
    return CacheRow(
        analysis_profile_id=data.get("analysis_profile_id"),
        evidence_contract_id=contract_id,
        identity_verified=_identity_verified(data),
        contract_satisfied=contract_satisfied(contract_id, data),
        populated_fields=populated_fields_of(data),
        values={f: data.get(f) for f in EVIDENCE_FIELDS},
    )


def display_upgrade_eligible(row: CacheRow) -> bool:
    """True when a stored cache row may re-annotate the played move's MoveList label.

    v1 gate (g-xox0): the row is identity-verified, contract-satisfied, carries a
    move-grain ``classification`` (so it can re-annotate the played move — bare
    position-grain / eval-only rows are excluded), and comes from a profile that
    DOMINATES ``browser-game-v1``. That single ``dominates`` check UNIFIES canonical
    (its ``dominates`` set includes ``browser-game-v1``) and ``browser-analysis-v1``
    (``dominates={browser-game-v1}``), and NATURALLY EXCLUDES a ``browser-game-v1``
    row (does not dominate itself), ``jeffml``, and legacy/unidentified rows (not
    identity-verified). Non-authority is intentional: overlaying a strictly-stronger
    browser-analysis label over an already-displayed untrusted browser-game d17 label
    is not a trust escalation.

    v2 (post-g-mk1d): swap the fixed ``dominates(browser-game-v1)`` test for the
    row-level strength comparator so dynamic-depth browser-game rows rank too.
    """
    if not (row.identity_verified and row.contract_satisfied):
        return False
    if "classification" not in row.populated_fields:
        return False
    profile = get_profile(row.effective_profile_id())
    return bool(profile and BROWSER_PROFILE_ID in profile.dominates)
