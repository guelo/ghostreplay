"""The shared coherent-tuple resolver and the shared classification validator
(g-v21l §4 and §6).

Before this bead each consumer that needed "the best move AND what the played move
scored" hand-rolled the pairing, and the strongest check any of them applied was
``compare_search_strength(...) is EQUAL``. That is not enough: two same-profile
sibling rows can compare EQUAL on strength while ASSERTING DIFFERENT FACTS.

``resolve_coherent_evidence_tuple`` is now the only sanctioned pairing, and
``validate_root_alternative_classification`` is the only classification rule —
shared by the analysis-evidence ingress and this read-time resolver, so a row that
could not be submitted can never be read back either.
"""
from __future__ import annotations

import pytest

from app.analysis_profiles import (
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
    CANONICAL_PROFILE_ID,
    CANONICAL_LINUX_PROFILE_ID,
    IDENTITY_FIELDS,
    get_profile,
    stamp_profile_full,
)
from app.analysis_trust import (
    CACHE_SOURCE,
    POSITION_STORAGE_SOURCE,
    describe_move_row,
    describe_position_row,
)
from app.evidence_coherence import (
    MoveGrain,
    PositionGrain,
    resolve_coherent_evidence_tuple,
)
from app.evidence_contracts import (
    MOVE_COMPLETE,
    POSITION_COMPLETE,
    RESOLVER_COMPLETE_V2,
)
from app.evidence_policy import Capability
from app.api.session import (
    EVIDENCE_CLASSIFICATION_MISMATCH,
    EVIDENCE_CONTRACT_UNSATISFIED,
)
from app.move_classification import validate_root_alternative_classification

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
BLACK_TO_MOVE = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

VIEWER = 7
REUSE = Capability.INTERACTIVE_ANALYSIS_REUSE


class _Row:
    """Minimal duck-typed row the descriptor builders read via ``getattr``."""

    def __init__(self, row_id: int, **kw):
        self.id = row_id
        for k, v in kw.items():
            setattr(self, k, v)


def _stamped(profile_id: str) -> dict:
    return {"analysis_profile_id": profile_id, **stamp_profile_full(profile_id)}


def _combined_row(row_id=1, profile=CANONICAL_PROFILE_ID, **over) -> _Row:
    data = {
        "evidence_contract_id": RESOLVER_COMPLETE_V2,
        "fen_before": START,
        "move_uci": "e2e4",
        "best_move_uci": "e2e4",
        "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5",
        "best_eval": 20,
        "best_eval_mate": None,
        "played_eval": 20,
        "played_eval_mate": None,
        "classification": "best",
        "eval_delta": 0,
        **_stamped(profile),
    }
    data.update(over)
    return _Row(row_id, **data)


def _grains(row: _Row, *, associated=False, position_row: _Row | None = None,
            position_source=CACHE_SOURCE):
    """Build the two grains from ONE combined row (or a distinct position row)."""
    p_row = position_row if position_row is not None else row
    p_assoc = associated if position_row is None else associated
    position = describe_position_row(
        p_row, source_table=position_source, viewer_associated=p_assoc
    )
    move = describe_move_row(row, viewer_associated=associated)
    return (
        PositionGrain(
            evidence=position,
            best_move_uci=p_row.best_move_uci,
            best_move_san=getattr(p_row, "best_move_san", None),
            best_line_uci=(p_row.best_line_uci or "").split() or None,
            best_eval=p_row.best_eval,
            best_eval_mate=p_row.best_eval_mate,
        ),
        MoveGrain(
            evidence=move,
            move_uci=row.move_uci,
            played_eval=row.played_eval,
            played_eval_mate=row.played_eval_mate,
            eval_delta=row.eval_delta,
            classification=row.classification,
            best_move_uci=getattr(row, "best_move_uci", None),
            best_line_uci=(getattr(row, "best_line_uci", None) or "").split() or None,
            best_eval=getattr(row, "best_eval", None),
            best_eval_mate=getattr(row, "best_eval_mate", None),
        ),
    )


def _resolve(row, *, associated=False, capability=REUSE, viewer=VIEWER, fen=START,
             position_row=None, position_source=CACHE_SOURCE):
    position, move = _grains(
        row,
        associated=associated,
        position_row=position_row,
        position_source=position_source,
    )
    return resolve_coherent_evidence_tuple(fen, position, move, capability, viewer)


# --------------------------------------------------------------------------- #
# happy paths
# --------------------------------------------------------------------------- #
def test_canonical_combined_row_emits_both_slices():
    tuple_ = _resolve(_combined_row())
    assert tuple_ is not None
    assert tuple_.best_move_uci == "e2e4"
    assert tuple_.best_line_uci == ["e2e4", "e7e5"]
    assert tuple_.played_eval == 20
    assert tuple_.eval_delta == 0
    assert tuple_.classification == "best"


def test_associated_browser_combined_row_emits_both_slices():
    row = _combined_row(profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert _resolve(row, associated=True) is not None


def test_an_unassociated_browser_row_is_refused():
    row = _combined_row(profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert _resolve(row, associated=False) is None


def test_a_browser_row_is_refused_for_a_viewerless_read():
    row = _combined_row(profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert _resolve(row, associated=True, viewer=None) is None


def test_an_ungranted_capability_is_refused():
    row = _combined_row(profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert _resolve(row, associated=True, capability=Capability.DRILL_GRADE) is None


# --------------------------------------------------------------------------- #
# the coherence requirement: strictly stronger than equal strength alone
# --------------------------------------------------------------------------- #
def test_equal_strength_siblings_with_a_disagreeing_best_move_are_refused():
    """The motivating case: two same-profile rows compare EQUAL on strength yet
    name DIFFERENT best moves. An equal-strength check alone would pair them."""
    move_row = _combined_row(row_id=1, profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    position_row = _combined_row(
        row_id=2,
        profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
        best_move_uci="d2d4",
        best_line_uci="d2d4 d7d5",
    )
    assert _resolve(move_row, associated=True, position_row=position_row) is None


def test_equal_strength_siblings_with_a_disagreeing_best_eval_are_refused():
    move_row = _combined_row(row_id=1, profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    position_row = _combined_row(
        row_id=2, profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, best_eval=400
    )
    assert _resolve(move_row, associated=True, position_row=position_row) is None


def test_a_disagreeing_pv_is_refused():
    move_row = _combined_row(row_id=1, profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    position_row = _combined_row(
        row_id=2,
        profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
        best_line_uci="e2e4 c7c5",
    )
    assert _resolve(move_row, associated=True, position_row=position_row) is None


def test_incompatible_settings_are_refused():
    """A canonical position paired with a browser move row: neither the identity
    snapshots nor ``compare_evidence_rows`` says EQUAL (the authority barrier fires
    A_SUPERSEDES), so no coherent tuple exists."""
    move_row = _combined_row(row_id=1, profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    position_row = _combined_row(row_id=2, profile=CANONICAL_PROFILE_ID)
    assert _resolve(move_row, associated=True, position_row=position_row) is None


def test_two_canonical_platform_profiles_still_pair():
    """The x86-64 / bmi2 pair are two profiles of ONE verified engine and compare
    EQUAL, so canonical results do not shift."""
    move_row = _combined_row(row_id=1, profile=CANONICAL_PROFILE_ID)
    position_row = _combined_row(row_id=2, profile=CANONICAL_LINUX_PROFILE_ID)
    assert _resolve(move_row, position_row=position_row) is not None


def test_an_authoritative_pair_keeps_its_pre_change_outcome():
    """Scope guard: the coherence requirement applies only when EITHER grain is
    non-canonical. A canonical/canonical pair whose facts differ keeps today's
    equal-strength behavior byte-for-byte (tightening it is g-open-canon-coherence)."""
    move_row = _combined_row(row_id=1, profile=CANONICAL_PROFILE_ID)
    position_row = _combined_row(
        row_id=2, profile=CANONICAL_PROFILE_ID, best_move_uci="d2d4",
        best_line_uci="d2d4 d7d5",
    )
    tuple_ = _resolve(move_row, position_row=position_row)
    assert tuple_ is not None
    # The position slice is always the resolved POSITION winner, never the move
    # row's own copy — the rule that outlived the pairing rewrite.
    assert tuple_.best_move_uci == "d2d4"


# --------------------------------------------------------------------------- #
# structural refusals
# --------------------------------------------------------------------------- #
def test_a_failed_grain_contract_is_refused():
    row = _combined_row(profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, best_line_uci=None)
    assert _resolve(row, associated=True) is None


def test_an_identity_mismatch_is_refused():
    row = _combined_row(
        profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, search_limit_value=99
    )
    assert _resolve(row, associated=True) is None


def test_a_missing_delta_is_refused():
    row = _combined_row(profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, eval_delta=None)
    assert _resolve(row, associated=True) is None


def test_an_unparseable_fen_is_refused():
    row = _combined_row(profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    assert _resolve(row, associated=True, fen="not-a-fen") is None


# --------------------------------------------------------------------------- #
# the move-only branch
# --------------------------------------------------------------------------- #
def _move_only_row(row_id=3, profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, **over) -> _Row:
    data = {
        "evidence_contract_id": MOVE_COMPLETE,
        "move_uci": "d2d4",
        "played_eval": -30,
        "played_eval_mate": None,
        "classification": "good",
        "eval_delta": 50,
        "best_move_uci": None,
        "best_line_uci": None,
        "best_eval": None,
        "best_eval_mate": None,
        **_stamped(profile),
    }
    data.update(over)
    return _Row(row_id, **data)


def _position_only_row(row_id=4, profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, **over) -> _Row:
    data = {
        "evidence_contract_id": POSITION_COMPLETE,
        "best_move_uci": "e2e4",
        "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5",
        "best_eval": 20,
        "best_eval_mate": None,
        **_stamped(profile),
    }
    data.update(over)
    return _Row(row_id, **data)


def test_a_move_only_row_pairs_when_the_recomputed_delta_matches():
    """White to move: best 20, played -30 -> a clamped loss of 50."""
    tuple_ = _resolve(
        _move_only_row(),
        associated=True,
        position_row=_position_only_row(),
    )
    assert tuple_ is not None
    assert tuple_.eval_delta == 50
    assert tuple_.best_move_uci == "e2e4"
    assert tuple_.played_eval == -30


def test_a_move_only_row_is_refused_when_the_delta_disagrees():
    assert (
        _resolve(
            _move_only_row(eval_delta=5),
            associated=True,
            position_row=_position_only_row(),
        )
        is None
    )


# --------------------------------------------------------------------------- #
# read-time classification revalidation (§6)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "corruption",
    [
        {"played_eval": 55},                 # unequal CP facts under matching UCIs
        {"played_eval_mate": 4},             # unequal mate facts
        {"eval_delta": 7},                   # nonzero loss on an exact-best row
    ],
)
def test_direct_corruption_of_an_exact_best_browser_row_is_refused(corruption):
    """The classifier short-circuits to ``best`` whenever the UCIs match, so
    matching UCIs alone must not bless unequal CP facts, unequal mate facts, or a
    nonzero loss. Each is refused SEPARATELY even though the stored label is
    ``best`` and the UCIs agree."""
    row = _combined_row(profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, **corruption)
    assert _resolve(row, associated=True) is None


def test_a_rederived_label_mismatch_is_refused():
    """A non-best move whose stored label does not follow from the two scores."""
    row = _combined_row(
        profile=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
        move_uci="a2a3",
        played_eval=-400,
        eval_delta=420,
        classification="good",  # a 420cp drop is not 'good'
    )
    assert _resolve(row, associated=True) is None


def test_an_authoritative_row_keeps_its_stored_classification_behavior():
    """The read-time revalidation is for NON-authoritative rows only; canonical
    rows retain their stored classification behavior byte-for-byte."""
    row = _combined_row(
        profile=CANONICAL_PROFILE_ID,
        move_uci="a2a3",
        played_eval=-400,
        eval_delta=420,
        classification="good",
    )
    assert _resolve(row) is not None


# --------------------------------------------------------------------------- #
# the shared validator itself
# --------------------------------------------------------------------------- #
def _valid(**over) -> bool:
    kwargs = {
        "mover": "white",
        "played_uci": "e2e4",
        "best_uci": "e2e4",
        "played_eval": 20,
        "played_eval_mate": None,
        "best_eval": 20,
        "best_eval_mate": None,
        "eval_delta": 0,
        "classification": "best",
    }
    kwargs.update(over)
    return validate_root_alternative_classification(**kwargs)


def test_validator_accepts_a_clean_exact_best():
    assert _valid() is True


def test_validator_accepts_a_black_to_move_non_best():
    # Black to move: white-relative best -20, played +180 -> a 200cp loss for black.
    assert _valid(
        mover="black",
        played_uci="g8f6",
        best_uci="e7e5",
        best_eval=-20,
        played_eval=180,
        eval_delta=200,
        classification="blunder",
    ) is True


def test_validator_accepts_a_mate_transition():
    # White throws away a forced mate: best is mate-in-3, played is +50cp.
    assert _valid(
        played_uci="a2a3",
        best_uci="e2e4",
        best_eval=1000,
        best_eval_mate=3,
        played_eval=50,
        eval_delta=950,
        classification="blunder",
    ) is True


@pytest.mark.parametrize(
    "over",
    [
        {"played_eval": 55},        # matching UCIs, unequal CP
        {"played_eval_mate": 2},    # matching UCIs, unequal mate
        {"eval_delta": 3},          # matching UCIs, nonzero loss
        {"eval_delta": None},       # a null delta is NOT a proven zero
        {"classification": "good"}, # matching UCIs must be labelled 'best'
    ],
)
def test_validator_rejects_every_exact_best_inconsistency(over):
    assert _valid(**over) is False


def test_validator_rejects_a_stored_best_on_nonmatching_ucis():
    assert _valid(played_uci="a2a3", classification="best") is False


def test_validator_rejects_an_unknown_label():
    assert _valid(classification="brilliant") is False


def test_validator_rejects_missing_ucis():
    assert _valid(best_uci=None) is False


def test_validator_rejects_missing_scores():
    assert _valid(
        played_uci="a2a3", classification="good", played_eval=None, eval_delta=0
    ) is False


# --------------------------------------------------------------------------- #
# ingress parity: the endpoint runs the SAME helper
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "over,expected",
    [
        # Matching UCIs with unequal CP / mate facts reach the classification
        # validator and are refused there — the classifier's ``is_best`` short
        # circuit alone would have blessed them.
        ({"played_eval": 55}, EVIDENCE_CLASSIFICATION_MISMATCH),
        ({"played_eval_mate": 2}, EVIDENCE_CLASSIFICATION_MISMATCH),
        # A nonzero delta under matching UCIs is arithmetically impossible for the
        # resolver-complete-v2 contract, so ingress refuses it one step EARLIER.
        # The read path has no such upstream check, which is exactly why the
        # validator must reject it there too (see the direct-corruption test above).
        ({"eval_delta": 3}, EVIDENCE_CONTRACT_UNSATISFIED),
    ],
)
def test_ingress_refuses_the_same_exact_best_inconsistencies(over, expected):
    """Proves both paths call the same FULL helper rather than the classifier's
    ``is_best`` short circuit."""
    from app.api.session import (
        AnalysisEvidenceRow,
        _build_evidence_cache_row,
    )

    payload = {
        "fen": START,
        "move_uci": "e2e4",
        "best_move_uci": "e2e4",
        "best_line_uci": ["e2e4", "e7e5"],
        "played_eval": 30,
        "best_eval": 30,
        "eval_delta": 0,
        "classification": "best",
        "played_eval_mate": None,
        "best_eval_mate": None,
    }
    payload.update(over)
    cache_row, reason = _build_evidence_cache_row(
        AnalysisEvidenceRow(**payload), BROWSER_ANALYSIS_MULTIPV_PROFILE_ID
    )
    assert cache_row is None
    assert reason == expected


def test_ingress_still_accepts_a_clean_exact_best():
    from app.api.session import AnalysisEvidenceRow, _build_evidence_cache_row

    cache_row, reason = _build_evidence_cache_row(
        AnalysisEvidenceRow(
            fen=START,
            move_uci="e2e4",
            best_move_uci="e2e4",
            best_line_uci=["e2e4", "e7e5"],
            played_eval=30,
            best_eval=30,
            eval_delta=0,
            classification="best",
        ),
        BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
    )
    assert reason is None
    assert cache_row["classification"] == "best"


# --------------------------------------------------------------------------- #
# provenance sanity
# --------------------------------------------------------------------------- #
def test_descriptors_identify_their_source_row_exactly():
    row = _combined_row(row_id=42, profile=CANONICAL_PROFILE_ID)
    position = describe_position_row(row, source_table=POSITION_STORAGE_SOURCE)
    move = describe_move_row(row)
    assert position.source_id == 42 and position.source_table == POSITION_STORAGE_SOURCE
    assert move.source_id == 42 and move.source_table == CACHE_SOURCE
    # Different tables -> NOT the same source, even at the same primary key.
    assert position.same_source(move) is False
    assert move.same_source(describe_move_row(row)) is True


def test_descriptor_identity_snapshot_covers_every_identity_field():
    row = _combined_row(profile=CANONICAL_PROFILE_ID)
    move = describe_move_row(row)
    profile = get_profile(CANONICAL_PROFILE_ID)
    assert dict(move.identity) == {f: getattr(profile, f) for f in IDENTITY_FIELDS}
