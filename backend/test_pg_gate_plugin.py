"""Self-tests for the Release-A PostgreSQL gate plugin (``pg_gate_plugin``).

These prove the *gate mechanism itself* — the piece that makes the required
PostgreSQL run fail closed instead of passing with missing coverage. If the gate
were broken (marker unregistered, a skip slipping through, an empty selection
reported green, a stale manifest) the whole point of the required run would be
lost, so the mechanism gets its own tests.

Each scenario runs pytest in a **subprocess** (``runpytest_subprocess``) so a
fresh interpreter activates the REAL importable plugin and rebinds its two
manifests to a *synthetic* body, then asserts the gate's behaviour. Subprocess
isolation keeps a nested run's env and manifest monkeypatching from leaking into
this outer session's own plugin state, and the synthetic project carries its own
``pytest.ini`` so the nested rootdir — and therefore the node ids the manifests
are keyed on — never depends on where the basetemp happens to live.

Covered (mirrors g-release-integrate's acceptance criteria):

1. missing test URL fails (required mode);
2. missing maintenance URL fails (required mode);
3. inline and fixture residual skips are promoted to failures;
4. an empty gated selection raises ``UsageError``;
5. an incomplete function manifest names the missing identities;
6. an incomplete required matrix names its missing bracketed case;
7. the positive converse — a complete manifest + non-empty selection collects
   and runs;
8. developer-default mode still skips cleanly;

plus a direct import check (an import error must not masquerade as gate
enforcement) and a drift guard that the shipped manifest matches the real
``@pg_gate`` decorations.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

import pg_gate_plugin

pytest_plugins = ["pytester"]

_BACKEND_DIR = pathlib.Path(__file__).resolve().parent

_PG_ENV_VARS = (
    "GHOSTREPLAY_TEST_PG_URL",
    "TEST_DATABASE_URL_PG",
    "GHOSTREPLAY_TEST_PG_MAINT_URL",
    "GHOSTREPLAY_REQUIRE_PG_TESTS",
)

# A syntactically valid but never-connected URL. The setup gate only checks that
# a URL is *present*; the synthetic bodies below never open a connection, so a
# fake URL is enough to drive the "URL is set" branch without a real database.
_FAKE_PG_URL = "postgresql://gate:gate@127.0.0.1:5432/ghostreplay_test"


def _set_env(monkeypatch, *, require: bool, pg_url: str | None = None,
             maint_url: str | None = None) -> None:
    """Set the four gate env vars for a nested subprocess run (which inherits them)."""
    for var in _PG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    if require:
        monkeypatch.setenv("GHOSTREPLAY_REQUIRE_PG_TESTS", "1")
    if pg_url:
        monkeypatch.setenv("GHOSTREPLAY_TEST_PG_URL", pg_url)
    if maint_url:
        monkeypatch.setenv("GHOSTREPLAY_TEST_PG_MAINT_URL", maint_url)


def _write_synthetic_project(pytester, *, body: str,
                             manifest_tests, manifest_cases) -> None:
    """Write a nested conftest that activates the real plugin with a synthetic
    manifest, plus the synthetic test body.

    The conftest inserts the backend dir on ``sys.path`` so the subprocess can
    ``import pg_gate_plugin``, rebinds both manifests to the synthetic identities
    (the gate hooks read the module globals at call time), and activates the
    plugin via ``pytest_plugins``.

    The project also ships its OWN ``pytest.ini``. That pins the nested run's
    rootdir to the pytester directory, which the synthetic manifests below depend
    on: node ids are relative to rootdir, so identities are ``test_synth.py::...``
    only while rootdir is this directory. Without the pin, a basetemp inside the
    repo (the documented ``TMPDIR=backend/.tmp``) lets the nested run discover
    ``backend/pytest.ini``, rootdir becomes ``backend/``, every id grows a long
    relative prefix, and every manifest assertion fails for the wrong reason.
    Pinning rootdir also cuts conftest collection off here, so the real
    ``backend/conftest.py`` never loads into the synthetic run.
    """
    pytester.makefile(".ini", pytest="[pytest]\n")
    pytester.makeconftest(
        "import sys\n"
        f"sys.path.insert(0, {str(_BACKEND_DIR)!r})\n"
        "import pg_gate_plugin\n"
        f"pg_gate_plugin.REQUIRED_PG_GATE_TESTS = frozenset({sorted(manifest_tests)!r})\n"
        f"pg_gate_plugin.REQUIRED_PG_GATE_PARAM_CASES = frozenset({sorted(manifest_cases)!r})\n"
        "pytest_plugins = ['pg_gate_plugin']\n"
    )
    pytester.makepyfile(test_synth=body)


def _combined(result) -> str:
    return result.stdout.str() + "\n" + result.stderr.str()


def _run_nested(pytester, *args):
    """Run the synthetic project in a subprocess, asserting it stayed isolated.

    Every assertion in this file is keyed on ``test_synth.py::...`` identities, and
    those only hold while the nested run's rootdir is the pytester directory (see
    ``_write_synthetic_project``). Checking the reported rootdir here turns an
    escape into one clear diagnosis instead of six unrelated-looking gate failures.
    """
    result = pytester.runpytest_subprocess(*args)
    assert f"rootdir: {pytester.path}\n" in result.stdout.str(), (
        "nested run escaped the synthetic project's rootdir, so its node ids are "
        "not the manifest identities under test:\n" + _combined(result)
    )
    return result


# ---------------------------------------------------------------------------
# 1. Missing test URL fails in required mode.
# ---------------------------------------------------------------------------


def test_missing_test_url_fails_in_required_mode(pytester, monkeypatch):
    _set_env(monkeypatch, require=True)  # no URL at all
    _write_synthetic_project(
        pytester,
        body=(
            "import pytest\n"
            "@pytest.mark.pg_gate\n"
            "def test_needs_url():\n"
            "    pass\n"
        ),
        manifest_tests={"test_synth.py::test_needs_url"},
        manifest_cases=set(),
    )
    result = _run_nested(pytester, "--strict-markers", "-rs")
    outcomes = result.parseoutcomes()
    assert outcomes.get("skipped", 0) == 0
    assert outcomes.get("passed", 0) == 0
    assert outcomes.get("failed", 0) + outcomes.get("errors", 0) == 1
    assert "GHOSTREPLAY_TEST_PG_URL is not set" in _combined(result)


# ---------------------------------------------------------------------------
# 2. Missing maintenance URL fails in required mode (disposable-DB path).
# ---------------------------------------------------------------------------


def test_missing_maintenance_url_fails_in_required_mode(pytester, monkeypatch):
    # Test URL present so the setup gate passes and we reach the fixture, which
    # requires the SEPARATE maintenance URL and must fail hard without it.
    _set_env(monkeypatch, require=True, pg_url=_FAKE_PG_URL)  # no maint URL
    _write_synthetic_project(
        pytester,
        body=(
            "import pytest\n"
            "@pytest.mark.pg_gate\n"
            "def test_needs_maint(pg_migration_db):\n"
            "    pass\n"
        ),
        manifest_tests={"test_synth.py::test_needs_maint"},
        manifest_cases=set(),
    )
    result = _run_nested(pytester, "--strict-markers", "-rs")
    outcomes = result.parseoutcomes()
    assert outcomes.get("skipped", 0) == 0
    assert outcomes.get("passed", 0) == 0
    assert outcomes.get("failed", 0) + outcomes.get("errors", 0) == 1
    assert "GHOSTREPLAY_TEST_PG_MAINT_URL is not set" in _combined(result)


# ---------------------------------------------------------------------------
# 3. Residual skips (inline + fixture) become failures in required mode.
# ---------------------------------------------------------------------------


def test_residual_skips_are_promoted_to_failures(pytester, monkeypatch):
    _set_env(monkeypatch, require=True, pg_url=_FAKE_PG_URL)  # URL present: gate passes
    _write_synthetic_project(
        pytester,
        body=(
            "import pytest\n"
            "@pytest.mark.pg_gate\n"
            "def test_inline_skip():\n"
            "    pytest.skip('inline residual skip')\n"
            "\n"
            "@pytest.fixture\n"
            "def skipping_fixture():\n"
            "    pytest.skip('fixture residual skip')\n"
            "\n"
            "@pytest.mark.pg_gate\n"
            "def test_fixture_skip(skipping_fixture):\n"
            "    pass\n"
        ),
        manifest_tests={
            "test_synth.py::test_inline_skip",
            "test_synth.py::test_fixture_skip",
        },
        manifest_cases=set(),
    )
    result = _run_nested(pytester, "--strict-markers", "-rs")
    outcomes = result.parseoutcomes()
    # Neither residual skip survives as a skip; both become non-passing (a
    # call-phase skip -> failed, a setup/fixture skip -> error).
    assert outcomes.get("skipped", 0) == 0
    assert outcomes.get("passed", 0) == 0
    assert outcomes.get("failed", 0) + outcomes.get("errors", 0) == 2
    assert "residual skip promoted to failure" in _combined(result)


# ---------------------------------------------------------------------------
# 3b. A failing xfail on a gated test is promoted too (must not exit green).
# ---------------------------------------------------------------------------


def test_failing_xfail_is_promoted_to_failure(pytester, monkeypatch):
    _set_env(monkeypatch, require=True, pg_url=_FAKE_PG_URL)  # URL present: gate passes
    _write_synthetic_project(
        pytester,
        body=(
            "import pytest\n"
            "@pytest.mark.pg_gate\n"
            "@pytest.mark.xfail(reason='must not hide a gated proof')\n"
            "def test_marked_xfail_fails():\n"
            "    assert False\n"
            "\n"
            "@pytest.mark.pg_gate\n"
            "def test_imperative_xfail():\n"
            "    pytest.xfail('imperative xfail')\n"
        ),
        manifest_tests={
            "test_synth.py::test_marked_xfail_fails",
            "test_synth.py::test_imperative_xfail",
        },
        manifest_cases=set(),
    )
    result = _run_nested(pytester, "--strict-markers", "-rx")
    outcomes = result.parseoutcomes()
    # An xfailed report (outcome="skipped" + wasxfail) must not survive as a
    # non-failure: both forms become failures, none remain xfailed/skipped.
    assert outcomes.get("xfailed", 0) == 0
    assert outcomes.get("skipped", 0) == 0
    assert outcomes.get("passed", 0) == 0
    assert outcomes.get("failed", 0) == 2
    assert "xfail promoted to failure" in _combined(result)


# ---------------------------------------------------------------------------
# 4. Empty gated selection is a hard UsageError.
# ---------------------------------------------------------------------------


def test_empty_gated_selection_raises_usage_error(pytester, monkeypatch):
    _set_env(monkeypatch, require=True, pg_url=_FAKE_PG_URL)
    _write_synthetic_project(
        pytester,
        body=(
            "def test_not_gated():\n"  # a real test, but NOT @pg_gate
            "    pass\n"
        ),
        manifest_tests=set(),
        manifest_cases=set(),
    )
    result = _run_nested(pytester, "--strict-markers")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "no @pg_gate tests were selected" in _combined(result)


# ---------------------------------------------------------------------------
# 5. Incomplete function manifest names the missing identities.
# ---------------------------------------------------------------------------


def test_incomplete_function_manifest_names_missing_identity(pytester, monkeypatch):
    _set_env(monkeypatch, require=True, pg_url=_FAKE_PG_URL)
    _write_synthetic_project(
        pytester,
        body=(
            "import pytest\n"
            "@pytest.mark.pg_gate\n"
            "def test_present():\n"
            "    pass\n"
        ),
        # The manifest requires an identity that is NOT collected.
        manifest_tests={
            "test_synth.py::test_present",
            "test_synth.py::test_absent",
        },
        manifest_cases=set(),
    )
    result = _run_nested(pytester, "--strict-markers")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    combined = _combined(result)
    assert "manifest incomplete" in combined
    assert "test_synth.py::test_absent" in combined


# ---------------------------------------------------------------------------
# 6. Incomplete required matrix names its missing bracketed case.
# ---------------------------------------------------------------------------


def test_incomplete_matrix_names_missing_case(pytester, monkeypatch):
    _set_env(monkeypatch, require=True, pg_url=_FAKE_PG_URL)
    _write_synthetic_project(
        pytester,
        body=(
            "import pytest\n"
            "@pytest.mark.parametrize('v', ['a'], ids=['case_a'])\n"
            "@pytest.mark.pg_gate\n"
            "def test_matrix(v):\n"
            "    pass\n"
        ),
        # Function identity IS satisfied; a required param CASE is missing.
        manifest_tests={"test_synth.py::test_matrix"},
        manifest_cases={
            "test_synth.py::test_matrix[case_a]",
            "test_synth.py::test_matrix[missing_case]",
        },
    )
    result = _run_nested(pytester, "--strict-markers")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    combined = _combined(result)
    assert "matrix incomplete" in combined
    assert "test_synth.py::test_matrix[missing_case]" in combined


# ---------------------------------------------------------------------------
# 7. Positive converse: complete manifest + non-empty selection collects & runs.
# ---------------------------------------------------------------------------


def test_complete_manifest_and_url_collects_and_passes(pytester, monkeypatch):
    _set_env(monkeypatch, require=True, pg_url=_FAKE_PG_URL)
    _write_synthetic_project(
        pytester,
        body=(
            "import pytest\n"
            "@pytest.mark.pg_gate\n"
            "def test_plain_gate():\n"
            "    pass\n"
            "\n"
            "@pytest.mark.parametrize('v', ['a', 'b'], ids=['a', 'b'])\n"
            "@pytest.mark.pg_gate\n"
            "def test_matrix(v):\n"
            "    pass\n"
        ),
        manifest_tests={
            "test_synth.py::test_plain_gate",
            "test_synth.py::test_matrix",
        },
        manifest_cases={
            "test_synth.py::test_matrix[a]",
            "test_synth.py::test_matrix[b]",
        },
    )
    result = _run_nested(pytester, "--strict-markers", "-rs")
    assert result.ret == pytest.ExitCode.OK
    outcomes = result.parseoutcomes()
    assert outcomes.get("passed", 0) == 3
    assert outcomes.get("skipped", 0) == 0
    assert outcomes.get("failed", 0) == 0
    assert outcomes.get("errors", 0) == 0


# ---------------------------------------------------------------------------
# 8. Developer-default mode still skips cleanly.
# ---------------------------------------------------------------------------


def test_developer_default_mode_skips_cleanly(pytester, monkeypatch):
    _set_env(monkeypatch, require=False)  # not required, no URL
    _write_synthetic_project(
        pytester,
        body=(
            "import pytest\n"
            "@pytest.mark.pg_gate\n"
            "def test_needs_url():\n"
            "    pass\n"
        ),
        manifest_tests={"test_synth.py::test_needs_url"},
        manifest_cases=set(),
    )
    result = _run_nested(pytester, "--strict-markers", "-rs")
    assert result.ret == pytest.ExitCode.OK
    outcomes = result.parseoutcomes()
    assert outcomes.get("skipped", 0) == 1
    assert outcomes.get("failed", 0) == 0
    assert outcomes.get("errors", 0) == 0
    assert outcomes.get("passed", 0) == 0


# ---------------------------------------------------------------------------
# Import + drift guards (run in the ordinary default suite, no subprocess).
# ---------------------------------------------------------------------------


def test_plugin_imports_and_manifests_are_populated():
    """An import error must not silently disable the gate: assert the module
    imports and its manifests are populated and internally consistent."""
    import importlib

    mod = importlib.import_module("pg_gate_plugin")
    assert isinstance(mod.REQUIRED_PG_GATE_TESTS, frozenset)
    assert len(mod.REQUIRED_PG_GATE_TESTS) >= 20
    assert isinstance(mod.REQUIRED_PG_GATE_PARAM_CASES, frozenset)
    # pg_required is an alias for the pg_gate marker (same object).
    assert mod.pg_required is mod.pg_gate
    # Every pinned matrix function identity and its cases agree. Stated as an
    # invariant over the manifests rather than as a hardcoded case count, so a
    # new gated matrix (Release B added the ply-coordinate detector's five row
    # sets) extends the manifest without editing this assertion — while a case
    # whose FUNCTION is missing from REQUIRED_PG_GATE_TESTS still fails here.
    matrix_fn = "test_writer_locks.py::test_srs_moves_cross_root_lock_matrix"
    assert matrix_fn in mod.REQUIRED_PG_GATE_TESTS
    assert (
        sum(1 for c in mod.REQUIRED_PG_GATE_PARAM_CASES if c.startswith(matrix_fn + "[")) == 4
    )
    assert mod.REQUIRED_PG_GATE_PARAM_CASES
    for case in mod.REQUIRED_PG_GATE_PARAM_CASES:
        assert case.endswith("]"), case
        assert case.split("[", 1)[0] in mod.REQUIRED_PG_GATE_TESTS, case


def test_bare_pg_session_factory_resets_before_each_construction(monkeypatch):
    """Direct Session users get the same per-test isolation as pg_client users."""
    from app.models import Base

    engines = (object(), object())
    expected_factories = [object(), object()]
    factories = iter(expected_factories)
    events = []

    def fake_truncate(engine, table_names):
        events.append(("reset", engine, table_names))

    def fake_sessionmaker(**kwargs):
        factory = next(factories)
        events.append(("construct", kwargs, factory))
        return factory

    monkeypatch.setattr(pg_gate_plugin, "_truncate_all", fake_truncate)
    monkeypatch.setattr(pg_gate_plugin, "sessionmaker", fake_sessionmaker)

    built = [
        pg_gate_plugin._make_isolated_pg_session_factory(engine)
        for engine in engines
    ]

    preserved = {"evidence_epoch", "shared_evidence_scope_invalidations"}
    expected_tables = ", ".join(
        table.name
        for table in reversed(Base.metadata.sorted_tables)
        if table.name not in preserved
    )
    assert built == expected_factories
    assert events == [
        ("reset", engines[0], expected_tables),
        (
            "construct",
            {"autocommit": False, "autoflush": False, "bind": engines[0]},
            built[0],
        ),
        ("reset", engines[1], expected_tables),
        (
            "construct",
            {"autocommit": False, "autoflush": False, "bind": engines[1]},
            built[1],
        ),
    ]


def test_manifest_matches_real_pg_gate_collection():
    """Drift guard: the shipped manifest must equal the actual ``@pg_gate``
    decorations. Because the required-mode collection guard is dormant in the
    default (developer) suite, this cheap ``--collect-only`` cross-check is what
    catches a new gate test added without a manifest entry (or vice-versa)."""
    env = {**os.environ}
    # Keep the guard dormant so a genuine drift surfaces here as a set mismatch,
    # not as the collection guard's UsageError.
    env.pop("GHOSTREPLAY_REQUIRE_PG_TESTS", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "pg_gate",
         "-p", "no:cacheprovider"],
        cwd=str(_BACKEND_DIR),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"collect-only failed:\n{proc.stdout}\n{proc.stderr}"
    collected = {
        line.strip()
        for line in proc.stdout.splitlines()
        if "::" in line and line.strip().startswith("test_")
    }
    assert collected, "no @pg_gate tests collected — marker wiring is broken"
    functions = {node.split("[", 1)[0] for node in collected}
    cases = {node for node in collected if "[" in node}
    assert functions == set(pg_gate_plugin.REQUIRED_PG_GATE_TESTS), (
        "REQUIRED_PG_GATE_TESTS is out of sync with the @pg_gate-decorated tests: "
        f"missing={set(pg_gate_plugin.REQUIRED_PG_GATE_TESTS) - functions}, "
        f"extra={functions - set(pg_gate_plugin.REQUIRED_PG_GATE_TESTS)}"
    )
    assert cases == set(pg_gate_plugin.REQUIRED_PG_GATE_PARAM_CASES), (
        "REQUIRED_PG_GATE_PARAM_CASES is out of sync with the collected matrix cases: "
        f"missing={set(pg_gate_plugin.REQUIRED_PG_GATE_PARAM_CASES) - cases}, "
        f"extra={cases - set(pg_gate_plugin.REQUIRED_PG_GATE_PARAM_CASES)}"
    )
