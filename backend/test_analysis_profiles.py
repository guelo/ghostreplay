"""Tests for the canonical profile manifest, identity split, and lifecycle."""

import dataclasses

import app.analysis_profiles as profiles
from app.analysis_profiles import (
    CANONICAL_PROFILE_ID,
    RESOLUTION_FIELDS,
    BROWSER_PROFILE_ID,
    JEFFML_PROFILE_ID,
    get_profile,
    resolve_profile,
    stamp_identity,
)


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
