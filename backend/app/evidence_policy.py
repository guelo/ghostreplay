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
    (measured-strength steps 4-5 return INCOMPARABLE here; g-mk1d fills them);
  * the base capability + overlay tables and :func:`has_capability`;
  * fail-closed registry-load assertions.

Deliberately OUT of scope (stable API laid, not wired): read/reuse grants beyond
DISPLAY_OVERLAY (g-v21l), dynamic identity validators and measured-strength
comparison (g-mk1d), and the cross-grain authority rule (g-6xc3).

Dependency tier: imports only :mod:`app.analysis_profiles` and
:mod:`app.evidence_contracts` (same tier as ``analysis_trust``; NO ORM). The
comparator duck-types its row arguments (``effective_profile_id()`` /
``is_effectively_authoritative()``) so it never imports ``analysis_cache_policy``
(which imports this module).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from app.analysis_profiles import (
    ANALYZER_PROTOCOL_VERSION,
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
    BROWSER_ANALYSIS_PROFILE_ID,
    BROWSER_ANALYZER_PROTOCOL_VERSION,
    BROWSER_PROFILE_ID,
    BROWSER_VISIBLE_MULTIPV_PROTOCOL_VERSION,
    CANONICAL_LINUX_PROFILE_ID,
    CANONICAL_PROFILE_ID,
    IDENTITY_FIELDS,
    JEFFML_PROFILE_ID,
    Profile,
    get_profile,
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


def verify_identity(source: object) -> bool:
    """True when a row's stored identity metadata matches its claimed profile.

    THE single implementation, replacing the five duplicated exact-equality loops
    (``analysis_cache_policy``, ``analysis_trust``, ``api/analysis``,
    ``analysis_cache_audit``, ``position_analysis_repo``). ``source`` may be a
    projection ``dict`` or an ORM row.

    For every profile registered today ``dynamic_fields`` is empty, so this is
    exact equality over all 12 :data:`IDENTITY_FIELDS` — byte-identical to the old
    checks (a legacy all-``None`` ``browser-game-v1`` row still verifies because
    ``None == None``). g-mk1d attaches per-field validators via ``dynamic_fields``
    without introducing a second verifier.
    """
    profile = get_profile(_identity_value(source, "analysis_profile_id"))
    if profile is None:
        return False
    dynamic = profile.dynamic_fields
    for f in IDENTITY_FIELDS:
        if f in dynamic:
            # g-mk1d: a declared-dynamic field is validated by a per-field rule,
            # not exact equality. No profile declares any today.
            continue
        if _identity_value(source, f) != getattr(profile, f):
            return False
    return True


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
    comparator never imports it (avoiding a cycle).
    """

    def effective_profile_id(self) -> str | None: ...

    def is_effectively_authoritative(self) -> bool: ...


class Supersession(Enum):
    A_SUPERSEDES = "a_supersedes"
    B_SUPERSEDES = "b_supersedes"
    INCOMPARABLE = "incomparable"


@dataclass(frozen=True)
class EvidenceComparison:
    outcome: Supersession
    kind: EdgeKind | None  # the edge kind justifying supersession, if any


_INCOMPARABLE = EvidenceComparison(Supersession.INCOMPARABLE, None)


def compare_evidence_rows(a: RowView, b: RowView) -> EvidenceComparison:
    """Which of two VALID rows supersedes the other, and by which edge kind.

    Precondition: both rows already passed the validity gate (invalid rows never
    reach the comparator). Steps implemented in this bead:

      2. AUTHORITY barrier — exactly one side effectively authoritative ⇒ that
         side supersedes (``AUTHORITY``).
      3. explicit EDGES — a registered (winner, loser) edge ⇒ winner supersedes
         (``PROTOCOL_CORRECTION`` or ``TIER_BASELINE``), directional.

    Steps 4-5 (measured search strength for unequal non-edged rows) return
    INCOMPARABLE here; g-mk1d fills them in. The completeness (contract/superset)
    gate is the CALLER's responsibility — this returns only the ordering + reason.
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

    # Steps 4-5 (measured strength) — g-mk1d. Unequal, non-edged ⇒ INCOMPARABLE.
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


class OverlayMode(Enum):
    ALWAYS = "always"  # overlay whenever DISPLAY_OVERLAY is held
    NEVER = "never"


# Per-profile capability grants (D5 base). Canonical holds all eight;
# browser-analysis-multipv-v2 and the retired browser-analysis-v1 hold only the
# retirement-surviving DISPLAY_OVERLAY; browser-game / jeffml / legacy hold none.
# The read/reuse call-site wiring for the other seven stays on
# ``_effectively_authoritative`` until g-v21l; this bead wires only DISPLAY_OVERLAY.
CAPABILITY_GRANTS: dict[str, frozenset[Capability]] = {
    CANONICAL_PROFILE_ID: ALL_CAPABILITIES,
    CANONICAL_LINUX_PROFILE_ID: ALL_CAPABILITIES,
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID: frozenset({Capability.DISPLAY_OVERLAY}),
    BROWSER_ANALYSIS_PROFILE_ID: frozenset({Capability.DISPLAY_OVERLAY}),
    BROWSER_PROFILE_ID: frozenset(),
    JEFFML_PROFILE_ID: frozenset(),
}

OVERLAY_MODE: dict[str, OverlayMode] = {
    CANONICAL_PROFILE_ID: OverlayMode.ALWAYS,
    CANONICAL_LINUX_PROFILE_ID: OverlayMode.ALWAYS,
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID: OverlayMode.ALWAYS,
    BROWSER_ANALYSIS_PROFILE_ID: OverlayMode.ALWAYS,
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

    for pid, profile in profiles.items():
        grants = CAPABILITY_GRANTS.get(pid, frozenset())
        if profile.authoritative and profile.active:
            # Every authoritative+active profile holds all eight capabilities.
            if grants != ALL_CAPABILITIES:
                raise ValueError(
                    f"authoritative profile {pid!r} must hold all capabilities"
                )
        else:
            # Non-authoritative profiles hold nothing unless explicitly granted;
            # the internally-inconsistent retired browser-analysis-v1 may never
            # hold an active-required (non-retirement-surviving) capability.
            if pid == BROWSER_ANALYSIS_PROFILE_ID and not (
                grants <= RETIREMENT_SURVIVING
            ):
                raise ValueError(
                    "browser-analysis-v1 may not hold an active-required capability"
                )


_assert_registry_consistent()
