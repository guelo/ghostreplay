"""Unit tests for the pure analysis_cache replacement comparator."""

import dataclasses

import pytest

from app.analysis_cache_policy import (
    CROSS_GRAIN_DERIVED_FIELDS,
    EVIDENCE_FIELDS,
    MOVE_GRAIN_FIELDS,
    POSITION_GRAIN_FIELDS,
    RELOCATABLE_GRAINS,
    CacheRow,
    Decision,
    Reason,
    decide_analysis_cache_replacement,
)
from app.analysis_profiles import (
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
    BROWSER_ANALYSIS_PROFILE_ID,
    BROWSER_PROFILE_ID,
    CANONICAL_LINUX_PROFILE_ID,
    CANONICAL_PROFILE_ID,
    JEFFML_PROFILE_ID,
)
from app.evidence_contracts import (
    MINIMAL_PLAYED_EVAL,
    MOVE_COMPLETE,
    RESOLVER_COMPLETE,
    RESOLVER_COMPLETE_V2,
    Grain,
)


def _row(
    *,
    profile=None,
    contract=None,
    verified=False,
    satisfied=True,
    fields=(),
    values=None,
):
    return CacheRow(
        analysis_profile_id=profile,
        evidence_contract_id=contract,
        identity_verified=verified,
        contract_satisfied=satisfied,
        populated_fields=frozenset(fields),
        values=values or {f: object() for f in fields},
    )


def _canonical(fields, contract=RESOLVER_COMPLETE, values=None):
    return _row(
        profile=CANONICAL_PROFILE_ID,
        contract=contract,
        verified=True,
        fields=fields,
        values=values,
    )


def _browser(fields, contract=RESOLVER_COMPLETE, values=None):
    return _row(
        profile=BROWSER_PROFILE_ID,
        contract=contract,
        verified=True,  # browser profile has all-None identity, trivially matches
        fields=fields,
        values=values,
    )


def _passive(fields, contract=RESOLVER_COMPLETE, values=None):
    """An ACTIVE profile carrying browser-game-v1's flags from before its retirement:
    non-authoritative, not replacement-eligible, no ``dominates`` edge, all-``None``
    identity (so ``identity_verified`` is trivially true).

    Used wherever a test needs a generic passive INCOMING row: browser-game-v1 is
    now inactive (g-bgv1-cutover) and every incoming v1 row is refused at the
    active gate with ``INACTIVE_PROFILE_KEEP`` before any rule can be exercised.
    """
    return _row(profile=JEFFML_PROFILE_ID, contract=contract, verified=True,
                fields=fields, values=values)


def _legacy(fields, values=None):
    return _row(profile=None, contract=None, verified=False, fields=fields, values=values)


# Rule 1 — missing key
def test_insert_on_missing_key():
    decision, reason = decide_analysis_cache_replacement(None, _passive({"played_eval"}, MINIMAL_PLAYED_EVAL))
    assert decision is Decision.INSERT
    assert reason is Reason.NEW_KEY


def test_invalid_incoming_missing_key_kept():
    incoming = _row(
        profile=BROWSER_PROFILE_ID,
        contract=MINIMAL_PLAYED_EVAL,
        verified=True,
        satisfied=False,
        fields={"played_eval"},
    )
    decision, reason = decide_analysis_cache_replacement(None, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.INVALID_INCOMING_KEEP


# Rule 3 — a non-authoritative row cannot replace canonical
def test_non_authoritative_cannot_replace_canonical():
    existing = _canonical({"best_move_uci", "best_line_uci", "classification", "eval_delta"})
    incoming = _passive({"best_move_uci", "best_line_uci", "classification", "eval_delta"})
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.NON_AUTHORITATIVE_KEEP


# Rule 4 — a non-authoritative row cannot reclaim legacy
def test_non_authoritative_cannot_reclaim_legacy():
    existing = _legacy({"played_eval", "best_eval"})
    incoming = _passive({"played_eval"}, MINIMAL_PLAYED_EVAL)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.LEGACY_KEEP_NON_AUTH


# Rule 4 — authoritative reclaims legacy only when >= complete
def test_authoritative_replaces_legacy_superset():
    existing = _legacy({"played_eval"})
    incoming = _canonical({"played_eval", "best_move_uci", "best_line_uci", "eval_delta"})
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.REPLACE
    assert reason is Reason.LEGACY_REPLACED_BY_AUTH


def test_authoritative_keeps_legacy_when_less_complete():
    existing = _legacy({"played_eval", "best_eval"})
    incoming = _canonical({"best_move_uci", "best_line_uci", "eval_delta"})
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.INCOMING_LESS_COMPLETE_KEEP


# Rule 5 — cross-family dominance requires explicit edge + contract succession + completeness
def test_canonical_dominates_jeffml():
    shared = object()
    existing = _row(
        profile=JEFFML_PROFILE_ID,
        contract=MINIMAL_PLAYED_EVAL,
        verified=True,
        fields={"played_eval"},
        values={"played_eval": shared},
    )
    incoming = _canonical(
        {"played_eval", "best_move_uci", "best_line_uci", "eval_delta"},
        values={"played_eval": shared, "best_move_uci": 1, "best_line_uci": 1, "eval_delta": 1},
    )
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    # resolver-complete-v1 supersedes minimal-played-eval-v1, canonical dominates
    # jeffml, and the canonical row preserves played_eval -> REPLACE.
    assert decision is Decision.REPLACE
    assert reason is Reason.DOMINATES_REPLACE


def test_canonical_keeps_jeffml_when_it_would_drop_a_field():
    # Canonical row lacks played_eval that the jeffml row had -> no silent loss.
    existing = _row(profile=JEFFML_PROFILE_ID, contract=MINIMAL_PLAYED_EVAL, verified=True, fields={"played_eval"})
    incoming = _canonical({"best_move_uci", "best_line_uci", "eval_delta"})
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.INCOMING_LESS_COMPLETE_KEEP


# Rule 2 — same profile idempotent for a passive producer
def test_same_passive_profile_idempotent():
    existing = _passive({"played_eval"}, MINIMAL_PLAYED_EVAL)
    incoming = _passive({"played_eval"}, MINIMAL_PLAYED_EVAL)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.SAME_PROFILE_IDEMPOTENT


# Rule 2 — contract-only successor upgrade (v1 -> v2, no new evidence field)
def test_same_profile_contract_upgrade():
    shared = {f: 1 for f in ("best_move_uci", "best_line_uci", "classification", "eval_delta")}
    fields = set(shared)
    existing = _canonical(fields, contract=RESOLVER_COMPLETE, values=shared)
    incoming = _canonical(fields, contract=RESOLVER_COMPLETE_V2, values=shared)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.MERGE
    assert reason is Reason.SAME_PROFILE_CONTRACT_UPGRADE


def test_same_profile_same_contract_no_new_field_idempotent():
    shared = {f: 1 for f in ("best_move_uci", "best_line_uci", "classification", "eval_delta")}
    fields = set(shared)
    existing = _canonical(fields, contract=RESOLVER_COMPLETE_V2, values=shared)
    incoming = _canonical(fields, contract=RESOLVER_COMPLETE_V2, values=shared)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.SAME_PROFILE_IDEMPOTENT


def test_retired_profile_row_is_not_authoritative(monkeypatch):
    """A retired (inactive) canonical row stops counting as a trusted hit."""
    import app.analysis_cache_policy as policy
    from app.analysis_profiles import get_profile

    canonical = get_profile(CANONICAL_PROFILE_ID)
    retired = dataclasses.replace(canonical, active=False)

    def fake_get_profile(pid):
        if pid == CANONICAL_PROFILE_ID:
            return retired
        return get_profile(pid)

    monkeypatch.setattr(policy, "get_profile", fake_get_profile)
    row = _canonical({"best_move_uci", "best_line_uci"}, contract=RESOLVER_COMPLETE_V2)
    assert row.is_effectively_authoritative() is False


# Sparse JeffML cannot replace richer existing
def test_jeffml_cannot_replace_canonical():
    existing = _canonical({"best_move_uci", "best_line_uci", "classification", "eval_delta", "played_eval"})
    incoming = _row(profile=JEFFML_PROFILE_ID, contract=MINIMAL_PLAYED_EVAL, verified=True, fields={"played_eval"})
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.NON_AUTHORITATIVE_KEEP


# Unverified canonical claim is treated as legacy, not same-profile
def test_unverified_canonical_claim_is_legacy():
    # existing claims canonical but identity not verified -> effective legacy
    existing = _row(profile=CANONICAL_PROFILE_ID, contract=RESOLVER_COMPLETE, verified=False, fields={"played_eval"})
    incoming = _canonical({"played_eval", "best_move_uci", "best_line_uci", "eval_delta"})
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.REPLACE
    assert reason is Reason.LEGACY_REPLACED_BY_AUTH


def test_unverified_profile_claim_not_inserted_on_missing_key():
    # Claims canonical but identity does not verify -> invalid, not a NEW_KEY.
    incoming = _row(
        profile=CANONICAL_PROFILE_ID,
        contract=RESOLVER_COMPLETE,
        verified=False,
        satisfied=True,
        fields={"best_move_uci", "best_line_uci", "classification", "eval_delta"},
    )
    decision, reason = decide_analysis_cache_replacement(None, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.INVALID_INCOMING_KEEP


def test_profileless_row_inserts_on_missing_key():
    incoming = _row(profile=None, contract=MINIMAL_PLAYED_EVAL, verified=False, fields={"played_eval"})
    decision, reason = decide_analysis_cache_replacement(None, incoming)
    assert decision is Decision.INSERT
    assert reason is Reason.NEW_KEY


def test_invalid_incoming_with_existing_kept():
    existing = _canonical({"best_move_uci", "best_line_uci", "classification", "eval_delta"})
    incoming = _row(profile=BROWSER_PROFILE_ID, contract=MINIMAL_PLAYED_EVAL, verified=True, satisfied=False, fields={"played_eval"})
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.INVALID_INCOMING_KEEP


# ============================================================================
# g-cache-stronger-evals: browser-analysis-v1 replacement eligibility & mate strip
# ============================================================================

def _browser_analysis(fields, contract=RESOLVER_COMPLETE_V2, values=None):
    # The RETIRED hidden analyzer profile (browser-analysis-v1, now inactive). Its
    # stored rows stay identity-verified (digest excludes ``active``), so they can
    # still be an EXISTING row that a successor correctively replaces, but an
    # INCOMING v1 row now fails closed (inactive_profile_keep).
    return _row(
        profile=BROWSER_ANALYSIS_PROFILE_ID,
        contract=contract,
        verified=True,
        fields=fields,
        values=values,
    )


def _browser_analysis_multipv(fields, contract=RESOLVER_COMPLETE_V2, values=None):
    # The corrective visible-MultiPV successor (browser-analysis-multipv-v2, active,
    # replacement-eligible). The endpoint stamps the full pinned identity, so its
    # rows identity-verify.
    return _row(
        profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
        contract=contract,
        verified=True,
        fields=fields,
        values=values,
    )


# Complete field shapes each producer stores for a fully-analyzed move.
_V2_CORE = frozenset(
    {"best_move_uci", "best_move_san", "best_line_uci", "played_eval", "best_eval", "eval_delta", "classification"}
)
_V2_WITH_MATE = _V2_CORE | {"played_eval_mate", "best_eval_mate"}


def _agree(fields, overrides=None):
    """Value dict so overlapping fields AGREE across existing/incoming rows."""
    values = {f: f for f in fields}
    if overrides:
        values.update(overrides)
    return values


# --- replacement eligibility flags ------------------------------------------
def test_browser_analysis_multipv_is_replacement_eligible_not_authoritative():
    row = _browser_analysis_multipv(_V2_CORE)
    assert row.is_replacement_eligible() is True
    assert row.is_effectively_authoritative() is False


def test_retired_browser_analysis_not_replacement_eligible():
    # browser-analysis-v1 is retired (inactive); is_replacement_eligible requires
    # an active profile, so its rows no longer participate as an INCOMING replacer.
    row = _browser_analysis(_V2_CORE)
    assert row.is_replacement_eligible() is False
    assert row.is_effectively_authoritative() is False


def test_browser_game_is_not_replacement_eligible():
    row = _browser(_V2_CORE, RESOLVER_COMPLETE)
    assert row.is_replacement_eligible() is False


def test_canonical_is_replacement_eligible_and_authoritative():
    row = _canonical(_V2_CORE, contract=RESOLVER_COMPLETE_V2)
    assert row.is_replacement_eligible() is True
    assert row.is_effectively_authoritative() is True


# --- Rule 5: browser-analysis-multipv (successor) dominates browser-game -----
def test_browser_analysis_multipv_replaces_browser_game():
    values = _agree(_V2_CORE)
    existing = _browser(_V2_CORE, RESOLVER_COMPLETE, values=values)
    incoming = _browser_analysis_multipv(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    # TIER_BASELINE edge -> dominates_replace (not the corrective reason).
    assert decision is Decision.REPLACE
    assert reason is Reason.DOMINATES_REPLACE


def test_browser_analysis_multipv_correctively_replaces_retired_analysis():
    # The corrective PROTOCOL_CORRECTION edge: the truthful visible-MultiPV
    # successor replaces a defective (but still identity-verified) retired
    # browser-analysis-v1 row for the exact key, independent of numeric depth.
    values = _agree(_V2_CORE)
    existing = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)
    incoming = _browser_analysis_multipv(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.REPLACE
    assert reason is Reason.PROTOCOL_CORRECTED_REPLACE


def test_retired_analysis_incoming_fails_closed():
    # A stale client that manages to submit a retired browser-analysis-v1 row is
    # kept out (inactive_profile_keep), even over a weaker browser-game row.
    values = _agree(_V2_CORE)
    existing = _browser(_V2_CORE, RESOLVER_COMPLETE, values=values)
    incoming = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.INACTIVE_PROFILE_KEEP


def test_retired_analysis_incoming_missing_key_fails_closed():
    # Even on a missing key, a retired-profile incoming is never inserted.
    incoming = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2)
    decision, reason = decide_analysis_cache_replacement(None, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.INACTIVE_PROFILE_KEEP


def test_non_eligible_incoming_stops_at_the_eligibility_gate():
    values = _agree(_V2_CORE)
    existing = _browser_analysis_multipv(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)
    incoming = _passive(_V2_CORE, RESOLVER_COMPLETE, values=values)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    # Stopped at the Rule 3 eligibility gate (existing is identified) — never
    # reaches Rule 5, so the reason is NON_AUTHORITATIVE_KEEP, not INCOMPATIBLE_KEEP.
    # The incoming profile must be NON-replacement-eligible for this path: the
    # eligible counterpart is test_browser_dynamic_policy.py's
    # test_v2_and_visible_multipv_are_incomparable_both_ways, where a
    # replacement-eligible browser-game-v2 clears this gate and is instead stopped
    # at Rule 5 with INCOMPATIBLE_KEEP.
    assert decision is Decision.KEEP
    assert reason is Reason.NON_AUTHORITATIVE_KEEP


def test_browser_analysis_multipv_cannot_replace_canonical():
    existing = _canonical(_V2_CORE, contract=RESOLVER_COMPLETE_V2)
    incoming = _browser_analysis_multipv(_V2_CORE, RESOLVER_COMPLETE_V2)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    # Canonical is authoritative; the comparator gives B_SUPERSEDES -> Rule 5
    # INCOMPATIBLE.
    assert decision is Decision.KEEP
    assert reason is Reason.INCOMPATIBLE_KEEP


def test_browser_analysis_multipv_cannot_reclaim_legacy():
    existing = _legacy({"played_eval"})
    incoming = _browser_analysis_multipv(_V2_CORE, RESOLVER_COMPLETE_V2)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    # Replacement-eligible but NOT authoritative -> Rule 4 true-authority gate.
    assert decision is Decision.KEEP
    assert reason is Reason.LEGACY_KEEP_NON_AUTH


def test_browser_analysis_multipv_cannot_reclaim_unidentified():
    existing = _row(profile=CANONICAL_PROFILE_ID, contract=RESOLVER_COMPLETE_V2, verified=False, fields=_V2_CORE)
    incoming = _browser_analysis_multipv(_V2_CORE, RESOLVER_COMPLETE_V2)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.LEGACY_KEEP_NON_AUTH


def test_canonical_v2_replaces_retired_browser_analysis():
    values = _agree(_V2_CORE)
    existing = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)
    incoming = _canonical(_V2_CORE, contract=RESOLVER_COMPLETE_V2, values=values)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    # AUTHORITY edge -> dominates_replace even over the retired row.
    assert decision is Decision.REPLACE
    assert reason is Reason.DOMINATES_REPLACE


def test_canonical_v2_replaces_browser_analysis_multipv():
    values = _agree(_V2_CORE)
    existing = _browser_analysis_multipv(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)
    incoming = _canonical(_V2_CORE, contract=RESOLVER_COMPLETE_V2, values=values)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.REPLACE
    assert reason is Reason.DOMINATES_REPLACE


# --- Rules 4b/5b: cross-grain authority replacement (g-6xc3) ----------------
#
# move-complete-v1 is deliberately NOT a superset/successor of resolver-complete-v2
# and populates none of the position fields, so BOTH halves of the completeness gate
# fail for every canonical move-grain write. The rule below is keyed on AUTHORITY
# instead, and every test here exists to pin one side of that asymmetry.

_MOVE_CORE = frozenset({"played_eval", "classification"})


def test_canonical_move_complete_replaces_browser_analysis_v2():
    """AC direction 1: authoritative move-grain evidence supersedes a
    NON-authoritative browser resolver-complete-v2 row for the same key.

    The position facts the write drops are not lost — the same canonical run wrote
    them to ``position_analysis`` — which is what licenses shedding them here.
    """
    existing = _browser_analysis_multipv(_V2_CORE, RESOLVER_COMPLETE_V2)
    incoming = _canonical(_MOVE_CORE, contract=MOVE_COMPLETE)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.REPLACE
    assert reason is Reason.CROSS_GRAIN_AUTHORITY_REPLACE


def test_non_authoritative_move_complete_cannot_replace_canonical_v2():
    """AC direction 2: the hatch is one-way. Canonical is authoritative, so the
    comparator gives B_SUPERSEDES and Rule 5 stops before the cross-grain rule."""
    existing = _canonical(_V2_CORE, contract=RESOLVER_COMPLETE_V2)
    incoming = _browser_analysis_multipv(_MOVE_CORE, contract=MOVE_COMPLETE)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.INCOMPATIBLE_KEEP


def test_non_authoritative_move_complete_refused_even_across_a_winning_edge():
    """The asymmetry is AUTHORITY, not merely winning the ordering.

    A PROTOCOL_CORRECTION edge makes the incoming visible-MultiPV row supersede the
    retired browser-analysis-v1 row, so Rule 5 reaches the completeness gate — and
    the cross-grain hatch still refuses it, because a non-authoritative producer
    never gets to shed a stored row's position evidence. (No browser producer can
    stamp move-complete-v1 today — ``select_browser_contract`` does not offer it —
    so this is the fail-closed pin for one that could.)
    """
    existing = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2)
    incoming = _browser_analysis_multipv(_MOVE_CORE, contract=MOVE_COMPLETE)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.INCOMING_LESS_COMPLETE_KEEP


def test_cross_grain_rule_does_not_fire_between_two_authoritative_rows():
    # Both canonical manifests are authoritative, so the asymmetry does not exist
    # and the two platform builds keep their historical incompatible_keep.
    existing = _row(
        profile=CANONICAL_LINUX_PROFILE_ID,
        contract=RESOLVER_COMPLETE_V2,
        verified=True,
        fields=_V2_CORE,
    )
    incoming = _canonical(_MOVE_CORE, contract=MOVE_COMPLETE)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.INCOMPATIBLE_KEEP


def test_canonical_move_complete_may_not_shed_retained_move_evidence():
    # Authority licenses handing off the POSITION grain, never a thinner row in the
    # grain analysis_cache owns: the stored row has a played_eval this mate-only
    # canonical row does not.
    existing = _browser_analysis_multipv(_V2_CORE, RESOLVER_COMPLETE_V2)
    incoming = _canonical(
        {"played_eval_mate", "classification"}, contract=MOVE_COMPLETE
    )
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.INCOMING_LESS_COMPLETE_KEEP


def test_canonical_minimal_played_eval_is_not_a_grain_split_write():
    # minimal-played-eval-v1 is move-grain and equally canonical, but it is narrow
    # because nobody produced the rest — no position row was written anywhere — so
    # it must not inherit the relocation licence.
    existing = _browser_analysis_multipv(_V2_CORE, RESOLVER_COMPLETE_V2)
    incoming = _canonical({"played_eval"}, contract=MINIMAL_PLAYED_EVAL)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.INCOMING_LESS_COMPLETE_KEEP


def test_canonical_move_complete_replaces_browser_game_v1_row():
    # resolver-complete-v1 also spans both grains, so the rule is about GRAIN, not
    # about the v2 contract specifically.
    existing = _browser(_V2_CORE, RESOLVER_COMPLETE)
    incoming = _canonical(_MOVE_CORE, contract=MOVE_COMPLETE)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.REPLACE
    assert reason is Reason.CROSS_GRAIN_AUTHORITY_REPLACE


def test_canonical_move_complete_reclaims_unidentified_v2_row():
    # Rule 4b: an unverified canonical-id row is effectively legacy, and the same
    # cross-grain licence applies at that completeness veto.
    existing = _row(
        profile=CANONICAL_PROFILE_ID,
        contract=RESOLVER_COMPLETE_V2,
        verified=False,
        fields=_V2_CORE,
    )
    incoming = _canonical(_MOVE_CORE, contract=MOVE_COMPLETE)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.REPLACE
    assert reason is Reason.CROSS_GRAIN_AUTHORITY_REPLACE


def test_canonical_move_complete_does_not_reclaim_an_uncontracted_legacy_row():
    # A profile-less row declares no contract, so its grain is UNKNOWN and the rule
    # fails closed rather than guessing that its evidence relocated.
    existing = _legacy(_V2_CORE)
    incoming = _canonical(_MOVE_CORE, contract=MOVE_COMPLETE)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.INCOMING_LESS_COMPLETE_KEEP


# --- Rule 2b: same-profile canonical v2 -> move-grain transition -----------


def test_same_profile_canonical_v2_transitions_to_move_complete():
    """A revisited canonical v2 key can converge after its position row is durable.

    Relocated position fields, the cross-grain delta snapshot, and optional mate
    annotations are intentionally outside the retained move-fact agreement gate.
    """
    existing_values = _agree(
        _V2_WITH_MATE,
        {
            "played_eval": 10,
            "classification": "good",
            "eval_delta": 20,
        },
    )
    incoming_fields = _MOVE_CORE | {"eval_delta"}
    incoming_values = _agree(
        incoming_fields,
        {
            "played_eval": 10,
            "classification": "good",
            "eval_delta": 999,
        },
    )
    existing = _canonical(
        _V2_WITH_MATE,
        contract=RESOLVER_COMPLETE_V2,
        values=existing_values,
    )
    incoming = _canonical(
        incoming_fields,
        contract=MOVE_COMPLETE,
        values=incoming_values,
    )

    decision, reason = decide_analysis_cache_replacement(existing, incoming)

    assert decision is Decision.REPLACE
    assert reason is Reason.SAME_PROFILE_GRAIN_TRANSITION_REPLACE


def test_same_profile_move_complete_never_transitions_back_to_v2():
    values = _agree(_V2_CORE)
    existing = _canonical(_MOVE_CORE, contract=MOVE_COMPLETE, values=values)
    incoming = _canonical(
        _V2_CORE,
        contract=RESOLVER_COMPLETE_V2,
        values=values,
    )

    decision, reason = decide_analysis_cache_replacement(existing, incoming)

    assert decision is Decision.KEEP
    assert reason is Reason.SAME_PROFILE_IDEMPOTENT


def test_same_profile_non_authoritative_v2_cannot_transition_grains():
    values = _agree(_V2_CORE)
    existing = _browser_analysis_multipv(
        _V2_CORE,
        contract=RESOLVER_COMPLETE_V2,
        values=values,
    )
    incoming = _browser_analysis_multipv(
        _MOVE_CORE,
        contract=MOVE_COMPLETE,
        values=values,
    )

    decision, reason = decide_analysis_cache_replacement(existing, incoming)

    assert decision is Decision.KEEP
    assert reason is Reason.SAME_PROFILE_IDEMPOTENT


@pytest.mark.parametrize(
    ("existing_contract", "incoming_contract"),
    [
        (RESOLVER_COMPLETE, MOVE_COMPLETE),
        (MINIMAL_PLAYED_EVAL, MOVE_COMPLETE),
        (None, MOVE_COMPLETE),
        (RESOLVER_COMPLETE_V2, MINIMAL_PLAYED_EVAL),
    ],
)
def test_same_profile_grain_transition_requires_exact_contract_pair(
    existing_contract, incoming_contract
):
    existing_fields = (
        _V2_CORE
        if existing_contract in {RESOLVER_COMPLETE, RESOLVER_COMPLETE_V2}
        else {"played_eval"}
    )
    incoming_fields = (
        _MOVE_CORE if incoming_contract == MOVE_COMPLETE else {"played_eval"}
    )
    values = _agree(_V2_CORE)
    existing = _canonical(
        existing_fields,
        contract=existing_contract,
        values=values,
    )
    incoming = _canonical(
        incoming_fields,
        contract=incoming_contract,
        values=values,
    )

    decision, reason = decide_analysis_cache_replacement(existing, incoming)

    assert decision is Decision.KEEP
    assert reason is Reason.SAME_PROFILE_IDEMPOTENT


def test_same_profile_grain_transition_refuses_retained_move_field_loss():
    values = _agree(_V2_WITH_MATE)
    existing = _canonical(
        _V2_WITH_MATE,
        contract=RESOLVER_COMPLETE_V2,
        values=values,
    )
    incoming = _canonical(
        {"played_eval_mate", "classification"},
        contract=MOVE_COMPLETE,
        values=values,
    )

    decision, reason = decide_analysis_cache_replacement(existing, incoming)

    assert decision is Decision.KEEP
    assert reason is Reason.SAME_PROFILE_IDEMPOTENT


@pytest.mark.parametrize("position_field", sorted(POSITION_GRAIN_FIELDS))
def test_same_profile_grain_transition_refuses_incoming_position_grain_fields(
    position_field,
):
    """A move contract label cannot smuggle relocated columns through REPLACE."""
    existing_values = _agree(_V2_WITH_MATE)
    incoming_fields = _MOVE_CORE | {position_field}
    incoming_values = _agree(incoming_fields)
    existing = _canonical(
        _V2_WITH_MATE,
        contract=RESOLVER_COMPLETE_V2,
        values=existing_values,
    )
    incoming = _canonical(
        incoming_fields,
        contract=MOVE_COMPLETE,
        values=incoming_values,
    )

    decision, reason = decide_analysis_cache_replacement(existing, incoming)

    assert decision is Decision.KEEP
    assert reason is Reason.SAME_PROFILE_IDEMPOTENT


@pytest.mark.parametrize(
    ("field", "incoming_value"),
    [("played_eval", 11), ("classification", "excellent")],
)
def test_same_profile_grain_transition_refuses_retained_move_disagreement(
    field, incoming_value
):
    existing_values = _agree(
        _V2_CORE,
        {"played_eval": 10, "classification": "good"},
    )
    incoming_values = _agree(
        _MOVE_CORE,
        {"played_eval": 10, "classification": "good", field: incoming_value},
    )
    existing = _canonical(
        _V2_CORE,
        contract=RESOLVER_COMPLETE_V2,
        values=existing_values,
    )
    incoming = _canonical(
        _MOVE_CORE,
        contract=MOVE_COMPLETE,
        values=incoming_values,
    )

    decision, reason = decide_analysis_cache_replacement(existing, incoming)

    assert decision is Decision.KEEP
    assert reason is Reason.SAME_PROFILE_IDEMPOTENT


def test_same_profile_grain_transition_can_heal_invalid_combined_v2_row():
    values = _agree(_V2_CORE)
    existing = dataclasses.replace(
        _canonical(
            _V2_CORE,
            contract=RESOLVER_COMPLETE_V2,
            values=values,
        ),
        contract_satisfied=False,
    )
    incoming = _canonical(
        _MOVE_CORE,
        contract=MOVE_COMPLETE,
        values=values,
    )

    decision, reason = decide_analysis_cache_replacement(existing, incoming)

    assert decision is Decision.REPLACE
    assert reason is Reason.SAME_PROFILE_GRAIN_TRANSITION_REPLACE


def test_grain_field_sets_partition_the_evidence_fields():
    # The rule's completeness comparison is only honest if every evidence field is
    # claimed exactly once. eval_delta is deliberately in NEITHER grain: it is
    # derived from both halves and recomposed at read time.
    assert POSITION_GRAIN_FIELDS | MOVE_GRAIN_FIELDS | CROSS_GRAIN_DERIVED_FIELDS == set(
        EVIDENCE_FIELDS
    )
    assert not POSITION_GRAIN_FIELDS & MOVE_GRAIN_FIELDS
    assert not (POSITION_GRAIN_FIELDS | MOVE_GRAIN_FIELDS) & CROSS_GRAIN_DERIVED_FIELDS
    assert CROSS_GRAIN_DERIVED_FIELDS == {"eval_delta"}


def test_move_grain_is_never_relocatable():
    # analysis_cache IS the move-grain table: nothing else stores those facts, so a
    # narrower row that sheds them is evidence loss however authoritative its writer.
    assert Grain.MOVE not in RELOCATABLE_GRAINS
    assert RELOCATABLE_GRAINS == {Grain.POSITION}


# --- Rule 2: same browser-analysis-multipv profile merges -------------------
def test_browser_analysis_multipv_same_profile_idempotent():
    values = _agree(_V2_CORE)
    existing = _browser_analysis_multipv(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)
    incoming = _browser_analysis_multipv(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.SAME_PROFILE_IDEMPOTENT


def test_browser_analysis_multipv_same_profile_superset_merge():
    values = _agree(_V2_WITH_MATE)
    existing = _browser_analysis_multipv(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)
    incoming = _browser_analysis_multipv(_V2_WITH_MATE, RESOLVER_COMPLETE_V2, values=values)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    # A mate field the incoming row adds counts as a contributed field for merge.
    assert decision is Decision.MERGE
    assert reason is Reason.SAME_PROFILE_SUPERSET_MERGE


def test_browser_analysis_multipv_same_profile_merge_conflict():
    existing = _browser_analysis_multipv(_V2_CORE, RESOLVER_COMPLETE_V2, values=_agree(_V2_CORE, {"played_eval": 10}))
    incoming = _browser_analysis_multipv(_V2_CORE, RESOLVER_COMPLETE_V2, values=_agree(_V2_CORE, {"played_eval": 20}))
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.MERGE_CONFLICT_KEEP


# --- Mate-field dominance exclusion (Rule 5) --------------------------------
def test_cp_only_browser_analysis_multipv_replaces_browser_game_with_mate():
    values = _agree(_V2_WITH_MATE)
    existing = _browser(_V2_WITH_MATE, RESOLVER_COMPLETE, values=values)  # has mate counts
    incoming = _browser_analysis_multipv(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)  # CP only
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    # Without the mate strip this would be INCOMING_LESS_COMPLETE_KEEP.
    assert decision is Decision.REPLACE
    assert reason is Reason.DOMINATES_REPLACE


def test_browser_analysis_multipv_with_mate_replaces_browser_game_cp_only():
    values = _agree(_V2_WITH_MATE)
    existing = _browser(_V2_CORE, RESOLVER_COMPLETE, values=values)  # CP only
    incoming = _browser_analysis_multipv(_V2_WITH_MATE, RESOLVER_COMPLETE_V2, values=values)  # has mate
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.REPLACE
    assert reason is Reason.DOMINATES_REPLACE


def test_cp_only_canonical_replaces_browser_analysis_multipv_with_mate():
    values = _agree(_V2_WITH_MATE)
    existing = _browser_analysis_multipv(_V2_WITH_MATE, RESOLVER_COMPLETE_V2, values=values)
    incoming = _canonical(_V2_CORE, contract=RESOLVER_COMPLETE_V2, values=values)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.REPLACE
    assert reason is Reason.DOMINATES_REPLACE


def test_cp_only_canonical_replaces_browser_game_with_mate_global_strip():
    """The Rule 5 mate strip is global, not special-cased to browser-analysis."""
    values = _agree(_V2_WITH_MATE)
    existing = _browser(_V2_WITH_MATE, RESOLVER_COMPLETE, values=values)
    incoming = _canonical(_V2_CORE, contract=RESOLVER_COMPLETE_V2, values=values)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.REPLACE
    assert reason is Reason.DOMINATES_REPLACE


def test_dominant_incoming_missing_non_mate_field_still_kept():
    """Exclusion is scoped to mate fields: a missing NON-mate field still blocks.

    ``best_move_san`` is the projector-reachable veto shape: no contract validates
    it, so a row can drop it and still project as contract-satisfied. (``best_line_uci``
    cannot be dropped — both resolver validators require a multi-move PV, so a
    line-less row is never contract-satisfied and never reaches Rule 5.)
    """
    fields = _V2_CORE - {"best_move_san"}
    existing = _browser(_V2_CORE, RESOLVER_COMPLETE, values=_agree(_V2_CORE))
    incoming = _browser_analysis_multipv(fields, RESOLVER_COMPLETE_V2, values=_agree(fields))
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.INCOMING_LESS_COMPLETE_KEEP
