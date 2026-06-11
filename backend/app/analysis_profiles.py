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
    authoritative: bool  # may this profile replace legacy/other-family rows?
    active: bool = True  # current-for-resolution; retired profiles are inactive
    # Legacy single-column network identity, retired from the identity set.
    network_id: str | None = None
    dominates: frozenset[str] = field(default_factory=frozenset)


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
BROWSER_PROFILE_ID = "browser-game-v1"
JEFFML_PROFILE_ID = "jeffml-scores-v1"


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
        active=m["active"],
        dominates=frozenset(m.get("dominates", ())),
    )


_CANONICAL = _load_manifest(CANONICAL_PROFILE_ID)

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

_REGISTRY: dict[str, Profile] = {
    p.profile_id: p for p in (_CANONICAL, _BROWSER, _JEFFML)
}


def get_profile(profile_id: str | None) -> Profile | None:
    """Return the registered profile, or ``None`` for unknown/legacy ids."""
    if profile_id is None:
        return None
    return _REGISTRY.get(profile_id)


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
