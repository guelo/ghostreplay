import copy
import json
from pathlib import Path

import pytest

from scripts.summarize_opening_score_cutover import evaluate, public_projection, shape_checks
from scripts import summarize_opening_score_qualification as qualification
from test_opening_score_qualification_release import _cell


def reports():
    cells = [_cell("C1", "S1", 24815), _cell("C2", "S1", 23806, publications=60)]
    memory = copy.deepcopy(cells[0])
    memory["cell"] = "C3"
    memory["result"] = {"children": {
        layout: [{"logical_rows": 24815, "untraced_worker_rss_highwater_bytes": 200_000_000,
                  "publication_allocation_peak_bytes": 50_000_000} for _ in range(5)]
        for layout in ("A", "B50")
    }}
    cells.append(memory)
    for cell in cells:
        cell.update(manifest_sha256="manifest", capture_sha256="capture", source_manifest={"app/file.py": "digest"})
        cell["profile_identity"]["settings"]["cluster_name"] = {"setting": "isolated"}
        cell["profile_identity"]["settings"]["max_wal_size"] = {"setting": "128", "unit": "MB"}
    return cells


def test_pass_proposes_measured_size_limits_without_authorizing_activation():
    result = evaluate(reports())
    assert result["relative_gates_pass"] is True
    assert result["activation_authorized"] is False
    assert result["proposed_real_path_ceilings"]["C1:publication_p95_ms"]["logical_rows"] == 24815
    assert all(p["reviewed"] is False for p in result["proposed_real_path_ceilings"].values())


def test_fewer_than_500_retained_reads_produces_no_proposed_limits():
    cells = reports()
    for records in cells[0]["result"]["records"].values():
        for row in records:
            row["composite_d"] = row["composite_d"][:4]
    result = evaluate(cells)
    assert result["relative_gates_pass"] is False
    assert result["proposed_real_path_ceilings"] == {}


def test_relative_regression_produces_no_proposed_limits():
    cells = reports()
    for row in cells[0]["result"]["records"]["B50"]:
        row["publish_ms"] *= 2
    result = evaluate(cells)
    assert result["relative_gates_pass"] is False
    assert result["proposed_real_path_ceilings"] == {}


@pytest.mark.parametrize("field,value", [("manifest_sha256", "other"), ("capture_sha256", "other"), ("copies", 2), ("complete", False)])
def test_mismatched_or_partial_inputs_refuse(field, value):
    cells = reports()
    cells[1][field] = value
    with pytest.raises(ValueError):
        evaluate(cells)


def test_settings_change_refuses_pooling():
    cells = reports()
    cells[1]["profile_identity"]["settings"]["max_wal_size"] = {"setting": "8192"}
    with pytest.raises(ValueError, match="settings changed"):
        evaluate(cells)


def test_memory_requires_qualification_five_worker_minimum():
    cells = reports()
    cells[2]["result"]["children"]["B50"] = cells[2]["result"]["children"]["B50"][:3]
    with pytest.raises(ValueError, match="five fresh memory"):
        evaluate(cells)


def test_linux_capture_parent_rss_cannot_become_a_budget():
    cells = reports()
    cells[2]["profile_identity"]["host"]["platform"] = "Linux"
    with pytest.raises(ValueError, match="clean parent"):
        evaluate(cells)
    cells[2]["result"]["memory_launch_mode"] = "clean_parent"
    assert evaluate(cells)["relative_gates_pass"] is True


def test_explicit_review_allows_only_the_warm_wal_exception():
    cells = reports()
    cells[0]["profile_identity"]["settings"]["max_wal_size"] = {"setting": "8192"}
    with pytest.raises(ValueError, match="settings changed"):
        evaluate(cells)
    result = evaluate(cells, allow_warm_wal_deviation=True)
    assert result["relative_gates_pass"] is True
    assert result["settings_review"]["deviating_cells"] == ["C1:S1"]
    assert result["allow_warm_wal_deviation"] is True
    for key in ("C1:composite_d_p95_ms", "C1:composite_t_format_stage_p95_ms"):
        assert result["proposed_real_path_ceilings"][key]["measurement_regime"] == "warm_only"
        assert result["proposed_real_path_ceilings"][key]["max_wal_size_mb"] == 8192
    cells[0]["profile_identity"]["settings"]["fsync"] = {"setting": "off"}
    with pytest.raises(ValueError):
        evaluate(cells, allow_warm_wal_deviation=True)


def test_flag_does_not_allow_c2_deviation():
    cells = reports()
    cells[1]["profile_identity"]["settings"]["max_wal_size"] = {"setting": "8192"}
    with pytest.raises(ValueError):
        evaluate(cells, allow_warm_wal_deviation=True)


def test_qualification_builder_reproduces_approved_post_checkpoint_vacuum_budget():
    report = json.loads((Path(__file__).resolve().parents[1] / "docs/analysis/opening-score-storage-qualification-2026-09-24.json").read_text())
    cells = {c["cell"]: c for c in report["cells"] if c["size_profile"] == "S1" and c["cell"] in ("C1", "C2")}
    checks, budget = shape_checks(cells, report)
    assert [p["ceiling"] for p in budget["at_measured_sizes"]] == [7340032, 16252928, 34078720]
    assert budget["ceiling"]["applies_to"] == "production_applicable"
    original = next(c for c in checks if c["cell"] == "C2" and c["metric"] == "vacuum_wal_bytes_per_ten_publications")
    assert original["passes"] is False
    assert original["verdict"] == "pending_review"
    regenerated = qualification.build_ceilings(report["cells"], [])["ceilings"]["vacuum_wal_bytes_per_ten_publications"]
    old = report["ceilings"]["ceilings"]["vacuum_wal_bytes_per_ten_publications"]
    for field in ("points", "fixed_overhead", "per_logical_row_slope", "max_positive_residual", "ceiling"):
        assert regenerated[field] == old[field]


def test_public_projection_only_removes_private_fields_and_keeps_failed_checks():
    result = evaluate(reports())
    result["shape_checks"] = [{"cell": "C2", "passes": False, "verdict": "pending_review"}]
    public = public_projection(result)
    assert public["activation_authorized"] is False
    assert public["shape_checks"] == result["shape_checks"]
    assert len(public["memory"]["B50"]) == 5
    for name, cell in public["cells"].items():
        assert "vacuum_windows_kept" in cell
        assert "vacuum_wal_max_bytes_by_layout" in cell
        assert all(v == result["cells"][name][k] for k, v in cell.items())
    assert result["cells"]["C1"].get("pool_identity") is not None
    assert "pool_identity" not in public["cells"]["C1"]
