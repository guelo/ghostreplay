"""Tests for the startup opening prewarm (g-prewarm-openings).

The prewarm thread forces get_opening_roots() (which transitively forces the
opening graph) in the background at boot. These tests never run the real ~30s
build: they patch the build entry points inside app.opening_roots, which
get_opening_roots resolves as module globals at call time.
"""

import threading
from unittest.mock import Mock

import pytest

import app.opening_roots as opening_roots
from app.opening_graph import _reset_opening_graph_for_testing
from app.opening_prewarm import prewarm_enabled, prewarm_openings, start_prewarm
from app.opening_roots import (
    _reset_opening_roots_for_testing,
    is_opening_roots_warm,
)

_DEPLOY_ENV_VARS = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_PROJECT_ID",
    "RENDER",
    "RENDER_SERVICE_ID",
)


@pytest.fixture(autouse=True)
def _reset_opening_singletons():
    # Reset before AND after: a fake-warmed registry must not leak into other
    # tests sharing the process, in either direction.
    _reset_opening_graph_for_testing()
    _reset_opening_roots_for_testing()
    yield
    _reset_opening_graph_for_testing()
    _reset_opening_roots_for_testing()


@pytest.fixture(autouse=True)
def _clean_prewarm_env(monkeypatch):
    monkeypatch.delenv("PREWARM_OPENINGS", raising=False)
    for name in _DEPLOY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _patch_fast_build(monkeypatch):
    """Replace the real graph/roots build with instant fakes; return the roots
    sentinel and the build mock."""
    fake_roots = object()
    build = Mock(return_value=fake_roots)
    monkeypatch.setattr(opening_roots, "get_opening_graph", Mock(return_value=object()))
    monkeypatch.setattr(opening_roots, "build_opening_roots", build)
    return fake_roots, build


# ---------------------------------------------------------------------------
# prewarm_enabled — env gating
# ---------------------------------------------------------------------------


def test_prewarm_disabled_by_default_locally():
    assert prewarm_enabled() is False


@pytest.mark.parametrize("env_var", _DEPLOY_ENV_VARS)
def test_prewarm_defaults_on_for_deploy_platforms(monkeypatch, env_var):
    monkeypatch.setenv(env_var, "production")
    assert prewarm_enabled() is True


def test_explicit_true_overrides_local_default(monkeypatch):
    monkeypatch.setenv("PREWARM_OPENINGS", "true")
    assert prewarm_enabled() is True


def test_explicit_false_overrides_deploy_default(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("PREWARM_OPENINGS", "false")
    assert prewarm_enabled() is False


def test_unrecognized_value_falls_back_to_platform_default(monkeypatch):
    monkeypatch.setenv("PREWARM_OPENINGS", "maybe")
    assert prewarm_enabled() is False
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert prewarm_enabled() is True


# ---------------------------------------------------------------------------
# prewarm_openings — warm/failure behaviour
# ---------------------------------------------------------------------------


def test_prewarm_openings_warms_roots_singleton(monkeypatch):
    fake_roots, build = _patch_fast_build(monkeypatch)

    prewarm_openings()

    assert is_opening_roots_warm() is True
    assert opening_roots.get_opening_roots() is fake_roots
    build.assert_called_once()


def test_prewarm_openings_swallows_build_failure_and_preserves_lazy_retry(monkeypatch):
    monkeypatch.setattr(
        opening_roots,
        "get_opening_graph",
        Mock(side_effect=RuntimeError("graph build exploded")),
    )

    prewarm_openings()  # must not raise

    # Singleton stays None so the next real request retries via the lazy path.
    assert is_opening_roots_warm() is False


# ---------------------------------------------------------------------------
# start_prewarm — thread spawn and gating
# ---------------------------------------------------------------------------


def test_start_prewarm_returns_unjoined_daemon_thread(monkeypatch):
    monkeypatch.setenv("PREWARM_OPENINGS", "true")
    fake_roots, _ = _patch_fast_build(monkeypatch)

    build_may_finish = threading.Event()
    original_build = opening_roots.build_opening_roots

    def blocking_build(graph):
        assert build_may_finish.wait(timeout=5.0)
        return original_build(graph)

    monkeypatch.setattr(opening_roots, "build_opening_roots", blocking_build)

    thread = start_prewarm()

    # The caller got control back while the build is still in flight — the
    # startup path is not blocked on the warm.
    assert isinstance(thread, threading.Thread)
    assert thread.daemon is True
    assert thread.is_alive()
    assert is_opening_roots_warm() is False

    build_may_finish.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert opening_roots.get_opening_roots() is fake_roots


def test_start_prewarm_gated_off_spawns_nothing(monkeypatch):
    monkeypatch.setenv("PREWARM_OPENINGS", "false")
    _, build = _patch_fast_build(monkeypatch)

    assert start_prewarm() is None
    build.assert_not_called()
    assert is_opening_roots_warm() is False


# ---------------------------------------------------------------------------
# /health/openings — observability probe (never triggers a build)
# ---------------------------------------------------------------------------


def test_health_openings_reports_warming_then_warm(monkeypatch, client):
    response = client.get("/health/openings")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "openings": "warming"}

    fake_roots, build = _patch_fast_build(monkeypatch)
    prewarm_openings()

    response = client.get("/health/openings")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "openings": "warm"}
    # The probe itself must never have forced a build: only the prewarm did.
    build.assert_called_once()
