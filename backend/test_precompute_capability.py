"""The precompute script must refuse to run until it can produce its target contract."""

import importlib

import pytest


def _load_script():
    import sys
    from pathlib import Path

    scripts_dir = Path(__file__).resolve().parent / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    return importlib.import_module("precompute_openings")


def test_assert_can_produce_target_contract_fails_for_resolver_complete():
    mod = _load_script()
    # AnalysisResult lacks best_line_uci, so it cannot satisfy resolver-complete-v1.
    with pytest.raises(SystemExit):
        mod.assert_can_produce_target_contract()
