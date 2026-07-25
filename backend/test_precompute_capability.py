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


def test_every_replacement_verdict_a_canonical_write_can_earn_is_accepted():
    """A successful shared-writer replacement must not be counted as a failure.

    `_ACCEPTED_REASONS` is the run's success allowlist: anything outside it lands in
    `write_failures` and exits the run unsuccessfully. `strength_replace` (D4 steps
    4-5) is latent for this producer today — the two canonical manifests compare
    EQUAL, so no canonical pair ranks — but a future deeper canonical profile with no
    explicit edge would rank, and the miscount would be the script's own doing.
    """
    from app.analysis_cache_policy import Reason

    mod = _load_script()
    earnable = {
        Reason.NEW_KEY,
        Reason.DOMINATES_REPLACE,
        Reason.LEGACY_REPLACED_BY_AUTH,
        Reason.STRENGTH_REPLACE,
    }
    assert earnable <= mod._ACCEPTED_REASONS
    # PROTOCOL_CORRECTED_REPLACE is deliberately excluded: this producer is
    # authoritative and the authority barrier resolves canonical-vs-browser before
    # explicit edges, so a canonical write can never earn that verdict.
    assert Reason.PROTOCOL_CORRECTED_REPLACE not in mod._ACCEPTED_REASONS
