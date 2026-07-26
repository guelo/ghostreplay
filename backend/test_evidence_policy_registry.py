"""Fail-closed registry-load assertions (g-parity-matrix-evpolicy, deliverable B).

``app.evidence_policy._assert_registry_consistent`` runs at module import and is
the only thing standing between a registry edit and a silently inconsistent
policy: EDGES that reference a profile nobody registered, an EDGES/``dominates``
pair that drifted apart, an ACTIVE-REQUIRED (non-``RETIREMENT_SURVIVING``)
capability granted to a profile whose analyzer protocol is not internally
consistent, a profile referencing an unregistered protocol, an authoritative
profile quietly stripped of a capability, or an ``OVERLAY_MODE`` entry that
disagrees with the profile's ``DISPLAY_OVERLAY`` grant in either direction. Each
of those must raise, and one test proves the enforcement really happens AT IMPORT
rather than only when called.

The capability rule is stated PROTOCOL-side and is independent of both authority
and lifecycle: retirement is enforced at USE time by ``has_capability``, which the
last section pins as the other half of the split.

Two mechanisms, deliberately different:

* most cases ``monkeypatch.setattr`` the module globals the assertion reads and
  call it directly — auto-restored, no import games;
* ONE case loads a FRESH module instance from ``evidence_policy.__file__`` under
  a drifted registry, which is the only way to observe the import-time call.

That fresh load is never an ``importlib.reload`` of the live module: a reload
rebuilds ``Capability`` / ``EdgeKind`` / ``Supersession`` / ``OverlayMode``, while
``analysis_cache_policy`` keeps references to the OLD enum members — so every
``is`` comparison in Rule 5 would silently go False for the rest of the session.
The fresh module is registered in ``sys.modules`` only for the duration of its
own execution (``dataclasses`` resolves ``cls.__module__`` during class creation
and fails otherwise) and popped again immediately.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys

import pytest

from app import analysis_profiles, evidence_policy
from app.analysis_cache_policy import (
    Decision,
    Reason,
    decide_analysis_cache_replacement,
    display_upgrade_eligible,
    project_cache_row,
)
from app.analysis_profiles import (
    ANALYZER_PROTOCOL_VERSION,
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
    BROWSER_ANALYSIS_PROFILE_ID,
    BROWSER_ANALYZER_PROTOCOL_VERSION,
    BROWSER_PROFILE_ID,
    BROWSER_VISIBLE_MULTIPV_PROTOCOL_VERSION,
    CANONICAL_PROFILE_ID,
    JEFFML_PROFILE_ID,
    stamp_profile_full,
)
from app.evidence_contracts import RESOLVER_COMPLETE_V2
from app.evidence_policy import (
    ALL_CAPABILITIES,
    CAPABILITY_GRANTS,
    EDGES,
    OVERLAY_MODE,
    PROTOCOLS,
    RETIREMENT_SURVIVING,
    Capability,
    Edge,
    EdgeKind,
    OverlayMode,
    _assert_registry_consistent,
    has_capability,
)

GHOST = "ghost-v9"

_EVIDENCE = {
    "fen_before": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "best_move_uci": "e2e4",
    "best_move_san": "e4",
    "best_line_uci": "e2e4 e7e5",
    "played_eval": 10,
    "best_eval": 30,
    "eval_delta": 20,
    "classification": "good",
}


def _row(profile_id: str):
    """A valid resolver-complete-v2 row stamped with ``profile_id``'s identity."""
    return project_cache_row(
        {
            "analysis_profile_id": profile_id,
            "evidence_contract_id": RESOLVER_COMPLETE_V2,
            **_EVIDENCE,
            **stamp_profile_full(profile_id),
        }
    )


# The ACTIVE-REQUIRED half of the enum — every capability that does NOT survive
# retirement, which is exactly the remainder the load rule keys on
# (``grants - RETIREMENT_SURVIVING``). Derived from RETIREMENT_SURVIVING rather
# than spelled out, so a capability added to either the enum or the surviving set
# lands on the right axis without editing this test.
ACTIVE_REQUIRED_CAPABILITIES = tuple(
    sorted(
        (c for c in Capability if c not in RETIREMENT_SURVIVING),
        key=lambda c: c.value,
    )
)

# Derived, NOT literal — a future defective producer is covered automatically. The
# values this reads are pinned by test_protocol_consistency_truth_table below, so a
# flipped ``internally_consistent`` shrinks this matrix loudly rather than silently.
INCONSISTENT_PROFILE_IDS = tuple(
    sorted(
        p.profile_id
        for p in analysis_profiles.list_profiles()
        if not PROTOCOLS[p.analyzer_protocol_version].internally_consistent
    )
)


def _profiles() -> tuple:
    return analysis_profiles.list_profiles()


def _with_profile(profile_id: str, **changes) -> tuple:
    """The live registry snapshot with ONE profile's fields replaced."""
    return tuple(
        dataclasses.replace(p, **changes) if p.profile_id == profile_id else p
        for p in _profiles()
    )


def _set_edges(monkeypatch, edges) -> None:
    monkeypatch.setattr(evidence_policy, "EDGES", tuple(edges))


def _set_grants(monkeypatch, **overrides) -> None:
    monkeypatch.setattr(
        evidence_policy, "CAPABILITY_GRANTS", {**CAPABILITY_GRANTS, **overrides}
    )


def _set_profiles(monkeypatch, profiles) -> None:
    monkeypatch.setattr(evidence_policy, "list_profiles", lambda: tuple(profiles))


def _set_overlay_modes(monkeypatch, **overrides) -> None:
    """``OVERLAY_MODE`` with ``overrides`` applied; a ``None`` value DELETES the entry.

    Deletion is a distinct case from ``OverlayMode.NEVER``, not a convenience:
    ``overlay_mode`` defaults an unlisted profile to NEVER, so an absent entry is
    the OTHER way a grant goes dead and the rule has to reject both.
    """
    modes = {**OVERLAY_MODE, **overrides}
    monkeypatch.setattr(
        evidence_policy,
        "OVERLAY_MODE",
        {pid: mode for pid, mode in modes.items() if mode is not None},
    )


# --- the two tables the capability rule reads --------------------------------------


@pytest.mark.parametrize(
    "version,expected",
    [
        (ANALYZER_PROTOCOL_VERSION, True),
        (BROWSER_ANALYZER_PROTOCOL_VERSION, False),
        (BROWSER_VISIBLE_MULTIPV_PROTOCOL_VERSION, True),
        (None, False),
    ],
    ids=["canonical", "hidden-browser", "visible-multipv", "unknown"],
)
def test_protocol_consistency_truth_table(version, expected):
    # The precondition for the derived matrix below: it reads the same values the
    # production rule trusts, so an accidental flip here would silently empty it.
    assert PROTOCOLS[version].internally_consistent is expected


def test_protocol_registry_has_exactly_the_pinned_protocols():
    # A protocol added without a truth-table entry fails here rather than silently
    # widening what may hold an active-required capability.
    assert set(PROTOCOLS) == {
        ANALYZER_PROTOCOL_VERSION,
        BROWSER_ANALYZER_PROTOCOL_VERSION,
        BROWSER_VISIBLE_MULTIPV_PROTOCOL_VERSION,
        None,
    }


def test_only_the_display_overlay_survives_retirement():
    """The OTHER table the capability rule reads, pinned for the same reason.

    ``RETIREMENT_SURVIVING`` is the subtrahend in BOTH production checks — the load
    rule's ``grants - RETIREMENT_SURVIVING`` remainder and ``has_capability``'s
    lifecycle disjunct — and ACTIVE_REQUIRED_CAPABILITIES derives from it. Widening
    it would therefore shrink the matrix below in silence (a capability moved out of
    the axis is a case that stops being tested, not a failure) while simultaneously
    letting an INACTIVE profile exercise the newly-surviving capability at use time.
    Pinned by value so that edit has to be deliberate.
    """
    assert RETIREMENT_SURVIVING == frozenset({Capability.DISPLAY_OVERLAY})


# --- control -------------------------------------------------------------------


def test_live_registry_is_consistent():
    # The control every mutation below is measured against: without it, a test
    # that "raises" proves nothing about the mutation.
    _assert_registry_consistent()


# --- EDGES <-> Profile.dominates parity ------------------------------------------


def test_removing_an_edge_that_dominates_still_claims_raises(monkeypatch):
    # canonical.dominates keeps jeffml while EDGES no longer derives it.
    _set_edges(
        monkeypatch,
        [
            e
            for e in EDGES
            if not (
                e.winner_profile_id == CANONICAL_PROFILE_ID
                and e.loser_profile_id == JEFFML_PROFILE_ID
            )
        ],
    )
    with pytest.raises(ValueError, match="but EDGES derive"):
        _assert_registry_consistent()


def test_adding_an_edge_no_dominates_set_backs_raises(monkeypatch):
    # The other direction: an edge exists that the winner's dominates omits.
    _set_edges(
        monkeypatch,
        EDGES
        + (
            Edge(
                BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
                JEFFML_PROFILE_ID,
                EdgeKind.TIER_BASELINE,
                "drift",
            ),
        ),
    )
    with pytest.raises(ValueError, match="but EDGES derive"):
        _assert_registry_consistent()


def test_shrinking_profile_dominates_with_edges_untouched_raises(monkeypatch):
    # Drift introduced from the PROFILE side rather than the EDGES side.
    _set_profiles(
        monkeypatch,
        _with_profile(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, dominates=frozenset()),
    )
    with pytest.raises(ValueError, match="but EDGES derive"):
        _assert_registry_consistent()


# --- EDGES endpoints must be registered ------------------------------------------


@pytest.mark.parametrize(
    "edge,expected",
    [
        (
            Edge(GHOST, BROWSER_PROFILE_ID, EdgeKind.TIER_BASELINE, "ghost winner"),
            f"EDGES winner '{GHOST}' not registered",
        ),
        (
            Edge(CANONICAL_PROFILE_ID, GHOST, EdgeKind.AUTHORITY, "ghost loser"),
            f"EDGES loser '{GHOST}' not registered",
        ),
    ],
    ids=["winner", "loser"],
)
def test_unregistered_edge_endpoint_raises(monkeypatch, edge, expected):
    _set_edges(monkeypatch, EDGES + (edge,))
    with pytest.raises(ValueError, match=expected):
        _assert_registry_consistent()


# --- capability grants ------------------------------------------------------------


def test_the_inconsistent_protocol_matrix_is_not_empty():
    # An empty matrix would make the parametrized test below vacuously green.
    assert INCONSISTENT_PROFILE_IDS
    assert ACTIVE_REQUIRED_CAPABILITIES


@pytest.mark.parametrize("profile_id", INCONSISTENT_PROFILE_IDS)
@pytest.mark.parametrize(
    "capability", ACTIVE_REQUIRED_CAPABILITIES, ids=lambda c: c.value
)
def test_inconsistent_protocol_may_not_hold_an_active_required_capability(
    monkeypatch, profile_id, capability
):
    # Both axes are DERIVED rather than sampled: every profile whose protocol is
    # internally inconsistent, crossed with every capability that does not survive
    # retirement. A new defective producer, or a new capability, is covered without
    # editing this test.
    _set_grants(
        monkeypatch,
        **{profile_id: frozenset({Capability.DISPLAY_OVERLAY, capability})},
    )
    # The overlay grant is paired with a non-NEVER mode so the OVERLAY_MODE parity
    # rule is SATISFIED for these profiles (browser-game-v1 and jeffml are NEVER
    # today): the protocol rule must be the only thing left to raise.
    _set_overlay_modes(monkeypatch, **{profile_id: OverlayMode.ALWAYS})
    with pytest.raises(ValueError, match="uses internally inconsistent protocol"):
        _assert_registry_consistent()


@pytest.mark.parametrize("profile_id", INCONSISTENT_PROFILE_IDS)
def test_overlay_only_grant_to_an_inconsistent_protocol_still_loads(
    monkeypatch, profile_id
):
    # The negative control for the rule above: it keys on the active-required
    # REMAINDER, not on "holds a grant at all", so a retirement-surviving grant to
    # the very same profiles loads fine. The mode moves with the grant for the same
    # reason as above — a bare grant would now trip the parity rule instead, which
    # would prove nothing about the protocol rule.
    _set_grants(
        monkeypatch, **{profile_id: frozenset({Capability.DISPLAY_OVERLAY})}
    )
    _set_overlay_modes(monkeypatch, **{profile_id: OverlayMode.ALWAYS})
    _assert_registry_consistent()


def test_authoritative_profile_with_an_inconsistent_protocol_raises(monkeypatch):
    """The rule fires INSIDE the authoritative branch too, not only beside it.

    Every currently-inconsistent profile is non-authoritative, so without this the
    check could sit under the old ``else:`` and the whole suite would still pass.
    Only the protocol is mutated — canonical keeps ALL_CAPABILITIES and
    ``active=True``, so the authoritative clause passes and the protocol clause is
    the sole raiser. This is also the jointly-unsatisfiable case: an
    authoritative-but-defective profile makes the registry unloadable, which is the
    correct answer.
    """
    _set_profiles(
        monkeypatch,
        _with_profile(
            CANONICAL_PROFILE_ID,
            analyzer_protocol_version=BROWSER_ANALYZER_PROTOCOL_VERSION,
        ),
    )
    with pytest.raises(ValueError, match="uses internally inconsistent protocol"):
        _assert_registry_consistent()


@pytest.mark.parametrize(
    "profile_id",
    [CANONICAL_PROFILE_ID, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, JEFFML_PROFILE_ID],
)
def test_unknown_analyzer_protocol_fails_closed(monkeypatch, profile_id):
    # Independent of BOTH authority and grant state: canonical is authoritative with
    # all eight grants, browser-analysis-multipv-v2 non-authoritative with one, and
    # jeffml holds none. All three must refuse to load.
    _set_profiles(
        monkeypatch,
        _with_profile(profile_id, analyzer_protocol_version=GHOST),
    )
    with pytest.raises(
        ValueError, match="references an unknown analyzer protocol"
    ):
        _assert_registry_consistent()


def test_capability_grants_to_an_unregistered_profile_raise(monkeypatch):
    _set_grants(monkeypatch, **{GHOST: frozenset({Capability.DISPLAY_OVERLAY})})
    with pytest.raises(
        ValueError, match=f"CAPABILITY_GRANTS references unregistered '{GHOST}'"
    ):
        _assert_registry_consistent()


def test_authoritative_profile_stripped_of_a_capability_raises(monkeypatch):
    _set_grants(
        monkeypatch,
        **{CANONICAL_PROFILE_ID: ALL_CAPABILITIES - {Capability.POSITION_READ}},
    )
    with pytest.raises(
        ValueError, match="authoritative profile .* must hold all capabilities"
    ):
        _assert_registry_consistent()


def test_read_grants_to_a_consistent_non_authoritative_protocol_load(monkeypatch):
    """Pins the boundary on the permissive side: the rule caps protocols, not
    non-authoritative profiles in general.

    ``browser-analysis-multipv-v2`` declares the internally consistent
    ``browser-visible-multipv-v1``, so a read/reuse grant to it loads without
    blocking — the seam g-v21l needs. ``browser-game-v1`` is NOT a case here: its
    protocol is inconsistent, so the same grant now raises and is covered by the
    matrix above.
    """
    _set_grants(
        monkeypatch,
        **{
            BROWSER_ANALYSIS_MULTIPV_PROFILE_ID: frozenset(
                {Capability.DISPLAY_OVERLAY, Capability.POSITION_READ}
            )
        },
    )
    _assert_registry_consistent()


# --- OVERLAY_MODE <-> DISPLAY_OVERLAY parity (g-overlay-mode-parity) --------------


def test_overlay_mode_for_an_unregistered_profile_raises(monkeypatch):
    # Same reference rule CAPABILITY_GRANTS already carried: a mode for a profile
    # nobody registered can never be reached, and is far more likely a typo'd id
    # than an intent.
    _set_overlay_modes(monkeypatch, **{GHOST: OverlayMode.ALWAYS})
    with pytest.raises(
        ValueError, match=f"OVERLAY_MODE references unregistered '{GHOST}'"
    ):
        _assert_registry_consistent()


@pytest.mark.parametrize(
    "profile_id",
    [BROWSER_ANALYSIS_PROFILE_ID, BROWSER_PROFILE_ID],
    ids=["granted", "ungranted"],
)
@pytest.mark.parametrize(
    "value",
    [OverlayMode.ALWAYS.value, OverlayMode.NEVER.value, None],
    ids=["always-as-str", "never-as-str", "none"],
)
def test_a_mode_that_is_not_an_overlay_mode_raises(monkeypatch, profile_id, value):
    """A malformed VALUE must fail closed before parity is ever evaluated.

    The parity rule keys on ``is not NEVER``, so any non-member — the enum's own
    ``"always"`` string, ``"never"``, ``None`` — reads as ENABLED there while every
    consumer's ``is`` comparison reads it as disabled. On a GRANTED profile that
    combination satisfies parity and reinstates the exact dead grant this section
    exists to prevent; on an UNGRANTED one parity does raise, but for the wrong
    reason, so both axes assert on the malformed-value message specifically.

    Patched directly rather than through ``_set_overlay_modes``, which reserves
    ``None`` for deleting an entry — here ``None`` is the stored value under test.
    """
    monkeypatch.setattr(
        evidence_policy, "OVERLAY_MODE", {**OVERLAY_MODE, profile_id: value}
    )
    with pytest.raises(ValueError, match="is not an OverlayMode"):
        _assert_registry_consistent()


@pytest.mark.parametrize(
    "mode",
    [OverlayMode.ALWAYS, OverlayMode.REQUIRES_COMPARISON],
    ids=lambda m: m.value,
)
def test_a_mode_without_the_grant_raises(monkeypatch, mode):
    # DEAD MODE. browser-game-v1 holds nothing, so has_capability rejects its rows
    # before overlay_mode is ever read — either non-NEVER mode is unreachable code
    # dressed as policy. Both modes are covered because the rule is stated over
    # "not NEVER", not over ALWAYS.
    _set_overlay_modes(monkeypatch, **{BROWSER_PROFILE_ID: mode})
    with pytest.raises(ValueError, match="does not match its DISPLAY_OVERLAY grant"):
        _assert_registry_consistent()


@pytest.mark.parametrize(
    "mode", [OverlayMode.NEVER, None], ids=["explicit-never", "entry-deleted"]
)
def test_a_grant_without_a_non_never_mode_raises(monkeypatch, mode):
    # DEAD GRANT, the other direction. browser-analysis-multipv-v2 holds
    # DISPLAY_OVERLAY; demoting it to NEVER and deleting its entry outright are the
    # same failure, which is why the rule reads OVERLAY_MODE through its NEVER
    # default rather than through its key set.
    _set_overlay_modes(monkeypatch, **{BROWSER_ANALYSIS_MULTIPV_PROFILE_ID: mode})
    with pytest.raises(ValueError, match="does not match its DISPLAY_OVERLAY grant"):
        _assert_registry_consistent()


def test_a_dead_grant_is_silent_everywhere_except_the_load_rule(monkeypatch):
    """Why this has to be a LOAD assertion: the drift has no downstream symptom.

    The row stays identity-verified, keeps its capability, and simply stops
    overlaying — indistinguishable from a profile that was never meant to overlay.
    Nothing raises, nothing logs, and the only remaining detector is comparing the
    two tables at import.
    """
    row = _row(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert display_upgrade_eligible(row) is True  # control: overlays today

    _set_overlay_modes(monkeypatch, **{BROWSER_ANALYSIS_MULTIPV_PROFILE_ID: None})
    assert has_capability(row, Capability.DISPLAY_OVERLAY) is True  # grant survives
    assert display_upgrade_eligible(row) is False  # ...and buys nothing

    with pytest.raises(ValueError, match="does not match its DISPLAY_OVERLAY grant"):
        _assert_registry_consistent()


def test_a_grant_and_a_mode_added_together_load(monkeypatch):
    # The permissive boundary: the rule blocks DRIFT between the tables, not the
    # arrival of a new overlaying profile. jeffml holds nothing and is NEVER today;
    # moved on both axes at once it loads — and the grant stays inside
    # RETIREMENT_SURVIVING, so the protocol rule (jeffml declares no protocol) has
    # nothing to say about it either.
    _set_grants(
        monkeypatch, **{JEFFML_PROFILE_ID: frozenset({Capability.DISPLAY_OVERLAY})}
    )
    _set_overlay_modes(monkeypatch, **{JEFFML_PROFILE_ID: OverlayMode.ALWAYS})
    _assert_registry_consistent()


# --- lifecycle is enforced at USE time, not at load -------------------------------


def test_inactive_consistent_profile_cannot_exercise_an_active_required_grant(
    monkeypatch,
):
    """Load PERMITS what use DENIES — the two halves of the split invariant.

    The witness must be a registry state the LOAD rule allows, so it uses the
    internally CONSISTENT ``browser-analysis-multipv-v2`` flipped inactive; the
    retired, inconsistent ``browser-analysis-v1`` would be rejected at load and
    would prove nothing about lifecycle.

    Two seams are patched because they are different lookups:
    ``_assert_registry_consistent`` reads ``evidence_policy.list_profiles``, while
    ``has_capability`` resolves lifecycle through ``evidence_policy.get_profile``.
    """
    profiles = _with_profile(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, active=False)
    _set_profiles(monkeypatch, profiles)
    by_id = {p.profile_id: p for p in profiles}
    monkeypatch.setattr(evidence_policy, "get_profile", by_id.get)
    _set_grants(
        monkeypatch,
        **{
            BROWSER_ANALYSIS_MULTIPV_PROFILE_ID: frozenset(
                {Capability.DISPLAY_OVERLAY, Capability.POSITION_READ}
            )
        },
    )

    _assert_registry_consistent()  # LOAD permits: the protocol is consistent

    row = _row(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert has_capability(row, Capability.POSITION_READ) is False  # USE denies
    assert has_capability(row, Capability.DISPLAY_OVERLAY) is True  # surviving


# --- enforcement AT IMPORT --------------------------------------------------------


def _load_fresh_evidence_policy(name: str = "_drift_probe_evidence_policy"):
    """Execute a SECOND, throwaway instance of the module from its own file.

    Registered in ``sys.modules`` before ``exec_module`` because ``dataclasses``
    looks the module up while creating each class, and popped in ``finally`` so no
    duplicate enum classes outlive the call.
    """
    spec = importlib.util.spec_from_file_location(name, evidence_policy.__file__)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module
    finally:
        sys.modules.pop(name, None)


def test_registry_drift_fails_at_module_import(monkeypatch):
    # A clean load succeeds: the failure below is the drift, not the probe.
    assert _load_fresh_evidence_policy() is not None

    # Drift the SHARED registry the fresh module binds during its own execution.
    # Snapshot EAGERLY: the patched name is the one _with_profile itself reads.
    drifted = _with_profile(
        BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, dominates=frozenset()
    )
    monkeypatch.setattr(analysis_profiles, "list_profiles", lambda: drifted)
    with pytest.raises(ValueError, match="but EDGES derive"):
        _load_fresh_evidence_policy()

    assert "_drift_probe_evidence_policy" not in sys.modules


def test_import_time_probe_leaves_no_residue():
    """The live module — and cross-module enum identity — survive the probe.

    Ordering-independent on purpose: it holds whether or not the import-time test
    ran first, and it is what would catch a future switch to ``importlib.reload``
    (which rebuilds the enums ``analysis_cache_policy`` compares with ``is``).
    """
    _assert_registry_consistent()
    assert "_drift_probe_evidence_policy" not in sys.modules
    assert CAPABILITY_GRANTS[BROWSER_ANALYSIS_PROFILE_ID] == frozenset(
        {Capability.DISPLAY_OVERLAY}
    )
    assert decide_analysis_cache_replacement(
        _row(BROWSER_ANALYSIS_PROFILE_ID), _row(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    ) == (Decision.REPLACE, Reason.PROTOCOL_CORRECTED_REPLACE)
