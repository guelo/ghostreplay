"""Immutable, versioned registry of analysis profiles.

A *profile* identifies the engine/search configuration a cached analysis row was
produced with. The registry is the single source of truth for compatibility,
authority, and cross-family dominance — the cache-replacement comparator never
infers ordering from raw numeric depth.

Two field sets exist by design (see ``g-canonical-precomp``):

* ``RESOLUTION_FIELDS`` — runtime-OBSERVABLE identity used at *write* time by
  :func:`resolve_profile`: the executable SHA-256, the EvalFile / EvalFileSmall
  *filenames* reported over UCI, engine version, search-limit, threads/hash/
  multipv, and the analyzer protocol version. A producer can only observe these
  without extracting an embedded NNUE blob.
* ``IDENTITY_FIELDS`` — stored-column identity verified at *read* time by
  ``identity_verified``: it compares the row's persisted *full* network-identity
  columns (``eval_file_id`` / ``eval_file_small_id``), protocol version, and
  manifest digest against the resolved profile's manifest values.

Write-time resolution stamps the manifest-derived full network identities onto
the row (phase 2), so read-time verification has full hashes to compare even
though runtime never hashed an embedded blob.

Profiles are pinned by committed manifests under ``app/canonical_profiles/``.
Profiles are IMMUTABLE once production rows reference them: any engine, network,
search, or analyzer change creates a ``-v2`` profile with an explicit
``dominates`` edge. Retirement only flips ``active`` (and may extend a
successor's ``dominates``); the manifest digest deliberately excludes ``active``
and ``dominates`` so a retired profile's rows stay ``identity_verified`` (still
recognizable for dominance) while no longer counting as trusted hits.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

_FULL_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# A manually-bumped version of THIS analyzer's output contract (search ordering,
# terminal-score synthesis, score conversion, PV/continuation rule, classifier
# port, reset protocol). Any change that alters analyzer output bumps this, so
# old rows stop matching identity against the canonical profile.
ANALYZER_PROTOCOL_VERSION = "analyzer-v1"

_MANIFEST_DIR = Path(__file__).resolve().parent / "canonical_profiles"


@dataclass(frozen=True)
class Profile:
    profile_id: str
    engine_name: str | None
    engine_version: str | None  # exact, e.g. "18"
    engine_build: str | None  # executable SHA-256
    # NNUE filenames reported over UCI (runtime-observable, for resolution).
    eval_file: str | None
    eval_file_small: str | None
    # Full network identities "<filename>:<hash>" (stored columns, for read-time
    # identity verification). Stamped from the manifest at write time.
    eval_file_id: str | None
    eval_file_small_id: str | None
    search_limit_type: str | None  # 'depth' | 'nodes' | 'movetime'
    search_limit_value: int | None
    threads: int | None
    hash_mb: int | None
    multipv: int | None
    analyzer_protocol_version: str | None
    profile_manifest_digest: str | None
    authoritative: bool  # canonical: read-trusted, reclaims legacy, resolve-stamped
    # Replacement eligibility split from authority (g-cache-stronger-evals): a
    # NON-authoritative profile may still replace a weaker COMPATIBLE profile via an
    # explicit ``dominates`` edge, without becoming a canonical/read-trusted hit or
    # gaining legacy-reclamation rights. Defaults False; the manifest loader defaults
    # it from ``authoritative`` so canonical profiles keep their existing behavior.
    replacement_eligible: bool = False
    active: bool = True  # current-for-resolution; retired profiles are inactive
    # Legacy single-column network identity, retired from the identity set.
    network_id: str | None = None
    dominates: frozenset[str] = field(default_factory=frozenset)
    # Identity fields validated by a per-field DYNAMIC rule instead of exact
    # equality (g-browser-policy-v2 D2.1 / g-mk1d). Empty for every profile
    # registered today, so ``verify_identity`` is exact equality over all
    # IDENTITY_FIELDS — byte-identical to the historical per-call-site checks.
    # g-mk1d attaches validators (e.g. a declared per-device depth range) here
    # without introducing a second verifier.
    dynamic_fields: frozenset[str] = field(default_factory=frozenset)


# Runtime-observable identity matched by ``resolve_profile`` at write time.
RESOLUTION_FIELDS = (
    "engine_name",
    "engine_version",
    "engine_build",
    "eval_file",
    "eval_file_small",
    "search_limit_type",
    "search_limit_value",
    "threads",
    "hash_mb",
    "multipv",
    "analyzer_protocol_version",
)

# Stored-column identity verified at read time (full network hashes present).
IDENTITY_FIELDS = (
    "engine_name",
    "engine_version",
    "engine_build",
    "eval_file_id",
    "eval_file_small_id",
    "search_limit_type",
    "search_limit_value",
    "threads",
    "hash_mb",
    "multipv",
    "analyzer_protocol_version",
    "profile_manifest_digest",
)

# Immutable analysis-identity fields the manifest digest is computed over.
# EXCLUDES lifecycle (``active``, ``dominates``) so retirement does not change
# the digest and thus does not invalidate existing rows' ``identity_verified``.
_DIGEST_FIELDS = (
    "engine_name",
    "engine_version",
    "engine_build",
    "eval_file_id",
    "eval_file_small_id",
    "search_limit_type",
    "search_limit_value",
    "threads",
    "hash_mb",
    "multipv",
    "analyzer_protocol_version",
)

CANONICAL_PROFILE_ID = "canonical-sf18-depth24-v1"
CANONICAL_LINUX_PROFILE_ID = "canonical-sf18-depth24-linux-v1"
BROWSER_PROFILE_ID = "browser-game-v1"
BROWSER_ANALYSIS_PROFILE_ID = "browser-analysis-v1"
# The corrective visible-MultiPV reuse producer (g-reuse-d21-search): evidence
# derived from the already-completed, unrestricted visible depth-21 MultiPV-3
# search, with best+played taken from two lines of the SAME request (no hidden
# child searches). Replaces the defective hidden ``browser-analysis-v1`` protocol
# for an exact key via a PROTOCOL_CORRECTION edge.
BROWSER_ANALYSIS_MULTIPV_PROFILE_ID = "browser-analysis-multipv-v2"
JEFFML_PROFILE_ID = "jeffml-scores-v1"

# The Stockfish-18 lite-single browser analyzer protocol: a root best-move search
# plus a post-played and post-best search at depth N, ``computeAnalysisResult`` for
# the eval triple, and ``classifyMoveAdvanced`` classification (accepted only when
# the worker reports ``canonical===true``). Same method as the browser-game
# producer, only deeper — bumped independently of ``ANALYZER_PROTOCOL_VERSION``
# (the canonical analyzer) because it is a distinct producer contract.
BROWSER_ANALYZER_PROTOCOL_VERSION = "browser-analyzer-v1"

# The corrective visible-MultiPV reuse protocol (g-reuse-d21-search): a single
# completed unrestricted visible depth-21 MultiPV-3 root search. The best and the
# played line are two lines of that SAME search reported from one root
# side-to-move perspective, so the tuple is internally consistent by construction
# (no independent post-move continuation searches, unlike browser-analyzer-v1).
BROWSER_VISIBLE_MULTIPV_PROTOCOL_VERSION = "browser-visible-multipv-v1"


def _manifest_digest(values: dict) -> str:
    payload = json.dumps(
        {f: values.get(f) for f in _DIGEST_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _network_hash(identity: str | None) -> str | None:
    """Extract the content hash from a ``<filename>:<sha256>`` network identity."""
    if not identity or ":" not in identity:
        return None
    return identity.rsplit(":", 1)[1]


_FULL_SHA1 = re.compile(r"^[0-9a-f]{40}$")
# Stockfish networks are content-addressed: nn-<first-12-hex-of-sha256>.nnue
_NNUE_NAME = re.compile(r"^nn-([0-9a-f]{12})\.nnue$")


def _assert_fully_pinned(m: dict) -> None:
    """Fail closed: an authoritative manifest must be a complete, self-consistent
    reproducibility pin.

    A profile marked ``authoritative`` may stamp rows as canonical trusted-cache
    hits, so refuse to register it unless:
      * ``engine_build`` is a full 64-hex executable SHA-256;
      * ``source_commit`` is a real 40-hex git commit (not a placeholder);
      * each NNUE identity is ``'<filename>:<full-64-hex-sha256>'`` whose filename
        equals the corresponding ``eval_file`` / ``eval_file_small`` AND whose hash
        is content-addressed by that filename (its first 12 hex match the
        ``nn-<prefix>.nnue`` name). This catches a copy-paste mismatch between the
        reported filename and the pinned hash.
    """
    pid = m.get("profile_id")
    if not m.get("authoritative"):
        return
    if not _FULL_SHA256.match(str(m.get("engine_build", ""))):
        raise ValueError(
            f"canonical profile {pid!r}: engine_build must be a full 64-hex "
            "SHA-256 of the executable"
        )
    if not _FULL_SHA1.match(str(m.get("source_commit", ""))):
        raise ValueError(
            f"canonical profile {pid!r}: source_commit must be a full 40-hex "
            "git commit of the pinned engine source"
        )
    for id_fld, name_fld in (
        ("eval_file_id", "eval_file"),
        ("eval_file_small_id", "eval_file_small"),
    ):
        identity = m.get(id_fld)
        filename = m.get(name_fld)
        if not identity or ":" not in identity:
            raise ValueError(
                f"canonical profile {pid!r}: {id_fld} must be "
                "'<filename>:<full-64-hex-sha256>'"
            )
        name, _, full = identity.rpartition(":")
        if not _FULL_SHA256.match(full):
            raise ValueError(
                f"canonical profile {pid!r}: {id_fld} hash must be full 64-hex"
            )
        if name != filename:
            raise ValueError(
                f"canonical profile {pid!r}: {id_fld} filename {name!r} does not "
                f"match {name_fld}={filename!r}"
            )
        match = _NNUE_NAME.match(str(filename))
        if not match:
            raise ValueError(
                f"canonical profile {pid!r}: {name_fld}={filename!r} is not a "
                "content-addressed nn-<12hex>.nnue network filename"
            )
        if full[:12] != match.group(1):
            raise ValueError(
                f"canonical profile {pid!r}: {id_fld} hash is not content-"
                f"addressed by filename {filename!r} (prefix mismatch)"
            )


def _load_manifest(profile_id: str) -> Profile:
    """Load a committed canonical profile manifest into a :class:`Profile`."""
    with open(_MANIFEST_DIR / f"{profile_id}.json") as f:
        m = json.load(f)
    _assert_fully_pinned(m)
    digest = _manifest_digest(m)
    return Profile(
        profile_id=m["profile_id"],
        engine_name=m["engine_name"],
        engine_version=m["engine_version"],
        engine_build=m["engine_build"],
        eval_file=m["eval_file"],
        eval_file_small=m["eval_file_small"],
        eval_file_id=m["eval_file_id"],
        eval_file_small_id=m["eval_file_small_id"],
        search_limit_type=m["search_limit_type"],
        search_limit_value=m["search_limit_value"],
        threads=m["threads"],
        hash_mb=m["hash_mb"],
        multipv=m["multipv"],
        analyzer_protocol_version=m["analyzer_protocol_version"],
        profile_manifest_digest=digest,
        authoritative=m["authoritative"],
        # A canonical manifest defaults replacement_eligible to its authoritative
        # value, so existing canonical profiles are unchanged; a manifest can opt a
        # non-authoritative profile into replacement eligibility explicitly.
        replacement_eligible=m.get("replacement_eligible", m["authoritative"]),
        active=m["active"],
        dominates=frozenset(m.get("dominates", ())),
    )


_CANONICAL = _load_manifest(CANONICAL_PROFILE_ID)
_CANONICAL_LINUX  = _load_manifest("canonical-sf18-depth24-linux-v1")

_BROWSER = Profile(
    profile_id=BROWSER_PROFILE_ID,
    engine_name=None,
    engine_version=None,
    engine_build=None,
    eval_file=None,
    eval_file_small=None,
    eval_file_id=None,
    eval_file_small_id=None,
    search_limit_type=None,
    search_limit_value=None,
    threads=None,
    hash_mb=None,
    multipv=None,
    analyzer_protocol_version=None,
    profile_manifest_digest=None,
    authoritative=False,
)

_JEFFML = Profile(
    profile_id=JEFFML_PROFILE_ID,
    engine_name=None,
    engine_version=None,
    engine_build=None,
    eval_file=None,
    eval_file_small=None,
    eval_file_id=None,
    eval_file_small_id=None,
    search_limit_type=None,
    search_limit_value=None,
    threads=None,
    hash_mb=None,
    multipv=None,
    analyzer_protocol_version=None,
    profile_manifest_digest=None,
    authoritative=False,
)

# --- Browser analysis-board profile (g-cache-stronger-evals) -------------------
#
# Depth-21 single-PV post-move analyzer evidence produced by reusing
# analysisWorker.ts at a deeper depth — the SAME protocol as browser-game, only
# deeper. NON-authoritative (never a trusted /lookup hit, never reclaims legacy
# rows, never overwrites canonical depth-24) but replacement_eligible, so it can
# replace the weaker depth-17 browser-game-v1 row through the explicit dominates
# edge below.
#
# ``engine_build`` is the SHA-256 of the compiled WASM artifact
# node_modules/stockfish/bin/stockfish-18-lite-single.wasm (canonical
# executable-hash semantics). Surrounding provenance, NOT part of identity:
#   * JS loader stockfish-18-lite-single.js SHA-256
#       2278005057f381491f1c9bb3e44c9f5920b3a00bef9759e33cc6582769a1f1fe
#   * npm package stockfish@18.0.7
#   * npm integrity
#       sha512-tJ+bfMAHs4fV7QYiLcHUicx1RzKOQwj8twx/z0iUTauMG+SE3l1rEtRSg+WER81ke3bc5tqKBWEWntUbydkgZg==
# The lite-single build embeds ONE net nn-9067e33176e8.nnue (SHA-256
# 9067e33176e8...314d, verified content-addressed by its filename) and has NO
# small net. This differs from canonical SF18's big net nn-c288c895ea92.nnue, so
# browser-analysis is network-incompatible with canonical and never dominates it.
# ``engine_version`` is the UCI id-name token ("Stockfish 18 Lite WASM" -> "18");
# the npm version 18.0.7 is provenance only.
#
# _assert_fully_pinned does not run for non-authoritative profiles, but this
# profile still pins the full net hash and engine artifact hash for honest
# provenance and stable read-time identity verification.
_BROWSER_ANALYSIS_IDENTITY = {
    "engine_name": "Stockfish",
    "engine_version": "18",
    "engine_build": "a8fbc05ec6920b56d7485826dcb02c5ffd2826bcbf751cf973046f237a9096f1",
    "eval_file_id": (
        "nn-9067e33176e8.nnue:"
        "9067e33176e8c5edb7aa8db6a3aedd012f84a1f39872e86357c6c2d0993f314d"
    ),
    "eval_file_small_id": None,
    "search_limit_type": "depth",
    "search_limit_value": 21,
    "threads": 1,
    "hash_mb": 128,
    "multipv": 1,
    "analyzer_protocol_version": BROWSER_ANALYZER_PROTOCOL_VERSION,
}

_BROWSER_ANALYSIS = Profile(
    profile_id=BROWSER_ANALYSIS_PROFILE_ID,
    # Runtime-observable NNUE filename (RESOLUTION_FIELDS only, NOT identity-
    # verified and deliberately excluded from stamp_profile_full / the digest).
    eval_file="nn-9067e33176e8.nnue",
    eval_file_small=None,
    profile_manifest_digest=_manifest_digest(_BROWSER_ANALYSIS_IDENTITY),
    authoritative=False,
    replacement_eligible=True,
    # RETIRED (g-reuse-d21-search): the hidden root + independent post-move
    # protocol is internally inconsistent (g-kgiq). Flipping ``active`` False stops
    # new v1 rows being written (the endpoint producer discriminator fails a stale
    # client closed) while KEEPING stored v1 rows identity-verified — the manifest
    # digest excludes ``active``/``dominates`` — so their DISPLAY_OVERLAY capability
    # (retirement-surviving) and corrective replacement by the successor keep
    # working.
    active=False,
    dominates=frozenset({BROWSER_PROFILE_ID}),
    **_BROWSER_ANALYSIS_IDENTITY,
)

# --- Browser visible-MultiPV reuse profile (g-reuse-d21-search) ----------------
#
# The corrective successor to the retired hidden ``browser-analysis-v1``. Evidence
# is derived by REUSING the already-completed, unrestricted visible depth-21
# MultiPV-3 search that the analysis board runs for arrows/lines — no additional
# Stockfish search. The identity is the ACTUAL visible worker (stockfishWorker.ts):
# the same pinned lite-single artifact and single net as browser-analysis-v1, but
# the visible worker's real Hash (64 MB) and MultiPV (3), under the internally
# consistent browser-visible-multipv-v1 protocol.
#
# NON-authoritative (never a trusted /lookup hit, never reclaims legacy rows,
# never overwrites canonical) but replacement_eligible, so it correctively
# replaces a defective browser-analysis-v1 row (PROTOCOL_CORRECTION edge) and a
# weaker browser-game-v1 d17 row (TIER_BASELINE edge) for the exact key.
_BROWSER_ANALYSIS_MULTIPV_IDENTITY = {
    "engine_name": "Stockfish",
    "engine_version": "18",
    "engine_build": "a8fbc05ec6920b56d7485826dcb02c5ffd2826bcbf751cf973046f237a9096f1",
    "eval_file_id": (
        "nn-9067e33176e8.nnue:"
        "9067e33176e8c5edb7aa8db6a3aedd012f84a1f39872e86357c6c2d0993f314d"
    ),
    "eval_file_small_id": None,
    "search_limit_type": "depth",
    "search_limit_value": 21,
    "threads": 1,
    # Actual visible-worker value (stockfishWorker.ts sets Hash 64), distinct from
    # the retired hidden worker's Hash 128. If the visible worker's Hash is ever
    # standardized elsewhere, change both the code and this manifest together.
    "hash_mb": 64,
    # Unrestricted visible root MultiPV.
    "multipv": 3,
    "analyzer_protocol_version": BROWSER_VISIBLE_MULTIPV_PROTOCOL_VERSION,
}

_BROWSER_ANALYSIS_MULTIPV = Profile(
    profile_id=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
    eval_file="nn-9067e33176e8.nnue",
    eval_file_small=None,
    profile_manifest_digest=_manifest_digest(_BROWSER_ANALYSIS_MULTIPV_IDENTITY),
    authoritative=False,
    replacement_eligible=True,
    active=True,
    dynamic_fields=frozenset(),  # fixed profile
    # Mirrors the two new outgoing EDGES in evidence_policy.py; the registry-load
    # assertion checks EDGES<->dominates parity in both directions.
    dominates=frozenset({BROWSER_ANALYSIS_PROFILE_ID, BROWSER_PROFILE_ID}),
    **_BROWSER_ANALYSIS_MULTIPV_IDENTITY,
)

_REGISTRY: dict[str, Profile] = {
        p.profile_id: p
        for p in (
            _CANONICAL,
            _CANONICAL_LINUX,
            _BROWSER,
            _BROWSER_ANALYSIS,
            _BROWSER_ANALYSIS_MULTIPV,
            _JEFFML,
        )
}


def get_profile(profile_id: str | None) -> Profile | None:
    """Return the registered profile, or ``None`` for unknown/legacy ids."""
    if profile_id is None:
        return None
    return _REGISTRY.get(profile_id)


def list_profiles() -> tuple[Profile, ...]:
    """Every registered profile (active and retired). Read-only snapshot."""
    return tuple(_REGISTRY.values())


def resolve_profile(observed: dict) -> str | None:
    """Resolve runtime-observed engine/search metadata to a profile id.

    Matches on :data:`RESOLUTION_FIELDS` (runtime-observable only) against
    profiles that are BOTH ``active`` and ``authoritative``. A new producer can
    never stamp a retired profile id. Off-spec / unknown producers resolve to
    ``None``; the canonical precompute treats that as a hard error rather than
    storing an expensive non-authoritative run.
    """
    for profile in _REGISTRY.values():
        if not (profile.authoritative and profile.active):
            continue
        if all(observed.get(f) == getattr(profile, f) for f in RESOLUTION_FIELDS):
            return profile.profile_id
    return None


def stamp_identity(profile_id: str) -> dict:
    """Phase-2 stamp: manifest-derived full identity columns for a resolved row.

    Runtime can only observe network *filenames*; this copies the resolved
    profile's full ``eval_file_id`` / ``eval_file_small_id``, analyzer protocol
    version, and manifest digest so the persisted row carries the full identity
    that read-time verification compares.
    """
    profile = get_profile(profile_id)
    if profile is None:
        return {}
    return {
        "eval_file_id": profile.eval_file_id,
        "eval_file_small_id": profile.eval_file_small_id,
        "analyzer_protocol_version": profile.analyzer_protocol_version,
        "profile_manifest_digest": profile.profile_manifest_digest,
    }


def stamp_profile_full(profile_id: str) -> dict:
    """Full read-time identity stamp for a non-canonical writer (browser-analysis).

    Returns EVERY :data:`IDENTITY_FIELDS` column from the registered profile so a
    row written outside the ``resolve_profile`` path still ``identity_verified``\\ s
    against the registry. Canonical writers observe most identity columns at run
    time and only need the narrower :func:`stamp_identity` manifest fields; the
    browser-analysis endpoint has no runtime observation, so it stamps all 12.

    Deliberately OMITS the ``RESOLUTION_FIELDS``-only runtime filenames
    ``eval_file`` / ``eval_file_small``: they are not part of ``IDENTITY_FIELDS`` and
    adding them would change the identity/digest contract. Returns ``{}`` for an
    unknown id.
    """
    profile = get_profile(profile_id)
    if profile is None:
        return {}
    return {f: getattr(profile, f) for f in IDENTITY_FIELDS}


# --- Cross-profile search-strength comparison (g-position-analysis Phase 2) -----
#
# A reusable, GUARDED comparator: it only ranks two profiles by search strength
# when they share identical scoring semantics, so a deeper run on a *different*
# net / multipv / analyzer protocol is never called "stronger". The move-grain
# ``analysis_cache_policy`` does NOT use this — it orders purely by authority +
# explicit ``dominates`` edges + completeness. Strength ranking is a position-grain
# concern (which of two equally-trusted canonical runs is the better engine truth).


class StrengthComparison(Enum):
    """Result of :func:`compare_search_strength`."""

    A_STRONGER = "a_stronger"
    B_STRONGER = "b_stronger"
    EQUAL = "equal"
    INCOMPARABLE = "incomparable"


# Scoring-semantics fields that MUST be identical before two profiles can be
# ranked by search strength. If any differ the two runs measured the position
# under different rules, so depth/version ordering is meaningless -> INCOMPARABLE.
# ``engine_build`` is deliberately NOT here: the two canonical profiles differ
# only by platform binary (x86-64 vs x86-64-bmi2) and must stay comparable.
_STRENGTH_INVARIANT_FIELDS = (
    "engine_name",
    "eval_file_id",
    "eval_file_small_id",
    "multipv",
    "analyzer_protocol_version",
    "search_limit_type",
)

# Deterministic preference order for the equal-strength / incomparable tiebreak
# in position-winner selection. The linux precompute profile is preferred. A new
# authoritative canonical profile MUST be added here so its rows tiebreak
# deterministically (and so the backfill pre-filter includes them). This is NOT a
# strength ranking — strictly-stronger search always wins through
# ``compare_search_strength`` regardless of this order.
AUTHORITATIVE_PROFILE_PRIORITY = (
    "canonical-sf18-depth24-linux-v1",
    "canonical-sf18-depth24-v1",
)


def _parse_engine_version(version: str | None) -> int | None:
    """Parse the leading integer of an engine_version (e.g. ``"18"`` -> 18).

    Returns ``None`` when the value is missing or has no leading integer, so the
    comparator can fall back to raw-string equality rather than mis-ranking.
    """
    if version is None:
        return None
    match = re.match(r"\d+", str(version))
    return int(match.group()) if match else None


def compare_search_strength(a: Profile, b: Profile) -> StrengthComparison:
    """Rank two profiles by search strength, guarded for comparability.

    INCOMPARABLE unless every :data:`_STRENGTH_INVARIANT_FIELDS` value is equal
    (same nets, multipv, analyzer protocol, engine, search-limit *type*). When
    comparable: compare ``engine_version`` (leading int; if either is non-numeric
    and the raw versions differ -> INCOMPARABLE), then ``search_limit_value``
    (higher = stronger). Equal on both axes -> EQUAL.
    """
    for f in _STRENGTH_INVARIANT_FIELDS:
        if getattr(a, f) != getattr(b, f):
            return StrengthComparison.INCOMPARABLE

    a_ver = _parse_engine_version(a.engine_version)
    b_ver = _parse_engine_version(b.engine_version)
    if a_ver is not None and b_ver is not None:
        if a_ver > b_ver:
            return StrengthComparison.A_STRONGER
        if b_ver > a_ver:
            return StrengthComparison.B_STRONGER
        # Equal engine version -> fall through to the search-limit comparison.
    elif a.engine_version != b.engine_version:
        # At least one version is non-numeric and they differ: cannot rank.
        return StrengthComparison.INCOMPARABLE

    a_lim, b_lim = a.search_limit_value, b.search_limit_value
    if a_lim == b_lim:
        return StrengthComparison.EQUAL
    if isinstance(a_lim, int) and isinstance(b_lim, int):
        return (
            StrengthComparison.A_STRONGER
            if a_lim > b_lim
            else StrengthComparison.B_STRONGER
        )
    # One limit is missing while the other is set: not safely rankable.
    return StrengthComparison.INCOMPARABLE
