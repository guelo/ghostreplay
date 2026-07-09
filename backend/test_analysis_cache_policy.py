"""Unit tests for the pure analysis_cache replacement comparator."""

import dataclasses

from app.analysis_cache_policy import (
    CacheRow,
    Decision,
    Reason,
    decide_analysis_cache_replacement,
)
from app.analysis_profiles import (
    BROWSER_ANALYSIS_PROFILE_ID,
    BROWSER_PROFILE_ID,
    CANONICAL_PROFILE_ID,
    JEFFML_PROFILE_ID,
)
from app.evidence_contracts import (
    MINIMAL_BEST_EVAL,
    MINIMAL_PLAYED_EVAL,
    MOVE_COMPLETE,
    RESOLVER_COMPLETE,
    RESOLVER_COMPLETE_V2,
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


def _legacy(fields, values=None):
    return _row(profile=None, contract=None, verified=False, fields=fields, values=values)


# Rule 1 — missing key
def test_insert_on_missing_key():
    decision, reason = decide_analysis_cache_replacement(None, _browser({"played_eval"}, MINIMAL_PLAYED_EVAL))
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


# Rule 3 — browser cannot replace canonical
def test_browser_cannot_replace_canonical():
    existing = _canonical({"best_move_uci", "best_line_uci", "classification", "eval_delta"})
    incoming = _browser({"best_move_uci", "best_line_uci", "classification", "eval_delta"})
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.NON_AUTHORITATIVE_KEEP


# Rule 4 — browser cannot replace legacy
def test_browser_cannot_replace_legacy():
    existing = _legacy({"played_eval", "best_eval"})
    incoming = _browser({"played_eval"}, MINIMAL_PLAYED_EVAL)
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


# Rule 2 — same profile idempotent for browser
def test_same_browser_profile_idempotent():
    existing = _browser({"played_eval"}, MINIMAL_PLAYED_EVAL)
    incoming = _browser({"played_eval"}, MINIMAL_PLAYED_EVAL)
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
    # The endpoint stamps the full pinned identity, so its rows identity-verify.
    return _row(
        profile=BROWSER_ANALYSIS_PROFILE_ID,
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
def test_browser_analysis_is_replacement_eligible_not_authoritative():
    row = _browser_analysis(_V2_CORE)
    assert row.is_replacement_eligible() is True
    assert row.is_effectively_authoritative() is False


def test_browser_game_is_not_replacement_eligible():
    row = _browser(_V2_CORE, RESOLVER_COMPLETE)
    assert row.is_replacement_eligible() is False


def test_canonical_is_replacement_eligible_and_authoritative():
    row = _canonical(_V2_CORE, contract=RESOLVER_COMPLETE_V2)
    assert row.is_replacement_eligible() is True
    assert row.is_effectively_authoritative() is True


# --- Rule 5: browser-analysis dominates browser-game ------------------------
def test_browser_analysis_replaces_browser_game():
    values = _agree(_V2_CORE)
    existing = _browser(_V2_CORE, RESOLVER_COMPLETE, values=values)
    incoming = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.REPLACE
    assert reason is Reason.DOMINATES_REPLACE


def test_browser_game_cannot_replace_browser_analysis():
    values = _agree(_V2_CORE)
    existing = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)
    incoming = _browser(_V2_CORE, RESOLVER_COMPLETE, values=values)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    # Stopped at the Rule 3 eligibility gate (existing is identified) — never
    # reaches Rule 5, so the reason is NON_AUTHORITATIVE_KEEP, not INCOMPATIBLE_KEEP.
    assert decision is Decision.KEEP
    assert reason is Reason.NON_AUTHORITATIVE_KEEP


def test_browser_analysis_cannot_replace_canonical():
    existing = _canonical(_V2_CORE, contract=RESOLVER_COMPLETE_V2)
    incoming = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    # Canonical is not in browser-analysis's dominates set -> Rule 5 INCOMPATIBLE.
    assert decision is Decision.KEEP
    assert reason is Reason.INCOMPATIBLE_KEEP


def test_browser_analysis_cannot_reclaim_legacy():
    existing = _legacy({"played_eval"})
    incoming = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    # Replacement-eligible but NOT authoritative -> Rule 4 true-authority gate.
    assert decision is Decision.KEEP
    assert reason is Reason.LEGACY_KEEP_NON_AUTH


def test_browser_analysis_cannot_reclaim_unidentified():
    existing = _row(profile=CANONICAL_PROFILE_ID, contract=RESOLVER_COMPLETE_V2, verified=False, fields=_V2_CORE)
    incoming = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.LEGACY_KEEP_NON_AUTH


def test_canonical_v2_replaces_browser_analysis():
    values = _agree(_V2_CORE)
    existing = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)
    incoming = _canonical(_V2_CORE, contract=RESOLVER_COMPLETE_V2, values=values)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.REPLACE
    assert reason is Reason.DOMINATES_REPLACE


def test_future_canonical_move_complete_would_not_replace_browser_analysis_v2():
    """g-6xc3 gap: a canonical move-complete-v1 row does NOT supersede
    resolver-complete-v2, so the current contract check keeps the browser-analysis
    v2 row. Captured so the cross-grain gap is a deliberate, tested behavior."""
    existing = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2)
    incoming = _canonical({"played_eval", "classification"}, contract=MOVE_COMPLETE)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    # Canonical dominates browser-analysis, but move-complete-v1 is not a superset
    # of resolver-complete-v2 -> contract_ok False -> kept.
    assert reason is Reason.INCOMING_LESS_COMPLETE_KEEP


# --- Rule 2: same browser-analysis profile merges ---------------------------
def test_browser_analysis_same_profile_idempotent():
    values = _agree(_V2_CORE)
    existing = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)
    incoming = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.SAME_PROFILE_IDEMPOTENT


def test_browser_analysis_same_profile_superset_merge():
    values = _agree(_V2_WITH_MATE)
    existing = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)
    incoming = _browser_analysis(_V2_WITH_MATE, RESOLVER_COMPLETE_V2, values=values)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    # A mate field the incoming row adds counts as a contributed field for merge.
    assert decision is Decision.MERGE
    assert reason is Reason.SAME_PROFILE_SUPERSET_MERGE


def test_browser_analysis_same_profile_merge_conflict():
    existing = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2, values=_agree(_V2_CORE, {"played_eval": 10}))
    incoming = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2, values=_agree(_V2_CORE, {"played_eval": 20}))
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.MERGE_CONFLICT_KEEP


# --- Mate-field dominance exclusion (Rule 5) --------------------------------
def test_cp_only_browser_analysis_replaces_browser_game_with_mate():
    values = _agree(_V2_WITH_MATE)
    existing = _browser(_V2_WITH_MATE, RESOLVER_COMPLETE, values=values)  # has mate counts
    incoming = _browser_analysis(_V2_CORE, RESOLVER_COMPLETE_V2, values=values)  # CP only
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    # Without the mate strip this would be INCOMING_LESS_COMPLETE_KEEP.
    assert decision is Decision.REPLACE
    assert reason is Reason.DOMINATES_REPLACE


def test_browser_analysis_with_mate_replaces_browser_game_cp_only():
    values = _agree(_V2_WITH_MATE)
    existing = _browser(_V2_CORE, RESOLVER_COMPLETE, values=values)  # CP only
    incoming = _browser_analysis(_V2_WITH_MATE, RESOLVER_COMPLETE_V2, values=values)  # has mate
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.REPLACE
    assert reason is Reason.DOMINATES_REPLACE


def test_cp_only_canonical_replaces_browser_analysis_with_mate():
    values = _agree(_V2_WITH_MATE)
    existing = _browser_analysis(_V2_WITH_MATE, RESOLVER_COMPLETE_V2, values=values)
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
    """Exclusion is scoped to mate fields: a missing NON-mate field still blocks."""
    values = _agree(_V2_CORE)
    existing = _browser(_V2_CORE, RESOLVER_COMPLETE, values=values)
    incoming = _browser_analysis(_V2_CORE - {"best_line_uci"}, RESOLVER_COMPLETE_V2, values=values)
    decision, reason = decide_analysis_cache_replacement(existing, incoming)
    assert decision is Decision.KEEP
    assert reason is Reason.INCOMING_LESS_COMPLETE_KEEP
