"""Tests for the canonical profile manifest, identity split, and lifecycle."""

import dataclasses

import app.analysis_profiles as profiles
from app.analysis_profiles import (
    AUTHORITATIVE_PROFILE_PRIORITY,
    CANONICAL_PROFILE_ID,
    RESOLUTION_FIELDS,
    BROWSER_PROFILE_ID,
    JEFFML_PROFILE_ID,
    StrengthComparison,
    compare_search_strength,
    get_profile,
    resolve_profile,
    stamp_identity,
)

LINUX_PROFILE_ID = "canonical-sf18-depth24-linux-v1"


def test_canonical_manifest_loads_full_identity():
    p = get_profile(CANONICAL_PROFILE_ID)
    assert p is not None
    assert p.engine_name == "Stockfish"
    assert p.engine_version == "18"
    # Full (untruncated) executable SHA-256.
    assert p.engine_build and len(p.engine_build) == 64
    # Both NNUE identities present, not collapsed into one column, each carrying a
    # full (untruncated) 64-hex content hash whose prefix matches the filename.
    assert p.eval_file == "nn-c288c895ea92.nnue"
    assert p.eval_file_small == "nn-37f18f62d772.nnue"
    for ident, filename in (
        (p.eval_file_id, "nn-c288c895ea92.nnue"),
        (p.eval_file_small_id, "nn-37f18f62d772.nnue"),
    ):
        name, _, full = ident.rpartition(":")
        assert name == filename
        assert profiles._FULL_SHA256.match(full)
        # Filename is the content-addressed first 12 hex of the full SHA-256.
        assert filename == f"nn-{full[:12]}.nnue"
    assert p.analyzer_protocol_version == profiles.ANALYZER_PROTOCOL_VERSION
    assert p.profile_manifest_digest and len(p.profile_manifest_digest) == 64
    assert p.authoritative is True
    assert p.active is True
    assert {BROWSER_PROFILE_ID, JEFFML_PROFILE_ID} <= p.dominates


def test_authoritative_manifest_must_be_fully_pinned():
    import pytest

    ha = "a" * 64
    hb = "b" * 64
    base = {
        "profile_id": "x", "authoritative": True,
        "source_commit": "cb3d4ee9b47d0c5aae855b12379378ea1439675c",
        "engine_build": "6593b3e937ac9f19629ea3b71d2eae84ae76e0b0f89995214a40b6c52b419ec6",
        "eval_file": "nn-aaaaaaaaaaaa.nnue",
        "eval_file_small": "nn-bbbbbbbbbbbb.nnue",
        "eval_file_id": f"nn-aaaaaaaaaaaa.nnue:{ha}",
        "eval_file_small_id": f"nn-bbbbbbbbbbbb.nnue:{hb}",
    }
    profiles._assert_fully_pinned(base)  # ok
    # Truncated network hash is rejected.
    with pytest.raises(ValueError):
        profiles._assert_fully_pinned({**base, "eval_file_id": "nn-aaaaaaaaaaaa.nnue:abc123"})
    # Non-64-hex executable build is rejected.
    with pytest.raises(ValueError):
        profiles._assert_fully_pinned({**base, "engine_build": "PLACEHOLDER"})
    # A non-40-hex / placeholder source commit is rejected.
    with pytest.raises(ValueError):
        profiles._assert_fully_pinned({**base, "source_commit": "verified"})
    with pytest.raises(ValueError):
        profiles._assert_fully_pinned({**base, "source_commit": "UNVERIFIED-x"})
    # Identity filename not matching eval_file is rejected.
    with pytest.raises(ValueError):
        profiles._assert_fully_pinned({**base, "eval_file": "nn-cccccccccccc.nnue"})
    # Hash not content-addressed by its filename (prefix mismatch) is rejected.
    with pytest.raises(ValueError):
        profiles._assert_fully_pinned(
            {**base, "eval_file_id": f"nn-aaaaaaaaaaaa.nnue:{'c' * 64}"}
        )
    # A non-authoritative manifest is exempt.
    profiles._assert_fully_pinned({"profile_id": "y", "authoritative": False})


def test_manifest_digest_excludes_lifecycle_fields():
    p = get_profile(CANONICAL_PROFILE_ID)
    base = {f: getattr(p, f) for f in profiles._DIGEST_FIELDS}
    digest = profiles._manifest_digest(base)
    # Adding/altering active + dominates does NOT change the digest.
    with_lifecycle = {**base, "active": False, "dominates": ["x"]}
    assert profiles._manifest_digest(with_lifecycle) == digest
    # Changing an immutable identity field DOES change the digest.
    changed = {**base, "engine_build": "deadbeef"}
    assert profiles._manifest_digest(changed) != digest


def test_resolve_exact_and_offspec():
    p = get_profile(CANONICAL_PROFILE_ID)
    observed = {f: getattr(p, f) for f in RESOLUTION_FIELDS}
    assert resolve_profile(observed) == CANONICAL_PROFILE_ID
    observed["engine_build"] = "wrong-sha"
    assert resolve_profile(observed) is None


def test_resolve_skips_retired_profile(monkeypatch):
    p = get_profile(CANONICAL_PROFILE_ID)
    retired = dataclasses.replace(p, active=False)
    monkeypatch.setitem(profiles._REGISTRY, CANONICAL_PROFILE_ID, retired)
    observed = {f: getattr(p, f) for f in RESOLUTION_FIELDS}
    # A retired profile is never resolved for a new producer.
    assert resolve_profile(observed) is None


def test_stamp_identity_returns_full_columns():
    p = get_profile(CANONICAL_PROFILE_ID)
    stamped = stamp_identity(CANONICAL_PROFILE_ID)
    assert stamped["eval_file_id"] == p.eval_file_id
    assert stamped["eval_file_small_id"] == p.eval_file_small_id
    assert stamped["analyzer_protocol_version"] == p.analyzer_protocol_version
    assert stamped["profile_manifest_digest"] == p.profile_manifest_digest
    assert stamp_identity("unknown") == {}


# --- compare_search_strength (g-position-analysis Phase 2) ---------------------


def test_priority_lists_both_canonical_profiles_linux_first():
    # The linux precompute profile is preferred for the equal-strength tiebreak.
    assert AUTHORITATIVE_PROFILE_PRIORITY == (LINUX_PROFILE_ID, CANONICAL_PROFILE_ID)


def test_strength_two_canonical_profiles_are_equal():
    # Today's two canonical profiles differ only by platform binary (engine_build),
    # which is NOT a strength invariant, so they rank EQUAL (both v18 / depth-24).
    a = get_profile(CANONICAL_PROFILE_ID)
    b = get_profile(LINUX_PROFILE_ID)
    assert compare_search_strength(a, b) == StrengthComparison.EQUAL
    assert compare_search_strength(b, a) == StrengthComparison.EQUAL


def test_strength_deeper_search_on_same_net_wins():
    base = get_profile(LINUX_PROFILE_ID)
    deeper = dataclasses.replace(base, search_limit_value=30)
    assert compare_search_strength(deeper, base) == StrengthComparison.A_STRONGER
    assert compare_search_strength(base, deeper) == StrengthComparison.B_STRONGER


def test_strength_higher_engine_version_wins_before_depth():
    base = get_profile(LINUX_PROFILE_ID)
    # Newer engine version with a SHALLOWER search still ranks stronger: version is
    # compared before search_limit_value.
    newer = dataclasses.replace(base, engine_version="19", search_limit_value=10)
    assert compare_search_strength(newer, base) == StrengthComparison.A_STRONGER


def test_strength_differing_net_is_incomparable():
    base = get_profile(LINUX_PROFILE_ID)
    other_net = dataclasses.replace(
        base, eval_file_id="nn-deadbeefdead.nnue:" + "d" * 64, search_limit_value=30
    )
    # A deeper run on a DIFFERENT net is not "stronger" — it measured differently.
    assert compare_search_strength(other_net, base) == StrengthComparison.INCOMPARABLE


def test_strength_differing_multipv_protocol_or_limit_type_is_incomparable():
    base = get_profile(LINUX_PROFILE_ID)
    for override in (
        {"multipv": 3},
        {"analyzer_protocol_version": "analyzer-v2"},
        {"search_limit_type": "nodes"},
        {"engine_name": "Lc0"},
    ):
        variant = dataclasses.replace(base, **override)
        assert (
            compare_search_strength(variant, base) == StrengthComparison.INCOMPARABLE
        )


def test_strength_non_numeric_unequal_version_is_incomparable():
    base = get_profile(LINUX_PROFILE_ID)
    a = dataclasses.replace(base, engine_version="dev-a")
    b = dataclasses.replace(base, engine_version="dev-b")
    assert compare_search_strength(a, b) == StrengthComparison.INCOMPARABLE
    # Same non-numeric version + equal depth falls through to EQUAL.
    a2 = dataclasses.replace(base, engine_version="dev-a")
    assert compare_search_strength(a, a2) == StrengthComparison.EQUAL


# --- browser-analysis-v1 profile (g-cache-stronger-evals) ----------------------

from app.analysis_profiles import (  # noqa: E402
    BROWSER_ANALYSIS_PROFILE_ID,
    IDENTITY_FIELDS,
    stamp_profile_full,
)

_WASM_SHA256 = "a8fbc05ec6920b56d7485826dcb02c5ffd2826bcbf751cf973046f237a9096f1"
_NET_ID = "nn-9067e33176e8.nnue:9067e33176e8c5edb7aa8db6a3aedd012f84a1f39872e86357c6c2d0993f314d"


def test_browser_analysis_profile_pinned_identity():
    p = get_profile(BROWSER_ANALYSIS_PROFILE_ID)
    assert p is not None
    assert p.engine_name == "Stockfish"
    assert p.engine_version == "18"
    # engine_build is the compiled WASM artifact SHA-256.
    assert p.engine_build == _WASM_SHA256
    assert profiles._FULL_SHA256.match(p.engine_build)
    assert p.search_limit_type == "depth"
    assert p.search_limit_value == 21
    assert p.multipv == 1
    assert p.threads == 1
    assert p.hash_mb == 128
    assert p.eval_file == "nn-9067e33176e8.nnue"
    assert p.eval_file_small is None
    # Content-addressed full net identity, distinct from canonical's big net.
    assert p.eval_file_id == _NET_ID
    name, _, full = p.eval_file_id.rpartition(":")
    assert name == "nn-9067e33176e8.nnue"
    assert profiles._FULL_SHA256.match(full)
    assert full[:12] == "9067e33176e8"
    assert p.eval_file_small_id is None
    assert p.analyzer_protocol_version == "browser-analyzer-v1"


def test_browser_analysis_profile_authority_and_dominance():
    p = get_profile(BROWSER_ANALYSIS_PROFILE_ID)
    assert p.authoritative is False
    assert p.replacement_eligible is True
    # RETIRED (g-reuse-d21-search): the hidden internally-inconsistent protocol is
    # now inactive. Its stored rows stay identity-verified (digest excludes
    # ``active``), so dominance/digest are unchanged.
    assert p.active is False
    assert p.dominates == frozenset({"browser-game-v1"})
    # Digest is non-null and stable (recomputable from the identity fields).
    assert p.profile_manifest_digest is not None
    recomputed = profiles._manifest_digest({f: getattr(p, f) for f in profiles._DIGEST_FIELDS})
    assert recomputed == p.profile_manifest_digest


def test_canonical_manifests_dominate_browser_analysis():
    for pid in (CANONICAL_PROFILE_ID, LINUX_PROFILE_ID):
        assert BROWSER_ANALYSIS_PROFILE_ID in get_profile(pid).dominates


def test_browser_game_not_replacement_eligible_by_default():
    assert get_profile(BROWSER_PROFILE_ID).replacement_eligible is False


def test_browser_analysis_not_resolvable_from_runtime():
    # Non-authoritative profiles are never resolve_profile targets; the endpoint
    # stamps identity from the registry instead.
    p = get_profile(BROWSER_ANALYSIS_PROFILE_ID)
    observed = {f: getattr(p, f) for f in RESOLUTION_FIELDS}
    assert resolve_profile(observed) is None


def test_stamp_profile_full_stamps_every_identity_column():
    p = get_profile(BROWSER_ANALYSIS_PROFILE_ID)
    stamped = stamp_profile_full(BROWSER_ANALYSIS_PROFILE_ID)
    assert set(stamped.keys()) == set(IDENTITY_FIELDS)
    for f in IDENTITY_FIELDS:
        assert stamped[f] == getattr(p, f)
    # RESOLUTION_FIELDS-only runtime filenames are deliberately omitted.
    assert "eval_file" not in stamped
    assert "eval_file_small" not in stamped
    assert stamp_profile_full("unknown") == {}


def test_stamp_identity_is_narrower_than_stamp_profile_full():
    narrow = stamp_identity(BROWSER_ANALYSIS_PROFILE_ID)
    full = stamp_profile_full(BROWSER_ANALYSIS_PROFILE_ID)
    assert set(narrow.keys()) == {
        "eval_file_id",
        "eval_file_small_id",
        "analyzer_protocol_version",
        "profile_manifest_digest",
    }
    assert set(narrow.keys()) < set(full.keys())


# --- browser-analysis-multipv-v2 successor profile (g-reuse-d21-search) ---------

from app.analysis_profiles import (  # noqa: E402
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
    BROWSER_VISIBLE_MULTIPV_PROTOCOL_VERSION,
)


def test_browser_analysis_multipv_profile_pinned_identity():
    # The successor's identity is the ACTUAL visible worker (stockfishWorker.ts):
    # same pinned lite-single artifact + single net as v1, but the visible worker's
    # real Hash (64) and MultiPV (3), under the internally-consistent protocol. If
    # the visible worker's engine params ever change, this pin and the code move
    # together — a silent drift would let forged rows identity-verify.
    p = get_profile(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert p is not None
    assert p.engine_name == "Stockfish"
    assert p.engine_version == "18"
    assert p.engine_build == _WASM_SHA256  # same artifact as retired v1
    assert profiles._FULL_SHA256.match(p.engine_build)
    assert p.search_limit_type == "depth"
    assert p.search_limit_value == 21
    # The two identity columns that DISTINGUISH it from the retired v1.
    assert p.multipv == 3
    assert p.hash_mb == 64
    assert p.threads == 1
    assert p.eval_file_id == _NET_ID  # same single net as v1
    assert p.eval_file_small_id is None
    assert p.analyzer_protocol_version == BROWSER_VISIBLE_MULTIPV_PROTOCOL_VERSION
    assert p.analyzer_protocol_version == "browser-visible-multipv-v1"


def test_browser_analysis_multipv_profile_authority_and_dominance():
    p = get_profile(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    # Active successor, non-authoritative but replacement-eligible.
    assert p.active is True
    assert p.authoritative is False
    assert p.replacement_eligible is True
    # Correctively replaces the retired hidden protocol AND the weaker d17 game
    # baseline for the same key (PROTOCOL_CORRECTION + TIER_BASELINE edges).
    assert p.dominates == frozenset(
        {BROWSER_ANALYSIS_PROFILE_ID, BROWSER_PROFILE_ID}
    )
    # Digest is non-null and recomputable from the identity fields.
    assert p.profile_manifest_digest is not None
    recomputed = profiles._manifest_digest(
        {f: getattr(p, f) for f in profiles._DIGEST_FIELDS}
    )
    assert recomputed == p.profile_manifest_digest


def test_multipv_successor_identity_distinct_from_retired_v1():
    # The successor and the retired v1 share build+net but MUST NOT collide: the
    # Hash/MultiPV/protocol differences give them distinct manifest digests, so a
    # v1-stamped row can never masquerade as the successor (and vice versa).
    v1 = get_profile(BROWSER_ANALYSIS_PROFILE_ID)
    v2 = get_profile(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert (v1.hash_mb, v1.multipv) != (v2.hash_mb, v2.multipv)
    assert v1.analyzer_protocol_version != v2.analyzer_protocol_version
    assert v1.profile_manifest_digest != v2.profile_manifest_digest


def test_canonical_manifests_dominate_multipv_successor():
    for pid in (CANONICAL_PROFILE_ID, LINUX_PROFILE_ID):
        assert BROWSER_ANALYSIS_MULTIPV_PROFILE_ID in get_profile(pid).dominates
