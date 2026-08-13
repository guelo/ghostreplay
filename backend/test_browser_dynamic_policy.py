"""Dynamic browser-evidence policy: identity, strength, replacement (g-mk1d).

Covers the declared-dynamic half of the shared evidence policy that
``browser-game-v2`` introduces — per-field identity validation, the untrusted-wire
provenance validator, the row-level measured-strength comparator, and the Rule 2a
same-profile replacement outcomes it drives.
"""

import json

import pytest

from app.analysis_cache_policy import (
    CacheRow,
    Decision,
    Reason,
    browser_live_descriptor,
    decide_analysis_cache_replacement,
    display_upgrade_eligible,
    display_upgrade_eligible_vs,
    project_cache_row,
)
from app.analysis_cache_repo import _dedupe_batch
from app.analysis_profiles import (
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
    BROWSER_ANALYSIS_PROFILE_ID,
    BROWSER_GAME_V2_DYNAMIC_FIELDS,
    BROWSER_GAME_V2_PROFILE_ID,
    BROWSER_PROFILE_ID,
    CANONICAL_PROFILE_ID,
    IDENTITY_FIELDS,
    StrengthComparison,
    get_profile,
    stamp_dynamic_profile,
    stamp_profile_full,
)
from app.evidence_contracts import RESOLVER_COMPLETE
from app.evidence_policy import (
    EdgeKind,
    Supersession,
    compare_evidence_rows,
    compare_row_strength,
    validate_browser_provenance,
    verify_identity,
)

# The identity the real client reports: the bundled lite-single artifact.
BUILD = "a8fbc05ec6920b56d7485826dcb02c5ffd2826bcbf751cf973046f237a9096f1"
NET = (
    "nn-9067e33176e8.nnue:"
    "9067e33176e8c5edb7aa8db6a3aedd012f84a1f39872e86357c6c2d0993f314d"
)


def provenance(depth=17, **overrides):
    """A valid client provenance payload at ``depth``."""
    return {
        "engine_version": "18",
        "engine_build": BUILD,
        "eval_file_id": NET,
        "search_limit_type": "depth",
        "search_limit_value": depth,
        "threads": 1,
        "hash_mb": 128,
        **overrides,
    }


EVIDENCE = {
    "best_move_uci": "d2d4",
    "best_move_san": "d4",
    "best_line_uci": "d2d4 g8f6",
    "played_eval": 10,
    "best_eval": 30,
    "eval_delta": 20,
    "classification": "good",
}


def v2_row_data(depth=17, **overrides):
    """A full stored-row dict for a browser-game-v2 row searched to ``depth``."""
    data = {
        "fen_before": "f",
        "move_uci": "e2e4",
        "move_san": "e4",
        "source": "game",
        "analysis_profile_id": BROWSER_GAME_V2_PROFILE_ID,
        "evidence_contract_id": RESOLVER_COMPLETE,
        **EVIDENCE,
        **stamp_dynamic_profile(BROWSER_GAME_V2_PROFILE_ID, provenance(depth)),
    }
    data.update(overrides)
    return data


def v2_row(depth=17, **overrides):
    return project_cache_row(v2_row_data(depth, **overrides))


# --- identity ------------------------------------------------------------------


def test_valid_dynamic_row_identity_verifies():
    assert verify_identity(v2_row_data(20)) is True


def test_every_declared_dynamic_field_is_required_non_null():
    for field in BROWSER_GAME_V2_DYNAMIC_FIELDS:
        data = v2_row_data(17)
        data[field] = None
        assert verify_identity(data) is False, field


def test_fixed_half_cannot_be_forged():
    # The server stamps engine_name / multipv / protocol / digest; a row claiming a
    # different fixed value fails identity outright, dynamic values notwithstanding.
    for field, bogus in (
        ("engine_name", "Leela"),
        ("multipv", 3),
        ("analyzer_protocol_version", "browser-visible-multipv-v1"),
        ("profile_manifest_digest", "0" * 64),
    ):
        data = v2_row_data(17)
        data[field] = bogus
        assert verify_identity(data) is False, field


def test_legacy_all_none_browser_game_v1_still_verifies():
    # Parity: g-mk1d must not disturb the rows already in the table.
    assert verify_identity(
        {"analysis_profile_id": BROWSER_PROFILE_ID, **{f: None for f in IDENTITY_FIELDS}}
    ) is True


def test_fixed_profiles_are_unchanged_by_the_dynamic_branch():
    for pid in (
        CANONICAL_PROFILE_ID,
        BROWSER_ANALYSIS_PROFILE_ID,
        BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
    ):
        assert get_profile(pid).dynamic_fields == frozenset()
        assert verify_identity(
            {"analysis_profile_id": pid, **stamp_profile_full(pid)}
        ) is True


# --- the untrusted wire validator ----------------------------------------------


@pytest.mark.parametrize("raw", [[], "depth", 17, 1.5, True, ()])
def test_non_mapping_provenance_is_the_none_sentinel(raw):
    # Pydantic used to do this shape check implicitly; the wire field is now `Any`
    # precisely so a non-object cannot 422 the whole batch, which makes this
    # explicit check load-bearing.
    assert validate_browser_provenance(raw) is None


def test_valid_provenance_returns_the_dynamic_subset():
    fields = validate_browser_provenance(provenance(19))
    assert fields is not None
    assert fields.values == provenance(19)


@pytest.mark.parametrize(
    "overrides",
    [
        {"search_limit_type": "clock"},
        {"search_limit_value": 0},
        {"search_limit_value": 61},  # above the depth ceiling
        {"engine_build": "abc"},
        {"engine_build": BUILD.upper()},  # hex must be lowercase
        {"eval_file_id": "nn-9067e33176e8.nnue"},  # no hash half
        {"eval_file_id": "a:" + "z" * 64},
        {"engine_version": ""},
        {"engine_version": "x" * 65},
        {"threads": 0},
        {"threads": 33},
        {"hash_mb": 2049},
    ],
)
def test_malformed_fields_are_rejected(overrides):
    assert validate_browser_provenance(provenance(17, **overrides)) is None


def test_missing_field_is_rejected():
    payload = provenance(17)
    del payload["threads"]
    assert validate_browser_provenance(payload) is None


@pytest.mark.parametrize("field", ["search_limit_value", "threads", "hash_mb"])
@pytest.mark.parametrize("value", [True, False])
def test_json_booleans_never_satisfy_an_integer_bound(field, value):
    # isinstance(True, int) is True in Python, so without an explicit bool guard a
    # JSON `true` would sail through as 1 — a depth-1 claim wearing a valid shape.
    assert validate_browser_provenance(provenance(17, **{field: value})) is None


@pytest.mark.parametrize("field", ["search_limit_value", "threads", "hash_mb"])
def test_fractional_floats_are_rejected_but_integral_ones_normalize(field):
    assert validate_browser_provenance(provenance(17, **{field: 1.5})) is None
    fields = validate_browser_provenance(provenance(17, **{field: 2.0}))
    assert fields is not None
    assert fields.values[field] == 2
    assert isinstance(fields.values[field], int)


def test_search_limit_bounds_are_per_type():
    # 100_000 plies is nonsense; 100_000 nodes and 100_000 ms are not.
    assert validate_browser_provenance(
        provenance(17, search_limit_type="depth", search_limit_value=100_000)
    ) is None
    for limit_type in ("nodes", "movetime"):
        assert validate_browser_provenance(
            provenance(17, search_limit_type=limit_type, search_limit_value=100_000)
        ) is not None


# --- measured strength ---------------------------------------------------------


def test_deeper_comparable_row_is_stronger():
    assert compare_row_strength(v2_row(20), v2_row(17)) is StrengthComparison.A_STRONGER
    assert compare_row_strength(v2_row(17), v2_row(20)) is StrengthComparison.B_STRONGER
    assert compare_row_strength(v2_row(17), v2_row(17)) is StrengthComparison.EQUAL


@pytest.mark.parametrize(
    "overrides",
    [
        {"eval_file_id": "nn-deadbeef1234.nnue:" + "a" * 64},  # different net
        {"multipv": 3},
        {"analyzer_protocol_version": "browser-visible-multipv-v1"},
        {"search_limit_type": "nodes"},
        {"engine_build": "b" * 64},  # unrelated self-reported build
    ],
)
def test_incompatible_scoring_semantics_are_incomparable(overrides):
    # A deeper search under DIFFERENT rules is not "stronger", it is unrankable.
    deep = project_cache_row(v2_row_data(20, **overrides))
    assert compare_row_strength(deep, v2_row(17)) is StrengthComparison.INCOMPARABLE


def test_all_none_legacy_row_is_incomparable_to_every_dynamic_row():
    legacy = project_cache_row(
        {
            "analysis_profile_id": BROWSER_PROFILE_ID,
            "evidence_contract_id": RESOLVER_COMPLETE,
            **EVIDENCE,
            **{f: None for f in IDENTITY_FIELDS},
        }
    )
    # Unknown strength is NOT weakness: a depth-20 v2 row may not reclaim a legacy
    # d17 row by depth, in either direction.
    assert compare_row_strength(v2_row(20), legacy) is StrengthComparison.INCOMPARABLE
    assert compare_row_strength(legacy, v2_row(20)) is StrengthComparison.INCOMPARABLE


def test_two_legacy_all_none_rows_are_also_incomparable():
    legacy = project_cache_row(
        {
            "analysis_profile_id": BROWSER_PROFILE_ID,
            "evidence_contract_id": RESOLVER_COMPLETE,
            **EVIDENCE,
            **{f: None for f in IDENTITY_FIELDS},
        }
    )
    # They agree field-for-field, but on NOTHING: both describe an unknown net.
    assert compare_row_strength(legacy, legacy) is StrengthComparison.INCOMPARABLE


def test_higher_engine_version_outranks_deeper_search():
    newer = project_cache_row(v2_row_data(17, engine_version="19"))
    assert compare_row_strength(newer, v2_row(20)) is StrengthComparison.A_STRONGER


# --- the two comparators agree (one comparison, two grains) --------------------


@pytest.mark.parametrize(
    "a_depth,b_depth,expected",
    [
        (20, 17, Supersession.A_STRONGER),
        (17, 20, Supersession.B_STRONGER),
        (17, 17, Supersession.EQUAL),
    ],
)
def test_shared_comparator_ranks_unequal_non_edged_rows_by_measured_strength(
    a_depth, b_depth, expected
):
    # Steps 4-5 must be REACHABLE through the shared comparator, not only through
    # the helper. When they were stubbed to INCOMPARABLE, this exact pair answered
    # "incomparable" here and "a_stronger" from compare_row_strength — one pair of
    # rows with two different orderings depending on which caller asked.
    comparison = compare_evidence_rows(v2_row(a_depth), v2_row(b_depth))
    assert comparison.outcome is expected
    # Measured, not edged: no registered EDGE justifies this, so callers keying off
    # `kind is EdgeKind.PROTOCOL_CORRECTION` correctly fall through.
    assert comparison.kind is None


@pytest.mark.parametrize("depths", [(20, 17), (17, 20), (17, 17)])
def test_the_two_comparators_report_the_same_measured_grain(depths):
    # The shared comparator reports measured results at the MEASURED grain, so the
    # two functions agree name-for-name rather than one flattening into the other.
    # Flattening A_STRONGER into A_SUPERSEDES would make a measured win
    # indistinguishable from an authority/edge win at the call site.
    a, b = (v2_row(depths[0]), v2_row(depths[1]))
    assert compare_evidence_rows(a, b).outcome.value == compare_row_strength(a, b).value


@pytest.mark.parametrize(
    "a,b",
    [
        ("multipv", "semantics"),
        ("legacy", "unknown"),
    ],
)
def test_shared_comparator_refuses_what_the_helper_refuses(a, b):
    # Whatever the helper calls unrankable, the shared comparator must also refuse
    # — a strength guard that only one of the two enforces is not a guard.
    if a == "multipv":
        left = project_cache_row(v2_row_data(20, multipv=3))
        right = v2_row(17)
    else:
        left = v2_row(20)
        right = project_cache_row(
            {
                "analysis_profile_id": BROWSER_PROFILE_ID,
                "evidence_contract_id": RESOLVER_COMPLETE,
                **EVIDENCE,
                **{f: None for f in IDENTITY_FIELDS},
            }
        )
    assert compare_row_strength(left, right) is StrengthComparison.INCOMPARABLE
    assert compare_evidence_rows(left, right).outcome is Supersession.INCOMPARABLE


def test_authority_still_outranks_a_stronger_measured_search():
    # Ordering of the steps matters: measured strength runs AFTER the authority
    # barrier, so a deep browser row never outranks canonical by depth.
    canonical = project_cache_row(
        {
            "analysis_profile_id": CANONICAL_PROFILE_ID,
            "evidence_contract_id": RESOLVER_COMPLETE,
            **EVIDENCE,
            **stamp_profile_full(CANONICAL_PROFILE_ID),
        }
    )
    comparison = compare_evidence_rows(v2_row(30), canonical)
    assert comparison.outcome is Supersession.B_SUPERSEDES
    assert comparison.kind is EdgeKind.AUTHORITY


# --- Rule 2a: same-profile replacement -----------------------------------------


def test_stronger_same_key_v2_row_replaces():
    assert decide_analysis_cache_replacement(v2_row(17), v2_row(20)) == (
        Decision.REPLACE,
        Reason.STRENGTH_REPLACE,
    )


def test_weaker_same_key_v2_row_is_kept_out():
    assert decide_analysis_cache_replacement(v2_row(20), v2_row(17)) == (
        Decision.KEEP,
        Reason.STRENGTH_WEAKER_KEEP,
    )


def test_incomparable_same_key_v2_rows_keep_the_stored_row():
    incoming = project_cache_row(v2_row_data(20, engine_build="c" * 64))
    assert decide_analysis_cache_replacement(v2_row(17), incoming) == (
        Decision.KEEP,
        Reason.STRENGTH_INCOMPARABLE_KEEP,
    )


def test_identical_provenance_keeps_the_historical_idempotent_path():
    assert decide_analysis_cache_replacement(v2_row(17), v2_row(17)) == (
        Decision.KEEP,
        Reason.SAME_PROFILE_IDEMPOTENT,
    )


def test_equal_strength_but_different_provenance_is_idempotent_never_merged():
    # Same depth, different device resources: neither is better evidence, and a
    # merged row would carry ONE provenance tuple while mixing two devices' numbers.
    other = project_cache_row(v2_row_data(17, hash_mb=256))
    decision, reason = decide_analysis_cache_replacement(v2_row(17), other)
    assert decision is Decision.KEEP
    assert reason is Reason.SAME_PROFILE_IDEMPOTENT


def test_stronger_but_less_complete_row_does_not_drop_evidence():
    stored = v2_row(17)
    sparse_data = v2_row_data(20)
    sparse_data["best_move_san"] = None
    assert decide_analysis_cache_replacement(
        stored, project_cache_row(sparse_data)
    ) == (Decision.KEEP, Reason.INCOMING_LESS_COMPLETE_KEEP)


# --- cross-profile ordering ----------------------------------------------------


def test_v2_does_not_reclaim_a_legacy_browser_game_v1_row():
    legacy = project_cache_row(
        {
            "analysis_profile_id": BROWSER_PROFILE_ID,
            "evidence_contract_id": RESOLVER_COMPLETE,
            **EVIDENCE,
            **{f: None for f in IDENTITY_FIELDS},
        }
    )
    assert decide_analysis_cache_replacement(legacy, v2_row(20)) == (
        Decision.KEEP,
        Reason.INCOMPATIBLE_KEEP,
    )


def _visible_multipv_row(**overrides):
    data = {
        "analysis_profile_id": BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
        "evidence_contract_id": RESOLVER_COMPLETE,
        **EVIDENCE,
        **stamp_profile_full(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID),
    }
    data.update(overrides)
    return project_cache_row(data)


def test_v2_and_visible_multipv_are_incomparable_without_a_session_witness():
    multipv = _visible_multipv_row()
    # MultiPV 3 vs 1 — different scoring semantics, so depth 21 vs 20 is not a
    # global ordering. In particular, the fixed profile has no categorical edge
    # over every row the declared-dynamic browser-game-v2 profile can represent.
    assert compare_row_strength(multipv, v2_row(20)) is StrengthComparison.INCOMPARABLE
    assert compare_evidence_rows(multipv, v2_row(20)).outcome is Supersession.INCOMPARABLE
    assert decide_analysis_cache_replacement(v2_row(20), multipv) == (
        Decision.KEEP,
        Reason.INCOMPATIBLE_KEEP,
    )
    assert decide_analysis_cache_replacement(multipv, v2_row(20)) == (
        Decision.KEEP,
        Reason.INCOMPATIBLE_KEEP,
    )


@pytest.mark.parametrize("depth", [17, 18, 19, 20])
def test_visible_d21_session_witness_replaces_matching_shallower_v2_rows(depth):
    existing = v2_row(depth)
    multipv = _visible_multipv_row()
    live = browser_live_descriptor(json.dumps(provenance(depth)))
    assert live is not None
    assert decide_analysis_cache_replacement(
        existing, multipv, visible_d21_live=live
    ) == (Decision.REPLACE, Reason.DOMINATES_REPLACE)


@pytest.mark.parametrize(
    "existing,raw_live",
    [
        # Equal depth: the visible result is not strictly deeper.
        (v2_row(21), provenance(21)),
        # The cache row belongs to a different search than this session's move.
        (v2_row(17), provenance(17, hash_mb=64)),
        # Even an exact witness cannot bridge a different engine network.
        (
            v2_row(17, eval_file_id="other.nnue:" + "1" * 64),
            provenance(17, eval_file_id="other.nnue:" + "1" * 64),
        ),
        # Alternate Hash is a different search configuration even when this
        # session exactly witnesses it; only shipped in-game Hash 128 is ordered
        # against the visible worker's fixed Hash 64.
        (v2_row(17, hash_mb=64), provenance(17, hash_mb=64)),
    ],
)
def test_visible_d21_session_witness_rejects_unproven_or_unrankable_v2_rows(
    existing, raw_live
):
    live = browser_live_descriptor(json.dumps(raw_live))
    assert live is not None
    assert decide_analysis_cache_replacement(
        existing, _visible_multipv_row(), visible_d21_live=live
    ) == (Decision.KEEP, Reason.INCOMPATIBLE_KEEP)


def test_visible_d21_session_witness_still_honors_completeness():
    live = browser_live_descriptor(json.dumps(provenance(17)))
    assert live is not None
    multipv = project_cache_row(
        {
            "analysis_profile_id": BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
            "evidence_contract_id": RESOLVER_COMPLETE,
            **{**EVIDENCE, "best_move_san": None},
            **stamp_profile_full(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID),
        }
    )
    assert decide_analysis_cache_replacement(
        v2_row(17), multipv, visible_d21_live=live
    ) == (
        Decision.KEEP,
        Reason.INCOMING_LESS_COMPLETE_KEEP,
    )


def hidden_d21_row():
    """A stored retired-`browser-analysis-v1` row: hidden protocol, MultiPV 1, d21.

    Shares engine, build, net, MultiPV and analyzer protocol with a `browser-game-v2`
    row — the two differ ONLY in search depth — and no EDGE connects them, so this is
    the pair that actually reaches D4 steps 4-5 through Rule 5.
    """
    return project_cache_row(
        {
            "analysis_profile_id": BROWSER_ANALYSIS_PROFILE_ID,
            "evidence_contract_id": RESOLVER_COMPLETE,
            **EVIDENCE,
            **stamp_profile_full(BROWSER_ANALYSIS_PROFILE_ID),
        }
    )


def test_a_measured_cross_profile_win_reports_strength_replace():
    # D9: a measured replacement reports strength_replace at BOTH grains — Rule 2a
    # (same profile) and Rule 5 (cross profile) — so "won on numbers" reads the same
    # either way and is never reported as an authority/edge win.
    #
    # A REAL pair, not a stub: a v2 row self-reporting depth 22 against the stored
    # hidden d21 row. The shipped client clamps to 17, but the dynamic provenance
    # contract is deliberately permissive about the value, so this is reachable.
    assert decide_analysis_cache_replacement(hidden_d21_row(), v2_row(22)) == (
        Decision.REPLACE,
        Reason.STRENGTH_REPLACE,
    )


@pytest.mark.parametrize(
    "depth,outcome",
    [
        (20, Supersession.B_STRONGER),
        (21, Supersession.EQUAL),
    ],
)
def test_rule_5_collapses_every_non_winning_measured_outcome(depth, outcome):
    # Rule 5's local decision genuinely treats these alike (keep the stored row), so
    # collapsing them HERE is the caller's choice — which is only possible because
    # the comparator hands back the distinct outcomes in the first place.
    stored = hidden_d21_row()
    incoming = v2_row(depth)
    assert compare_evidence_rows(incoming, stored).outcome is outcome
    assert decide_analysis_cache_replacement(stored, incoming) == (
        Decision.KEEP,
        Reason.INCOMPATIBLE_KEEP,
    )


def test_visible_multipv_still_correctively_replaces_browser_analysis_v1():
    stored = project_cache_row(
        {
            "analysis_profile_id": BROWSER_ANALYSIS_PROFILE_ID,
            "evidence_contract_id": RESOLVER_COMPLETE,
            **EVIDENCE,
            **stamp_profile_full(BROWSER_ANALYSIS_PROFILE_ID),
        }
    )
    incoming = project_cache_row(
        {
            "analysis_profile_id": BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
            "evidence_contract_id": RESOLVER_COMPLETE,
            **EVIDENCE,
            **stamp_profile_full(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID),
        }
    )
    assert decide_analysis_cache_replacement(stored, incoming) == (
        Decision.REPLACE,
        Reason.PROTOCOL_CORRECTED_REPLACE,
    )


def test_canonical_dominates_a_dynamic_browser_row():
    canonical = project_cache_row(
        {
            "analysis_profile_id": CANONICAL_PROFILE_ID,
            "evidence_contract_id": RESOLVER_COMPLETE,
            **EVIDENCE,
            **stamp_profile_full(CANONICAL_PROFILE_ID),
        }
    )
    assert decide_analysis_cache_replacement(v2_row(20), canonical) == (
        Decision.REPLACE,
        Reason.DOMINATES_REPLACE,
    )
    # ...and never the reverse: the authority barrier is not a strength question.
    assert (
        compare_evidence_rows(v2_row(20), canonical).outcome
        is Supersession.B_SUPERSEDES
    )
    assert decide_analysis_cache_replacement(canonical, v2_row(20))[0] is Decision.KEEP


def test_dynamic_browser_row_is_never_authoritative():
    assert v2_row(20).is_effectively_authoritative() is False
    assert get_profile(BROWSER_GAME_V2_PROFILE_ID).authoritative is False


# --- intra-batch dedupe --------------------------------------------------------


def _batch_survivor(rows):
    surviving, rejected = _dedupe_batch(rows)
    return surviving, rejected


def test_stronger_row_survives_the_batch_in_either_order():
    weak, strong = v2_row_data(17), v2_row_data(20)
    for batch in ([weak, strong], [strong, weak]):
        surviving, rejected = _batch_survivor([dict(r) for r in batch])
        assert rejected == []
        assert len(surviving) == 1
        assert surviving[0]["search_limit_value"] == 20


def test_incomparable_same_key_rows_conflict_the_key():
    a = v2_row_data(17)
    b = v2_row_data(20, engine_build="d" * 64)
    surviving, rejected = _batch_survivor([a, b])
    assert surviving == []
    assert [reason for _, reason in rejected] == [Reason.DUPLICATE_CONFLICT]


def test_equal_strength_different_provenance_conflicts_rather_than_guessing():
    # Neither replaces the other, so "most complete" is undefined and the survivor
    # would depend on arrival order — reject the key instead.
    surviving, rejected = _batch_survivor([v2_row_data(17), v2_row_data(17, hash_mb=256)])
    assert surviving == []
    assert [reason for _, reason in rejected] == [Reason.DUPLICATE_CONFLICT]


def test_identical_provenance_rows_still_collapse():
    surviving, rejected = _batch_survivor([v2_row_data(17), v2_row_data(17)])
    assert rejected == []
    assert len(surviving) == 1


# --- display -------------------------------------------------------------------


def _live(depth):
    return browser_live_descriptor(json.dumps(provenance(depth)))


def test_one_row_seam_never_overlays_a_requires_comparison_row():
    # display_upgrade_eligible (the ALWAYS-only predicate) is what the one-row
    # Part-B seam uses; a per-device row cannot be judged without a live operand.
    assert display_upgrade_eligible(v2_row(20)) is False
    assert display_upgrade_eligible_vs(v2_row(20), None) is False


def test_stronger_stored_row_overlays_a_weaker_live_operand():
    assert display_upgrade_eligible_vs(v2_row(20), _live(17)) is True


@pytest.mark.parametrize("live_depth", [20, 21])
def test_equal_or_weaker_stored_row_does_not_overlay(live_depth):
    assert display_upgrade_eligible_vs(v2_row(20), _live(live_depth)) is False


def test_incomparable_live_operand_does_not_overlay():
    live = browser_live_descriptor(json.dumps(provenance(17, engine_build="e" * 64)))
    assert display_upgrade_eligible_vs(v2_row(20), live) is False


def test_always_mode_profiles_ignore_the_live_operand():
    canonical = project_cache_row(
        {
            "analysis_profile_id": CANONICAL_PROFILE_ID,
            "evidence_contract_id": RESOLVER_COMPLETE,
            **EVIDENCE,
            **stamp_profile_full(CANONICAL_PROFILE_ID),
        }
    )
    assert display_upgrade_eligible_vs(canonical, None) is True
    assert display_upgrade_eligible_vs(canonical, _live(60)) is True


def test_storage_and_display_agree_on_ordering():
    # The same comparison decides both, so a row the writer calls stronger is
    # exactly a row the reader is willing to overlay.
    stored, live_depth = v2_row(20), 17
    replaced = decide_analysis_cache_replacement(v2_row(live_depth), stored)[0]
    assert (replaced is Decision.REPLACE) == display_upgrade_eligible_vs(
        stored, _live(live_depth)
    )


# --- the live descriptor -------------------------------------------------------


def test_live_descriptor_rebuilds_a_verifying_operand():
    live = _live(18)
    assert live is not None
    assert verify_identity(
        {"analysis_profile_id": BROWSER_GAME_V2_PROFILE_ID, **live.identity_values()}
    )
    # The fixed half comes from the registry, NOT the stored blob.
    profile = get_profile(BROWSER_GAME_V2_PROFILE_ID)
    assert live.identity_values()["profile_manifest_digest"] == (
        profile.profile_manifest_digest
    )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json",
        "[1,2,3]",
        json.dumps(provenance(17, search_limit_value=0)),  # tampered out of range
    ],
)
def test_unusable_stored_provenance_yields_no_operand(raw):
    assert browser_live_descriptor(raw) is None


def test_stored_blob_cannot_claim_a_fixed_identity():
    # A hand-edited row naming fixed fields must not be able to change them: the
    # stamp only honors the profile's DECLARED-dynamic keys.
    tampered = json.dumps({**provenance(20), "multipv": 3, "engine_name": "Leela"})
    live = browser_live_descriptor(tampered)
    assert live is not None
    assert live.identity_values()["multipv"] == 1
    assert live.identity_values()["engine_name"] == "Stockfish"


def test_descriptor_is_never_itself_displayable():
    live = _live(20)
    assert isinstance(live, CacheRow)
    assert display_upgrade_eligible_vs(live, _live(1)) is False


# --- audit ---------------------------------------------------------------------


def test_a_valid_dynamic_row_audits_as_non_authoritative_but_valid():
    from app.analysis_cache_audit import Category, classify_row, should_invalidate

    category = classify_row(v2_row_data(20))
    assert category is Category.NON_AUTH_VALID
    # Not contaminated: the repair tool must not delete honest browser evidence
    # just because its identity columns vary per device.
    assert should_invalidate(category, include_legacy_null=False) is False


def test_a_forged_dynamic_row_still_audits_as_contaminated():
    from app.analysis_cache_audit import Category, classify_row, should_invalidate

    # A row claiming browser-game-v2 with an unusable dynamic value cannot back up
    # its profile claim, so the guard rejects it and the audit flags it.
    forged = v2_row_data(20, search_limit_value=None)
    category = classify_row(forged)
    assert category is Category.CONTAMINATED_PROFILE_CLAIM
    assert should_invalidate(category, include_legacy_null=False) is True
