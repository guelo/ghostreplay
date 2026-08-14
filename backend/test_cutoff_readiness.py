from __future__ import annotations

import dataclasses
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.calibrate_opening_scores_v2 as cal
import test_calibrate_opening_scores as tc


AS_OF = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
SCORES = (1.0, 3.0, 7.0, 10.0, 20.0, 30.0, 45.0, 55.0)


def _pairs(
    *, normal=10, drill=2, scores=SCORES, count=20,
    scores_by_subject=None, modes_by_subject=None,
):
    return tuple(
        cal.CutoffReadinessPair(
            pair_id=f"pair-{index:02d}",
            subject_id=f"subject-{index:02d}",
            player_color="white" if index % 2 == 0 else "black",
            session_mode_counts=(
                modes_by_subject[index]
                if modes_by_subject is not None else cal.SessionModeCounts(normal, drill)
            ),
            named_scores=tuple(
                scores_by_subject[index] if scores_by_subject is not None else scores
            ),
        )
        for index in range(count)
    )


def _report(
    *,
    artifact="1" * 64,
    as_of=AS_OF,
    pairs=None,
    baseline=None,
    captured_model=None,
):
    return cal.build_cutoff_readiness_report(
        artifact_sha256=artifact,
        provenance_record_sha256="2" * 64,
        artifact_as_of=as_of,
        captured_model_version=captured_model or cal.SCORE_MODEL_VERSION,
        scored_model_version=cal.SCORE_MODEL_VERSION,
        config_fingerprint=cal._cfg_fp(cal.SM_V2_5_DEFAULT_CELL),
        scorer_source_digest_value="3" * 64,
        pairs=_pairs() if pairs is None else pairs,
        baseline_report=baseline,
    )


def _passing_report():
    baseline = _report(artifact="4" * 64, as_of=AS_OF - timedelta(days=15))
    return _report(artifact="5" * 64, baseline=baseline)


def _rehash(report):
    body = {key: value for key, value in report.items() if key != "report_sha256"}
    report["report_sha256"] = __import__("hashlib").sha256(
        cal._canonical_dumps(body)
    ).hexdigest()
    return report


def _check(report, name):
    return next(check for check in report["checks"] if check["name"] == name)


def _historical(report, *, schema=None, policy=None, captured=None, scored=None, config=None):
    archived = json.loads(cal._canonical_dumps(report))
    if schema is not None:
        archived["schema_version"] = schema
    if policy is not None:
        archived["policy"]["version"] = policy
    identity = archived["identity"]
    if captured is not None:
        identity["captured_model_version"] = captured
    if scored is not None:
        identity["scored_model_version"] = scored
    if config is not None:
        identity["config_fingerprint"] = config
    return _rehash(archived)


def _run_readiness_cli(
    tmp_path, monkeypatch, capsys, *, report=None, baseline_bytes=None,
    build=None, artifact_bytes=b"placeholder", preexisting_output=None,
):
    """Drive the readiness CLI against a store outside a synthetic repository root."""
    monkeypatch.setattr(cal, "_REPO_ROOT", tc._git_init(tmp_path / "repo"))
    store = tmp_path / "store"
    store.mkdir(exist_ok=True)
    artifact = store / "artifact.json"
    if artifact_bytes is not None:
        artifact.write_bytes(artifact_bytes)
    output = store / "readiness.json"
    if preexisting_output is not None:
        output.write_text(preexisting_output)
    baseline = None
    argv = [
        "cutoff-readiness", "--artifact", str(artifact),
        "--report-output", str(output),
    ]
    if baseline_bytes is not None:
        baseline = store / "baseline.json"
        baseline.write_bytes(baseline_bytes)
        argv.extend(["--baseline-report", str(baseline)])
    if build is not None:
        monkeypatch.setattr(cal, "_build_cutoff_readiness_from_artifact", build)
    elif report is not None:
        monkeypatch.setattr(
            cal, "_build_cutoff_readiness_from_artifact", lambda _a, _b: report
        )
    capsys.readouterr()
    code = cal.main(argv)
    captured = capsys.readouterr()
    return SimpleNamespace(
        code=code, out=captured.out, err=captured.err, output=output,
        artifact=artifact, baseline=baseline,
    )


def test_representative_two_snapshot_cohort_is_ready_but_never_authoritative():
    report = _passing_report()
    assert report["ready_for_recalibration"] is True
    assert report["reason_codes"] == []
    assert report["authorizes_cutoff_emission"] is False
    assert cal.CUTOFF_SUFFICIENCY_CRITERIA_VERSION == 0
    assert cal.CUTOFF_SUFFICIENCY_CRITERIA_VERSION < cal.MIN_CUTOFF_SUFFICIENCY_CRITERIA_VERSION
    assert not set(cal._READINESS_CHECK_NAMES) & set(cal._FITNESS_CHECK_NAMES)
    assert {"scored_model_current", "config_current"}.isdisjoint(
        cal._READINESS_CHECK_NAMES
    )
    assert all(check["passed"] for check in report["checks"])


def test_missing_temporal_baseline_fails_only_temporal_checks_on_otherwise_ready_data():
    report = _report()
    assert report["ready_for_recalibration"] is False
    assert report["reason_codes"] == [
        "temporal_baseline_age",
        "temporal_baseline_compatible",
        "temporal_grade_stability",
    ]


def test_stale_capture_model_is_visible_as_its_own_reason():
    baseline = _report(artifact="6" * 64, as_of=AS_OF - timedelta(days=15))
    report = _report(
        artifact="7" * 64,
        baseline=baseline,
        captured_model="sm-v2-4",
    )
    assert "captured_model_current" in report["reason_codes"]
    assert report["identity"]["scored_model_version"] == "sm-v2-5"


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"schema": 1}, id="schema-version"),
        pytest.param({"policy": 1}, id="policy"),
        pytest.param({"captured": "sm-v2-4"}, id="captured-model"),
        pytest.param({"scored": "sm-v2-4"}, id="scored-model"),
        pytest.param({"config": "c" * 64}, id="config-fingerprint"),
    ],
)
def test_archived_contract_drift_is_valid_but_temporally_incompatible(kwargs):
    if "captured" in kwargs:
        baseline = _report(
            artifact="6" * 64,
            as_of=AS_OF - timedelta(days=15),
            captured_model=kwargs["captured"],
        )
    else:
        baseline = _historical(
            _report(artifact="6" * 64, as_of=AS_OF - timedelta(days=15)), **kwargs
        )
    report = _report(artifact="7" * 64, baseline=baseline)
    assert report["stability"]["temporal"]["compatible"] is False
    assert "temporal_baseline_compatible" in report["reason_codes"]


def test_attempted_bootstrap_without_success_reports_why_p95_is_unavailable(
    monkeypatch,
):
    def all_collisions(**_kwargs):
        return {
            "requested_replicates": 1_000,
            "attempted_replicates": 1_000,
            "successful_replicates": 0,
            "cutoff_collision_count": 1_000,
            "insufficient_score_count": 0,
            "collision_rate": 1.0,
            "grade_reassignment_p95": None,
            "cutoff_intervals": None,
        }

    monkeypatch.setattr(cal, "_bootstrap_readiness", all_collisions)
    report = _report()
    check = _check(report, "bootstrap_grade_stability")
    assert check["measured"] == "no_successful_replicates"
    assert check["passed"] is False
    assert cal.validate_cutoff_readiness_report(report)


def test_drill_heavy_and_concentrated_population_reports_distinct_reasons():
    pairs = list(_pairs(normal=1, drill=20))
    pairs[0] = dataclasses.replace(
        pairs[0], session_mode_counts=cal.SessionModeCounts(1, 500)
    )
    report = _report(pairs=tuple(pairs))
    assert "normal_session_share" in report["reason_codes"]
    assert "subject_session_concentration" in report["reason_codes"]


def test_too_few_or_one_color_subjects_fail_census_without_reading_guards():
    pairs = tuple(
        dataclasses.replace(pair, player_color="white") for pair in _pairs()[:8]
    )
    report = _report(pairs=pairs)
    assert "subject_count" in report["reason_codes"]
    assert "black_subject_count" in report["reason_codes"]
    assert "normal_black_subject_count" in report["reason_codes"]


def test_white_and_normal_population_checks_have_independent_failing_operands():
    black_only = tuple(
        dataclasses.replace(pair, player_color="black") for pair in _pairs()
    )
    report = _report(pairs=black_only)
    assert not _check(report, "white_subject_count")["passed"]
    assert not _check(report, "normal_white_subject_count")["passed"]

    modes = tuple(
        cal.SessionModeCounts(100, 0) if index < 11 else cal.SessionModeCounts(0, 1)
        for index in range(20)
    )
    report = _report(pairs=_pairs(modes_by_subject=modes))
    assert not _check(report, "normal_subject_count")["passed"]
    assert _check(report, "normal_session_share")["passed"]


def test_named_score_concentration_has_a_real_failing_population():
    scores = [SCORES for _ in range(20)]
    scores[0] = SCORES * 5
    report = _report(pairs=_pairs(scores_by_subject=tuple(scores)))
    assert report["cohort"]["max_subject_named_score_share"] > 0.15
    assert not _check(report, "subject_score_concentration")["passed"]


def test_cutoff_collision_fails_derivation_loo_and_bootstrap_closed():
    baseline = _report(artifact="8" * 64, as_of=AS_OF - timedelta(days=15))
    report = _report(
        artifact="9" * 64, pairs=_pairs(scores=(50.0, 50.0)), baseline=baseline
    )
    assert report["candidate_cutoffs"] is None
    assert report["cutoff_derivation_status"] == "cutoff_collision"
    bootstrap = report["stability"]["bootstrap"]
    assert bootstrap["attempted_replicates"] == 0
    assert bootstrap["cutoff_collision_count"] == 0
    assert bootstrap["collision_rate"] is None
    assert _check(report, "bootstrap_grade_stability")["measured"] == "not_attempted"
    assert report["stability"]["temporal"]["compatible"] is False
    for reason in (
        "base_cutoffs_derivable",
        "leave_one_out_complete",
        "leave_one_out_grade_stability",
        "bootstrap_sample_sufficiency",
        "bootstrap_collision_stability",
        "bootstrap_grade_stability",
        "temporal_baseline_compatible",
    ):
        assert reason in report["reason_codes"]


def test_insufficient_scores_are_not_reported_as_collisions():
    sparse = [() for _ in range(20)]
    sparse[0] = SCORES
    report = _report(pairs=_pairs(scores_by_subject=tuple(sparse)))
    loo = report["stability"]["leave_one_subject_out"]["subjects"]
    failed = next(item for item in loo if item["subject_id"] == "subject-00")
    assert failed["failure_reason"] == "insufficient_scores"
    assert all(item["failure_reason"] != "cutoff_collision" for item in loo)

    bootstrap = report["stability"]["bootstrap"]
    assert bootstrap["attempted_replicates"] == 1_000
    assert bootstrap["insufficient_score_count"] > 0
    assert bootstrap["cutoff_collision_count"] == 0
    assert not _check(report, "bootstrap_sample_sufficiency")["passed"]

    one_subject = _report(pairs=_pairs(count=1))
    bootstrap = one_subject["stability"]["bootstrap"]
    assert bootstrap["attempted_replicates"] == 0
    assert bootstrap["collision_rate"] is None
    assert bootstrap["grade_reassignment_p95"] is None


def test_partial_loo_collision_and_measured_stability_failures_are_visible():
    scores = (
        0, 3, 3, 21, 22, 26, 31, 39, 47, 49,
        55, 55, 64, 67, 76, 77, 77, 77, 79, 88,
    )
    report = _report(
        artifact="a" * 64,
        pairs=_pairs(scores_by_subject=tuple((score,) for score in scores)),
    )
    loo = report["stability"]["leave_one_subject_out"]
    assert loo["successful_subjects"] == 19
    assert [
        item["subject_id"] for item in loo["subjects"]
        if item["failure_reason"] == "cutoff_collision"
    ] == ["subject-19"]
    bootstrap = report["stability"]["bootstrap"]
    assert 0 < bootstrap["cutoff_collision_count"] < 1_000
    assert bootstrap["collision_rate"] > 0.01
    assert bootstrap["grade_reassignment_p95"] > 0.10
    for name in (
        "leave_one_out_complete",
        "bootstrap_collision_stability",
        "bootstrap_grade_stability",
    ):
        assert not _check(report, name)["passed"]


def test_leave_one_out_grade_stability_fails_on_a_measured_reassignment():
    scores = (
        0, 5, 10, 17, 21, 23, 34, 42, 43, 47,
        48, 59, 64, 70, 77, 78, 86, 89, 90, 93,
    )
    report = _report(
        artifact="b" * 64,
        pairs=_pairs(scores_by_subject=tuple((score,) for score in scores)),
    )
    loo = report["stability"]["leave_one_subject_out"]
    assert loo["successful_subjects"] == 20
    assert loo["max_grade_reassignment_rate"] == 0.15
    assert not _check(report, "leave_one_out_grade_stability")["passed"]


def test_real_young_and_moved_baselines_fail_their_distinct_temporal_checks():
    young = _report(artifact="c" * 64, as_of=AS_OF - timedelta(days=5))
    young_report = _report(artifact="d" * 64, baseline=young)
    assert young_report["stability"]["temporal"]["compatible"] is True
    assert young_report["stability"]["temporal"]["age_days"] == 5.0
    assert not _check(young_report, "temporal_baseline_age")["passed"]

    moved = _report(
        artifact="e" * 64,
        as_of=AS_OF - timedelta(days=15),
        pairs=_pairs(scores=(1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0)),
    )
    moved_report = _report(artifact="f" * 64, baseline=moved)
    assert moved_report["stability"]["temporal"]["compatible"] is True
    assert moved_report["stability"]["temporal"]["grade_reassignment_rate"] == 0.625
    assert not _check(moved_report, "temporal_grade_stability")["passed"]


def test_every_readiness_check_has_passing_and_failing_fixture():
    passing = _passing_report()
    assert {check["name"] for check in passing["checks"] if check["passed"]} == set(
        cal._READINESS_CHECK_NAMES
    )

    drill_heavy = list(_pairs(normal=1, drill=20))
    drill_heavy[0] = dataclasses.replace(
        drill_heavy[0], session_mode_counts=cal.SessionModeCounts(1, 500)
    )
    white_only = tuple(
        dataclasses.replace(pair, player_color="white") for pair in _pairs()[:8]
    )
    black_only = tuple(
        dataclasses.replace(pair, player_color="black") for pair in _pairs()
    )
    sparse_normal = tuple(
        cal.SessionModeCounts(100, 0) if index < 11 else cal.SessionModeCounts(0, 1)
        for index in range(20)
    )
    concentrated_scores = [SCORES for _ in range(20)]
    concentrated_scores[0] = SCORES * 5
    young = _report(artifact="a" * 64, as_of=AS_OF - timedelta(days=5))
    moved = _report(
        artifact="b" * 64,
        as_of=AS_OF - timedelta(days=15),
        pairs=_pairs(scores=(1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0)),
    )
    reports = [
        _report(),
        _report(captured_model="sm-v2-4"),
        _report(pairs=tuple(drill_heavy)),
        _report(pairs=white_only),
        _report(pairs=black_only),
        _report(pairs=_pairs(modes_by_subject=sparse_normal)),
        _report(pairs=_pairs(scores_by_subject=tuple(concentrated_scores))),
        _report(pairs=_pairs(scores=(50.0, 50.0))),
        _report(artifact="c" * 64, baseline=young),
        _report(artifact="d" * 64, baseline=moved),
    ]
    failed = {
        check["name"]
        for report in reports
        for check in report["checks"]
        if not check["passed"]
    }
    assert failed == set(cal._READINESS_CHECK_NAMES)


def test_bootstrap_and_report_bytes_are_deterministic():
    baseline = _report(artifact="8" * 64, as_of=AS_OF - timedelta(days=15))
    first = _report(artifact="9" * 64, baseline=baseline)
    second = _report(artifact="9" * 64, baseline=baseline)
    assert first == second
    assert cal._canonical_dumps(first) == cal._canonical_dumps(second)


def test_full_validator_rejects_tamper_even_when_attacker_rehashes():
    report = _passing_report()
    report["authorizes_cutoff_emission"] = True
    _rehash(report)
    with pytest.raises(cal.ReadinessReportSchemaError, match="never authorize"):
        cal.validate_cutoff_readiness_report(report)


def test_full_validator_rejects_digest_mismatch_and_extra_fields():
    report = _passing_report()
    report["report_sha256"] = "0" * 64
    with pytest.raises(cal.ReadinessReportSchemaError, match="report_sha256"):
        cal.validate_cutoff_readiness_report(report)
    report = _passing_report()
    report["private_path"] = "/secret"
    with pytest.raises(cal.ReadinessReportSchemaError, match="closed key"):
        cal.validate_cutoff_readiness_report(report)


def test_full_validator_rejects_rehashed_cross_field_contradictions():
    report = _passing_report()
    report["cohort"]["session_mode_counts"]["normal"] += 1
    _rehash(report)
    with pytest.raises(cal.ReadinessReportSchemaError, match="session_mode_counts"):
        cal.validate_cutoff_readiness_report(report)

    report = _passing_report()
    subject_check = next(
        check for check in report["checks"] if check["name"] == "subject_count"
    )
    subject_check["measured"] += 1
    _rehash(report)
    with pytest.raises(cal.ReadinessReportSchemaError, match="private report operands"):
        cal.validate_cutoff_readiness_report(report)


def test_current_claiming_but_self_inconsistent_baseline_is_an_input_refusal():
    baseline = _report(artifact="1" * 64, as_of=AS_OF - timedelta(days=15))
    baseline["cohort"]["normal_subject_count"] -= 1
    _rehash(baseline)
    with pytest.raises(cal.BaselineReadinessReportError, match="current contract"):
        _report(artifact="2" * 64, baseline=baseline)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("scored_model_version", "sm-v2-4", "running model"),
        ("config_fingerprint", "0" * 64, "default-cell config"),
    ],
)
def test_current_report_scoring_identity_is_an_invariant_not_a_policy_check(
    field, value, match
):
    report = _passing_report()
    report["identity"][field] = value
    _rehash(report)
    with pytest.raises(cal.ReadinessReportSchemaError, match=match):
        cal.validate_cutoff_readiness_report(report)


def test_validator_rejects_loo_bootstrap_and_temporal_contradictions():
    report = _passing_report()
    report["stability"]["leave_one_subject_out"]["successful_subjects"] -= 1
    _rehash(report)
    with pytest.raises(cal.ReadinessReportSchemaError, match="loo successful count"):
        cal.validate_cutoff_readiness_report(report)

    report = _passing_report()
    report["stability"]["bootstrap"]["insufficient_score_count"] = 1
    _rehash(report)
    with pytest.raises(cal.ReadinessReportSchemaError, match="partition"):
        cal.validate_cutoff_readiness_report(report)

    report = _report()
    report["stability"]["temporal"]["grade_reassignment_rate"] = 0.0
    _rehash(report)
    with pytest.raises(cal.ReadinessReportSchemaError, match="fail-closed rate"):
        cal.validate_cutoff_readiness_report(report)


def test_validator_reaches_temporal_compatible_without_current_cutoffs_guard():
    report = _passing_report()
    report["cutoff_derivation_status"] = "cutoff_collision"
    report["candidate_cutoffs"] = None
    loo = report["stability"]["leave_one_subject_out"]
    loo.update({
        "successful_subjects": 0,
        "max_grade_reassignment_rate": 1.0,
        "subjects": [],
    })
    report["stability"]["bootstrap"] = {
        "requested_replicates": 1_000,
        "attempted_replicates": 0,
        "successful_replicates": 0,
        "cutoff_collision_count": 0,
        "insufficient_score_count": 0,
        "collision_rate": None,
        "grade_reassignment_p95": None,
        "cutoff_intervals": None,
    }
    _rehash(report)
    with pytest.raises(
        cal.ReadinessReportSchemaError,
        match="temporal compatibility requires current candidate cutoffs",
    ):
        cal.validate_cutoff_readiness_report(report)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"a": 5, "b": 5, "c": 3, "d": 1, "alert": 2, "watch": 5}, "grade"),
        ({"a": 6, "b": 5, "c": 3, "d": 1, "alert": 5, "watch": 5}, "tone"),
    ],
)
def test_cutoff_payload_ordering_is_closed(payload, match):
    with pytest.raises(cal.ReadinessReportSchemaError, match=match):
        cal._cutoffs_from_payload(payload)


@pytest.mark.parametrize(
    ("value", "label", "kwargs", "match"),
    [
        (True, "n", {}, "finite"),
        (math.inf, "n", {}, "finite"),
        (-0.1, "n", {"minimum": 0.0}, "below"),
        (1.1, "n", {"maximum": 1.0}, "above"),
    ],
)
def test_readiness_number_rejects_non_numbers_and_bounds(value, label, kwargs, match):
    with pytest.raises(cal.ReadinessReportSchemaError, match=match):
        cal._readiness_number(value, label, **kwargs)


@pytest.mark.parametrize("value", [None, "2026-08-01T12:00:00Z", "not-a-date"])
def test_readiness_timestamp_parser_rejects_noncanonical_values(value):
    with pytest.raises(cal.ReadinessReportSchemaError, match="canonical"):
        cal._parse_readiness_as_of(value, "as_of")


@pytest.mark.parametrize("value", [math.nan, {1: "bad"}, {"bad": object()}])
def test_readiness_json_tree_rejects_non_json_and_nonfinite_values(value):
    with pytest.raises(cal.ReadinessReportSchemaError):
        cal._validate_readiness_json_tree(value)


@pytest.mark.parametrize(
    ("replacement", "match"),
    [
        ({"pair_id": "bad"}, "pair_id"),
        ({"subject_id": "bad"}, "subject_id"),
        ({"player_color": "green"}, "player_color"),
        ({"session_mode_counts": object()}, "wrong type"),
        ({"session_mode_counts": cal.SessionModeCounts(True, 0)}, "non-negative"),
        ({"named_scores": (math.nan,)}, "finite numbers"),
    ],
)
def test_cutoff_readiness_pair_rejects_each_invalid_surface(replacement, match):
    kwargs = {
        "pair_id": "pair-00",
        "subject_id": "subject-00",
        "player_color": "white",
        "session_mode_counts": cal.SessionModeCounts(1, 0),
        "named_scores": (10.0,),
    }
    kwargs.update(replacement)
    with pytest.raises(cal.ReadinessReportError, match=match):
        cal.CutoffReadinessPair(**kwargs)


def test_redacted_summary_contains_no_operands_cutoffs_subjects_or_private_paths():
    report = _passing_report()
    summary = cal.build_redacted_readiness_summary(report)
    rendered = json.dumps(summary, sort_keys=True)
    for forbidden in (
        "measured", "limit", "candidate_cutoffs", "subject-", "session_mode_counts",
        "/private", "age_days", "grade_reassignment_rate",
    ):
        assert forbidden not in rendered
    assert summary["authorizes_cutoff_emission"] is False
    assert all(set(check) == {"name", "passed"} for check in summary["checks"])


def test_redacted_summary_validator_rejects_scalar_array_and_check_leaks():
    report = _passing_report()
    summary = cal.build_redacted_readiness_summary(report)

    changed = dict(summary, report_sha256="0" * 64)
    with pytest.raises(cal.ReadinessReportSchemaError, match="disagrees"):
        cal.validate_redacted_readiness_summary(changed, report)

    changed = dict(summary, checks="not-an-array")
    with pytest.raises(cal.ReadinessReportSchemaError, match="arrays"):
        cal.validate_redacted_readiness_summary(changed, report)

    changed = json.loads(json.dumps(summary))
    changed["checks"][0]["measured"] = 1
    with pytest.raises(cal.ReadinessReportSchemaError, match="leaked"):
        cal.validate_redacted_readiness_summary(changed, report)


def test_parser_exposes_readiness_options_only_on_its_subcommand():
    args = cal._parse_args([
        "cutoff-readiness",
        "--artifact", "/abs/a.json",
        "--report-output", "/abs/r.json",
        "--baseline-report", "/abs/b.json",
    ])
    assert args.mode == "cutoff-readiness"
    assert args.artifact == Path("/abs/a.json")
    with pytest.raises(SystemExit) as exc:
        cal._parse_args([
            "select-candidates", "--artifact", "/abs/a.json",
            "--result-output", "/abs/r.json", "--baseline-report", "/abs/b.json",
        ])
    assert exc.value.code == 2


def test_artifact_builder_joins_schema_v3_mix_to_current_default_scores(
    tmp_path, monkeypatch
):
    graph, roots, artifact, provenance, _as_of, _prov = tc._bsi_artifact(tmp_path)
    monkeypatch.setattr(cal, "COHORT_PROVENANCE_PATH", provenance)
    monkeypatch.setattr(cal, "get_opening_graph", lambda: graph)
    monkeypatch.setattr(cal, "get_opening_roots", lambda: roots)
    report = cal._build_cutoff_readiness_from_artifact(artifact, None)
    assert report["identity"]["artifact_sha256"] == __import__("hashlib").sha256(
        artifact.read_bytes()
    ).hexdigest()
    assert report["identity"]["scored_model_version"] == cal.SCORE_MODEL_VERSION
    assert report["cohort"]["subject_count"] == 2
    assert report["authorizes_cutoff_emission"] is False


def test_private_baseline_loader_hardened_parses_validates_and_returns_envelope(
    tmp_path
):
    path = tmp_path / "baseline.json"
    report = _report()
    path.write_bytes(cal._canonical_dumps(report))
    assert cal._load_private_readiness_report(path) == report

    path.write_bytes(b'{"schema_version":1,"schema_version":2}')
    with pytest.raises(cal.BaselineReadinessReportError):
        cal._load_private_readiness_report(path)

    path.write_bytes(b'{"value":NaN}')
    with pytest.raises(cal.BaselineReadinessReportError):
        cal._load_private_readiness_report(path)


def test_cli_publishes_full_report_and_only_redacted_stdout_under_repo_tmpdir(
    tmp_path, monkeypatch, capsys
):
    report = _report()
    result = _run_readiness_cli(
        tmp_path, monkeypatch, capsys, report=report
    )
    assert result.code == 0
    assert cal.validate_cutoff_readiness_report(json.loads(result.output.read_text()))
    assert str(tmp_path) not in result.out
    assert "measured" not in result.out and "candidate_cutoffs" not in result.out
    assert json.loads(result.out)["report_sha256"] == report["report_sha256"]


def test_cli_refuses_to_clobber_report_under_repo_tmpdir(tmp_path, monkeypatch, capsys):
    result = _run_readiness_cli(
        tmp_path, monkeypatch, capsys, report=_report(), preexisting_output="reviewed"
    )
    assert result.code == 5
    assert result.output.read_text() == "reviewed"
    assert str(tmp_path) not in result.err
    assert result.out == ""


def test_cli_loads_valid_baseline_bytes_and_hands_them_to_builder(
    tmp_path, monkeypatch, capsys
):
    baseline = _report(artifact="3" * 64, as_of=AS_OF - timedelta(days=15))
    expected = _report(artifact="4" * 64, baseline=baseline)
    seen = []

    def build(_artifact, loaded_baseline):
        seen.append(loaded_baseline)
        return expected

    result = _run_readiness_cli(
        tmp_path,
        monkeypatch,
        capsys,
        baseline_bytes=cal._canonical_dumps(baseline),
        build=build,
    )
    assert result.code == 0
    assert seen == [baseline]
    assert json.loads(result.output.read_text())["report_sha256"] == expected["report_sha256"]


def test_cli_real_artifact_path_builds_and_publishes_report(
    tmp_path, monkeypatch, capsys
):
    graph, roots, artifact, provenance, _as_of, _prov = tc._bsi_artifact(tmp_path)
    monkeypatch.setattr(cal, "COHORT_PROVENANCE_PATH", provenance)
    monkeypatch.setattr(cal, "get_opening_graph", lambda: graph)
    monkeypatch.setattr(cal, "get_opening_roots", lambda: roots)
    judged_repo = tc._git_init(tmp_path / "repo")
    monkeypatch.setattr(cal, "_private_path_forbidden_roots", lambda: (judged_repo,))
    output = tmp_path / "readiness.json"
    capsys.readouterr()
    code = cal.main([
        "cutoff-readiness", "--artifact", str(artifact),
        "--report-output", str(output),
    ])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert cal.validate_cutoff_readiness_report(json.loads(output.read_text()))
    assert "candidate_cutoffs" not in captured.out


@pytest.mark.parametrize(
    ("raised", "code", "diagnostic"),
    [
        (cal.ScorerSourceUnstableError("moved"), 3, "scorer source is unstable"),
        (cal.ArtifactSemanticError("bad"), 4, "ArtifactSemanticError"),
        (cal.SelectionBindingError("bad"), 4, "SelectionBindingError"),
        (cal.ReadinessReportError("artifact moved"), 4, "scored and frozen"),
        (cal.ReadinessReportSchemaError("producer drift"), 5, "own serialization"),
        (RuntimeError("surprise"), 6, "unexpected RuntimeError"),
    ],
)
def test_cli_routes_source_input_producer_and_unexpected_failures(
    tmp_path, monkeypatch, capsys, raised, code, diagnostic
):
    def build(_artifact, _baseline):
        raise raised

    result = _run_readiness_cli(tmp_path, monkeypatch, capsys, build=build)
    assert result.code == code
    assert diagnostic in result.err
    assert result.out == ""
    assert not result.output.exists()


def test_cli_validates_the_serialized_full_report_once(
    tmp_path, monkeypatch, capsys
):
    report = _report()
    real_validate = cal.validate_cutoff_readiness_report
    calls = 0

    def counting_validate(payload):
        nonlocal calls
        calls += 1
        return real_validate(payload)

    monkeypatch.setattr(cal, "validate_cutoff_readiness_report", counting_validate)
    result = _run_readiness_cli(
        tmp_path, monkeypatch, capsys, report=report
    )
    assert result.code == 0, result.err
    assert calls == 1


def test_cli_missing_artifact_is_exit_2(tmp_path, monkeypatch, capsys):
    result = _run_readiness_cli(
        tmp_path, monkeypatch, capsys, report=_report(), artifact_bytes=None
    )
    assert result.code == 2
    assert "existing regular file" in result.err
    assert result.out == ""


def test_cli_malformed_baseline_is_exit_4_with_class_diagnostic(
    tmp_path, monkeypatch, capsys
):
    result = _run_readiness_cli(
        tmp_path,
        monkeypatch,
        capsys,
        baseline_bytes=b'{"not":"a report"}',
        report=_report(),
    )
    assert result.code == 4
    assert "BaselineReadinessReportError" in result.err
    assert result.out == ""


def test_macos_private_component_does_not_mangle_restricted_refusal_prose():
    redact = cal._path_redactor([
        "/private/tmp/calibration/artifact.json",
    ])
    message = redact(
        "use a restricted-store path; /private/tmp/calibration/artifact.json was refused"
    )
    assert message.startswith("use a restricted-store path")
    assert message.endswith("<redacted> was refused")
