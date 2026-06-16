from __future__ import annotations

import os

import pytest

from app import posthog_client


def test_test_suite_forces_analytics_disabled():
    """conftest must force analytics off so the suite never emits real events.

    Regression guard for the setdefault bug: an ambient POSTHOG_DISABLED=false +
    token would otherwise survive and build a live client.
    """
    assert os.environ.get("POSTHOG_DISABLED") == "true"
    assert "POSTHOG_PROJECT_TOKEN" not in os.environ
    posthog_client._reset()
    assert posthog_client.get_client() is None


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Drop any cached client before and after each test (env vars vary)."""
    posthog_client._reset()
    yield
    posthog_client._reset()


# ---------------------------------------------------------------------------
# Disable / no-op behaviour
# ---------------------------------------------------------------------------


def test_get_client_none_when_disabled(monkeypatch):
    monkeypatch.setenv("POSTHOG_DISABLED", "true")
    monkeypatch.setenv("POSTHOG_PROJECT_TOKEN", "phc_test")
    posthog_client._reset()
    assert posthog_client.get_client() is None


def test_get_client_none_when_no_token(monkeypatch):
    monkeypatch.setenv("POSTHOG_DISABLED", "false")
    monkeypatch.delenv("POSTHOG_PROJECT_TOKEN", raising=False)
    posthog_client._reset()
    assert posthog_client.get_client() is None


def test_capture_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("POSTHOG_DISABLED", "true")
    posthog_client._reset()
    # No client → must not raise and must not attempt a send.
    posthog_client.capture("123", "api_request", {"route": "/x"})


def test_shutdown_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("POSTHOG_DISABLED", "true")
    posthog_client._reset()
    posthog_client.shutdown()  # must not raise


# ---------------------------------------------------------------------------
# Exception safety (analytics must never break a request)
# ---------------------------------------------------------------------------


def test_capture_swallows_client_exceptions(monkeypatch):
    class FaultyClient:
        def capture(self, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(posthog_client, "get_client", lambda: FaultyClient())
    # Must swallow, not propagate.
    posthog_client.capture("123", "api_request", {"route": "/x"})


def test_shutdown_swallows_client_exceptions(monkeypatch):
    class FaultyClient:
        def shutdown(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(posthog_client, "get_client", lambda: FaultyClient())
    posthog_client.shutdown()  # must not raise


# ---------------------------------------------------------------------------
# Forwarding shape
# ---------------------------------------------------------------------------


def test_capture_forwards_keyword_args(monkeypatch):
    calls = []

    class RecordingClient:
        def capture(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(posthog_client, "get_client", lambda: RecordingClient())
    posthog_client.capture("123", "api_request", {"route": "/x"})

    assert len(calls) == 1
    assert calls[0]["distinct_id"] == "123"
    assert calls[0]["event"] == "api_request"
    assert calls[0]["properties"] == {"route": "/x"}


def test_capture_anon_fallback_when_no_distinct_id(monkeypatch):
    calls = []

    class RecordingClient:
        def capture(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(posthog_client, "get_client", lambda: RecordingClient())
    posthog_client.capture(None, "api_request")

    assert calls[0]["distinct_id"] == "anon"
    assert calls[0]["properties"] == {}
