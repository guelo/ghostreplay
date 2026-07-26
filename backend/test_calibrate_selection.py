"""g-p4ih-selection: select_candidate binding checks, gates, and SelectionResult semantics.

Every unit test here hand-builds a SelectionInputs with the exact required cells, honestly
copied fingerprints/constants, one consistent clock, and FABRICATED-but-in-domain scores —
which is exactly what the bead's "the checks prove consistency, not authenticity" claim
predicts: such an input SELECTS a winner, while any stamp/type/cardinality/role/clock/domain
inconsistency raises SelectionBindingError.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import inspect
import json
import math
import platform
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import chess
import pytest

import scripts.calibrate_opening_scores_v2 as cal
from app.opening_cache import evidence_derivation_fingerprint

UTC = timezone.utc
AS_OF = datetime(2025, 6, 1, tzinfo=UTC)
HEX = "abcdef0123456789" * 4  # 64-char lowercase hex
SEALED_REV = "0123456789abcdef" * 2 + "01234567"  # 40-char lowercase hex
BOUNDARY = "macos-hdiutil-udro"
OS_BUILD = "macOS 26.0 (25C56)"


@pytest.fixture(autouse=True)
def _pretend_this_process_is_sealed(monkeypatch):
    """Every cohort here carries scorer_source_verified_preexec=True, which after
    g-release-os-boundary means "this run was sealed inside an OS boundary".

    The binding checks re-derive that from module constants rather than trusting the object
    — cohort.runtime_image_sha256 must equal the digest of the volume THIS interpreter is
    running from, exactly as runtime_python must equal this interpreter's version. A
    synthetic cohort therefore needs a synthetic process to be consistent with, and stubbing
    the constant is how the suite says "assume the sealed case" without building a 900MB
    volume for arithmetic tests. The real thing is exercised in
    test_release_calibration_launcher.py::TestSealedCheckout.
    """
    monkeypatch.setattr(cal, "_RUNTIME_IMAGE_SHA256", HEX)

OPENING = cal.RELEASE_GUARD_OPENING_KEY
CHILD = cal.RELEASE_GUARD_CHILD_OPENING_KEY
REQUIRED = cal._required_cells(cal.RELEASE_ARMS)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _cell_score(named_scores=(), named_score_map=None):
    return cal.CellScore(
        named_scores=tuple(named_scores),
        named_score_map=dict(named_score_map or {}),
        named_confidence_map={},
        synthetic_score=None,
        synthetic_confidence=None,
        observation_total=0,
    )


# The candidate operand set that makes an ARM-1 cell ADMITTED (see module design):
#   parent=25 (D) <= child=30 (C) + eps; fold multipliers equal (0.5); report-stage
#   identities exact; opp/leak within tolerance of the A-grade reference; user-tp D <= C.
_CAND_OPS = dict(
    synth_black_root_score=25.0,
    synth_caro_child_score=30.0,
    synth_root_coverage_fraction=0.25,
    synth_user_turn_multiplier=0.5,
    synth_opp_turn_multiplier=0.5,
    synth_user_turn_pre_fold_quality=50.0,
    synth_opp_turn_score=50.0,
    synth_opp_turn_pre_fold_quality=100.0,
    broad_guard_opp_score=75.0,
    specialist_pre_fold_quality=80.0,
    user_tp_score=25.0,
)
# The CURRENT reference: user-tp grades A, opp/leak references are A-grade.
_REF_OPS = dict(
    synth_black_root_score=40.0,
    synth_caro_child_score=45.0,
    synth_root_coverage_fraction=0.5,
    synth_user_turn_multiplier=1.0,
    synth_opp_turn_multiplier=1.0,
    synth_user_turn_pre_fold_quality=40.0,
    synth_opp_turn_score=40.0,
    synth_opp_turn_pre_fold_quality=40.0,
    broad_guard_opp_score=80.0,
    specialist_pre_fold_quality=80.0,
    user_tp_score=80.0,
)
# ARM-2 admissible operands: user multiplier < 1 strict, opp multiplier == 1.0 exact.
_ARM2_OPS = dict(_CAND_OPS)
_ARM2_OPS.update(
    synth_user_turn_multiplier=0.5,
    synth_opp_turn_multiplier=1.0,
    synth_opp_turn_score=100.0,   # pfq_opp(100) * 1.0
    synth_opp_turn_pre_fold_quality=100.0,
)
# ARM-1 raw-fail operands (multiplier equality broken -> fold_symmetry_i fails).
_ARM1_FAIL_OPS = dict(_CAND_OPS)
_ARM1_FAIL_OPS.update(synth_opp_turn_multiplier=0.6, synth_opp_turn_score=60.0)

# The two quantile-pool halves; pooled per cell -> [10,30,50,70,90,100].
_POOL_A = (10.0, 30.0, 50.0)
_POOL_B = (70.0, 90.0, 100.0)


def _dcr(ops):
    return cal.DiagnosticCellResult(**ops)


def _ops_for(cell, *, arm1_ops=_CAND_OPS, arm2_ops=_ARM2_OPS):
    if cell == cal.CURRENT_SM_V2_3_CELL:
        return _REF_OPS
    if cell.report_fold_scope == "user":  # ARM-2 cells
        return arm2_ops
    return arm1_ops


def _provenance(**overrides):
    base = dict(
        artifact_sha256=HEX,
        artifact_as_of=AS_OF,
        graph_fingerprint=HEX,
        roots_fingerprint=HEX,
        captured_model_version="sm-v2-3",
        schema_version=cal.ARTIFACT_SCHEMA_VERSION,
        pair_count=4,
        min_observations=cal.DEFAULT_MIN_OBSERVATIONS,
        cohort_rules=cal.COHORT_RULES_ID,
        evidence_derivation_fingerprint=evidence_derivation_fingerprint(),
        release_guard_opening_key=OPENING,
        release_guard_child_opening_key=CHILD,
    )
    base.update(overrides)
    return cal.ArtifactProvenance(**base)


def _quantile_pair(pair_id, surrogate, color, pool):
    grid = {cell: _cell_score(named_scores=pool) for cell in REQUIRED}
    return cal.ScoredPair(pair_id, "subject-quant", surrogate, "quantile", color, grid)


def _guard_pair(pair_id, surrogate, color, opening, child=None):
    def score_map():
        m = {OPENING: opening}
        if child is not None:
            m[CHILD] = child
        return m
    grid = {cell: _cell_score(named_score_map=score_map()) for cell in REQUIRED}
    return cal.ScoredPair(pair_id, "subject-guard", surrogate, "release_guard", color, grid)


def _cohort(pairs=None, **overrides):
    if pairs is None:
        pairs = _default_pairs()
    base = dict(
        provenance=_provenance(),
        as_of=AS_OF,
        model_version=cal.SCORE_MODEL_VERSION,
        scorer_contract_id=cal.REPORT_SCORER_CONTRACT_ID,
        source_revision=None,
        source_dirty_paths=(),
        scorer_source_digest=HEX,
        scorer_source_verified_preexec=True,
        provenance_record_sha256=HEX,
        runtime_python=platform.python_version(),
        runtime_chess_version=chess.__version__,
        execution_boundary=BOUNDARY,
        runtime_image_sha256=HEX,
        os_build=OS_BUILD,
        sealed_revision=SEALED_REV,
        config_fingerprints={cell: cal._cfg_fp(cell) for cell in REQUIRED},
        required_cells=frozenset(REQUIRED),
        manifest_pair_ids=frozenset(p.pair_id for p in pairs),
        pairs=tuple(pairs),
    )
    base.update(overrides)
    # The runtime attestation is all-or-nothing with the verified flag, and the binding checks
    # enforce that pairing (g-release-os-boundary). Derive it here so a test that flips the
    # flag gets a COHERENT cohort rather than one that could not come out of a real run —
    # unless it named the field itself, which is how the validator's own tests reach the
    # inconsistent shapes on purpose.
    if not base["scorer_source_verified_preexec"]:
        for name in ("execution_boundary", "runtime_image_sha256", "os_build", "sealed_revision"):
            if name not in overrides:
                base[name] = None
    return cal.ScoredCalibrationCohort(**base)


def _default_pairs(black_opening=25.0, black_child=30.0, white_opening=27.0):
    return [
        _quantile_pair("pair-0", 1, "black", _POOL_A),
        _quantile_pair("pair-1", 2, "black", _POOL_B),
        _guard_pair("pair-2", 3, "white", white_opening),
        _guard_pair("pair-3", 4, "black", black_opening, child=black_child),
    ]


def _diagnostics(ops_for=_ops_for, **overrides):
    cells = {cell: _dcr(ops_for(cell)) for cell in REQUIRED}
    for cell in cal.DEMO_CELLS:
        cells[cell] = _dcr(_CAND_OPS)
    fps = {cell: cal._cfg_fp(cell) for cell in cells}
    base = dict(
        as_of=AS_OF,
        model_version=cal.SCORE_MODEL_VERSION,
        scorer_contract_id=cal.REPORT_SCORER_CONTRACT_ID,
        config_fingerprints=fps,
        cells=cells,
    )
    base.update(overrides)
    return cal.DiagnosticSuite(**base)


def _inputs(cohort=None, diagnostics=None):
    return cal.SelectionInputs(
        cohort=cohort if cohort is not None else _cohort(),
        diagnostics=diagnostics if diagnostics is not None else _diagnostics(),
    )


def _arm1_winner_inputs():
    return _inputs()


def _arm2_winner_inputs():
    """ARM-1 wholly inadmissible (fold multiplier equality broken); ARM-2 admissible."""
    def ops_for(cell):
        if cell == cal.CURRENT_SM_V2_3_CELL:
            return _REF_OPS
        if cell.report_fold_scope == "user":
            return _ARM2_OPS
        if cell.report_fold_p != 0.0:  # ARM-1 swept cells
            return _ARM1_FAIL_OPS
        return _CAND_OPS
    return _inputs(diagnostics=_diagnostics(ops_for=ops_for))


def _no_ship_inputs():
    """Both arms fail fold_symmetry_i."""
    fail2 = dict(_ARM2_OPS)
    fail2.update(synth_opp_turn_multiplier=0.7, synth_opp_turn_score=70.0)  # opp != 1.0

    def ops_for(cell):
        if cell == cal.CURRENT_SM_V2_3_CELL:
            return _REF_OPS
        if cell.report_fold_scope == "user":
            return fail2
        if cell.report_fold_p != 0.0:
            return _ARM1_FAIL_OPS
        return _CAND_OPS
    return _inputs(diagnostics=_diagnostics(ops_for=ops_for))


def _collision_inputs():
    """Raw gates pass but every arm cell's quantile pool is all-equal -> CutoffCollision."""
    flat = (50.0, 50.0, 50.0)
    pairs = [
        _quantile_pair("pair-0", 1, "black", flat),
        _quantile_pair("pair-1", 2, "black", flat),
        _guard_pair("pair-2", 3, "white", 27.0),
        _guard_pair("pair-3", 4, "black", 25.0, child=30.0),
    ]
    return _inputs(cohort=_cohort(pairs=pairs))


# ---------------------------------------------------------------------------
# Happy path: ARM-1 winner, ARM-2 winner, no-ship, collision
# ---------------------------------------------------------------------------


class TestHappyPaths:
    def test_arm1_winner_selects(self):
        r = cal.select_candidate(_arm1_winner_inputs())
        assert not r.no_ship
        assert r.winner is not None
        assert r.winner.role == "arm1"
        assert r.winner.p == 0.25  # tie on identical pools -> smallest p
        assert r.winner.admitted
        assert r.winner_cutoffs == r.winner.provisional_cutoffs
        assert r.no_ship_reason is None

    def test_candidate_enumeration_is_eight_in_pinned_order(self):
        r = cal.select_candidate(_arm1_winner_inputs())
        assert len(r.candidates) == 8
        seq = [(c.role, c.p) for c in r.candidates]
        assert seq == [
            ("arm1", 0.25), ("arm1", 0.5), ("arm1", 0.75), ("arm1", 1.0),
            ("arm2", 0.25), ("arm2", 0.5), ("arm2", 0.75), ("arm2", 1.0),
        ]

    def test_arm1_win_leaves_arm2_lazy(self):
        r = cal.select_candidate(_arm1_winner_inputs())
        arm2 = [c for c in r.candidates if c.role == "arm2"]
        for c in arm2:
            assert c.evaluated is False
            assert c.raw_gates == () and c.derived_gates == ()
            assert c.cutoffs_outcome is None and c.provisional_cutoffs is None
            assert c.distribution is None and c.rejection_reason is None

    def test_arm2_winner_when_arm1_wholly_inadmissible(self):
        r = cal.select_candidate(_arm2_winner_inputs())
        assert not r.no_ship
        assert r.winner is not None and r.winner.role == "arm2"
        arm1 = [c for c in r.candidates if c.role == "arm1"]
        assert all(c.evaluated and not c.admitted for c in arm1)

    def test_no_ship_is_first_class(self):
        r = cal.select_candidate(_no_ship_inputs())
        assert r.no_ship
        assert r.winner is None and r.winner_cutoffs is None and r.winner_binding is None
        assert r.no_ship_reason
        assert "fold_symmetry_i" in r.no_ship_reason

    def test_collision_records_cutoff_collision(self):
        r = cal.select_candidate(_collision_inputs())
        assert r.no_ship
        colliders = [c for c in r.candidates if c.rejection_reason == "cutoff_collision"]
        assert colliders
        c = colliders[0]
        assert c.cutoffs_outcome is not None and not c.cutoffs_outcome.passed
        assert c.provisional_cutoffs is None

    def test_determinism_equal_result(self):
        i = _arm1_winner_inputs()
        assert cal.select_candidate(i) == cal.select_candidate(i)


# ---------------------------------------------------------------------------
# B1 reference channel
# ---------------------------------------------------------------------------


class TestB1Reference:
    def test_present_on_ship_and_no_ship(self):
        for inputs in (_arm1_winner_inputs(), _no_ship_inputs()):
            r = cal.select_candidate(inputs)
            assert r.b1_reference.cell == cal.B1_CELL
            assert r.b1_reference.role == "b1"
            assert isinstance(r.b1_reference.distribution, cal.DistributionStats)
            assert r.b1_reference.real_black_1e4_raw == 25.0

    def test_b1_excluded_from_candidates_and_reason(self):
        r = cal.select_candidate(_arm1_winner_inputs())
        assert all(c.role in ("arm1", "arm2") for c in r.candidates)
        assert cal.B1_CELL not in {c.cell for c in r.candidates}

    def test_reference_result_invariants(self):
        with pytest.raises(ValueError):
            cal.ReferenceResult(cal.B1_CELL, "arm1", cal.distribution_stats([1.0, 2.0]), None, 1.0, None)
        with pytest.raises(ValueError):
            cal.ReferenceResult(cal.ORIGINAL_CELL, "b1", cal.distribution_stats([1.0, 2.0]), None, 1.0, None)
        with pytest.raises(ValueError):
            # cutoffs None but grade non-None
            cal.ReferenceResult(cal.B1_CELL, "b1", cal.distribution_stats([1.0, 2.0]), None, 1.0, "C")


# ---------------------------------------------------------------------------
# Release policy is not caller-controlled
# ---------------------------------------------------------------------------


class TestReleasePolicy:
    def test_public_signature_is_single_argument(self):
        params = list(inspect.signature(cal.select_candidate).parameters)
        assert params == ["inputs"]

    def test_private_arm_swap_elects_arm2(self):
        inputs = _arm1_winner_inputs()  # ARM-1 legitimately wins
        assert cal.select_candidate(inputs).winner.role == "arm1"
        swapped = cal._select_candidate(inputs, (cal.ARM2, cal.ARM1))
        assert swapped.winner is not None and swapped.winner.role == "arm2"


# ---------------------------------------------------------------------------
# Arm descriptors + enumeration
# ---------------------------------------------------------------------------


class TestArmDescriptors:
    def test_release_arms(self):
        assert [a.role for a in cal.RELEASE_ARMS] == ["arm1", "arm2"]
        assert cal.ARM1.fold_symmetry_check_count == 3
        assert cal.ARM2.fold_symmetry_check_count == 4

    def test_scope_cell_mismatch_raises(self):
        with pytest.raises(ValueError):
            cal.Arm("arm1", "all", cal.arm2_cells(), 3)  # arm2 cells carry scope "user"

    def test_bad_p_grid_raises(self):
        with pytest.raises(ValueError):
            cal.Arm("arm1", "all", cal.arm1_cells((0.25, 0.5)), 3)

    def test_bad_fold_count_raises(self):
        with pytest.raises(ValueError):
            cal.Arm("arm1", "all", cal.arm1_cells(), 4)


# ---------------------------------------------------------------------------
# GateCheck / GateOutcome: passed is DERIVED and RE-VERIFIED
# ---------------------------------------------------------------------------


class TestGateCheck:
    def test_contradictory_passed_raises(self):
        with pytest.raises(ValueError):
            cal.GateCheck("x", 100, 1, "<=", True)   # 100 <= 1 is False
        with pytest.raises(ValueError):
            cal.GateCheck("x", 1, 100, "<=", False)  # 1 <= 100 is True

    def test_in_requires_tuple_str_limit(self):
        with pytest.raises(ValueError):
            cal.GateCheck("x", "C", "{C,D,F}", "in", True)  # str limit -> substring
        with pytest.raises(ValueError):
            cal.GateCheck("x", "C", frozenset({"C"}), "in", True)  # set limit
        ok = cal.GateCheck("x", "C", ("C", "D", "F"), "in", True)
        assert ok.passed

    def test_substring_regression_pinned(self):
        # With the tuple, "," is NOT in NOT_A_READY_GRADES (membership, not substring).
        assert "," not in cal.NOT_A_READY_GRADES
        assert cal.NOT_A_READY_GRADES == ("C", "D", "F")

    def test_ordering_ops_reject_bool_and_str(self):
        with pytest.raises(ValueError):
            cal.GateCheck("x", True, 1, "<=", True)
        with pytest.raises(ValueError):
            cal.GateCheck("x", "5", 1, "<=", True)

    def test_gateoutcome_verdict_matches_checks(self):
        good = cal.GateCheck("a", 1, 2, "<=", True)
        bad = cal.GateCheck("b", 3, 2, "<=", False)
        with pytest.raises(ValueError):
            cal.GateOutcome("opp_guard", "raw", True, (good, bad), "d")
        with pytest.raises(ValueError):
            cal.GateOutcome("opp_guard", "raw", False, (good,), "d")
        with pytest.raises(ValueError):
            cal.GateOutcome("not_a_gate", "raw", True, (good,), "d")


# ---------------------------------------------------------------------------
# CandidateResult self-consistency
# ---------------------------------------------------------------------------


class TestCandidateResult:
    def _admitted(self):
        r = cal.select_candidate(_arm1_winner_inputs())
        return r.winner

    def test_p_must_match_cell(self):
        w = self._admitted()
        with pytest.raises(ValueError):
            dataclasses.replace(w, p=0.9)

    def test_role_must_be_valid(self):
        w = self._admitted()
        with pytest.raises(ValueError):
            dataclasses.replace(w, role="b1")

    def test_lazy_must_be_empty(self):
        with pytest.raises(ValueError):
            cal.CandidateResult(
                cell=cal.ARM1.cells[0], role="arm1", p=0.25, evaluated=False,
                raw_gates=(), cutoffs_outcome=None, provisional_cutoffs=None,
                distribution=None, derived_gates=(), admitted=True,  # contradiction
                rejection_reason=None, order_keys=None,
            )

    def test_cutoffs_outcome_must_be_cutoff_derivation(self):
        w = self._admitted()
        raw = w.raw_gates[0]  # a passing GateOutcome named 'parent_le_child_raw'
        assert raw.passed and raw.name != "cutoff_derivation"
        with pytest.raises(ValueError):
            dataclasses.replace(w, cutoffs_outcome=raw)

    def test_cutoff_repeated_checks_rejected(self):
        # A cutoff_derivation with four repeated look-alike checks (wrong names) is refused.
        w = self._admitted()
        dup = cal.GateCheck("cutoff_d_lt_c", 1, 2, "<", True)
        fake = cal.GateOutcome("cutoff_derivation", "derived", True, (dup, dup, dup, dup), "x")
        with pytest.raises(ValueError):
            dataclasses.replace(w, cutoffs_outcome=fake)

    def test_cutoff_fabricated_operands_rejected(self):
        # Correct names/ops but operands not matching the emitted provisional_cutoffs.
        w = self._admitted()
        names = ("cutoff_d_lt_c", "cutoff_c_lt_b", "cutoff_b_lt_a", "cutoff_alert_lt_watch")
        checks = tuple(cal.GateCheck(n, 1, 2, "<", True) for n in names)
        fake = cal.GateOutcome("cutoff_derivation", "derived", True, checks, "x")
        with pytest.raises(ValueError):
            dataclasses.replace(w, cutoffs_outcome=fake)

    def _raw_failed(self):
        r = cal.select_candidate(_no_ship_inputs())
        return next(c for c in r.candidates
                    if c.evaluated and c.rejection_reason in cal.RAW_GATE_ORDER)

    def test_raw_rejected_cannot_carry_distribution(self):
        c = self._raw_failed()
        assert c.distribution is None  # step 2 never ran
        dist = cal.distribution_stats([1.0, 2.0])
        with pytest.raises(ValueError):
            dataclasses.replace(c, distribution=dist, order_keys=cal._order_keys(dist, c.p))


# ---------------------------------------------------------------------------
# SelectionResult truth table
# ---------------------------------------------------------------------------


class TestSelectionResultTruthTable:
    def _ship(self):
        inputs = _arm1_winner_inputs()
        return inputs.cohort, cal.select_candidate(inputs)

    def test_ship_with_no_ship_true_raises(self):
        cohort, r = self._ship()
        with pytest.raises(ValueError):
            cal.SelectionResult(
                candidates=r.candidates, winner=r.winner, winner_cutoffs=r.winner_cutoffs,
                no_ship=True, no_ship_reason=None, b1_reference=r.b1_reference,
                cohort_provenance=r.cohort_provenance, winner_binding=r.winner_binding,
                cohort=cohort,
            )

    def test_ship_forged_binding_raises(self):
        cohort, r = self._ship()
        forged = dataclasses.replace(r.winner_binding, artifact_sha256="0" * 64)
        with pytest.raises(ValueError):
            cal.SelectionResult(
                candidates=r.candidates, winner=r.winner, winner_cutoffs=r.winner_cutoffs,
                no_ship=False, no_ship_reason=None, b1_reference=r.b1_reference,
                cohort_provenance=r.cohort_provenance, winner_binding=forged,
                cohort=cohort,
            )

    def test_forged_provenance_disagreeing_with_cohort_raises(self):
        cohort, r = self._ship()
        forged_prov = dataclasses.replace(cohort.provenance, artifact_sha256="0" * 64)
        with pytest.raises(ValueError):
            cal.SelectionResult(
                candidates=r.candidates, winner=r.winner, winner_cutoffs=r.winner_cutoffs,
                no_ship=False, no_ship_reason=None, b1_reference=r.b1_reference,
                cohort_provenance=forged_prov, winner_binding=r.winner_binding,
                cohort=cohort,
            )

    def test_truncated_candidates_raises(self):
        cohort, r = self._ship()
        with pytest.raises(ValueError):
            cal.SelectionResult(
                candidates=r.candidates[:1], winner=r.winner, winner_cutoffs=r.winner_cutoffs,
                no_ship=False, no_ship_reason=None, b1_reference=r.b1_reference,
                cohort_provenance=r.cohort_provenance, winner_binding=r.winner_binding,
                cohort=cohort,
            )

    def test_swapped_candidate_cell_raises(self):
        # A non-winner candidate whose cell is swapped for an unscored look-alike sharing
        # role and p (but a different GridCell identity) is rejected — the sweep is keyed
        # on (role, cell), not (role, p).
        cohort, r = self._ship()
        cands = list(r.candidates)
        i = next(i for i, c in enumerate(cands)
                 if c.role == "arm1" and c.p == 0.5 and c is not r.winner)
        fake_cell = cal.GridCell(lcb_z=9.0, coverage_fold="off", report_fold_p=0.5,
                                 report_fold_scope="all")
        cands[i] = dataclasses.replace(cands[i], cell=fake_cell)
        with pytest.raises(ValueError):
            cal.SelectionResult(
                candidates=tuple(cands), winner=r.winner, winner_cutoffs=r.winner_cutoffs,
                no_ship=False, no_ship_reason=None, b1_reference=r.b1_reference,
                cohort_provenance=r.cohort_provenance, winner_binding=r.winner_binding,
                cohort=cohort,
            )

    def test_no_ship_reason_pinned_order_and_stable(self):
        r1 = cal.select_candidate(_no_ship_inputs())
        r2 = cal.select_candidate(_no_ship_inputs())
        assert r1.no_ship_reason == r2.no_ship_reason
        # canonical "<code>×<count>" segments, rendered in REASON_CODE_ORDER
        assert "×" in r1.no_ship_reason
        codes = [seg.split("×")[0] for seg in r1.no_ship_reason.split(", ")]
        idx = [cal.REASON_CODE_ORDER.index(c) for c in codes]
        assert idx == sorted(idx)


# ---------------------------------------------------------------------------
# Deep immutability
# ---------------------------------------------------------------------------


class TestDeepImmutability:
    def test_frozen_and_no_mutable_containers(self):
        r = cal.select_candidate(_arm1_winner_inputs())
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.cohort_provenance.pair_count = 0
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.winner_binding.artifact_sha256 = "x"
        with pytest.raises(TypeError):
            r.winner_binding.source_dirty_paths[0] = "x"
        _assert_no_mutable_containers(r)


def _assert_no_mutable_containers(obj, seen=None):
    seen = seen if seen is not None else set()
    if id(obj) in seen:
        return
    seen.add(id(obj))
    if isinstance(obj, (dict, list, set, frozenset)):
        raise AssertionError(f"mutable/unordered container found: {type(obj).__name__}")
    if isinstance(obj, MappingProxyType):
        for k, v in obj.items():
            _assert_no_mutable_containers(k, seen)
            _assert_no_mutable_containers(v, seen)
        return
    if isinstance(obj, tuple):
        for x in obj:
            _assert_no_mutable_containers(x, seen)
        return
    if dataclasses.is_dataclass(obj):
        for f in dataclasses.fields(obj):
            _assert_no_mutable_containers(getattr(obj, f.name), seen)


# ---------------------------------------------------------------------------
# Binding checks 0-5
# ---------------------------------------------------------------------------


class TestBindingChecks:
    def test_healthy_selects(self):
        assert cal.select_candidate(_inputs()).winner is not None

    def test_runtime_python_skew(self):
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(runtime_python="3.0.0")))

    def test_runtime_chess_skew(self):
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(runtime_chess_version="0.0.0")))

    def test_tampered_release_guard_key(self):
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(provenance=_provenance(release_guard_opening_key="x"))))

    def test_pair_count_disagreement(self):
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(provenance=_provenance(pair_count=9))))

    def test_schema_version_skew(self):
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(provenance=_provenance(schema_version=99))))

    def test_bad_hex_digest(self):
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(provenance=_provenance(artifact_sha256="NOTHEX"))))

    def test_model_version_skew(self):
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(model_version="sm-vX")))

    def test_clock_skew(self):
        other = datetime(2025, 6, 2, tzinfo=UTC)
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(as_of=other)))

    def test_config_fingerprint_mismatch(self):
        fps = {cell: cal._cfg_fp(cell) for cell in REQUIRED}
        fps[cal.ORIGINAL_CELL] = "wrong"
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(config_fingerprints=fps)))


class TestBindingCheck0WrapperTypes:
    def test_naive_datetime_raises(self):
        naive = datetime(2025, 6, 1)
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(provenance=_provenance(artifact_as_of=naive),
                                                        as_of=naive)))

    def test_datetime_subclass_with_lying_eq_raises(self):
        class LyingDatetime(datetime):
            def __eq__(self, other):
                return True
            def __hash__(self):
                return 0
        lying = LyingDatetime(2025, 6, 1, tzinfo=UTC)
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(as_of=lying)))

    def test_bool_pair_count_raises(self):
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(provenance=_provenance(pair_count=True))))

    def test_empty_captured_model_version_raises(self):
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(provenance=_provenance(captured_model_version=""))))

    def test_diagnostics_stamp_str_subclass_raises(self):
        class LyingStr(str):
            def __eq__(self, other):
                return True
            def __hash__(self):
                return hash("x")
        # A str subclass overriding __eq__ satisfies the check-1(c) comparison against
        # anything; check 0 must reject it by EXACT type before that comparison runs.
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(diagnostics=_diagnostics(model_version=LyingStr("wrong"))))
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(diagnostics=_diagnostics(scorer_contract_id=LyingStr("wrong"))))

    def test_mutable_list_in_source_dirty_paths_raises(self):
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(source_dirty_paths=(["audit"],))))

    def test_provenance_subclass_with_mutable_field_raises(self):
        @dataclasses.dataclass(frozen=True)
        class EvilProvenance(cal.ArtifactProvenance):
            extra: list = dataclasses.field(default_factory=list)
        prov = EvilProvenance(
            artifact_sha256=HEX, artifact_as_of=AS_OF, graph_fingerprint=HEX,
            roots_fingerprint=HEX, captured_model_version="sm-v2-3",
            schema_version=cal.ARTIFACT_SCHEMA_VERSION, pair_count=4,
            min_observations=cal.DEFAULT_MIN_OBSERVATIONS, cohort_rules=cal.COHORT_RULES_ID,
            evidence_derivation_fingerprint=evidence_derivation_fingerprint(),
            release_guard_opening_key=OPENING, release_guard_child_opening_key=CHILD,
        )
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(provenance=prov)))

    def test_str_surrogate_raises(self):
        pairs = _default_pairs()
        bad = dataclasses.replace(pairs[0], surrogate_user_id="1")
        pairs[0] = bad
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(pairs=pairs)))

    def test_bool_surrogate_raises(self):
        pairs = _default_pairs()
        pairs[0] = dataclasses.replace(pairs[0], surrogate_user_id=True)
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(pairs=pairs)))

    def test_gridcell_subclass_key_raises(self):
        class EvilCell(cal.GridCell):
            pass
        evil = EvilCell(lcb_z=0.0, coverage_fold="off")
        fps = {cell: cal._cfg_fp(cell) for cell in REQUIRED}
        fps[evil] = "x"
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(config_fingerprints=fps)))

    def test_cellscore_subclass_raises(self):
        class EvilScore(cal.CellScore):
            pass
        pairs = _default_pairs()
        grid = dict(pairs[0].grid)
        cell = next(iter(grid))
        grid[cell] = EvilScore(
            named_scores=grid[cell].named_scores, named_score_map={},
            named_confidence_map={}, synthetic_score=None, synthetic_confidence=None,
            observation_total=0,
        )
        pairs[0] = cal.ScoredPair("pair-0", "subject-quant", 1, "quantile", "black", grid)
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(pairs=pairs)))

    def test_guard_score_out_of_range_raises(self):
        pairs = _default_pairs(black_opening=101.0)  # out of [0, 100]
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(pairs=pairs)))


class TestMultiGateFailure:
    def test_rejection_reason_is_earliest_of_two_failures(self):
        # fold_symmetry_i (index 2) and opp_guard (index 3) both fail; the earlier wins.
        ops = dict(_CAND_OPS, synth_opp_turn_multiplier=0.6, synth_opp_turn_score=60.0,
                   broad_guard_opp_score=30.0)

        def ops_for(cell):
            if cell == cal.CURRENT_SM_V2_3_CELL:
                return _REF_OPS
            if cell.report_fold_scope == "user":
                return _ARM2_OPS  # keep ARM-2 out of the way; irrelevant to the assertion
            return ops
        r = cal.select_candidate(_inputs(diagnostics=_diagnostics(ops_for=ops_for)))
        arm1 = [c for c in r.candidates if c.role == "arm1" and c.evaluated]
        assert arm1
        for c in arm1:
            failed = [g.name for g in c.raw_gates if not g.passed]
            assert "fold_symmetry_i" in failed and "opp_guard" in failed
            assert c.rejection_reason == "fold_symmetry_i"  # earlier in RAW_GATE_ORDER


class TestBindingCheck3PairBinding:
    def test_unknown_role_raises(self):
        pairs = _default_pairs()
        pairs[0] = dataclasses.replace(pairs[0], cohort_role="control")
        # keep the partition inputs valid otherwise
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(pairs=pairs)))

    def test_non_aligned_surrogate_raises(self):
        pairs = _default_pairs()
        pairs[0] = dataclasses.replace(pairs[0], surrogate_user_id=5)
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(pairs=pairs)))

    def test_third_guard_raises(self):
        pairs = _default_pairs()
        pairs.append(_guard_pair("pair-4", 5, "white", 27.0))
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(pairs=pairs, provenance=_provenance(pair_count=5))))

    def test_one_quantile_pair_raises(self):
        pairs = [
            _quantile_pair("pair-0", 1, "black", _POOL_A + _POOL_B),
            _guard_pair("pair-1", 2, "white", 27.0),
            _guard_pair("pair-2", 3, "black", 25.0, child=30.0),
        ]
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(pairs=pairs, provenance=_provenance(pair_count=3))))

    def test_zero_pooled_scores_raises(self):
        pairs = [
            _quantile_pair("pair-0", 1, "black", ()),
            _quantile_pair("pair-1", 2, "black", ()),
            _guard_pair("pair-2", 3, "white", 27.0),
            _guard_pair("pair-3", 4, "black", 25.0, child=30.0),
        ]
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(pairs=pairs)))

    def test_guard_shape_error_reraised_with_cause(self):
        # black guard missing the CHILD key -> ReleaseGuardShapeError -> SelectionBindingError
        pairs = _default_pairs()
        pairs[3] = _guard_pair("pair-3", 4, "black", 25.0, child=None)
        with pytest.raises(cal.SelectionBindingError) as ei:
            cal.select_candidate(_inputs(cohort=_cohort(pairs=pairs)))
        assert isinstance(ei.value.__cause__, cal.ReleaseGuardShapeError)


class TestBindingCheck5OperandDomain:
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), 1.5, -0.1])
    def test_bad_coverage_raises(self, bad):
        def ops_for(cell):
            base = dict(_ops_for(cell))
            if cell in cal.ARM1.cells:
                base = dict(base, synth_root_coverage_fraction=bad)
            return base
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(diagnostics=_diagnostics(ops_for=ops_for)))

    def test_bool_operand_raises(self):
        def ops_for(cell):
            base = dict(_ops_for(cell))
            if cell in cal.ARM1.cells:
                base = dict(base, synth_root_coverage_fraction=True)
            return base
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(diagnostics=_diagnostics(ops_for=ops_for)))

    def test_out_of_range_pool_score_raises(self):
        pairs = _default_pairs()
        pairs[0] = _quantile_pair("pair-0", 1, "black", (10.0, 101.0, 50.0))
        with pytest.raises(cal.SelectionBindingError):
            cal.select_candidate(_inputs(cohort=_cohort(pairs=pairs)))


# ---------------------------------------------------------------------------
# Optimization invariance
# ---------------------------------------------------------------------------


def _reachable_functions(entry_name):
    """Transitively collect same-module functions AND dataclass __post_init__ validators
    reachable from ``entry_name``. A constructor call ``GateCheck(...)`` reaches
    ``GateCheck.__post_init__`` — so a future assert inside any validator is in scope
    (the reviewer's gap: the old walker followed only function calls, never constructors)."""
    src = inspect.getsource(cal)
    tree = ast.parse(src)
    funcs = {}            # module-level function name -> node
    class_methods = {}    # class name -> {method name -> node}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            funcs[node.name] = node
        elif isinstance(node, ast.ClassDef):
            class_methods[node.name] = {
                item.name: item for item in node.body if isinstance(item, ast.FunctionDef)
            }
    reachable = {}
    stack = [entry_name]
    while stack:
        key = stack.pop()
        if key in reachable:
            continue
        if key in funcs:
            node = funcs[key]
        elif "." in key:
            cls, meth = key.split(".", 1)
            node = class_methods.get(cls, {}).get(meth)
        elif key in class_methods:
            # A bare class name reached via a constructor call -> its __post_init__ validator.
            if "__post_init__" in class_methods[key]:
                stack.append(f"{key}.__post_init__")
            continue
        else:
            continue
        if node is None:
            continue
        reachable[key] = node
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                name = sub.func.id
                if name in funcs:
                    stack.append(name)
                elif name in class_methods:
                    stack.append(name)  # constructor -> resolves to __post_init__ above
            elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                if sub.func.attr in funcs:
                    stack.append(sub.func.attr)
    return reachable


class TestOptimizationInvariance:
    def test_no_assert_in_reachable_set(self):
        reachable = _reachable_functions("select_candidate")
        assert "grade_rank" in reachable  # sanity: the walk reaches the primitive
        # The dataclass validators ARE in scope (constructor calls reach __post_init__),
        # so a future assert inside any of them would be caught rather than silently
        # stripped under -O.
        for validator in (
            "GateCheck.__post_init__", "GateOutcome.__post_init__",
            "CandidateResult.__post_init__", "SelectionResult.__post_init__",
            "ReferenceResult.__post_init__",
        ):
            assert validator in reachable, f"{validator} not reached by the AST walk"
        for name, node in reachable.items():
            for sub in ast.walk(node):
                assert not isinstance(sub, ast.Assert), f"assert reachable via {name}"

    def test_ast_check_would_fail_on_an_assert(self):
        # A control: a function body containing an assert IS detected as an ast.Assert.
        tree = ast.parse("def f():\n    assert True\n")
        assert any(isinstance(n, ast.Assert) for n in ast.walk(tree))

    def test_distribution_only_via_distribution_stats(self):
        # No selection-owned reachable function calls _percentiles directly; only the
        # g-p4ih.1.2 primitives derive_cutoffs / distribution_stats may.
        reachable = _reachable_functions("select_candidate")
        allowed = {"derive_cutoffs", "distribution_stats"}
        for name, node in reachable.items():
            if name in allowed:
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                    assert sub.func.id != "_percentiles", f"{name} calls _percentiles directly"


# ---------------------------------------------------------------------------
# (ii-b) real-data gates, on constructed inputs
# ---------------------------------------------------------------------------


class TestRealDataGates:
    def _inputs_with_black(self, opening, child):
        pairs = _default_pairs(black_opening=opening, black_child=child)
        return _inputs(cohort=_cohort(pairs=pairs))

    def test_real_black_1e4_high_grade_not_admitted(self):
        # Black 1.e4 grades A (95) -> real_black_1e4_grade / real_parent_le_child_raw fail.
        r = cal.select_candidate(self._inputs_with_black(95.0, 96.0))
        assert r.no_ship or (r.winner and r.winner.role == "arm2")
        arm1 = [c for c in r.candidates if c.role == "arm1"]
        assert all(not c.admitted for c in arm1)

    def test_real_parent_le_child_raw_rejects_parent_above_child(self):
        # raw parent (30) > child (25) + eps -> real_parent_le_child_raw fails at step 1.
        r = cal.select_candidate(self._inputs_with_black(30.0, 25.0))
        arm1 = [c for c in r.candidates if c.role == "arm1"]
        failing = [c for c in arm1 if c.rejection_reason == "real_parent_le_child_raw"]
        assert failing


# ---------------------------------------------------------------------------
# Pinned tolerances + total order
# ---------------------------------------------------------------------------


class TestTolerances:
    def test_fold_identity_inclusive_1e9(self):
        # residual of exactly 1e-9 passes; 1.000001e-9 fails.
        ok = cal.GateCheck("id", 1.0e-9, cal.FOLD_IDENTITY_TOL, "<=", True)
        assert ok.passed
        with pytest.raises(ValueError):
            cal.GateCheck("id", 1.000001e-9, cal.FOLD_IDENTITY_TOL, "<=", True)

    def test_arm2_multiplier_exactly_one_fails(self):
        # user multiplier == 1.0 (coverage saturated) -> ARM-2 fold gate fails (strict <).
        ops = dict(_ARM2_OPS, synth_user_turn_multiplier=1.0, synth_black_root_score=50.0,
                   synth_user_turn_pre_fold_quality=50.0)

        def ops_for(cell):
            if cell == cal.CURRENT_SM_V2_3_CELL:
                return _REF_OPS
            if cell.report_fold_scope == "user":
                return ops
            return _ARM1_FAIL_OPS  # force ARM-1 out so ARM-2 is evaluated
        r = cal.select_candidate(_inputs(diagnostics=_diagnostics(ops_for=ops_for)))
        arm2 = [c for c in r.candidates if c.role == "arm2" and c.evaluated]
        assert arm2 and all(not c.admitted for c in arm2)

    def test_order_keys_spread_then_p(self):
        wide = cal.distribution_stats([0.0, 100.0])
        narrow = cal.distribution_stats([40.0, 60.0])
        assert cal._order_keys(wide, 0.5) > cal._order_keys(narrow, 0.25)
        # equal distribution -> smaller p wins (larger -p).
        assert cal._order_keys(narrow, 0.25) > cal._order_keys(narrow, 0.5)


# ---------------------------------------------------------------------------
# Bounded security claim: preexec echoed, not gated
# ---------------------------------------------------------------------------


class TestBoundedSecurityClaim:
    def test_preexec_echoed_not_gated(self):
        # A False verified-preexec flag still SELECTS (not gated here), and travels onto
        # winner_binding for Phase 3 to refuse.
        r = cal.select_candidate(_inputs(cohort=_cohort(scorer_source_verified_preexec=False)))
        assert r.winner is not None
        assert r.winner_binding.scorer_source_verified_preexec is False

    def test_winner_distribution_matches_recompute(self):
        r = cal.select_candidate(_arm1_winner_inputs())
        pool = cal._pool_for(r.winner.cell,
                             tuple(p for p in _cohort().pairs if p.cohort_role == "quantile"))
        assert r.winner.distribution == cal.distribution_stats(pool)


# ---------------------------------------------------------------------------
# Optimization invariance: -O vs plain, full normalized result, five decision classes
# ---------------------------------------------------------------------------


def _norm(obj):
    """Deterministic JSON-able normalization of any selection object graph."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, cal.GridCell):
        return cal._cell_axes(obj)
    if isinstance(obj, (tuple, list)):
        return [_norm(x) for x in obj]
    if isinstance(obj, MappingProxyType) or isinstance(obj, dict):
        return {str(_norm(k)): _norm(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if dataclasses.is_dataclass(obj):
        return {f.name: _norm(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    return str(obj)


def normalized_result(kind):
    builders = {
        "arm1": _arm1_winner_inputs,
        "arm2": _arm2_winner_inputs,
        "noship": _no_ship_inputs,
        "collision": _collision_inputs,
    }
    result = cal.select_candidate(builders[kind]())
    return _norm(result)


_RUNNER = """
import json, sys
import test_calibrate_selection as t
import scripts.calibrate_opening_scores_v2 as cal
# What the autouse _pretend_this_process_is_sealed fixture does, restated for a process
# pytest never touches: the synthetic cohorts claim a sealed run, and the binding checks
# compare that claim against THIS interpreter's constants.
cal._RUNTIME_IMAGE_SHA256 = t.HEX
kind = sys.argv[1]
if kind == "binding":
    try:
        t.cal.select_candidate(t._inputs(cohort=t._cohort(runtime_python="0.0.0")))
    except cal.SelectionBindingError:
        sys.exit(3)
    sys.exit(0)
print(json.dumps(t.normalized_result(kind), sort_keys=True))
"""


class TestMinusOSubprocess:
    @pytest.mark.parametrize("kind", ["arm1", "arm2", "noship", "collision"])
    def test_full_result_byte_identical_under_O(self, kind):
        cwd = str(cal.__file__).rsplit("/scripts/", 1)[0]
        plain = subprocess.run([sys.executable, "-c", _RUNNER, kind],
                               capture_output=True, text=True, cwd=cwd, timeout=180)
        opt = subprocess.run([sys.executable, "-O", "-c", _RUNNER, kind],
                             capture_output=True, text=True, cwd=cwd, timeout=180)
        assert plain.returncode == 0, plain.stderr
        assert opt.returncode == 0, opt.stderr
        assert plain.stdout == opt.stdout
        assert plain.stdout.strip()

    def test_binding_failure_raises_nonzero_under_O(self):
        cwd = str(cal.__file__).rsplit("/scripts/", 1)[0]
        opt = subprocess.run([sys.executable, "-O", "-c", _RUNNER, "binding"],
                             capture_output=True, text=True, cwd=cwd, timeout=180)
        assert opt.returncode == 3, (opt.returncode, opt.stderr)


# ---------------------------------------------------------------------------
# g-p4ih-release-cli: build_redacted_summary + validate_summary_schema against
# real SelectionResult fixtures (ship, no-ship, lazy arm-2, B1 collision).
# ---------------------------------------------------------------------------

MOUNTED = "1234567890abcdef" * 4  # 64-char lowercase hex, the launcher's mounted digest


def _summary_for(inputs):
    """A result + its summary, built exactly as the CLI builds them."""
    cohort = dataclasses.replace(inputs.cohort, provenance_record_sha256=MOUNTED)
    result = cal.select_candidate(cal.SelectionInputs(cohort=cohort, diagnostics=inputs.diagnostics))
    dumped = cal.serialize_full(result)
    result_sha256 = hashlib.sha256(dumped.encode("utf-8")).hexdigest()
    return result, cal.build_redacted_summary(result, MOUNTED, result_sha256)


class TestSummaryAllowlist:
    def test_winner_binding_key_tuple_matches_the_dataclass(self):
        # ENUMERATED, not derived: a field ADDED to WinnerBinding must fail HERE rather than
        # silently escaping the allowlist (or silently vanishing from the summary).
        assert list(cal._SUMMARY_BINDING_KEYS) == [
            f.name for f in dataclasses.fields(cal.WinnerBinding)
        ]
        assert len(cal._SUMMARY_BINDING_KEYS) == 20

    def test_cohort_provenance_keys_are_the_eleven_gating_ones(self):
        fields = {f.name for f in dataclasses.fields(cal.ArtifactProvenance)}
        assert set(cal._SUMMARY_PROVENANCE_KEYS) == fields - {"pair_count"}
        assert "pair_count" not in cal._SUMMARY_PROVENANCE_KEYS

    def test_the_result_object_alone_cannot_supply_the_record_digest(self):
        # The three facts that force mounted_digest to be a PARAMETER.
        cohort_field = next(
            f for f in dataclasses.fields(cal.SelectionResult) if f.name == "cohort_provenance"
        )
        assert cohort_field is not None
        assert not hasattr(cal.select_candidate(_arm1_winner_inputs()), "cohort")  # unstored InitVar
        assert "provenance_record_sha256" not in {
            f.name for f in dataclasses.fields(cal.ArtifactProvenance)
        }
        no_ship = cal.select_candidate(_no_ship_inputs())
        assert no_ship.winner_binding is None  # the obvious carrier is absent on no-ship


class TestSummaryFixtures:
    @pytest.mark.parametrize("builder", [
        _arm1_winner_inputs, _arm2_winner_inputs, _no_ship_inputs, _collision_inputs,
    ])
    def test_every_fixture_validates(self, builder):
        _result, summary = _summary_for(builder())
        cal.validate_summary_schema(summary, MOUNTED)
        assert set(summary) == set(cal._SUMMARY_TOP_KEYS)

    def test_ship_agrees_three_ways_on_the_record_digest(self):
        _result, summary = _summary_for(_arm1_winner_inputs())
        assert summary["no_ship"] is False
        assert summary["provenance_record_sha256"] == MOUNTED
        assert summary["winner_binding"]["provenance_record_sha256"] == MOUNTED

    def test_no_ship_still_carries_the_record_digest(self):
        _result, summary = _summary_for(_no_ship_inputs())
        assert summary["no_ship"] is True
        assert summary["winner"] is summary["winner_cutoffs"] is summary["winner_binding"] is None
        assert summary["provenance_record_sha256"] == MOUNTED
        cal.validate_summary_schema(summary, MOUNTED)

    def test_lazy_arm2_candidates_are_honestly_empty(self):
        _result, summary = _summary_for(_arm1_winner_inputs())
        lazy = [c for c in summary["candidates"] if not c["evaluated"]]
        assert lazy  # arm-1 won, so arm-2 was never reached
        for candidate in lazy:
            assert candidate["gates"] == []
            assert candidate["admitted"] is False
            assert candidate["rejection_reason"] is None

    def test_b1_collision_is_a_bit_and_never_a_grade(self):
        _result, summary = _summary_for(_collision_inputs())
        assert summary["b1_reference"] == {"role": "b1", "cutoffs_collided": True}
        _result, shipped = _summary_for(_arm1_winner_inputs())
        assert shipped["b1_reference"] == {"role": "b1", "cutoffs_collided": False}

    def test_the_winners_cutoffs_are_the_named_carve_out(self):
        result, summary = _summary_for(_arm1_winner_inputs())
        assert summary["winner_cutoffs"] == {
            "a": result.winner_cutoffs.a, "b": result.winner_cutoffs.b,
            "c": result.winner_cutoffs.c, "d": result.winner_cutoffs.d,
            "alert": result.winner_cutoffs.alert, "watch": result.winner_cutoffs.watch,
        }
        # ...and no REJECTED candidate's cutoffs ride along with them.
        for candidate in summary["candidates"]:
            assert set(candidate) == set(cal._SUMMARY_CANDIDATE_KEYS)


class TestSummaryValidation:
    def _ship(self):
        return _summary_for(_arm1_winner_inputs())[1]

    def test_a_dropped_cutoffs_outcome_fails(self):
        summary = self._ship()
        winner = next(c for c in summary["candidates"] if c["admitted"])
        winner["gates"] = [g for g in winner["gates"] if g["name"] != "cutoff_derivation"]
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)

    def test_emptied_derived_gates_fail(self):
        summary = self._ship()
        winner = next(c for c in summary["candidates"] if c["admitted"])
        winner["gates"] = [g for g in winner["gates"] if g["scale"] == "raw"] + [
            g for g in winner["gates"] if g["name"] == "cutoff_derivation"
        ]
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)

    def test_a_reordered_gate_sequence_fails(self):
        summary = self._ship()
        winner = next(c for c in summary["candidates"] if c["admitted"])
        winner["gates"] = list(reversed(winner["gates"]))
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)

    def test_an_extra_key_at_any_level_fails(self):
        for mutate in (
            lambda s: s.__setitem__("extra", 1),
            lambda s: s["cohort_provenance"].__setitem__("pair_count", 4),
            lambda s: s["winner_binding"].__setitem__("extra", 1),
            lambda s: s["b1_reference"].__setitem__("real_black_1e4_grade", "A"),
            lambda s: s["candidates"][0].__setitem__("distribution", {}),
            lambda s: s["candidates"][0]["gates"] and s["candidates"][0]["gates"][0].__setitem__("detail", "x"),
        ):
            summary = self._ship()
            mutate(summary)
            with pytest.raises(cal.SummarySchemaError):
                cal.validate_summary_schema(summary, MOUNTED)

    def test_a_mismatched_record_digest_fails(self):
        summary = self._ship()
        summary["provenance_record_sha256"] = "f" * 64
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)

    def test_a_mismatched_binding_digest_fails(self):
        summary = self._ship()
        summary["winner_binding"]["provenance_record_sha256"] = "f" * 64
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)

    def test_cutoff_ordering_is_enforced(self):
        summary = self._ship()
        summary["winner_cutoffs"]["d"] = summary["winner_cutoffs"]["a"] + 1
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)

    def test_a_p_outside_the_domain_fails(self):
        summary = self._ship()
        summary["winner"]["p"] = 0.0
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)

    def test_b1_is_never_a_candidate_or_winner_role(self):
        summary = self._ship()
        summary["winner"]["role"] = "b1"
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)
        summary = self._ship()
        summary["candidates"][0]["role"] = "b1"
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)

    def test_a_truncated_candidate_sweep_fails(self):
        summary = self._ship()
        summary["candidates"] = summary["candidates"][:-1]
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)

    def test_an_off_inventory_reason_code_fails(self):
        summary = self._ship()
        summary["no_ship_reason_codes"] = ["not_a_gate"]
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)

    def test_a_free_form_no_ship_reason_never_reaches_the_summary(self):
        _result, summary = _summary_for(_no_ship_inputs())
        assert "no_ship_reason" not in summary
        assert all(code in cal._SUMMARY_REASON_CODES for code in summary["no_ship_reason_codes"])

    def test_cross_field_ship_consistency_is_enforced(self):
        summary = self._ship()
        summary["no_ship"] = True
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)
        summary = self._ship()
        summary["winner_binding"] = None
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)


class TestSummaryDecisionConsistency:
    """Key sets and enums do not stop a summary that CONTRADICTS itself. These are the
    cross-checks that make an approval record mean what it says."""

    def _ship(self):
        return _summary_for(_arm1_winner_inputs())[1]

    def test_an_empty_reason_list_on_a_no_ship_fails(self):
        _result, summary = _summary_for(_no_ship_inputs())
        assert summary["no_ship_reason_codes"]  # the honest value is non-empty
        summary["no_ship_reason_codes"] = []
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)

    def test_reason_codes_must_be_the_candidates_own_rejections(self):
        summary = self._ship()
        summary["no_ship_reason_codes"] = ["leak"]  # in the enum, sorted, and untrue
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)

    def test_a_duplicated_candidate_standing_in_for_a_dropped_one_fails(self):
        summary = self._ship()
        summary["candidates"][1] = copy.deepcopy(summary["candidates"][0])
        assert len(summary["candidates"]) == 8  # the COUNT is still right
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)

    def test_a_renamed_candidate_identity_fails(self):
        summary = self._ship()
        summary["candidates"][0]["p"] = 0.99
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)

    def test_a_rejected_candidate_named_as_winner_fails(self):
        summary = self._ship()
        rejected = next(
            c for c in summary["candidates"] if not c["admitted"] and c["evaluated"]
        ) if any(not c["admitted"] and c["evaluated"] for c in summary["candidates"]) else None
        if rejected is None:  # arm-1 sweep is wholly admitted; use the lazy arm-2 instead
            rejected = next(c for c in summary["candidates"] if not c["evaluated"])
        summary["winner"] = {"role": rejected["role"], "p": rejected["p"]}
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)

    def test_a_winner_naming_no_candidate_at_all_fails(self):
        summary = self._ship()
        summary["winner"] = {"role": "arm2", "p": 0.99}
        with pytest.raises(cal.SummarySchemaError):
            cal.validate_summary_schema(summary, MOUNTED)

    def test_a_no_ship_carrying_an_admitted_candidate_fails(self):
        # A SHIP result relabelled as no-ship: every candidate stays internally consistent,
        # so only the cross-check between the verdict and the candidate set catches it.
        summary = self._ship()
        summary.update(no_ship=True, winner=None, winner_cutoffs=None, winner_binding=None)
        with pytest.raises(cal.SummarySchemaError, match="admitted candidate"):
            cal.validate_summary_schema(summary, MOUNTED)
