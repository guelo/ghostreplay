"""Fail-closed registry-load assertions (g-parity-matrix-evpolicy, deliverable B).

``app.evidence_policy._assert_registry_consistent`` runs at module import and is
the only thing standing between a registry edit and a silently inconsistent
policy: EDGES that reference a profile nobody registered, an EDGES/``dominates``
pair that drifted apart, a capability granted to the retired (internally
inconsistent) ``browser-analysis-v1`` protocol, or an authoritative profile
quietly stripped of a capability. Each of those must raise, and one test proves
the enforcement really happens AT IMPORT rather than only when called.

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
    project_cache_row,
)
from app.analysis_profiles import (
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
    BROWSER_ANALYSIS_PROFILE_ID,
    BROWSER_PROFILE_ID,
    CANONICAL_PROFILE_ID,
    JEFFML_PROFILE_ID,
    stamp_profile_full,
)
from app.evidence_contracts import RESOLVER_COMPLETE_V2
from app.evidence_policy import (
    ALL_CAPABILITIES,
    CAPABILITY_GRANTS,
    EDGES,
    Capability,
    Edge,
    EdgeKind,
    _assert_registry_consistent,
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


# Every capability the retired browser-analysis-v1 protocol may NOT hold: it is
# granted exactly the retirement-surviving DISPLAY_OVERLAY and nothing else.
NON_OVERLAY_CAPABILITIES = tuple(
    sorted(
        (c for c in Capability if c is not Capability.DISPLAY_OVERLAY),
        key=lambda c: c.value,
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


@pytest.mark.parametrize(
    "capability", NON_OVERLAY_CAPABILITIES, ids=lambda c: c.value
)
def test_browser_analysis_v1_may_not_hold_an_active_required_capability(
    monkeypatch, capability
):
    # Parametrized over EVERY non-overlay capability rather than a sample, so a
    # capability added to the enum later is covered without editing this test.
    _set_grants(
        monkeypatch,
        **{
            BROWSER_ANALYSIS_PROFILE_ID: frozenset(
                {Capability.DISPLAY_OVERLAY, capability}
            )
        },
    )
    with pytest.raises(
        ValueError, match="browser-analysis-v1 may not hold an active-required"
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


@pytest.mark.parametrize(
    "profile_id", [BROWSER_PROFILE_ID, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID]
)
def test_grants_to_other_non_authoritative_profiles_are_not_load_blocked(
    monkeypatch, profile_id
):
    """Pins a DELIBERATE boundary of the current rule, so nobody reads it as
    universal.

    The active-required-capability rule is scoped to ``browser-analysis-v1`` — the
    one protocol known to be internally inconsistent — NOT to non-authoritative
    profiles in general. Granting a read/reuse capability to another
    non-authoritative profile therefore loads fine today; the general
    non-authoritative read-trust seam is g-v21l's, and generalizing the rule to
    "any INACTIVE profile may hold only RETIREMENT_SURVIVING capabilities" would
    be a production change, not a test edit.
    """
    _set_grants(
        monkeypatch,
        **{profile_id: frozenset({Capability.DISPLAY_OVERLAY, Capability.POSITION_READ})},
    )
    _assert_registry_consistent()


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
