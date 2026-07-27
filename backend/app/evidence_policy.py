"""Shared browser-evidence policy (g-browser-policy-v2 D1, minimal slice).

The approved policy separates five concerns that used to be tangled across
profile identity, ``authoritative``/``replacement_eligible`` flags, explicit
``dominates`` edges, evidence contracts, and read-trust gates:

    identity  — does a row's stored metadata match its claimed profile?
    protocol  — is the producer's analyzer internally consistent?
    contract  — is the evidence-shape complete? (``evidence_contracts``)
    comparison — which of two valid rows supersedes the other, and why?
    capability — which consumers may reuse a given (identity-verified) row?

This module lands ONLY the slice the fixed visible-MultiPV producer
(g-reuse-d21-search) depends on:

  * one shared :func:`verify_identity` (replacing five duplicated exact-equality
    checks), with an empty per-profile ``dynamic_fields`` seam for g-mk1d;
  * the PROTOCOLS / EDGES / BUILD_EQUIVALENCE registries;
  * the authority + explicit-edge steps of :func:`compare_evidence_rows`
    (measured-strength steps 4-5 returned INCOMPARABLE here; g-mk1d fills them);
  * the base capability + overlay tables and :func:`has_capability`;
  * fail-closed registry-load assertions.

g-mk1d then EXTENDS it with the declared-dynamic half:

  * :data:`DYNAMIC_FIELD_VALIDATORS` — the per-field rules ``verify_identity``
    runs for a profile's ``dynamic_fields`` instead of exact equality;
  * :func:`validate_browser_provenance` — the same rules applied to an untrusted
    wire payload, returning the validated dynamic subset or the ``None`` sentinel;
  * measured-strength steps 4-5 of :func:`compare_evidence_rows`, delegated to
    :func:`compare_row_strength` and guarded by row-level scoring semantics;
  * :data:`OverlayMode.REQUIRES_COMPARISON` for rows that may only overlay when
    provably STRONGER than a live operand.

Deliberately OUT of scope (stable API laid, not wired): read/reuse grants beyond
DISPLAY_OVERLAY (g-v21l) and the cross-grain authority rule (g-6xc3).

Dependency tier: imports only :mod:`app.analysis_profiles` and
:mod:`app.evidence_contracts` (same tier as ``analysis_trust``; NO ORM). The
comparator duck-types its row arguments (``effective_profile_id()`` /
``is_effectively_authoritative()``) so it never imports ``analysis_cache_policy``
(which imports this module).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from app.analysis_profiles import (
    ANALYZER_PROTOCOL_VERSION,
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
    BROWSER_ANALYSIS_PROFILE_ID,
    BROWSER_ANALYZER_PROTOCOL_VERSION,
    BROWSER_GAME_V2_DYNAMIC_FIELDS,
    BROWSER_GAME_V2_PROFILE_ID,
    BROWSER_PROFILE_ID,
    BROWSER_VISIBLE_MULTIPV_PROTOCOL_VERSION,
    CANONICAL_LINUX_PROFILE_ID,
    CANONICAL_PROFILE_ID,
    IDENTITY_FIELDS,
    JEFFML_PROFILE_ID,
    Profile,
    StrengthComparison,
    get_profile,
    parse_engine_version,
    list_profiles,
)


# --- D2.1: the single identity verifier ----------------------------------------


def _identity_value(source: object, name: str):
    """Read an identity field from a dict row OR an ORM row uniformly.

    Plain projection dicts use ``data.get(name)``; the ``api/analysis`` call-site
    passes an ``AnalysisCache`` ORM row and reads ``getattr(row, name)``. Both
    resolve a missing attribute to ``None`` so behavior is byte-identical to the
    historical per-call-site checks.
    """
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


# --- D2: declared-dynamic per-field validators (g-mk1d) ------------------------
#
# A declared-dynamic identity field is self-reported by the producer, so it can
# never be checked by equality against the registry. It is checked for SHAPE and
# RANGE instead. These bounds are deliberately generous: their job is to keep a
# malformed/garbage claim out of the identity columns (where it would corrupt the
# strength comparator), NOT to authenticate the producer. Per the D2 threat model
# these values are self-reported diagnostics — a modified client can always report
# a syntactically valid identity, and that only reorders NON-authoritative browser
# rows within the browser tier. It can never cross the authority barrier, earn a
# capability, or touch ``position_analysis``.

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_EVAL_FILE_ID = re.compile(r"^[^:]+:[0-9a-f]{64}$")

# Per-``search_limit_type`` bounds for ``search_limit_value``. The value's meaning
# (plies / nodes / milliseconds) depends on the type, so one flat range would be
# meaningless.
SEARCH_LIMIT_BOUNDS: dict[str, tuple[int, int]] = {
    "depth": (1, 60),
    "nodes": (1, 1_000_000_000),
    "movetime": (1, 3_600_000),  # milliseconds
}


def _is_int(value: object) -> bool:
    """True for a real integer. JSON booleans are REJECTED.

    ``isinstance(True, int)`` is True in Python, so a JSON ``true`` would silently
    satisfy an integer bound as ``1``. A float is accepted only when it is
    integral (JSON has one number type, so ``17.0`` is a legitimate wire encoding
    of 17); ``17.5`` is not.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value.is_integer()
    return False


def _bounded_int(lo: int, hi: int):
    def check(value: object) -> bool:
        return _is_int(value) and lo <= int(value) <= hi

    return check


def _valid_engine_version(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 64


def _valid_engine_build(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX64.match(value))


def _valid_eval_file_id(value: object) -> bool:
    return isinstance(value, str) and bool(_EVAL_FILE_ID.match(value))


def _valid_search_limit_type(value: object) -> bool:
    return value in SEARCH_LIMIT_BOUNDS


# The per-field rules :func:`verify_identity` runs for a profile's declared
# ``dynamic_fields``. ``search_limit_value`` is checked here for basic integrality
# and again, against its TYPE's bounds, by the cross-field rule below.
DYNAMIC_FIELD_VALIDATORS: dict[str, object] = {
    "engine_version": _valid_engine_version,
    "engine_build": _valid_engine_build,
    "eval_file_id": _valid_eval_file_id,
    "search_limit_type": _valid_search_limit_type,
    "search_limit_value": _is_int,
    "threads": _bounded_int(1, 32),
    "hash_mb": _bounded_int(1, 2048),
}


def _search_limit_in_bounds(limit_type: object, limit_value: object) -> bool:
    """Cross-field rule: ``search_limit_value`` must fit its ``search_limit_type``."""
    bounds = SEARCH_LIMIT_BOUNDS.get(limit_type) if isinstance(limit_type, str) else None
    if bounds is None:
        return False
    lo, hi = bounds
    return _is_int(limit_value) and lo <= int(limit_value) <= hi


def _dynamic_values_valid(values: dict) -> bool:
    """True when every declared-dynamic value present in ``values`` is well-formed.

    Requires every key it is given to be NON-null and to pass its per-field rule,
    plus the cross-field search-limit bound. Callers decide WHICH fields must be
    present (``verify_identity`` uses the profile's declared set;
    :func:`validate_browser_provenance` uses browser-game-v2's).
    """
    for name, value in values.items():
        if value is None:
            return False
        check = DYNAMIC_FIELD_VALIDATORS.get(name)
        if check is None:
            return False
        if not check(value):
            return False
    if "search_limit_type" in values or "search_limit_value" in values:
        if not _search_limit_in_bounds(
            values.get("search_limit_type"), values.get("search_limit_value")
        ):
            return False
    return True


def verify_identity(source: object) -> bool:
    """True when a row's stored identity metadata matches its claimed profile.

    THE single implementation, replacing the five duplicated exact-equality loops
    (``analysis_cache_policy``, ``analysis_trust``, ``api/analysis``,
    ``analysis_cache_audit``, ``position_analysis_repo``). ``source`` may be a
    projection ``dict`` or an ORM row.

    For a profile with an EMPTY ``dynamic_fields`` (every profile but
    ``browser-game-v2``) this is exact equality over all 12
    :data:`IDENTITY_FIELDS` — byte-identical to the old checks (a legacy all-
    ``None`` ``browser-game-v1`` row still verifies because ``None == None``).

    For a DECLARED-DYNAMIC profile (g-mk1d) the fixed fields are still exact-
    equality checked — including ``profile_manifest_digest``, so the server-stamped
    fixed half cannot be forged from the wire — while each declared-dynamic field
    must be NON-null and pass its :data:`DYNAMIC_FIELD_VALIDATORS` rule.
    """
    profile = get_profile(_identity_value(source, "analysis_profile_id"))
    if profile is None:
        return False
    dynamic = profile.dynamic_fields
    for f in IDENTITY_FIELDS:
        if f in dynamic:
            # A declared-dynamic field is validated by a per-field rule below,
            # never by equality against the (None) registry value.
            continue
        if _identity_value(source, f) != getattr(profile, f):
            return False
    if not dynamic:
        return True
    return _dynamic_values_valid({f: _identity_value(source, f) for f in dynamic})


@dataclass(frozen=True)
class ProvenanceFields:
    """The validated declared-dynamic subset a browser client may self-report."""

    values: dict


def validate_browser_provenance(raw: object) -> ProvenanceFields | None:
    """Validate an untrusted per-row ``provenance`` payload (g-mk1d §2.2 step 5).

    The wire field is typed ``Any`` on purpose (see ``SessionMoveInput``): a
    constrained Pydantic shape would 422 the ENTIRE ``/moves`` batch on a single
    malformed row, breaking the per-row-degradation contract. So EVERY check —
    starting with "is this even a mapping?" — happens here, per row.

    Returns the validated dynamic subset, or ``None`` — the sentinel meaning
    "malformed, drop only this row's cache evidence". Absent/``null`` provenance
    must not reach this function at all (the caller stamps ``browser-game-v1``).

    Rejects, among others: a non-mapping payload (list/str/int/float/bool), a
    missing or null required field, an unknown ``search_limit_type``, an
    out-of-range ``search_limit_value`` for its type, a non-hex ``engine_build``,
    a malformed ``eval_file_id``, and a JSON boolean where an integer is required.
    """
    # First: mapping shape. This is the check Pydantic used to do implicitly.
    if not isinstance(raw, dict):
        return None
    # Every declared-dynamic field must be present (missing reads as None below).
    values = {f: raw.get(f) for f in BROWSER_GAME_V2_DYNAMIC_FIELDS}
    if not _dynamic_values_valid(values):
        return None
    # Normalize integral floats to int so the persisted identity columns and the
    # strength comparator always see real integers.
    normalized = {
        f: (int(v) if f in _DYNAMIC_INT_FIELDS else v) for f, v in values.items()
    }
    return ProvenanceFields(values=normalized)


_DYNAMIC_INT_FIELDS = frozenset({"search_limit_value", "threads", "hash_mb"})


# --- D3: protocol registry ------------------------------------------------------


@dataclass(frozen=True)
class AnalyzerProtocol:
    """A producer's analyzer protocol semantics.

    ``internally_consistent`` records whether the protocol's best/played facts are
    guaranteed self-consistent by construction. ``browser-analyzer-v1`` is NOT
    (its hidden root search and independent post-move searches can disagree —
    g-kgiq); ``browser-visible-multipv-v1`` IS (best and played are two lines of
    the SAME completed request).
    """

    version: str | None
    internally_consistent: bool
    description: str


PROTOCOLS: dict[str | None, AnalyzerProtocol] = {
    ANALYZER_PROTOCOL_VERSION: AnalyzerProtocol(
        version=ANALYZER_PROTOCOL_VERSION,
        internally_consistent=True,
        description="Canonical Stockfish analyzer (root + post-move, consistent).",
    ),
    BROWSER_ANALYZER_PROTOCOL_VERSION: AnalyzerProtocol(
        version=BROWSER_ANALYZER_PROTOCOL_VERSION,
        internally_consistent=False,
        description=(
            "Retired hidden browser analyzer: root best-move search plus "
            "independent post-played and post-best searches; the post-move scores "
            "are independent of the hidden root ordering and can contradict it "
            "(g-kgiq)."
        ),
    ),
    BROWSER_VISIBLE_MULTIPV_PROTOCOL_VERSION: AnalyzerProtocol(
        version=BROWSER_VISIBLE_MULTIPV_PROTOCOL_VERSION,
        internally_consistent=True,
        description=(
            "Completed unrestricted visible depth-21 MultiPV-3 search; best and "
            "played are two lines of the SAME request, no child searches."
        ),
    ),
    None: AnalyzerProtocol(
        version=None,
        internally_consistent=False,
        description="Unknown / legacy producer (no declared protocol).",
    ),
}


# --- D3: explicit dominance edges ----------------------------------------------


class EdgeKind(Enum):
    """Why a winner profile supersedes a loser profile for an exact key."""

    AUTHORITY = "authority"  # canonical outranks any non-authoritative row
    PROTOCOL_CORRECTION = "protocol_correction"  # truthful protocol fixes a defective one
    TIER_BASELINE = "tier_baseline"  # a deeper same-family tier replaces a shallower one


@dataclass(frozen=True)
class Edge:
    winner_profile_id: str
    loser_profile_id: str
    kind: EdgeKind
    rationale: str


EDGES: tuple[Edge, ...] = (
    # AUTHORITY — canonical depth-24 outranks every non-authoritative producer.
    Edge(CANONICAL_PROFILE_ID, BROWSER_PROFILE_ID, EdgeKind.AUTHORITY, "canonical > browser game"),
    Edge(CANONICAL_PROFILE_ID, BROWSER_ANALYSIS_PROFILE_ID, EdgeKind.AUTHORITY, "canonical > hidden analysis"),
    Edge(CANONICAL_PROFILE_ID, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, EdgeKind.AUTHORITY, "canonical > visible-multipv"),
    Edge(CANONICAL_PROFILE_ID, JEFFML_PROFILE_ID, EdgeKind.AUTHORITY, "canonical > jeffml"),
    Edge(CANONICAL_LINUX_PROFILE_ID, BROWSER_PROFILE_ID, EdgeKind.AUTHORITY, "canonical(linux) > browser game"),
    Edge(CANONICAL_LINUX_PROFILE_ID, BROWSER_ANALYSIS_PROFILE_ID, EdgeKind.AUTHORITY, "canonical(linux) > hidden analysis"),
    Edge(CANONICAL_LINUX_PROFILE_ID, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, EdgeKind.AUTHORITY, "canonical(linux) > visible-multipv"),
    Edge(CANONICAL_LINUX_PROFILE_ID, JEFFML_PROFILE_ID, EdgeKind.AUTHORITY, "canonical(linux) > jeffml"),
    # TIER_BASELINE — the shipped depth-tier baseline (g-cache-stronger-evals).
    Edge(BROWSER_ANALYSIS_PROFILE_ID, BROWSER_PROFILE_ID, EdgeKind.TIER_BASELINE, "hidden analysis > d17 game"),
    # PROTOCOL_CORRECTION — the truthful visible-root protocol supersedes the
    # defective hidden protocol for an exact key, INDEPENDENT of numeric strength.
    Edge(
        BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
        BROWSER_ANALYSIS_PROFILE_ID,
        EdgeKind.PROTOCOL_CORRECTION,
        "visible-multipv corrects the internally-inconsistent hidden protocol",
    ),
    # TIER_BASELINE — preserve the motivating easy-win path (upgrade legacy d17).
    Edge(
        BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
        BROWSER_PROFILE_ID,
        EdgeKind.TIER_BASELINE,
        "visible-multipv > d17 game",
    ),
)


def _edge(winner: str | None, loser: str | None) -> Edge | None:
    if winner is None or loser is None:
        return None
    for edge in EDGES:
        if edge.winner_profile_id == winner and edge.loser_profile_id == loser:
            return edge
    return None


# --- D4 seed: build equivalence families ---------------------------------------
#
# Two builds are search-strength-comparable only within one family. The two
# canonical platform binaries (x86-64 vs x86-64-bmi2) form the one seed family so
# canonical-vs-canonical comparability is preserved; arbitrary browser builds are
# not depth-comparable across builds. The full comparator strength math is g-mk1d.
BUILD_EQUIVALENCE: tuple[frozenset[str], ...] = (
    frozenset({CANONICAL_PROFILE_ID, CANONICAL_LINUX_PROFILE_ID}),
)


def same_build_family(profile_a: str | None, profile_b: str | None) -> bool:
    if profile_a is None or profile_b is None:
        return False
    if profile_a == profile_b:
        return True
    return any(
        profile_a in family and profile_b in family for family in BUILD_EQUIVALENCE
    )


# --- comparison -----------------------------------------------------------------


class RowView(Protocol):
    """The minimal row surface :func:`compare_evidence_rows` needs.

    ``CacheRow`` (analysis_cache_policy) satisfies this structurally, so the
    comparator never imports it (avoiding a cycle). ``identity_values()`` returns
    the row's own :data:`IDENTITY_FIELDS` values — the measured-strength steps
    compare ROW values (not profile values) because a declared-dynamic profile's
    registry values are ``None``.
    """

    def effective_profile_id(self) -> str | None: ...

    def is_effectively_authoritative(self) -> bool: ...

    def identity_values(self) -> dict: ...


class Supersession(Enum):
    """Whether one row outranks another, at the GRAIN that decided it (D4).

    The two grains are deliberately distinct, not collapsed:

      * ``A_SUPERSEDES`` / ``B_SUPERSEDES`` — a CATEGORICAL win from the authority
        barrier (step 2) or a registered EDGE (step 3). Independent of any number
        either row measured: a depth-30 browser row still loses to canonical.
      * ``A_STRONGER`` / ``B_STRONGER`` / ``EQUAL`` — a MEASURED ordering of two
        comparably-scored searches (steps 4-5). Only meaningful once the semantic
        compatibility guard has passed.

    Keeping them apart is what lets a caller report a measured cross-profile
    replacement as ``strength_replace`` rather than ``dominates_replace`` (D9).
    Callers whose local decision genuinely treats outcomes alike are free to
    collapse them — e.g. Rule 5 keeps the stored row for ``B_STRONGER``, ``EQUAL``
    and ``INCOMPARABLE`` alike — but the collapse is theirs to choose, not this
    module's to impose.
    """

    A_SUPERSEDES = "a_supersedes"
    B_SUPERSEDES = "b_supersedes"
    A_STRONGER = "a_stronger"
    B_STRONGER = "b_stronger"
    EQUAL = "equal"
    INCOMPARABLE = "incomparable"


@dataclass(frozen=True)
class EvidenceComparison:
    outcome: Supersession
    kind: EdgeKind | None  # the edge kind justifying supersession, if any


_INCOMPARABLE = EvidenceComparison(Supersession.INCOMPARABLE, None)


# --- D4 steps 4-5: measured search strength (g-mk1d) ---------------------------
#
# Row-level twin of ``analysis_profiles.compare_search_strength``. The profile-level
# function cannot serve a DECLARED-DYNAMIC profile: browser-game-v2's registry
# values for the seven dynamic fields are all ``None``, so every pair of v2 rows
# would compare EQUAL. These steps therefore read the ROW's stored identity
# columns.
#
# Step 4 (SEMANTIC COMPATIBILITY GUARD): two rows are strength-rankable only when
# they measured the position under the same rules — same engine, same net(s), same
# MultiPV, same analyzer protocol, same search-limit TYPE — and compatible builds.
# Any difference (including a ``None`` on either side, as on a legacy all-``None``
# browser-game-v1 row) means UNKNOWN, not weaker → INCOMPARABLE.
_ROW_STRENGTH_INVARIANT_FIELDS = (
    "engine_name",
    "eval_file_id",
    "eval_file_small_id",
    "multipv",
    "analyzer_protocol_version",
    "search_limit_type",
)

# Invariants that must additionally be NON-null for a rankable comparison. Two
# rows that both report ``eval_file_id=None`` (legacy browser-game-v1) agree on the
# equality test yet describe an UNKNOWN network — ranking them by depth would be
# exactly the false ordering D7.1 forbids.
_ROW_STRENGTH_REQUIRED_FIELDS = (
    "engine_name",
    "eval_file_id",
    "analyzer_protocol_version",
    "search_limit_type",
)


def _self_reports_build(profile_id: str | None) -> bool:
    """True when ``engine_build`` is a DECLARED-DYNAMIC (self-reported) field."""
    profile = get_profile(profile_id)
    return profile is not None and "engine_build" in profile.dynamic_fields


def _builds_compatible(a: RowView, b: RowView, a_vals: dict, b_vals: dict) -> bool:
    """True when two rows' engine builds are search-strength-comparable.

    Identical ``engine_build`` values are trivially compatible. Differing builds
    are compatible ONLY inside a registered :data:`BUILD_EQUIVALENCE` family — the
    canonical x86-64 vs x86-64-bmi2 platform pair, which are two distinct PROFILES
    of one verified engine.

    That family lookup is keyed by profile id, which makes it meaningless for a
    declared-dynamic profile: every device shares the id ``browser-game-v2`` while
    self-reporting its OWN build, so a shared id proves nothing about the binary.
    Differing self-reported builds are therefore incomparable, full stop.
    """
    a_build, b_build = a_vals.get("engine_build"), b_vals.get("engine_build")
    if a_build is not None and a_build == b_build:
        return True
    a_eff, b_eff = a.effective_profile_id(), b.effective_profile_id()
    if _self_reports_build(a_eff) or _self_reports_build(b_eff):
        return False
    return same_build_family(a_eff, b_eff)


def _row_identity_values(row: RowView) -> dict | None:
    getter = getattr(row, "identity_values", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:  # pragma: no cover - defensive
        return None


def compare_row_strength(a: RowView, b: RowView) -> StrengthComparison:
    """Rank two VALID rows by measured search strength, guarded for comparability.

    This is steps 4-5 ALONE. :func:`compare_evidence_rows` runs the categorical
    steps 2-3 (authority, explicit edges) first and only then delegates here, so the
    two agree name-for-name on the measured grain — A_STRONGER / B_STRONGER / EQUAL
    / INCOMPARABLE — and never disagree about a pair of rows.

    Call this directly when the two rows are known to share an effective profile
    (Rule 2a), where steps 2-3 cannot fire and the extra dispatch is noise. Both
    keep EQUAL separate from INCOMPARABLE: equal strength is idempotent, unrankable
    strength is a refusal, and a caller that treats them alike should say so itself.
    """
    a_vals, b_vals = _row_identity_values(a), _row_identity_values(b)
    if a_vals is None or b_vals is None:
        return StrengthComparison.INCOMPARABLE

    # Step 4: semantic compatibility guard.
    for f in _ROW_STRENGTH_INVARIANT_FIELDS:
        if a_vals.get(f) != b_vals.get(f):
            return StrengthComparison.INCOMPARABLE
    for f in _ROW_STRENGTH_REQUIRED_FIELDS:
        if a_vals.get(f) is None:
            return StrengthComparison.INCOMPARABLE
    if not _builds_compatible(a, b, a_vals, b_vals):
        return StrengthComparison.INCOMPARABLE

    # Step 5: engine version (leading int), then the search limit.
    a_ver = parse_engine_version(a_vals.get("engine_version"))
    b_ver = parse_engine_version(b_vals.get("engine_version"))
    if a_ver is not None and b_ver is not None:
        if a_ver > b_ver:
            return StrengthComparison.A_STRONGER
        if b_ver > a_ver:
            return StrengthComparison.B_STRONGER
    elif a_vals.get("engine_version") != b_vals.get("engine_version"):
        # At least one version is non-numeric and they differ: cannot rank.
        return StrengthComparison.INCOMPARABLE

    a_lim, b_lim = a_vals.get("search_limit_value"), b_vals.get("search_limit_value")
    if not (isinstance(a_lim, int) and isinstance(b_lim, int)):
        return StrengthComparison.INCOMPARABLE
    if a_lim == b_lim:
        return StrengthComparison.EQUAL
    return (
        StrengthComparison.A_STRONGER
        if a_lim > b_lim
        else StrengthComparison.B_STRONGER
    )


def compare_evidence_rows(a: RowView, b: RowView) -> EvidenceComparison:
    """Which of two VALID rows supersedes the other, and by which edge kind.

    Precondition: both rows already passed the validity gate (invalid rows never
    reach the comparator). Steps implemented in this bead:

      2. AUTHORITY barrier — exactly one side effectively authoritative ⇒ that
         side supersedes (``AUTHORITY``).
      3. explicit EDGES — a registered (winner, loser) edge ⇒ winner supersedes
         (``PROTOCOL_CORRECTION`` or ``TIER_BASELINE``), directional.
      4-5. measured SEARCH STRENGTH — unequal, non-edged rows are ranked by
         :func:`compare_row_strength` (semantic guard, then engine version and
         search limit) and reported as ``A_STRONGER`` / ``B_STRONGER`` / ``EQUAL``
         with ``kind=None``, because no registered edge justifies a measured win.

    Steps 2-3 are CATEGORICAL and steps 4-5 are MEASURED; the outcome says which
    grain decided, so a caller can report the two differently (see
    :class:`Supersession`). The completeness (contract/superset) gate is the
    CALLER's responsibility — this returns only the ordering + reason.
    """
    a_auth = a.is_effectively_authoritative()
    b_auth = b.is_effectively_authoritative()

    # Step 2: authority barrier (exactly one side canonical-trusted).
    if a_auth and not b_auth:
        return EvidenceComparison(Supersession.A_SUPERSEDES, EdgeKind.AUTHORITY)
    if b_auth and not a_auth:
        return EvidenceComparison(Supersession.B_SUPERSEDES, EdgeKind.AUTHORITY)

    # Step 3: explicit non-authority edges (both non-canonical here).
    a_eff = a.effective_profile_id()
    b_eff = b.effective_profile_id()
    ab = _edge(a_eff, b_eff)
    if ab is not None:
        return EvidenceComparison(Supersession.A_SUPERSEDES, ab.kind)
    ba = _edge(b_eff, a_eff)
    if ba is not None:
        return EvidenceComparison(Supersession.B_SUPERSEDES, ba.kind)

    # Steps 4-5: measured strength (g-mk1d). DELEGATED, never re-derived — a second
    # ordering here would let one pair of rows compare differently depending on
    # which caller asked, which is exactly the single-comparison contract this
    # module exists to hold.
    #
    # Reported at the MEASURED grain (A_STRONGER / B_STRONGER / EQUAL), never
    # flattened into A_SUPERSEDES: those are reserved for the categorical wins
    # above, and a caller that cannot tell the two apart cannot report a measured
    # replacement as strength_replace (D9). ``kind`` is None because no registered
    # EDGE justifies a measured win.
    strength = compare_row_strength(a, b)
    if strength is StrengthComparison.A_STRONGER:
        return EvidenceComparison(Supersession.A_STRONGER, None)
    if strength is StrengthComparison.B_STRONGER:
        return EvidenceComparison(Supersession.B_STRONGER, None)
    if strength is StrengthComparison.EQUAL:
        return EvidenceComparison(Supersession.EQUAL, None)
    return _INCOMPARABLE


# --- D5 base: capabilities + overlay -------------------------------------------


class Capability(Enum):
    """A consumer surface a stored evidence row may serve."""

    DISPLAY_OVERLAY = "display_overlay"  # re-annotate a played move's MoveList label
    INTERACTIVE_ANALYSIS_REUSE = "interactive_analysis_reuse"
    GAME_ANALYSIS_REUSE = "game_analysis_reuse"
    POSITION_READ = "position_read"
    MOVE_READ = "move_read"
    DRILL_GRADE = "drill_grade"
    TREE_EVAL = "tree_eval"
    OPENING_EVIDENCE = "opening_evidence"


ALL_CAPABILITIES: frozenset[Capability] = frozenset(Capability)

# Capabilities that survive profile RETIREMENT (still granted after ``active``
# flips False). Only the display overlay survives: showing a stronger label over
# an already-displayed untrusted label is not a trust escalation, and retiring a
# defective protocol must not blank overlays already computed from its stored rows.
RETIREMENT_SURVIVING: frozenset[Capability] = frozenset({Capability.DISPLAY_OVERLAY})

# Capabilities a NON-AUTHORITATIVE row may satisfy only for a viewer who
# independently submitted a consistent tuple for it (g-v21l). Everything except
# DISPLAY_OVERLAY, which is purely presentational re-labeling, already ships
# unscoped, and whose cross-user question is filed separately (g-overlay-owner-scope).
#
# Stated as "every capability except ..." rather than an enumerated allow-list on
# purpose: a capability added later is owner-scoped by DEFAULT, which is the
# fail-closed direction. Effectively AUTHORITATIVE rows are unaffected — canonical
# evidence is server-produced and carries no associations at all.
OWNER_SCOPED: frozenset[Capability] = ALL_CAPABILITIES - RETIREMENT_SURVIVING


class OverlayMode(Enum):
    ALWAYS = "always"  # overlay whenever DISPLAY_OVERLAY is held
    # Overlay only when the stored row is provably STRONGER than a LIVE operand
    # (g-mk1d). A dynamic browser row's strength is per-device, so "it is a
    # browser-game row" no longer implies "it beats what the viewer already has";
    # the one-row seam cannot decide it and must return no upgrade.
    REQUIRES_COMPARISON = "requires_comparison"
    NEVER = "never"


# Per-profile capability grants. Canonical holds all eight.
#
# g-v21l adds five ACTIVE-REQUIRED grants to browser-analysis-multipv-v2 — the only
# non-canonical read-trust candidate in scope: it is active, non-authoritative,
# replacement-eligible, and fixed to the truthful visible-MultiPV protocol (lite net
# nn-9067e33176e8.nnue, depth 21, MultiPV 3). Its position and move facts may be
# displayed, reused, and consumed by the opening path — FOR THEIR OWN SUBMITTER ONLY
# (every capability but DISPLAY_OVERLAY is in :data:`OWNER_SCOPED`).
#
# DRILL_GRADE and TREE_EVAL stay UNGRANTED to everything non-canonical:
#   * DRILL_GRADE — a fabricated row must never grade a drill;
#   * TREE_EVAL — the tree resolves a SHARED graph node with no per-viewer identity,
#     so owner scoping is not expressible there (the same node would have to
#     evaluate differently per viewer). Withholding it keeps the tree ROOT eval and
#     the TRUSTED move tiers 1-2 canonical; it does NOT make the tree canonical-only,
#     because the untrusted tiers 3-4 are source-agnostic and sit outside this
#     capability system entirely. Revisiting it is g-tree-eval-browser.
#
# The retired browser-analysis-v1 and the browser-game profiles keep exactly the
# retirement-surviving DISPLAY_OVERLAY (or nothing); jeffml / legacy hold none. That
# is not a free choice for the defective ones: their protocols are internally
# inconsistent, so ``_assert_registry_consistent`` refuses to load a grant beyond
# RETIREMENT_SURVIVING to any of them.
CAPABILITY_GRANTS: dict[str, frozenset[Capability]] = {
    CANONICAL_PROFILE_ID: ALL_CAPABILITIES,
    CANONICAL_LINUX_PROFILE_ID: ALL_CAPABILITIES,
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID: frozenset(
        {
            Capability.DISPLAY_OVERLAY,
            Capability.POSITION_READ,
            Capability.MOVE_READ,
            Capability.INTERACTIVE_ANALYSIS_REUSE,
            Capability.GAME_ANALYSIS_REUSE,
            Capability.OPENING_EVIDENCE,
        }
    ),
    BROWSER_ANALYSIS_PROFILE_ID: frozenset({Capability.DISPLAY_OVERLAY}),
    # browser-game-v2 HOLDS the overlay capability but under REQUIRES_COMPARISON:
    # a stronger cross-user in-game diagnostic may re-label a weaker one, but only
    # against a live operand. browser-game-v1 (all-None, unknown strength) still
    # holds nothing.
    BROWSER_GAME_V2_PROFILE_ID: frozenset({Capability.DISPLAY_OVERLAY}),
    BROWSER_PROFILE_ID: frozenset(),
    JEFFML_PROFILE_ID: frozenset(),
}

# Per-profile overlay mode. Inert on its own: a mode is only ever consulted for a
# row whose profile already holds DISPLAY_OVERLAY, and the grant is only ever
# spendable through a non-NEVER mode — so ``_assert_registry_consistent`` pins the
# two tables to agree (g-overlay-mode-parity). WHICH non-NEVER mode a profile takes
# stays a policy choice; that it takes one at all does not.
OVERLAY_MODE: dict[str, OverlayMode] = {
    CANONICAL_PROFILE_ID: OverlayMode.ALWAYS,
    CANONICAL_LINUX_PROFILE_ID: OverlayMode.ALWAYS,
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID: OverlayMode.ALWAYS,
    BROWSER_ANALYSIS_PROFILE_ID: OverlayMode.ALWAYS,
    BROWSER_GAME_V2_PROFILE_ID: OverlayMode.REQUIRES_COMPARISON,
    BROWSER_PROFILE_ID: OverlayMode.NEVER,
    JEFFML_PROFILE_ID: OverlayMode.NEVER,
}


def overlay_mode(profile_id: str | None) -> OverlayMode:
    if profile_id is None:
        return OverlayMode.NEVER
    return OVERLAY_MODE.get(profile_id, OverlayMode.NEVER)


def has_capability(row: RowView, capability: Capability) -> bool:
    """True when an identity-verified row's profile grants ``capability``.

    ``has_capability = identity-verified AND granted AND (profile.active OR
    capability ∈ RETIREMENT_SURVIVING)``. ``effective_profile_id()`` already
    returns ``None`` unless the row is identity-verified, so a legacy/unidentified
    or profile-mismatched row holds nothing.

    The lifecycle disjunct below is the ONLY lifecycle enforcement: the invariant
    is split LOAD = protocol (:func:`_assert_registry_consistent`), USE = lifecycle
    (here), so retirement is deliberately not duplicated into the load assertion.
    """
    profile_id = row.effective_profile_id()
    if profile_id is None:
        return False
    profile = get_profile(profile_id)
    if profile is None:
        return False
    if capability not in CAPABILITY_GRANTS.get(profile_id, frozenset()):
        return False
    return profile.active or capability in RETIREMENT_SURVIVING


# --- fail-closed registry-load assertions --------------------------------------


def _assert_registry_consistent() -> None:
    profiles: dict[str, Profile] = {p.profile_id: p for p in list_profiles()}

    # EDGES reference only registered profiles.
    for edge in EDGES:
        if edge.winner_profile_id not in profiles:
            raise ValueError(f"EDGES winner {edge.winner_profile_id!r} not registered")
        if edge.loser_profile_id not in profiles:
            raise ValueError(f"EDGES loser {edge.loser_profile_id!r} not registered")

    # EDGES <-> Profile.dominates parity in BOTH directions (drift fails closed).
    for pid, profile in profiles.items():
        edge_losers = frozenset(
            e.loser_profile_id for e in EDGES if e.winner_profile_id == pid
        )
        if edge_losers != profile.dominates:
            raise ValueError(
                f"profile {pid!r} dominates {set(profile.dominates)} but EDGES "
                f"derive {set(edge_losers)}"
            )

    # Capability grants reference only registered profiles.
    for pid in CAPABILITY_GRANTS:
        if pid not in profiles:
            raise ValueError(f"CAPABILITY_GRANTS references unregistered {pid!r}")

    # Overlay modes are well-formed: a registered profile, a real OverlayMode.
    #
    # The type check is not belt-and-braces. The parity rule below keys on "is not
    # NEVER", so a value that is not an OverlayMode at all — the bare string
    # ``"always"``, a ``None`` — reads as ENABLED there and satisfies parity, while
    # every consumer compares with ``is`` against a member and reads the same value
    # as disabled. That is precisely the dead-grant state the parity rule exists to
    # catch, so the table's shape has to fail closed BEFORE parity is evaluated.
    # Note the asymmetry with CAPABILITY_GRANTS: a malformed capability value
    # already fails closed (it satisfies no ``in`` test, so parity rejects it),
    # whereas a malformed mode fails OPEN and needs this.
    for pid, mode in OVERLAY_MODE.items():
        if pid not in profiles:
            raise ValueError(f"OVERLAY_MODE references unregistered {pid!r}")
        if not isinstance(mode, OverlayMode):
            raise ValueError(
                f"OVERLAY_MODE for {pid!r} is not an OverlayMode: {mode!r}"
            )

    for pid, profile in profiles.items():
        grants = CAPABILITY_GRANTS.get(pid, frozenset())
        if profile.authoritative and profile.active:
            # Every authoritative+active profile holds all eight capabilities.
            if grants != ALL_CAPABILITIES:
                raise ValueError(
                    f"authoritative profile {pid!r} must hold all capabilities"
                )
        # Non-authoritative profiles hold nothing unless explicitly granted, and NO
        # profile — whatever its authority or lifecycle state — may hold an
        # ACTIVE-REQUIRED (non-retirement-surviving) capability unless its declared
        # analyzer protocol is internally consistent. Stated over the PROTOCOL
        # rather than a profile id because that is the actual reason
        # browser-analysis-v1 was capped (g-kgiq): a protocol whose best/played
        # facts can contradict each other cannot support read/reuse trust, and that
        # does not change when the profile is retired or un-retired. DELIBERATELY
        # outside the branch above, so a future authoritative-but-defective profile
        # cannot escape it (such a profile makes the two rules jointly
        # unsatisfiable — the registry is then unloadable, which is the correct
        # answer).
        protocol = PROTOCOLS.get(profile.analyzer_protocol_version)
        if protocol is None:
            raise ValueError(
                f"profile {pid!r} references an unknown analyzer protocol"
            )
        active_required = grants - RETIREMENT_SURVIVING
        if active_required and not protocol.internally_consistent:
            raise ValueError(
                f"profile {pid!r} uses internally inconsistent protocol "
                f"{protocol.version!r} and may not hold active-required "
                "capabilities"
            )

        # OVERLAY_MODE <-> DISPLAY_OVERLAY parity in BOTH directions. Drift here is
        # SILENT — each half is a no-op without the other, so neither direction
        # surfaces as a failure anywhere downstream:
        #   * a non-NEVER mode without the grant is a dead mode — ``has_capability``
        #     rejects the row before ``overlay_mode`` is ever read;
        #   * the grant without a non-NEVER entry is a dead grant — ``overlay_mode``
        #     defaults to NEVER for an unlisted profile, so the overlay gates return
        #     False for a row the grant says may overlay.
        # Both read as "this profile does not overlay", which is exactly what a
        # correctly-NEVER profile reads as, so only comparing the two tables can
        # tell an intended NEVER from a half-finished edit. Stated LAST in the loop
        # so a profile that also violates the protocol rule above reports that
        # instead: the parity mismatch is the weaker statement.
        # The loop above validated every present value, and the default is a member,
        # so ``mode`` is a real OverlayMode here — the ``is`` test means what it says.
        mode = OVERLAY_MODE.get(pid, OverlayMode.NEVER)
        granted_overlay = Capability.DISPLAY_OVERLAY in grants
        if (mode is not OverlayMode.NEVER) != granted_overlay:
            raise ValueError(
                f"profile {pid!r} overlay mode {mode.value!r} does not match its "
                f"DISPLAY_OVERLAY grant (granted={granted_overlay})"
            )


_assert_registry_consistent()
