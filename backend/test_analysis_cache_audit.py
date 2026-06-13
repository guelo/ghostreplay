"""Unit tests for the pure analysis_cache audit classifier.

The classifier is anchored on the write guard's own validity gate
(``incoming_is_valid``): the rows it flags for invalidation are exactly the rows
the comparator would reject as ``INVALID_INCOMING_KEEP`` today, split into a
default tier (profile-claiming) and a legacy opt-in tier (profile-less).
"""
from app.analysis_cache_audit import (
    Category,
    classify_row,
    should_invalidate,
)
from app.analysis_profiles import IDENTITY_FIELDS, get_profile
from app.evidence_contracts import MINIMAL_PLAYED_EVAL, RESOLVER_COMPLETE_V2

PROFILE_ID = "canonical-sf18-depth24-linux-v1"
FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def _identity_columns() -> dict:
    profile = get_profile(PROFILE_ID)
    return {f: getattr(profile, f) for f in IDENTITY_FIELDS}


def _canonical_row(**overrides) -> dict:
    row = {
        "fen_before": FEN,
        "move_uci": "e2e4",
        "move_san": "e4",
        "source": "precomputed",
        "analysis_profile_id": PROFILE_ID,
        "evidence_contract_id": RESOLVER_COMPLETE_V2,
        "best_move_uci": "e2e4",
        "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5",
        "played_eval": 10,
        "played_eval_mate": None,
        "best_eval": 30,
        "best_eval_mate": None,
        "eval_delta": 20,  # white to move: 30 - 10
        "classification": "good",
    }
    row.update(_identity_columns())
    row.update(overrides)
    return row


def _legacy_row(**overrides) -> dict:
    """A profile-less row (all profile/identity columns NULL)."""
    row = _canonical_row(analysis_profile_id=None, evidence_contract_id=None)
    row.update({f: None for f in IDENTITY_FIELDS})
    row.update(overrides)
    return row


def test_canonical_trusted_row_is_kept():
    cat = classify_row(_canonical_row())
    assert cat is Category.CANONICAL_TRUSTED
    assert not should_invalidate(cat, include_legacy_null=False)


def test_profile_claim_with_broken_identity_is_contaminated():
    cat = classify_row(_canonical_row(engine_build="0" * 64))
    assert cat is Category.CONTAMINATED_PROFILE_CLAIM
    assert should_invalidate(cat, include_legacy_null=False)


def test_profile_claim_failing_contract_is_contaminated():
    cat = classify_row(_canonical_row(eval_delta=999))
    assert cat is Category.CONTAMINATED_PROFILE_CLAIM
    assert should_invalidate(cat, include_legacy_null=False)


def test_unknown_profile_id_is_contaminated():
    cat = classify_row(_canonical_row(analysis_profile_id="totally-made-up-v9"))
    assert cat is Category.CONTAMINATED_PROFILE_CLAIM
    assert should_invalidate(cat, include_legacy_null=False)


def test_legacy_row_with_satisfied_contract_is_kept():
    # Profile-less but declares a contract its evidence satisfies -> the guard
    # would accept it, so it is kept in every mode.
    cat = classify_row(
        _legacy_row(evidence_contract_id=MINIMAL_PLAYED_EVAL, played_eval=12)
    )
    assert cat is Category.LEGACY_VALID
    assert not should_invalidate(cat, include_legacy_null=False)
    assert not should_invalidate(cat, include_legacy_null=True)


def test_legacy_null_contract_with_evidence_is_invalid_opt_in():
    # Has evidence but a NULL contract -> the guard rejects it (contract not
    # satisfied). Default keeps it; the legacy opt-in invalidates it. (Finding 1.)
    cat = classify_row(_legacy_row())  # null contract, full evidence
    assert cat is Category.LEGACY_INVALID
    assert not should_invalidate(cat, include_legacy_null=False)
    assert should_invalidate(cat, include_legacy_null=True)


def test_legacy_empty_row_is_invalid_opt_in():
    cat = classify_row(
        _legacy_row(
            best_move_uci=None, best_move_san=None, best_line_uci=None,
            played_eval=None, best_eval=None, eval_delta=None, classification=None,
        )
    )
    assert cat is Category.LEGACY_INVALID
    assert should_invalidate(cat, include_legacy_null=True)
