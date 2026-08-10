"""Per-request `api_request` analytics capture in HTTPLoggingMiddleware.

The real `capture` no-ops in the test suite (POSTHOG_DISABLED=true). These tests
patch `app.http_logging.capture` with a recorder so they assert the middleware's
behaviour — route-template extraction, skip rules, and that an analytics failure
never alters the response — independent of the disabled client.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.http_logging import HTTPLoggingMiddleware


@pytest.fixture
def recorded_captures(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr("app.http_logging.capture", lambda *a, **k: calls.append(a))
    return calls


def _api_request_props(calls: list[tuple]) -> dict:
    # capture(distinct_id, "api_request", properties)
    api = [a for a in calls if len(a) >= 2 and a[1] == "api_request"]
    assert api, "expected an api_request capture"
    assert len(api) == 1, "expected exactly one api_request per request"
    return api[0][2]


def test_route_is_template_not_concrete_path(recorded_captures):
    mini = FastAPI()
    mini.add_middleware(HTTPLoggingMiddleware)

    @mini.get("/api/items/{item_id}/sub")
    async def get_item(item_id: str):
        return {"id": item_id}

    with TestClient(mini) as c:
        r = c.get("/api/items/abc-123/sub")
    assert r.status_code == 200

    props = _api_request_props(recorded_captures)
    assert props["route"] == "/api/items/{item_id}/sub"
    assert props["method"] == "GET"
    assert props["status_code"] == 200
    assert props["ok"] is True
    assert props["status_class"] == "2xx"
    assert isinstance(props["duration_ms"], (int, float))
    assert props["request_id"]


def test_response_echoes_request_id_matching_captured_event(recorded_captures):
    """The `X-Request-ID` response header is the client↔server correlation key:
    it must equal the `request_id` on the captured `api_request` event."""
    mini = FastAPI()
    mini.add_middleware(HTTPLoggingMiddleware)

    @mini.get("/api/ok")
    async def ok():
        return {"ok": True}

    with TestClient(mini) as c:
        r = c.get("/api/ok")
    assert r.status_code == 200

    header_id = r.headers.get("x-request-id")
    assert header_id
    assert _api_request_props(recorded_captures)["request_id"] == header_id


@pytest.mark.parametrize(
    "headers, dnt",
    [
        ({}, False),
        # This header no longer gates capture unless DNT is also asserted.
        ({"Sec-GPC": "1"}, False),
        ({"DNT": "1"}, True),
        ({"DNT": "yes"}, True),
        # Non-asserting values must NOT gate (matches the client's strict reads).
        ({"DNT": "0"}, False),
    ],
)
def test_privacy_signals_tag_api_request(recorded_captures, headers, dnt):
    """`api_request` carries the DNT header signal so the true gate rate is
    measurable server-side even while client capture is silenced (g-client-event-gap)."""
    mini = FastAPI()
    mini.add_middleware(HTTPLoggingMiddleware)

    @mini.get("/api/ok")
    async def ok():
        return {"ok": True}

    with TestClient(mini) as c:
        c.get("/api/ok", headers=headers)

    props = _api_request_props(recorded_captures)
    assert props["dnt_signaled"] is dnt
    assert props["client_capture_gated"] is dnt


def test_unmatched_route_falls_back_to_label(recorded_captures):
    mini = FastAPI()
    mini.add_middleware(HTTPLoggingMiddleware)

    @mini.get("/api/items/{item_id}")
    async def get_item(item_id: str):
        return {"id": item_id}

    with TestClient(mini) as c:
        r = c.get("/totally/missing")
    assert r.status_code == 404

    props = _api_request_props(recorded_captures)
    assert props["route"] == "unmatched"
    assert props["ok"] is False
    assert props["status_class"] == "4xx"


def test_distinct_id_is_user_id_when_authenticated(client, monkeypatch):
    from app.security import create_access_token

    calls: list[tuple] = []
    monkeypatch.setattr("app.http_logging.capture", lambda *a, **k: calls.append(a))

    token = create_access_token(user_id=77, username="ghost_tester", is_anonymous=False)
    client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers={"Authorization": f"Bearer {token}"},
    )
    api = [a for a in calls if len(a) >= 2 and a[1] == "api_request" and a[2]["route"] == "/api/game/start"]
    assert api
    assert api[0][0] == "77"


def test_distinct_id_is_anon_when_unauthenticated(recorded_captures):
    mini = FastAPI()
    mini.add_middleware(HTTPLoggingMiddleware)

    @mini.get("/api/public")
    async def public():
        return {"ok": True}

    with TestClient(mini) as c:
        c.get("/api/public")

    api = [a for a in recorded_captures if len(a) >= 2 and a[1] == "api_request"]
    assert api
    assert api[0][0] == "anon"


@pytest.mark.parametrize("path", ["/", "/health", "/health/live", "/docs", "/openapi.json"])
def test_skipped_paths_are_not_captured(recorded_captures, path):
    mini = FastAPI()
    mini.add_middleware(HTTPLoggingMiddleware)

    @mini.get("/")
    async def root():
        return {"ok": True}

    @mini.get("/health")
    async def health():
        return {"ok": True}

    @mini.get("/health/live")
    async def health_live():
        return {"ok": True}

    with TestClient(mini) as c:
        c.get(path)

    assert not any(len(a) >= 2 and a[1] == "api_request" for a in recorded_captures)


def test_capture_failure_never_alters_response(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("analytics down")

    monkeypatch.setattr("app.http_logging.capture", boom)

    mini = FastAPI()
    mini.add_middleware(HTTPLoggingMiddleware)

    @mini.get("/api/ok")
    async def ok():
        return {"ok": True, "value": 42}

    with TestClient(mini) as c:
        r = c.get("/api/ok")

    assert r.status_code == 200
    assert r.json() == {"ok": True, "value": 42}
