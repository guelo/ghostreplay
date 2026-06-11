"""Immutable, versioned registry of analysis profiles.

A *profile* identifies the engine/search configuration a cached analysis row was
produced with. The registry is the single source of truth for compatibility,
authority, and cross-family dominance — the cache-replacement comparator never
infers ordering from raw numeric depth.

The registry holds each profile's *expected* settings; the ``analysis_cache``
columns hold the *actual* settings a row was produced with. ``identity_verified``
(see :mod:`app.analysis_cache_policy`) is exactly the equality check between the
two, so settings must be persisted as columns (not registry-only).

NOTE: the real canonical engine artifacts (binary SHA-256 / NNUE hash) are pinned
by the child issue ``g-canonical-precomp``. This module ships the registry
infrastructure plus a fixture authoritative profile with placeholder identity so
the comparator/repo can be exercised before the canonical values are filled in.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Profile:
    profile_id: str
    engine_name: str | None
    engine_version: str | None  # exact, e.g. "16.1"
    engine_build: str | None  # build/commit/binary checksum identifier
    network_id: str | None  # NNUE EvalFile name + content hash
    search_limit_type: str | None  # 'depth' | 'nodes' | 'movetime'
    search_limit_value: int | None
    threads: int | None
    hash_mb: int | None
    multipv: int | None
    authoritative: bool  # may this profile replace legacy/other-family rows?
    dominates: frozenset[str] = field(default_factory=frozenset)


# Identity-bearing metadata columns compared by ``identity_verified``.
IDENTITY_FIELDS = (
    "engine_name",
    "engine_version",
    "engine_build",
    "network_id",
    "search_limit_type",
    "search_limit_value",
    "threads",
    "hash_mb",
    "multipv",
)

CANONICAL_PROFILE_ID = "canonical-sf-depth24-v1"
BROWSER_PROFILE_ID = "browser-game-v1"
JEFFML_PROFILE_ID = "jeffml-scores-v1"

# Placeholder identity for the canonical profile. ``g-canonical-precomp`` replaces
# these with the concrete pinned binary SHA-256 + EvalFile hash of the chosen
# Stockfish build. Until then, real precompute runs resolve to ``None`` (legacy),
# while tests stamp these exact placeholder values to exercise authority.
_CANONICAL = Profile(
    profile_id=CANONICAL_PROFILE_ID,
    engine_name="Stockfish",
    engine_version="16.1",
    engine_build="PLACEHOLDER-canonical-binary-sha256",
    network_id="PLACEHOLDER-nnue-evalfile-hash",
    search_limit_type="depth",
    search_limit_value=24,
    threads=1,
    hash_mb=128,
    multipv=1,
    authoritative=True,
    dominates=frozenset({BROWSER_PROFILE_ID, JEFFML_PROFILE_ID}),
)

_BROWSER = Profile(
    profile_id=BROWSER_PROFILE_ID,
    engine_name=None,
    engine_version=None,
    engine_build=None,
    network_id=None,
    search_limit_type=None,
    search_limit_value=None,
    threads=None,
    hash_mb=None,
    multipv=None,
    authoritative=False,
    dominates=frozenset(),
)

_JEFFML = Profile(
    profile_id=JEFFML_PROFILE_ID,
    engine_name=None,
    engine_version=None,
    engine_build=None,
    network_id=None,
    search_limit_type=None,
    search_limit_value=None,
    threads=None,
    hash_mb=None,
    multipv=None,
    authoritative=False,
    dominates=frozenset(),
)

_REGISTRY: dict[str, Profile] = {
    p.profile_id: p for p in (_CANONICAL, _BROWSER, _JEFFML)
}


def get_profile(profile_id: str | None) -> Profile | None:
    """Return the registered profile, or ``None`` for unknown/legacy ids."""
    if profile_id is None:
        return None
    return _REGISTRY.get(profile_id)


def resolve_profile(observed: dict) -> str | None:
    """Resolve observed engine/search metadata to a registered profile id.

    Returns the matching authoritative profile id only when *every* identity
    field matches exactly. Off-spec / unknown producers resolve to ``None`` and
    are persisted as effectively-legacy, non-authoritative rows (the observed
    metadata is still written to the columns for diagnostics, but the row carries
    no profile claim and never gains authority).
    """
    for profile in _REGISTRY.values():
        if not profile.authoritative:
            continue
        if all(observed.get(f) == getattr(profile, f) for f in IDENTITY_FIELDS):
            return profile.profile_id
    return None
