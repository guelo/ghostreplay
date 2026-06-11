"""The precompute script can now produce its target contract (resolver-complete-v2)."""

import importlib

import pytest


def _load_script():
    import sys
    from pathlib import Path

    scripts_dir = Path(__file__).resolve().parent / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    return importlib.import_module("precompute_openings")


def test_assert_can_produce_target_contract_succeeds_for_v2():
    mod = _load_script()
    # AnalysisResult now produces best_line_uci + classification + the eval triple
    # + fen_before, so it satisfies resolver-complete-v2.
    mod.assert_can_produce_target_contract()


def test_assert_can_produce_target_contract_rejects_unknown_contract():
    mod = _load_script()
    with pytest.raises(SystemExit):
        mod.assert_can_produce_target_contract("does-not-exist")
