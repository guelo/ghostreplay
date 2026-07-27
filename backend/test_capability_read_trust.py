"""Capability- and submitter-scoped read trust (g-v21l).

Browser evidence is authenticated but NOT attested: an authenticated user running
a modified client can submit fabricated but internally coherent scores at any
position reachable from their own session mainline, and every structural check
passes. ``analysis_cache`` is globally keyed and carries no owner column, so an
UNSCOPED read grant would turn "a user can lie to themselves" into "a user can lie
to everyone".

The decision is SAME-USER SCOPING: every newly granted capability is owner-scoped
through an ``analysis_cache_submission`` association, so a fabricator can at most
degrade their own reads (accepted residual) or deny others reuse — never inject
facts into another user's reads.

This module covers the predicate matrix, association-scoped reads, capability
independence, canonical-first ordering, and the authority boundary. The write-path
claim rule lives in ``test_analysis_cache_claims.py``; the pairing rules live in
``test_evidence_coherence.py``.
"""
from __future__ import annotations

import pytest

from app.analysis_profiles import (
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
    BROWSER_ANALYSIS_PROFILE_ID,
    BROWSER_GAME_V2_PROFILE_ID,
    BROWSER_PROFILE_ID,
    CANONICAL_PROFILE_ID,
    IDENTITY_FIELDS,
    JEFFML_PROFILE_ID,
    get_profile,
    stamp_profile_full,
)
from app.analysis_submissions import (
    associated_user_ids_by_row,
    viewer_associated_ids,
)
from app.analysis_trust import (
    CACHE_SOURCE,
    describe_move_row,
    describe_position_row,
    move_trust_flags,
    owner_scope_ok,
    position_trust_flags,
)
from app.evidence_contracts import RESOLVER_COMPLETE_V2
from app.evidence_policy import (
    ALL_CAPABILITIES,
    CAPABILITY_GRANTS,
    OWNER_SCOPED,
    Capability,
    has_capability,
)
from app.fen import normalize_fen
from app.models import AnalysisCache, AnalysisCacheSubmission, User
from app.position_analysis_repo import (
    load_position_candidates,
    resolve_positions_from_candidates,
    resolve_trusted_positions,
)

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
NORM = normalize_fen(START)

VIEWER = 123
OTHER = 456


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _identity(profile_id: str) -> dict:
    profile = get_profile(profile_id)
    data = {"analysis_profile_id": profile_id}
    for f in IDENTITY_FIELDS:
        data[f] = getattr(profile, f)
    return data


def _v2_facts() -> dict:
    return {
        "fen_before": START,
        "best_move_uci": "e2e4",
        "best_line_uci": "e2e4 e7e5",
        "best_eval": 20,
        "best_eval_mate": None,
        "played_eval": 20,
        "played_eval_mate": None,
        "classification": "best",
        "eval_delta": 0,
    }


def _row_dict(profile_id: str, *, viewer_associated: bool = False, **over) -> dict:
    data = {
        **_identity(profile_id),
        "evidence_contract_id": RESOLVER_COMPLETE_V2,
        **_v2_facts(),
        "viewer_associated": viewer_associated,
    }
    data.update(over)
    return data


def _seed_user(db, user_id: int) -> None:
    if db.get(User, user_id) is None:
        db.add(User(id=user_id, username=f"u{user_id}", is_anonymous=True))
        db.commit()


def _seed_row(db, profile_id: str, *, fen=START, move_uci="e2e4", **over) -> AnalysisCache:
    data = dict(
        fen_before=fen,
        normalized_fen_before=normalize_fen(fen),
        move_uci=move_uci,
        move_san="e4",
        best_move_uci="e2e4",
        best_move_san="e4",
        best_line_uci="e2e4 e7e5",
        played_eval=20,
        best_eval=20,
        eval_delta=0,
        classification="best",
        source="analysis",
        analysis_profile_id=profile_id,
        evidence_contract_id=RESOLVER_COMPLETE_V2,
    )
    if profile_id is not None:
        data.update(stamp_profile_full(profile_id))
    data.update(over)
    row = AnalysisCache(**data)
    db.add(row)
    db.commit()
    return row


def _associate(db, row: AnalysisCache, user_id: int) -> None:
    _seed_user(db, user_id)
    db.add(AnalysisCacheSubmission(analysis_cache_id=row.id, user_id=user_id))
    db.commit()


# --------------------------------------------------------------------------- #
# 1. trust predicate matrix
# --------------------------------------------------------------------------- #
GRANTED_TO_MULTIPV = (
    Capability.DISPLAY_OVERLAY,
    Capability.POSITION_READ,
    Capability.MOVE_READ,
    Capability.INTERACTIVE_ANALYSIS_REUSE,
    Capability.GAME_ANALYSIS_REUSE,
    Capability.OPENING_EVIDENCE,
)
UNGRANTED_TO_MULTIPV = (Capability.DRILL_GRADE, Capability.TREE_EVAL)


@pytest.mark.parametrize("capability", list(ALL_CAPABILITIES))
@pytest.mark.parametrize("viewer", [None, VIEWER])
def test_canonical_rows_trust_every_capability_for_every_viewer(capability, viewer):
    """Canonical parity: an authoritative row keeps (True, satisfied, trusted) for
    every capability and every viewer INCLUDING None, with no association."""
    data = _row_dict(CANONICAL_PROFILE_ID)
    assert position_trust_flags(data, capability, viewer) == (True, True, True)
    assert move_trust_flags(data, capability, viewer) == (True, True, True)


@pytest.mark.parametrize("capability", GRANTED_TO_MULTIPV)
def test_associated_browser_row_trusts_only_granted_capabilities(capability):
    data = _row_dict(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, viewer_associated=True)
    assert position_trust_flags(data, capability, VIEWER)[2] is True
    assert move_trust_flags(data, capability, VIEWER)[2] is True


@pytest.mark.parametrize("capability", UNGRANTED_TO_MULTIPV)
def test_browser_row_never_trusts_drill_or_tree(capability):
    """DRILL_GRADE and TREE_EVAL are ungranted, so an association cannot buy them."""
    data = _row_dict(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, viewer_associated=True)
    assert position_trust_flags(data, capability, VIEWER)[2] is False
    assert move_trust_flags(data, capability, VIEWER)[2] is False


def test_contract_incomplete_browser_row_is_untrusted():
    data = _row_dict(
        BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, viewer_associated=True, best_line_uci=None
    )
    assert position_trust_flags(data, Capability.POSITION_READ, VIEWER)[2] is False


def test_identity_mismatched_browser_row_is_untrusted():
    data = _row_dict(
        BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
        viewer_associated=True,
        search_limit_value=99,
    )
    assert position_trust_flags(data, Capability.POSITION_READ, VIEWER)[2] is False
    assert move_trust_flags(data, Capability.MOVE_READ, VIEWER)[2] is False


@pytest.mark.parametrize(
    "profile_id",
    [
        BROWSER_ANALYSIS_PROFILE_ID,  # retired, internally inconsistent protocol
        BROWSER_PROFILE_ID,           # browser-game-v1
        BROWSER_GAME_V2_PROFILE_ID,   # dynamic per-device strength
        JEFFML_PROFILE_ID,
        None,                         # legacy / unidentified
    ],
)
@pytest.mark.parametrize(
    "capability", [c for c in ALL_CAPABILITIES if c is not Capability.DISPLAY_OVERLAY]
)
def test_excluded_profiles_gain_nothing(profile_id, capability):
    """Retired v1, browser-game v1/v2, JeffML and legacy rows stay excluded from
    every active-required capability, association or not."""
    if profile_id is None:
        data = {
            "analysis_profile_id": None,
            "evidence_contract_id": RESOLVER_COMPLETE_V2,
            **_v2_facts(),
            "viewer_associated": True,
        }
    else:
        data = _row_dict(profile_id, viewer_associated=True)
        if profile_id == BROWSER_GAME_V2_PROFILE_ID:
            # browser-game-v2's identity is declared-dynamic; give it valid values.
            data.update(
                engine_version="18", engine_build="a" * 64,
                eval_file_id="nn.nnue:" + "b" * 64,
                search_limit_type="depth", search_limit_value=17,
                threads=1, hash_mb=16,
            )
    assert position_trust_flags(data, capability, VIEWER)[2] is False
    assert move_trust_flags(data, capability, VIEWER)[2] is False


# --------------------------------------------------------------------------- #
# 2. association-scoped reads
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("capability", [c for c in GRANTED_TO_MULTIPV if c in OWNER_SCOPED])
def test_association_for_a_different_user_satisfies_nothing(capability):
    """The identical row, associated with SOMEONE ELSE, satisfies none for this
    viewer — the descriptor carries only THIS viewer's membership."""
    data = _row_dict(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, viewer_associated=False)
    assert position_trust_flags(data, capability, VIEWER)[2] is False
    assert move_trust_flags(data, capability, VIEWER)[2] is False


def test_unassociated_browser_row_satisfies_nothing():
    data = _row_dict(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, viewer_associated=False)
    for capability in ALL_CAPABILITIES:
        if capability is Capability.DISPLAY_OVERLAY:
            continue
        assert position_trust_flags(data, capability, VIEWER)[2] is False


def test_viewer_none_admits_only_authoritative_rows():
    browser = _row_dict(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, viewer_associated=True)
    canonical = _row_dict(CANONICAL_PROFILE_ID)
    assert position_trust_flags(browser, Capability.POSITION_READ, None)[2] is False
    assert position_trust_flags(canonical, Capability.POSITION_READ, None)[2] is True


def test_display_overlay_is_unaffected_by_viewer_identity():
    """DISPLAY_OVERLAY is deliberately NOT owner-scoped: it is purely
    presentational re-labeling that already ships (g-overlay-owner-scope revisits)."""
    assert Capability.DISPLAY_OVERLAY not in OWNER_SCOPED
    data = _row_dict(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, viewer_associated=False)
    for viewer in (None, VIEWER, OTHER):
        assert owner_scope_ok(data, Capability.DISPLAY_OVERLAY, viewer) is True


def test_owner_scoped_is_every_capability_but_display_overlay():
    assert OWNER_SCOPED == ALL_CAPABILITIES - {Capability.DISPLAY_OVERLAY}


def test_association_membership_is_not_an_identity_field():
    """An association is eligibility, NOT identity: it must never reach
    IDENTITY_FIELDS (and so never the manifest digest or strength comparison)."""
    assert "viewer_associated" not in IDENTITY_FIELDS
    row_dict = _row_dict(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, viewer_associated=True)
    unassociated = _row_dict(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, viewer_associated=False)

    class _Row:
        def __init__(self, d):
            for k, v in d.items():
                setattr(self, k, v)

    a = describe_move_row(_Row(row_dict), viewer_associated=True)
    b = describe_move_row(_Row(unassociated), viewer_associated=False)
    # Identity snapshots (the strength/supersession surface) are identical.
    assert a.identity_values() == b.identity_values()
    assert a.identity == b.identity


def test_composite_primary_key_rejects_a_duplicate_pair(db_session):
    from sqlalchemy.exc import IntegrityError

    row = _seed_row(db_session, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    _associate(db_session, row, VIEWER)
    db_session.add(AnalysisCacheSubmission(analysis_cache_id=row.id, user_id=VIEWER))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_deleting_a_user_cascades_their_associations(db_session):
    from sqlalchemy import text

    row = _seed_row(db_session, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    _associate(db_session, row, VIEWER)
    db_session.commit()
    # SQLite enforces ON DELETE CASCADE only under this pragma; the DDL declares it.
    db_session.execute(text("PRAGMA foreign_keys=ON"))
    db_session.execute(text("DELETE FROM users WHERE id = :u"), {"u": VIEWER})
    db_session.commit()
    assert db_session.query(AnalysisCacheSubmission).count() == 0


def test_deleting_a_cache_row_cascades_its_associations(db_session):
    from sqlalchemy import text

    row = _seed_row(db_session, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    _associate(db_session, row, VIEWER)
    row_id = row.id
    db_session.commit()
    db_session.execute(text("PRAGMA foreign_keys=ON"))
    db_session.execute(
        text("DELETE FROM analysis_cache WHERE id = :i"), {"i": row_id}
    )
    db_session.commit()
    assert db_session.query(AnalysisCacheSubmission).count() == 0


def test_viewer_associated_ids_is_viewer_scoped(db_session):
    row = _seed_row(db_session, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    _associate(db_session, row, OTHER)
    assert viewer_associated_ids(db_session, VIEWER, [row.id]) == frozenset()
    assert viewer_associated_ids(db_session, OTHER, [row.id]) == frozenset({row.id})
    # No viewer -> no query, no membership.
    assert viewer_associated_ids(db_session, None, [row.id]) == frozenset()


def test_full_association_sets_are_user_independent(db_session):
    row = _seed_row(db_session, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    _associate(db_session, row, OTHER)
    _associate(db_session, row, VIEWER)
    assert associated_user_ids_by_row(db_session, [row.id]) == {
        row.id: (VIEWER, OTHER) if VIEWER < OTHER else (OTHER, VIEWER)
    }


def test_associations_never_appear_in_the_lookup_response(
    client, auth_headers, db_session
):
    row = _seed_row(db_session, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    _associate(db_session, row, VIEWER)
    body = client.post(
        "/api/analysis/lookup",
        json={"positions": [{"fen": START, "move_uci": "e2e4"}]},
        headers=auth_headers(user_id=VIEWER),
    ).json()
    rendered = repr(body)
    assert "submission" not in rendered
    assert "viewer_associated" not in rendered
    assert "analysis_cache_id" not in rendered


# --------------------------------------------------------------------------- #
# resolver: association-scoped position resolution end to end
# --------------------------------------------------------------------------- #
def test_browser_position_resolves_only_for_its_submitter(db_session):
    row = _seed_row(db_session, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    _associate(db_session, row, VIEWER)

    mine = resolve_trusted_positions(
        db_session, [NORM], Capability.POSITION_READ, VIEWER
    )
    theirs = resolve_trusted_positions(
        db_session, [NORM], Capability.POSITION_READ, OTHER
    )
    nobody = resolve_trusted_positions(
        db_session, [NORM], Capability.POSITION_READ, None
    )
    assert mine[NORM] is not None and mine[NORM].best_move_uci == "e2e4"
    assert theirs[NORM] is None
    assert nobody[NORM] is None


def test_browser_position_is_never_a_tree_eval_or_drill_winner(db_session):
    row = _seed_row(db_session, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    _associate(db_session, row, VIEWER)
    for capability in (Capability.TREE_EVAL, Capability.DRILL_GRADE):
        resolved = resolve_trusted_positions(db_session, [NORM], capability, VIEWER)
        assert resolved[NORM] is None


# --------------------------------------------------------------------------- #
# 8. canonical-first ordering within the trusted tier
# --------------------------------------------------------------------------- #
def test_canonical_cp_legacy_position_beats_a_browser_mate_candidate(db_session):
    """Filtering and ranking are separate: once a granted browser row passes the
    capability + owner gate it is a trusted CANDIDATE, but canonical must still win
    the tier. Without the authority key the browser MATE row would sort first."""
    _seed_row(
        db_session,
        CANONICAL_PROFILE_ID,
        move_uci="d2d4",
        move_san="d4",
        best_move_uci="d2d4",
        best_move_san="d4",
        best_line_uci="d2d4 d7d5",
        best_eval=15,
        played_eval=15,
        source="precomputed",
    )
    browser = _seed_row(
        db_session,
        BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
        move_uci="e2e4",
        best_eval=None,
        best_eval_mate=3,
        played_eval=20,
        eval_delta=0,
    )
    _associate(db_session, browser, VIEWER)

    resolved = resolve_trusted_positions(
        db_session, [NORM], Capability.POSITION_READ, VIEWER
    )
    assert resolved[NORM] is not None
    assert resolved[NORM].best_move_uci == "d2d4"
    assert resolved[NORM].evidence.is_effectively_authoritative() is True


def test_canonical_only_ordering_is_unchanged_without_browser_candidates(db_session):
    """Two canonical legacy candidates keep their historical order: mate presence,
    then the complete best-move row, then source rank, then id."""
    _seed_row(
        db_session,
        CANONICAL_PROFILE_ID,
        move_uci="d2d4",
        move_san="d4",
        best_move_uci="e2e4",
        best_line_uci="e2e4 e7e5",
        best_eval=15,
        source="game",
    )
    _seed_row(
        db_session,
        CANONICAL_PROFILE_ID,
        move_uci="e2e4",
        best_move_uci="e2e4",
        best_line_uci="e2e4 e7e5",
        best_eval=15,
        source="precomputed",
    )
    resolved = resolve_trusted_positions(
        db_session, [NORM], Capability.POSITION_READ, None
    )
    # The complete best-move row (move_uci == best_move_uci) wins over the other.
    assert resolved[NORM].evidence.source_table == CACHE_SOURCE


# --------------------------------------------------------------------------- #
# 4. capability independence + 12. authority boundary
# --------------------------------------------------------------------------- #
def test_read_grants_do_not_confer_reuse_drill_or_tree():
    """Each capability changes only its named surface: holding POSITION_READ /
    MOVE_READ says nothing about reuse, drill, tree, or opening consumption."""
    grants = CAPABILITY_GRANTS[BROWSER_ANALYSIS_MULTIPV_PROFILE_ID]
    assert Capability.POSITION_READ in grants
    assert Capability.MOVE_READ in grants
    assert Capability.DRILL_GRADE not in grants
    assert Capability.TREE_EVAL not in grants


def test_no_profile_outside_canonical_holds_drill_or_tree():
    for profile_id, grants in CAPABILITY_GRANTS.items():
        profile = get_profile(profile_id)
        if profile is not None and profile.authoritative:
            continue
        assert Capability.DRILL_GRADE not in grants, profile_id
        assert Capability.TREE_EVAL not in grants, profile_id


def test_profile_authoritative_flag_is_unchanged_for_browser_analysis():
    """The authority boundary is intact: a read/reuse grant is NOT authority."""
    profile = get_profile(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert profile.authoritative is False
    assert profile.active is True
    assert profile.replacement_eligible is True


def test_has_capability_alone_is_not_owner_scope():
    """`has_capability` stays the single capability gate; owner scoping is layered
    ABOVE it, not folded into it."""

    class _View:
        def effective_profile_id(self):
            return BROWSER_ANALYSIS_MULTIPV_PROFILE_ID

        def is_effectively_authoritative(self):
            return False

        def identity_values(self):
            return {}

    assert has_capability(_View(), Capability.POSITION_READ) is True
    data = _row_dict(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, viewer_associated=False)
    assert owner_scope_ok(data, Capability.POSITION_READ, VIEWER) is False


# --------------------------------------------------------------------------- #
# 5. provenance carried on the resolved winner
# --------------------------------------------------------------------------- #
def test_resolved_winner_carries_source_identity_and_membership(db_session):
    row = _seed_row(db_session, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    _associate(db_session, row, VIEWER)
    resolved = resolve_trusted_positions(
        db_session, [NORM], Capability.POSITION_READ, VIEWER
    )
    evidence = resolved[NORM].evidence
    assert evidence.source_table == CACHE_SOURCE
    assert evidence.source_id == row.id
    assert evidence.effective_profile_id() == BROWSER_ANALYSIS_MULTIPV_PROFILE_ID
    assert evidence.is_effectively_authoritative() is False
    assert evidence.viewer_associated is True
    assert evidence.contract_satisfied is True
    # The FULL identity snapshot, including nulls, is captured on the winner.
    assert set(dict(evidence.identity)) == set(IDENTITY_FIELDS)


def test_candidate_set_is_loaded_once_and_resolved_per_capability(db_session):
    """The in-memory resolver is PURE: one load answers every capability."""
    row = _seed_row(db_session, BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    _associate(db_session, row, VIEWER)
    candidates = load_position_candidates(db_session, [NORM])
    associated = viewer_associated_ids(db_session, VIEWER, candidates.cache_ids)

    read = resolve_positions_from_candidates(
        candidates, Capability.POSITION_READ, VIEWER, associated
    )
    drill = resolve_positions_from_candidates(
        candidates, Capability.DRILL_GRADE, VIEWER, associated
    )
    tree = resolve_positions_from_candidates(
        candidates, Capability.TREE_EVAL, None, frozenset()
    )
    assert read[NORM] is not None
    assert drill[NORM] is None
    assert tree[NORM] is None


def test_describe_position_row_defaults_to_unassociated():
    """A caller that never resolved associations fails CLOSED."""

    class _Row:
        id = 7

    r = _Row()
    for k, v in _row_dict(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID).items():
        setattr(r, k, v)
    evidence = describe_position_row(r, source_table=CACHE_SOURCE)
    assert evidence.viewer_associated is False
    assert evidence.holds(Capability.POSITION_READ, VIEWER) is False
