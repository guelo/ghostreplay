"""Client-generated correlation id handling in HTTPLoggingMiddleware (g-upload-observe).

The middleware is the join-key plumbing: it validates + normalizes the inbound
``X-Client-Request-ID`` ONCE, publishes it (and the server-generated request id) to
request state for the endpoint to read, and carries it as a SEPARATE
``client_request_id`` field on the ``api_request`` analytics event. The server
request id stays server-generated and is never overwritten by client input.

These tests patch ``app.http_logging.capture`` with a recorder so they assert the
middleware's behaviour independent of the (disabled) real client.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.http_logging import HTTPLoggingMiddleware, _normalize_client_request_id


@pytest.fixture
def recorded_captures(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr("app.http_logging.capture", lambda *a, **k: calls.append(a))
    return calls


def _api_request_props(calls: list[tuple]) -> dict:
    api = [a for a in calls if len(a) >= 2 and a[1] == "api_request"]
    assert api, "expected an api_request capture"
    assert len(api) == 1, "expected exactly one api_request per request"
    return api[0][2]


def _mini_app() -> FastAPI:
    """A minimal app that echoes what the middleware published to request state."""
    mini = FastAPI()
    mini.add_middleware(HTTPLoggingMiddleware)

    @mini.get("/api/echo")
    async def echo(request: Request):
        return {
            "state_client_request_id": getattr(
                request.state, "client_request_id", "MISSING"
            ),
            "state_request_id": getattr(request.state, "request_id", "MISSING"),
        }

    return mini


# --- pure normalizer ---------------------------------------------------------


def test_normalizer_canonicalizes_and_rejects():
    canonical = "7f3e4d2a-1b2c-4d5e-8f90-abcdef123456"
    # Already canonical -> unchanged.
    assert _normalize_client_request_id(canonical) == canonical
    # Uppercase and braces are accepted by uuid.UUID but normalized to canonical
    # lowercase hyphenated form (this is what bounds cardinality / dedups).
    assert _normalize_client_request_id(canonical.upper()) == canonical
    assert _normalize_client_request_id("{" + canonical + "}") == canonical
    assert (
        _normalize_client_request_id("urn:uuid:" + canonical) == canonical
    )
    # Absent / malformed -> None.
    assert _normalize_client_request_id(None) is None
    assert _normalize_client_request_id("") is None
    assert _normalize_client_request_id("not-a-uuid") is None
    assert _normalize_client_request_id("12345") is None


# --- state propagation -------------------------------------------------------


def test_valid_client_id_published_to_state_and_normalized(recorded_captures):
    with TestClient(_mini_app()) as c:
        r = c.get(
            "/api/echo",
            headers={"X-Client-Request-ID": "7F3E4D2A-1B2C-4D5E-8F90-ABCDEF123456"},
        )
    assert r.status_code == 200
    body = r.json()
    # Normalized to canonical lowercase and readable by the endpoint via state.
    assert body["state_client_request_id"] == "7f3e4d2a-1b2c-4d5e-8f90-abcdef123456"
    # The server request id is also published to state (endpoint reads both).
    assert body["state_request_id"] == r.headers.get("x-request-id")


def test_absent_client_id_is_null_in_state_and_event(recorded_captures):
    with TestClient(_mini_app()) as c:
        r = c.get("/api/echo")
    assert r.status_code == 200
    # Absent header -> None in state (JSON null).
    assert r.json()["state_client_request_id"] is None
    props = _api_request_props(recorded_captures)
    assert props["client_request_id"] is None
    # ...but the server request id is always present.
    assert props["request_id"] == r.headers.get("x-request-id")


def test_malformed_client_id_is_null(recorded_captures):
    with TestClient(_mini_app()) as c:
        r = c.get("/api/echo", headers={"X-Client-Request-ID": "not-a-uuid"})
    assert r.status_code == 200
    assert r.json()["state_client_request_id"] is None
    assert _api_request_props(recorded_captures)["client_request_id"] is None


def test_repeated_client_id_header_uses_the_first(recorded_captures):
    first = "11111111-1111-4111-8111-111111111111"
    second = "22222222-2222-4222-8222-222222222222"
    with TestClient(_mini_app()) as c:
        r = c.get(
            "/api/echo",
            headers=[
                ("x-client-request-id", first),
                ("x-client-request-id", second),
            ],
        )
    assert r.status_code == 200
    assert r.json()["state_client_request_id"] == first


# --- api_request event carries BOTH ids, distinctly --------------------------


def test_event_carries_separate_server_and_client_ids(recorded_captures):
    client_id = "7f3e4d2a-1b2c-4d5e-8f90-abcdef123456"
    with TestClient(_mini_app()) as c:
        r = c.get("/api/echo", headers={"X-Client-Request-ID": client_id})
    assert r.status_code == 200
    props = _api_request_props(recorded_captures)
    server_id = props["request_id"]
    # Server id is server-generated (a real uuid), echoed on the response header,
    # and NEVER the client-supplied value.
    assert server_id == r.headers.get("x-request-id")
    uuid.UUID(server_id)  # parses -> it is a real uuid
    assert server_id != client_id
    # The client id is carried as its own, separate field.
    assert props["client_request_id"] == client_id
