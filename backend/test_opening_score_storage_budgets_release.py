"""Review budget contracts; run with the storage release-seal experiment."""

import copy

import pytest

from scripts.remeasure_opening_score_budgets import (
    MIB,
    cell_summary,
    derive_budgets,
)

from scripts.summarize_opening_score_budgets import (
    add_fixed_wal_profile,
    paired_p95_ratio,
    review_analysis,
)

pytestmark = pytest.mark.release_seal


def sample_cell():
    return {
        "records": [
            {
                "publish_ms": 1000,
                "client_cpu_ms": 700,
                "protocol_datarow_bytes": 11_200_000,
                "bounded_read_samples_ms": [4, 4, 4, 4, 100] if i == 0 else [4] * 5,
                "post_checkpoint": i == 0,
                "wal": {"total_bytes": 6 * MIB if i == 0 else MIB},
            }
            for i in range(20)
        ],
        "windows": [
            {
                "vacuum_wal": {"total_bytes": 2 * MIB},
                "after_vacuum": {
                    "total_bytes": 32 * MIB,
                    "relations": {"positions": {"live_tuples": 20000}},
                },
            }
            for _ in range(2)
        ],
    }


def inputs():
    cell = sample_cell()
    cell["summary"] = cell_summary(cell)
    a = copy.deepcopy(cell)
    a["summary"]["publication_ms"]["p95"] = 3000
    report = {
        "fixed100": {"a": a, "b50": cell},
        "checkpoint_each20": {"a": a, "b50": cell},
        "memory_repeats": [
            {
                "untraced_persistence_worker_rss_highwater_bytes": rss * MIB,
                "publication_new_allocations_peak_bytes": 50 * MIB,
            }
            for rss in [225, 230, 240, 220, 235]
        ],
    }
    original = {"cells": {"b50": dict(cell, vacuumed_bytes=32 * MIB)}}
    return report, original


def test_individual_reads_are_pooled_without_nesting_maxima():
    result = cell_summary(sample_cell())
    assert result["bounded_read_ms"]["count"] == 100
    assert result["bounded_read_ms"]["p95"] == 4
    assert result["bounded_read_ms"]["max"] == 100  # Never silently discard outliers.


def test_selected_ceilings_do_not_loosen_when_a_gets_slower():
    report, original = inputs()
    before = derive_budgets(report, original)
    report["fixed100"]["a"]["summary"]["publication_ms"]["p95"] *= 10
    after = derive_budgets(report, original)
    assert before["selected_design_ceilings"] == after["selected_design_ceilings"]
    assert after["a_relative_minimum_improvement"]["combined_wal_ratio_max"] == 0.5
    assert after["approved"] is False


def test_checkpoint_ceiling_is_separate_and_memory_uses_repeated_maximum():
    report, original = inputs()
    budgets = derive_budgets(report, original)
    limits = budgets["selected_design_ceilings"]
    assert limits["warm_publication_wal_bytes"] == 1.5 * MIB
    assert limits["post_checkpoint_publication_wal_bytes"] == 9 * MIB
    assert limits["isolated_persistence_worker_rss_bytes"] == 360 * MIB
    assert limits["publication_allocation_peak_bytes"] == 75 * MIB
    assert limits["local_publication_p95_ms"] == 1500
    assert limits["local_bounded_read_p95_ms"] == 8
    assert (
        "actual application-to-database network path" in budgets["latency_finalization"]
    )


@pytest.mark.parametrize("ratio, verdict", [(0.9, "pass"), (1.3, "fail")])
def test_paired_read_uncertainty_preserves_matched_blocks(ratio, verdict):
    reference = [{"reads": [4.0] * 5} for _ in range(20)]
    selected = [{"reads": [4.0 * ratio] * 5} for _ in range(20)]
    result = paired_p95_ratio(reference, selected, "reads", repetitions=200)
    assert result["relative_1_1_gate"] == verdict
    assert result["paired_block_percentile_95_interval"] == [ratio, ratio]


def test_uncertain_read_comparison_is_not_automatic_failure_or_pass():
    reference = [{"reads": [4.0] * 5} for _ in range(20)]
    selected = [{"reads": [4.0 if i < 10 else 5.0] * 5} for i in range(20)]
    result = paired_p95_ratio(reference, selected, "reads", repetitions=200)
    assert result["relative_1_1_gate"] == "inconclusive"


def test_total_wal_cannot_hide_behind_passing_per_state_percentiles():
    report, original = inputs()
    report["proposed_release_budgets"] = derive_budgets(report, original)
    for phase in ["fixed100", "checkpoint_each20"]:
        for cell in report[phase].values():
            cell["exact_parity"] = True
    result = review_analysis(report)
    envelope = result["absolute_wal_envelopes"]["fixed100"]
    assert envelope["warm_publications"] == 19
    assert envelope["post_checkpoint_publications"] == 1
    assert envelope["ceiling_bytes"] == (19 * 1.5 + 9 + 2 * 3) * MIB
    assert result["absolute_checks"]["fixed100_combined_wal"]
    report["fixed100"]["b50"]["summary"]["combined_wal_bytes"] = (
        envelope["ceiling_bytes"] + 1
    )
    assert not review_analysis(report)["absolute_checks"]["fixed100_combined_wal"]


def test_fixed_set_cannot_use_growing_membership_wal_allowance():
    report, original = inputs()
    report["proposed_release_budgets"] = derive_budgets(report, original)
    for phase in ["fixed100", "checkpoint_each20"]:
        for cell in report[phase].values():
            cell["exact_parity"] = True
    limits = report["proposed_release_budgets"]["selected_design_ceilings"]
    limits["warm_publication_wal_bytes"] = 100 * MIB
    add_fixed_wal_profile(report)
    assert limits["fixed_working_set_wal"]["warm_publication_wal_bytes"] == 1.5 * MIB
    result = review_analysis(report)
    assert (
        result["absolute_wal_envelopes"]["fixed100"]["ceiling_bytes"]
        == (19 * 1.5 + 9 + 2 * 3) * MIB
    )
