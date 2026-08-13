"""Pure decision function governing analysis_cache replacement.

Every cache writer routes through :func:`decide_analysis_cache_replacement` so
the quality-aware replacement policy lives in exactly one tested place rather
than being duplicated across SQL conflict clauses.

The decision separates three axes:
  * profile identity / authority — from :mod:`app.analysis_profiles`
  * evidence completeness — ``populated_fields`` vs. the row's selected contract
    (:mod:`app.evidence_contracts`)
  * evidence GRAIN — which half of a position a contract describes. Completeness is
    only comparable WITHIN a grain, so a narrower post-split contract is judged by
    :func:`cross_grain_authority_replaces` or the exact same-profile migration
    predicate instead (g-6xc3, g-move-grain-same-prof)

Ordinary ordering signals are authority + explicit ``dominates`` edges +
completeness masks, never a bare numeric depth. The one contextual exception is
the analysis-evidence endpoint's visible-d21 correction: it supplies the current
session's validated in-game provenance as an exact compare-and-replace witness,
so the locked row may be ordered only when it is that same shipped sub-d21 search.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum

from app.analysis_profiles import (
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
    BROWSER_GAME_V2_PROFILE_ID,
    IDENTITY_FIELDS,
    StrengthComparison,
    get_profile,
    stamp_dynamic_profile,
)
from app.evidence_contracts import (
    MOVE_COMPLETE,
    RESOLVER_COMPLETE_V2,
    Grain,
    contract_grains,
    contract_satisfied,
    is_grain_split_contract,
    is_strict_successor,
    is_superset_or_successor,
)
from app.evidence_policy import (
    Capability,
    EdgeKind,
    OverlayMode,
    Supersession,
    compare_evidence_rows,
    compare_row_strength,
    has_capability,
    overlay_mode,
    validate_browser_provenance,
    verify_identity,
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

# EVIDENCE_FIELDS partitioned by GRAIN (g-6xc3). POSITION facts describe the
# position itself and are owned, post-split, by the normalized-FEN-keyed
# ``position_analysis`` table; MOVE facts describe the played move and stay in
# ``analysis_cache``. ``eval_delta`` belongs to NEITHER: it is
# ``best_eval - played_eval``, derived from BOTH halves, which is exactly why
# ``move-complete-v1`` deliberately does not validate it (a move-only row has no
# best_eval) and why the delta is recomposed at read time from the canonical
# position winner. Counting it as move-grain evidence would make the cross-grain
# rule below unreachable: every legacy v2 row carries a delta and a native move row
# need not. The three sets partition EVIDENCE_FIELDS exactly (pinned by a test).
POSITION_GRAIN_FIELDS = frozenset(
    {"best_move_uci", "best_move_san", "best_line_uci", "best_eval", "best_eval_mate"}
)
MOVE_GRAIN_FIELDS = frozenset({"played_eval", "played_eval_mate", "classification"})
CROSS_GRAIN_DERIVED_FIELDS = frozenset({"eval_delta"})

_GRAIN_FIELDS: dict[Grain, frozenset[str]] = {
    Grain.POSITION: POSITION_GRAIN_FIELDS,
    Grain.MOVE: MOVE_GRAIN_FIELDS,
}

# The only grain ``analysis_cache`` may hand off. A grain-split write does not DROP
# the position half, it RELOCATES it: the same producer run writes those facts to
# ``position_analysis``. Nothing relocates the MOVE grain — this IS the move-grain
# table — so a narrower row that would shed move evidence is plain evidence loss and
# never qualifies, however authoritative its writer.
RELOCATABLE_GRAINS = frozenset({Grain.POSITION})


class Decision(str, Enum):
    INSERT = "insert"
    REPLACE = "replace"
    MERGE = "merge"
    KEEP = "keep"


class Reason(str, Enum):
    NEW_KEY = "new_key"
    INVALID_INCOMING_KEEP = "invalid_incoming_keep"
    DOMINATES_REPLACE = "dominates_replace"
    # A corrective PROTOCOL_CORRECTION edge replaced the row (g-reuse-d21-search):
    # the truthful visible-MultiPV protocol supersedes the defective hidden
    # protocol for an exact key, independent of numeric strength. An accepted write.
    PROTOCOL_CORRECTED_REPLACE = "protocol_corrected_replace"
    NON_AUTHORITATIVE_KEEP = "non_authoritative_keep"
    # Incoming row claims a RETIRED (inactive) profile: kept (rejected), closing
    # the fail-open retirement window (g-reuse-d21-search D6). NOT an accepted write.
    INACTIVE_PROFILE_KEEP = "inactive_profile_keep"
    LEGACY_KEEP_NON_AUTH = "legacy_keep_non_auth"
    LEGACY_REPLACED_BY_AUTH = "legacy_replaced_by_auth"
    # An AUTHORITATIVE grain-split row replaced a NON-authoritative row that spanned a
    # grain it relocates (g-6xc3) — canonical ``move-complete-v1`` over a stored
    # browser ``resolver-complete-v2`` row. An accepted write. Distinct from
    # ``dominates_replace`` because the ordinary completeness gate did NOT pass and
    # could not: the two contracts describe different grains on purpose.
    CROSS_GRAIN_AUTHORITY_REPLACE = "cross_grain_authority_replace"
    # A post-split AUTHORITATIVE move row replaced the SAME canonical profile's
    # legacy combined v2 row after that run durably committed the position grain.
    # This is a storage-grain transition, not evidence-contract supersession: a
    # move-only row still is not a semantic superset of resolver-complete-v2.
    SAME_PROFILE_GRAIN_TRANSITION_REPLACE = (
        "same_profile_grain_transition_replace"
    )
    SAME_PROFILE_IDEMPOTENT = "same_profile_idempotent"
    SAME_PROFILE_SUPERSET_MERGE = "same_profile_superset_merge"
    SAME_PROFILE_CONTRACT_UPGRADE = "same_profile_contract_upgrade"
    MERGE_CONFLICT_KEEP = "merge_conflict_keep"
    # A same-profile MERGE was refused because the stored NON-AUTHORITATIVE row is
    # associated with a submitter other than the incoming one (g-v21l). ``_build_merged``
    # starts from the EXISTING row and writes evidence columns only, so merging B's
    # superset into A's row would let A read fields only B produced. NOT a denial of
    # access: the claim rule still runs, so B associates with the UNMERGED row, which
    # by the coverage condition contains only fields B produced. Never reached for an
    # effectively authoritative existing row — canonical merges skip the precondition.
    MERGE_OWNER_MISMATCH_KEEP = "merge_owner_mismatch_keep"
    # --- same-profile MEASURED-STRENGTH outcomes (g-mk1d, declared-dynamic
    # profiles only). Two rows from one dynamic profile are not interchangeable:
    # they may have searched to different depths on different devices.
    # A strictly stronger comparable search replaced the stored row. An accepted write.
    STRENGTH_REPLACE = "strength_replace"
    # The incoming search is strictly WEAKER than the stored one: kept (rejected).
    STRENGTH_WEAKER_KEEP = "strength_weaker_keep"
    # The two searches are not strength-rankable (different net / multipv /
    # protocol / limit type / build family, or an unknown-strength legacy row):
    # kept (rejected). Never guess an ordering across incompatible semantics.
    STRENGTH_INCOMPARABLE_KEEP = "strength_incomparable_keep"
    INCOMPATIBLE_KEEP = "incompatible_keep"
    INCOMING_LESS_COMPLETE_KEEP = "incoming_less_complete_keep"
    DUPLICATE_CONFLICT = "duplicate_conflict"
    # The batch writer could neither write nor resolve this key: under a
    # persistent concurrent deleter it vanished at every lock and lost every
    # ON CONFLICT re-insert past the recovery-pass budget. NOT an accepted write
    # (the incoming row is not stored) — callers should treat it as a failure.
    RECOVERY_ABORTED_KEEP = "recovery_aborted_keep"


# Central acceptance triage for every shared-writer verdict. Keep these groups
# exhaustive and disjoint: adding a Reason without deciding whether it is rejected,
# accepted by both consumers, or accepted by exactly one producer is a maintenance
# error. The browser-analysis endpoint and canonical precompute script derive their
# allowlists here, so their intentional differences live at one seam.
# Do not demote an accepted verdict merely because a current consumer cannot earn it:
# STRENGTH_REPLACE (g-mk1d) and CROSS_GRAIN_AUTHORITY_REPLACE (g-6xc3) are latent by design.
SHARED_PRODUCER_ACCEPTED_REASONS = frozenset(
    {
        Reason.NEW_KEY,
        Reason.DOMINATES_REPLACE,
        Reason.SAME_PROFILE_IDEMPOTENT,
        Reason.SAME_PROFILE_SUPERSET_MERGE,
        Reason.SAME_PROFILE_CONTRACT_UPGRADE,
        Reason.STRENGTH_REPLACE,
    }
)
CANONICAL_ONLY_ACCEPTED_REASONS = frozenset(
    {
        Reason.LEGACY_REPLACED_BY_AUTH,
        Reason.CROSS_GRAIN_AUTHORITY_REPLACE,
        Reason.SAME_PROFILE_GRAIN_TRANSITION_REPLACE,
    }
)
BROWSER_ANALYSIS_ONLY_ACCEPTED_REASONS = frozenset(
    {Reason.PROTOCOL_CORRECTED_REPLACE}
)
STORED_ROW_REJECTS_INCOMING_REASONS = frozenset(
    {
        Reason.INVALID_INCOMING_KEEP,
        Reason.NON_AUTHORITATIVE_KEEP,
        Reason.INACTIVE_PROFILE_KEEP,
        Reason.LEGACY_KEEP_NON_AUTH,
        Reason.MERGE_CONFLICT_KEEP,
        Reason.MERGE_OWNER_MISMATCH_KEEP,
        Reason.STRENGTH_WEAKER_KEEP,
        Reason.STRENGTH_INCOMPARABLE_KEEP,
        Reason.INCOMPATIBLE_KEEP,
        Reason.INCOMING_LESS_COMPLETE_KEEP,
        Reason.DUPLICATE_CONFLICT,
        Reason.RECOVERY_ABORTED_KEEP,
    }
)

_REASON_TRIAGE_GROUPS = (
    SHARED_PRODUCER_ACCEPTED_REASONS,
    CANONICAL_ONLY_ACCEPTED_REASONS,
    BROWSER_ANALYSIS_ONLY_ACCEPTED_REASONS,
    STORED_ROW_REJECTS_INCOMING_REASONS,
)
_MISCLASSIFIED_REASONS = frozenset(
    reason
    for reason in Reason
    if sum(reason in group for group in _REASON_TRIAGE_GROUPS) != 1
)
if _MISCLASSIFIED_REASONS:
    raise RuntimeError(
        "analysis-cache Reasons require exactly one stored-row acceptance "
        f"classification; misclassified={_MISCLASSIFIED_REASONS!r}"
    )

STORED_ROW_MATCHES_INCOMING_REASONS = (
    SHARED_PRODUCER_ACCEPTED_REASONS
    | CANONICAL_ONLY_ACCEPTED_REASONS
    | BROWSER_ANALYSIS_ONLY_ACCEPTED_REASONS
)

# Idempotence is accepted because the stored row already is the incoming evidence,
# but it is the one accepted verdict that performs no database mutation.
ROW_MUTATING_REASONS = STORED_ROW_MATCHES_INCOMING_REASONS - {
    Reason.SAME_PROFILE_IDEMPOTENT
}

# Producer-specific impossible verdicts stay fail-closed. The canonical writer's
# authority barrier resolves canonical-vs-browser before a protocol-correction edge;
# the fixed non-authoritative browser producer cannot earn authority-only replacement
# or the canonical same-profile grain transition.
CANONICAL_PRECOMPUTE_ACCEPTED_REASONS = (
    SHARED_PRODUCER_ACCEPTED_REASONS | CANONICAL_ONLY_ACCEPTED_REASONS
)
BROWSER_ANALYSIS_ACCEPTED_REASONS = (
    SHARED_PRODUCER_ACCEPTED_REASONS
    | BROWSER_ANALYSIS_ONLY_ACCEPTED_REASONS
)


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
    # The row's OWN stored IDENTITY_FIELDS values (g-mk1d). Needed because a
    # declared-dynamic profile carries ``None`` for its dynamic fields in the
    # registry, so measured strength must read the row, not the profile. Defaults
    # empty: a row projected without them is simply never strength-rankable.
    metadata: dict = field(default_factory=dict)

    def identity_values(self) -> dict:
        """The row's stored identity metadata (the ``RowView`` strength surface)."""
        return self.metadata

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


_VISIBLE_D21_INTENTIONAL_DIFFERENCES = frozenset(
    {
        "search_limit_value",
        "hash_mb",
        "multipv",
        "analyzer_protocol_version",
        "profile_manifest_digest",
    }
)


def _visible_d21_session_promotion_replaces(
    existing: CacheRow,
    incoming: CacheRow,
    live: CacheRow | None,
) -> bool:
    """Whether a session-witnessed d21 result may replace matching in-game evidence.

    ``browser-game-v2`` is declared-dynamic and intentionally has no categorical
    edge from ``browser-analysis-multipv-v2``: the profile can represent depth 21+
    and arbitrary valid builds/nets. The analysis-evidence endpoint can authorize
    the shipped path more narrowly because it owns a third operand — the exact
    validated provenance persisted on this session's move.

    The locked cache row must match that live operand field-for-field, and the
    two producers must share every identity field except the five deliberate
    visible-search differences above. This proves configuration equivalence, not
    authorship: an identically configured row originally written by another
    client is equally eligible because its evidence has the same ordering. An
    alternate build/net/hash, a nodes/movetime search, or a depth-21+ row remains
    incomparable and cannot be downgraded.
    """
    if live is None:
        return False
    if existing.effective_profile_id() != BROWSER_GAME_V2_PROFILE_ID:
        return False
    if live.effective_profile_id() != BROWSER_GAME_V2_PROFILE_ID:
        return False
    if incoming.effective_profile_id() != BROWSER_ANALYSIS_MULTIPV_PROFILE_ID:
        return False

    # This is a compare-and-replace witness, not merely a same-profile hint.
    if any(
        existing.metadata.get(field) != live.metadata.get(field)
        for field in IDENTITY_FIELDS
    ):
        return False

    if any(
        existing.metadata.get(field) != incoming.metadata.get(field)
        for field in IDENTITY_FIELDS
        if field not in _VISIBLE_D21_INTENTIONAL_DIFFERENCES
    ):
        return False

    existing_limit = existing.metadata.get("search_limit_value")
    incoming_limit = incoming.metadata.get("search_limit_value")
    return (
        existing.metadata.get("search_limit_type") == "depth"
        and incoming.metadata.get("search_limit_type") == "depth"
        # The current in-game worker's shipped Hash is 128 MB. Hash affects
        # search strength, so an arbitrary server-valid dynamic value cannot
        # inherit this narrowly-reviewed 128 -> visible-64 ordering.
        and existing.metadata.get("hash_mb") == 128
        and incoming.metadata.get("hash_mb") == 64
        and isinstance(existing_limit, int)
        and isinstance(incoming_limit, int)
        and incoming_limit == 21
        and existing_limit < incoming_limit
    )


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


def declared_profile_inactive(incoming: CacheRow) -> bool:
    """True when the row claims a registered but RETIRED (inactive) profile.

    Such a row is never stored — not even as a NEW_KEY insert (closes the
    fail-open retirement window, g-reuse-d21-search D6). This is deliberately
    SEPARATE from :func:`incoming_is_valid`: a retired-profile row's identity
    verifies fine, so the audit anchor keeps treating it as valid (not
    contaminated); it is simply refused storage. Callers that partition rows
    before the replacement decision (the batch writer's insert path) must apply
    this gate themselves so a retired row can never be inserted as a NEW_KEY
    without ever reaching :func:`decide_analysis_cache_replacement`.
    """
    declared = get_profile(incoming.analysis_profile_id)
    return declared is not None and not declared.active


def dynamic_identity_of(row: CacheRow) -> dict | None:
    """The row's DECLARED-DYNAMIC identity values, or ``None`` for a fixed profile.

    ``None`` means "this row's profile has no dynamic half" (every profile but
    ``browser-game-v2``), which is how callers detect the fixed-profile path.
    """
    profile = get_profile(row.effective_profile_id())
    if profile is None or not profile.dynamic_fields:
        return None
    return {f: row.metadata.get(f) for f in profile.dynamic_fields}


def _same_profile_strength_decision(
    existing: CacheRow, incoming: CacheRow
) -> tuple[Decision, Reason] | None:
    """Rule 2a: order two same-DYNAMIC-profile rows by measured search strength.

    Returns ``None`` when this rule does not apply — a fixed profile, or two rows
    whose dynamic provenance is IDENTICAL (then the historical idempotent/merge
    path is safe, because a merged row would carry the same provenance tuple it
    already has).

    There is deliberately NO cross-provenance MERGE: a merged row carries exactly
    ONE provenance tuple, so unioning evidence produced under DIFFERENT search
    settings would attribute one device's numbers to another device's identity.
    Equal-strength-but-different-provenance rows are therefore idempotent (first
    wins), not merged.
    """
    existing_dynamic = dynamic_identity_of(existing)
    if existing_dynamic is None:
        return None  # fixed profile — historical path.
    if existing_dynamic == dynamic_identity_of(incoming):
        return None  # identical provenance — merging cannot fabricate anything.

    strength = compare_row_strength(incoming, existing)
    if strength is StrengthComparison.A_STRONGER:
        # A strictly stronger comparable search may replace the stored row, but
        # only if it does not DROP evidence (same completeness guard as Rule 5).
        contract_ok = is_superset_or_successor(
            incoming.evidence_contract_id, existing.evidence_contract_id
        )
        superset_ok = (incoming.populated_fields - OPTIONAL_MATE_FIELDS) >= (
            existing.populated_fields - OPTIONAL_MATE_FIELDS
        )
        if contract_ok and superset_ok:
            return Decision.REPLACE, Reason.STRENGTH_REPLACE
        return Decision.KEEP, Reason.INCOMING_LESS_COMPLETE_KEEP
    if strength is StrengthComparison.B_STRONGER:
        return Decision.KEEP, Reason.STRENGTH_WEAKER_KEEP
    if strength is StrengthComparison.INCOMPARABLE:
        return Decision.KEEP, Reason.STRENGTH_INCOMPARABLE_KEEP
    # EQUAL strength, different provenance (e.g. same depth, different hash_mb):
    # neither row is better evidence and they cannot be merged, so first wins.
    return Decision.KEEP, Reason.SAME_PROFILE_IDEMPOTENT


def _fields_for_grains(grains: frozenset[Grain]) -> frozenset[str]:
    """The evidence fields ``grains`` claims.

    Never includes :data:`CROSS_GRAIN_DERIVED_FIELDS`, which belong to no single
    grain, so no grain-scoped comparison ever charges a row for them.
    """
    fields: frozenset[str] = frozenset()
    for grain in grains:
        fields |= _GRAIN_FIELDS[grain]
    return fields


def cross_grain_authority_replaces(existing: CacheRow, incoming: CacheRow) -> bool:
    """May an AUTHORITATIVE grain-split row replace a wider-grain stored row? (g-6xc3)

    The escape hatch from the completeness (superset) gate, which measures the wrong
    thing for a post-split write. ``move-complete-v1`` is deliberately NOT a
    superset/successor of ``resolver-complete-v2`` — a move-only row cannot satisfy
    v2's cross-grain ``eval_delta == f(best_eval, played_eval)`` invariant — and it
    populates none of the position fields, so BOTH halves of the gate fail and an
    authoritative canonical move write would lose to a NON-authoritative browser v2
    row. The position facts are not lost, they moved: the same producer run wrote them
    to ``position_analysis``.

    So this rule is keyed on AUTHORITY rather than contract supersession, and it is
    ASYMMETRIC in exactly that:

    1. ``incoming`` is effectively AUTHORITATIVE. A non-authoritative grain-split row
       never gets the hatch — not even across a PROTOCOL_CORRECTION / TIER_BASELINE
       edge, which is enough to WIN the ordering but never enough to license shedding
       a stored row's evidence;
    2. ``existing`` is NOT effectively authoritative. Two authoritative rows (canonical
       vs. canonical-linux) keep ``incompatible_keep``;
    3. ``incoming`` declares a ``grain_split`` contract — its narrowness is a
       relocation, not missing evidence. A legacy ``minimal-*`` row is equally narrow
       and equally canonical and still does NOT qualify;
    4. every grain it drops is RELOCATABLE (position only — see
       :data:`RELOCATABLE_GRAINS`), and it drops at least one, so a same-or-wider-grain
       pair falls through to the ordinary gate;
    5. within the grains it RETAINS it sheds nothing, with ``OPTIONAL_MATE_FIELDS``
       stripped symmetrically exactly as Rule 5 does. Authority licenses handing off a
       grain; it never licenses a thinner row in the grain this table owns.

    An unknown/absent contract on EITHER side yields an empty grain set and fails
    closed at (3)/(4) — a legacy uncontracted row is never replaced by this rule.
    """
    if not incoming.is_effectively_authoritative():
        return False
    if existing.is_effectively_authoritative():
        return False
    if not is_grain_split_contract(incoming.evidence_contract_id):
        return False
    incoming_grains = contract_grains(incoming.evidence_contract_id)
    existing_grains = contract_grains(existing.evidence_contract_id)
    dropped = existing_grains - incoming_grains
    if not dropped or not dropped <= RELOCATABLE_GRAINS:
        return False
    retained = _fields_for_grains(incoming_grains) - OPTIONAL_MATE_FIELDS
    return (incoming.populated_fields & retained) >= (
        existing.populated_fields & retained
    )


def same_profile_grain_transition_replaces(
    existing: CacheRow, incoming: CacheRow
) -> bool:
    """May canonical v2 transition in place to its same-profile move row?

    This is the narrow Rule 2 migration path for the canonical grain-split writer.
    ``move-complete-v1`` cannot generally supersede ``resolver-complete-v2``: it
    intentionally omits the relocated position grain and v2's cross-grain delta
    invariant.  But keeping a same-profile v2 row forever also prevents a canonical
    writer from converging a revisited key to the new storage contract.

    The caller must durably commit and verify the matching ``position_analysis``
    winner *before* attempting this move-row write.  The pure comparator cannot
    query that table, so the producer's position-first order is a required safety
    precondition, not something this predicate can prove.

    The transition is deliberately exact and fail-closed:

    * both rows resolve to the same active authoritative profile;
    * only a declared legacy ``resolver-complete-v2`` row may become a declared
      grain-split ``move-complete-v1`` row;
    * the contract transition relocates exactly the POSITION grain; and
    * the incoming row carries no fields from that relocated grain; and
    * every populated non-optional move fact is retained and agrees.  Optional
      mate annotations may appear, disappear, or change.  Position facts and
      ``eval_delta`` are outside this agreement gate because the former have a new
      owner and the latter is a cross-grain canonical-run snapshot.

    The stored v2 row need not satisfy its whole combined contract.  A valid
    incoming move row may heal malformed position/delta evidence, but never a
    disagreement or loss in the move grain that ``analysis_cache`` still owns.
    """
    existing_eff = existing.effective_profile_id()
    incoming_eff = incoming.effective_profile_id()
    if existing_eff is None or existing_eff != incoming_eff:
        return False
    if not (
        existing.is_effectively_authoritative()
        and incoming.is_effectively_authoritative()
    ):
        return False
    if existing.evidence_contract_id != RESOLVER_COMPLETE_V2:
        return False
    if incoming.evidence_contract_id != MOVE_COMPLETE:
        return False
    if not is_grain_split_contract(incoming.evidence_contract_id):
        return False

    existing_grains = contract_grains(existing.evidence_contract_id)
    incoming_grains = contract_grains(incoming.evidence_contract_id)
    dropped = existing_grains - incoming_grains
    if dropped != RELOCATABLE_GRAINS or Grain.MOVE not in incoming_grains:
        return False
    # Contract satisfaction proves that required move fields are present; it does
    # not forbid extra evidence.  A full-row REPLACE would persist any extra
    # position fields instead of clearing the relocated columns, so fail closed
    # unless the incoming row is physically grain-clean as well as contract-labelled.
    if incoming.populated_fields & _fields_for_grains(dropped):
        return False

    retained = _fields_for_grains(incoming_grains) - OPTIONAL_MATE_FIELDS
    existing_retained = existing.populated_fields & retained
    if not (incoming.populated_fields & retained) >= existing_retained:
        return False
    return all(
        incoming.values.get(field) == existing.values.get(field)
        for field in existing_retained
    )


def merge_owner_ok(
    existing: CacheRow,
    existing_submitters: frozenset[int],
    incoming_submitter: int | None,
) -> bool:
    """May a same-profile MERGE fold ``incoming``'s evidence into ``existing``?

    ``_build_merged`` keeps the EXISTING row's provenance and only fills its null
    evidence columns, so the merged row is read by everyone already associated with
    ``existing``. That is only safe when nobody but the incoming submitter holds an
    association — i.e. the existing association set is a subset of
    ``{incoming submitter}`` (which includes the empty set).

    Evaluated ONLY for a non-authoritative existing row. Canonical merges skip it
    entirely and are byte-for-byte unchanged; that is belt-and-braces on top of the
    claim restriction (canonical rows never acquire associations in the first place,
    so even an unguarded subset test would hold) and it makes canonical parity
    independent of the claim rule being correct.
    """
    if existing.is_effectively_authoritative():
        return True
    allowed = (
        frozenset() if incoming_submitter is None else frozenset({incoming_submitter})
    )
    return existing_submitters <= allowed


def is_browser_claimable(row: CacheRow) -> bool:
    """True when ``row`` is effectively browser-analysis-multipv-v2 (g-v21l).

    The browser-only restriction on the claim rule: identity verified, the ACTIVE
    visible-MultiPV profile, and NOT effectively authoritative. Applied to BOTH the
    incoming row and the row that will be stored after the decision — condition 3 is
    the load-bearing one, because a browser submission can agree with and cover a
    canonical tuple (Rule 5 then returns INCOMPATIBLE_KEEP), and a profile-agnostic
    claim rule would attach a browser user to the canonical row. That association
    would later fail :func:`merge_owner_ok` and block a canonical merge, breaking
    canonical parity. Canonical rows must carry no associations, by construction and
    at every moment.
    """
    profile = get_profile(row.effective_profile_id())
    return (
        profile is not None
        and profile.profile_id == BROWSER_ANALYSIS_MULTIPV_PROFILE_ID
        and profile.active
        and not row.is_effectively_authoritative()
    )


def keep_or_merge_claim_ok(existing: CacheRow, incoming: CacheRow) -> bool:
    """The KEEP/MERGE half of the claim rule (g-v21l).

    Associate the incoming submitter iff BOTH hold:

    1. every overlapping populated evidence field is equal (:func:`_fields_agree`);
    2. ``existing.populated_fields <= incoming.populated_fields`` — the stored row
       populates nothing the submitter did not independently produce.

    Condition 2 is what makes an association SAFE rather than merely plausible: it
    guarantees a user can only ever read fields they produced themselves. Without it
    a fabricator who agreed on every overlapping field could still inject, say, a
    mate field the corroborating user left empty. Its deliberate cost is that a user
    submitting a strict SUBSET of a stored row does not associate and falls back to
    the worker.
    """
    return _fields_agree(existing, incoming) and (
        existing.populated_fields <= incoming.populated_fields
    )


def decide_analysis_cache_replacement(
    existing: CacheRow | None,
    incoming: CacheRow,
    *,
    existing_submitters: frozenset[int] = frozenset(),
    incoming_submitter: int | None = None,
    visible_d21_live: CacheRow | None = None,
) -> tuple[Decision, Reason]:
    # An incoming row that fails its contract or makes an unverifiable profile
    # claim is never stored.
    if not incoming_is_valid(incoming):
        return Decision.KEEP, Reason.INVALID_INCOMING_KEEP

    # Validity: an incoming row claiming a RETIRED (inactive) profile is never
    # stored — not even as a NEW_KEY insert. The endpoint producer discriminator
    # already fails a stale client closed before the writer, so this is defense in
    # depth; the batch writer applies the same gate on its insert path.
    if declared_profile_inactive(incoming):
        return Decision.KEEP, Reason.INACTIVE_PROFILE_KEEP

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
        # Rule 2a (g-mk1d): for a DECLARED-DYNAMIC profile, two same-profile rows
        # are NOT interchangeable — each carries its own device's search settings.
        # Order them by measured strength before the idempotent/merge path.
        strength_decision = _same_profile_strength_decision(existing, incoming)
        if strength_decision is not None:
            return strength_decision
        # Rule 2b (g-move-grain-same-prof): once the canonical producer has
        # durably committed the matching position winner, let its move-grain row
        # transition this exact profile's legacy combined v2 row in place.  This
        # precedes the ordinary contract-superset path because MOVE_COMPLETE is
        # intentionally not registered as a semantic successor of v2.
        if same_profile_grain_transition_replaces(existing, incoming):
            return (
                Decision.REPLACE,
                Reason.SAME_PROFILE_GRAIN_TRANSITION_REPLACE,
            )
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
        # Single-affected-submitter precondition (g-v21l), guarding BOTH merge
        # decisions and nothing else: an idempotent KEEP writes no evidence column,
        # so it needs no ownership check and must keep its historical reason.
        merge_allowed = merge_owner_ok(existing, existing_submitters, incoming_submitter)
        if incoming.populated_fields - existing.populated_fields:
            if not merge_allowed:
                return Decision.KEEP, Reason.MERGE_OWNER_MISMATCH_KEEP
            return Decision.MERGE, Reason.SAME_PROFILE_SUPERSET_MERGE
        if is_strict_successor(
            incoming.evidence_contract_id, existing.evidence_contract_id
        ):
            if not merge_allowed:
                return Decision.KEEP, Reason.MERGE_OWNER_MISMATCH_KEEP
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
        # Rule 4b (g-6xc3): an authoritative grain-split write reclaims a legacy /
        # unidentified row that spans a grain it relocates, even though it populates
        # fewer fields. Same rule as 5b below, at the other completeness veto — the
        # true-authority gate this branch already applied is condition (1).
        if cross_grain_authority_replaces(existing, incoming):
            return Decision.REPLACE, Reason.CROSS_GRAIN_AUTHORITY_REPLACE
        return Decision.KEEP, Reason.INCOMING_LESS_COMPLETE_KEEP

    # Rule 5: different family, incoming replacement-eligible. The raw
    # ``dominates`` lookup is rerouted through the shared comparator so the
    # authority barrier and explicit edges (AUTHORITY / PROTOCOL_CORRECTION /
    # TIER_BASELINE) live in one place (g-reuse-d21-search D6). The comparator
    # gives ordering + edge kind. The endpoint-scoped visible-d21 witness may
    # additionally prove the exact current-game row described above; it is not a
    # global profile edge. The completeness gate below is unchanged.
    comparison = compare_evidence_rows(incoming, existing)
    contextual_d21_win = _visible_d21_session_promotion_replaces(
        existing, incoming, visible_d21_live
    )
    # Two comparator grains of win — categorical A_SUPERSEDES and measured
    # A_STRONGER — let the incoming row through. Ordinarily every other outcome
    # keeps the stored row; the contextual d21 witness is the one additional,
    # explicitly-scoped admission from INCOMPARABLE.
    if (
        comparison.outcome
        not in (Supersession.A_SUPERSEDES, Supersession.A_STRONGER)
        and not contextual_d21_win
    ):
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
        # A MEASURED win reports strength_replace — the same reason Rule 2a uses
        # for the same fact, so "this row won on numbers" reads identically whether
        # the two rows shared a profile or not. Of the categorical wins,
        # PROTOCOL_CORRECTION gets its own reason so the corrective replacement of
        # a defective hidden row is observable; AUTHORITY / TIER_BASELINE keep the
        # historical dominates_replace reason for parity. The contextual d21 win
        # deliberately shares dominates_replace to preserve the endpoint's wire
        # contract; introduce a distinct Reason only with an explicit telemetry/API
        # requirement.
        if comparison.outcome is Supersession.A_STRONGER:
            reason = Reason.STRENGTH_REPLACE
        elif comparison.kind is EdgeKind.PROTOCOL_CORRECTION:
            reason = Reason.PROTOCOL_CORRECTED_REPLACE
        else:
            reason = Reason.DOMINATES_REPLACE
        return Decision.REPLACE, reason
    # Rule 5b (g-6xc3): the completeness gate cannot be satisfied ACROSS GRAINS, by
    # construction, so an authoritative grain-split write is judged on authority
    # instead. This point is reached after either a comparator win OR the contextual
    # d21 witness (whose comparator result is INCOMPARABLE). The contextual path is
    # non-authoritative and therefore cannot satisfy this rule; for the authoritative-
    # over-non-authoritative pair the rule requires, step 2's authority barrier
    # returns A_SUPERSEDES.
    if cross_grain_authority_replaces(existing, incoming):
        return Decision.REPLACE, Reason.CROSS_GRAIN_AUTHORITY_REPLACE
    return Decision.KEEP, Reason.INCOMING_LESS_COMPLETE_KEEP


def populated_fields_of(data: dict) -> frozenset[str]:
    """Evidence fields that are non-null in ``data``."""
    return frozenset(f for f in EVIDENCE_FIELDS if data.get(f) is not None)


def _identity_verified(data: dict) -> bool:
    """True when stored identity metadata matches the claimed profile.

    Delegates to the single :func:`app.evidence_policy.verify_identity` so all
    five call-sites share one implementation.
    """
    return verify_identity(data)


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
        metadata={f: data.get(f) for f in IDENTITY_FIELDS},
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

    v2 (g-reuse-d21-search): the fixed ``dominates(browser-game-v1)`` test is
    replaced by the capability model — ``has_capability(row, DISPLAY_OVERLAY)`` AND
    the profile's ``OVERLAY_MODE == ALWAYS``. The truth table is IDENTICAL to the
    old test (canonical + browser-analysis-v1 both grant DISPLAY_OVERLAY and are
    ALWAYS; browser-game/jeffml/legacy grant nothing), now additionally admitting
    the corrective browser-analysis-multipv-v2. DISPLAY_OVERLAY is
    retirement-surviving, so a retired browser-analysis-v1 row still overlays.
    """
    if not (row.identity_verified and row.contract_satisfied):
        return False
    if "classification" not in row.populated_fields:
        return False
    if not has_capability(row, Capability.DISPLAY_OVERLAY):
        return False
    return overlay_mode(row.effective_profile_id()) is OverlayMode.ALWAYS


def browser_live_descriptor(raw_provenance: str | None) -> CacheRow | None:
    """Rebuild the LIVE comparison operand from a persisted provenance JSON blob.

    ``session_moves.browser_provenance`` stores ONLY the seven declared-dynamic
    values a client may self-report. The FIXED half (engine_name, small-net,
    multipv, analyzer protocol) and the ``profile_manifest_digest`` are NOT stored
    — they are reconstructed here from the server registry, so a hand-edited
    session-move row can never claim a fixed identity it did not earn.

    Returns ``None`` for a NULL, unparseable, non-object, or identity-failing blob
    (a tampered or legacy row). ``None`` reads downstream as "no comparable live
    evidence" → the REQUIRES_COMPARISON overlay is withheld, which is the safe
    direction: the viewer keeps their own label.
    """
    if not raw_provenance:
        return None
    try:
        parsed = json.loads(raw_provenance)
    except (TypeError, ValueError):
        return None
    fields = validate_browser_provenance(parsed)
    if fields is None:
        return None
    data = {
        "analysis_profile_id": BROWSER_GAME_V2_PROFILE_ID,
        **stamp_dynamic_profile(BROWSER_GAME_V2_PROFILE_ID, fields.values),
    }
    if not verify_identity(data):
        return None
    return CacheRow(
        analysis_profile_id=BROWSER_GAME_V2_PROFILE_ID,
        evidence_contract_id=None,
        identity_verified=True,
        # A descriptor is an identity/strength operand only — it carries no
        # evidence, so it is never contract-satisfied and never overlays anything
        # itself.
        contract_satisfied=False,
        populated_fields=frozenset(),
        values={},
        metadata={f: data.get(f) for f in IDENTITY_FIELDS},
    )


def display_upgrade_eligible_vs(stored: CacheRow, live: CacheRow | None) -> bool:
    """Overlay gate for a stored row that MAY need a live comparison (g-mk1d).

    The full two-operand gate for the refetch overlay:

    * an ALWAYS-mode row (canonical, browser-analysis-v1/-multipv-v2) overlays
      unconditionally — identical to :func:`display_upgrade_eligible`, and ``live``
      is not consulted. Parity for every profile that shipped before g-mk1d;
    * a REQUIRES_COMPARISON row (``browser-game-v2``) overlays ONLY when it is
      STRICTLY STRONGER than ``live`` under the same guarded comparison that
      governs STORAGE replacement (:func:`compare_row_strength`). Equal, weaker,
      incomparable, or a missing/unverifiable ``live`` operand → no overlay, and
      the viewer keeps the label they already have;
    * NEVER-mode / uncapable rows never overlay.

    Sharing ``compare_row_strength`` with Rule 2a is the point: what the writer
    considers a stronger row and what the reader is willing to display cannot
    drift apart.
    """
    if not (stored.identity_verified and stored.contract_satisfied):
        return False
    if "classification" not in stored.populated_fields:
        return False
    if not has_capability(stored, Capability.DISPLAY_OVERLAY):
        return False
    mode = overlay_mode(stored.effective_profile_id())
    if mode is OverlayMode.ALWAYS:
        return True
    if mode is not OverlayMode.REQUIRES_COMPARISON:
        return False
    if live is None:
        return False
    return compare_row_strength(stored, live) is StrengthComparison.A_STRONGER
