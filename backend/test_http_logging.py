from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.http_logging import HTTPLoggingMiddleware
from app.security import create_access_token


@pytest.fixture
def http_log(caplog):
    target = logging.getLogger("ghostreplay.http")
    target.addHandler(caplog.handler)
    with caplog.at_level(logging.INFO, logger="ghostreplay.http"):
        yield caplog
    target.removeHandler(caplog.handler)


# ---------------------------------------------------------------------------
# Basic metadata
# ---------------------------------------------------------------------------


def test_logs_metadata_on_success(client, http_log):
    client.get("/health")
    records = [r for r in http_log.records if hasattr(r, "http")]
    assert records, "expected at least one http log record"
    rec = next(r for r in records if r.http.get("path") == "/health")
    h = rec.http
    assert h["method"] == "GET"
    assert h["status_code"] == 200
    assert h["duration_ms"] >= 0
    assert "request_id" in h


def test_logs_user_id_when_authenticated(client, http_log):
    token = create_access_token(user_id=42, username="testuser", is_anonymous=False)
    client.get("/health", headers={"Authorization": f"Bearer {token}"})
    rec = next(r for r in http_log.records if hasattr(r, "http") and r.http.get("path") == "/health")
    # /health is exempt from auth so user_id will be None; use an authenticated endpoint
    # instead — the path that matters is that user_id flows through when auth succeeds.
    # We'll hit /api/game/start which requires auth (but will 422 without body — that's fine).
    pass


def test_logs_authenticated_user_id(client, http_log):
    token = create_access_token(user_id=77, username="ghost_tester", is_anonymous=False)
    client.post("/api/game/start", json={"engine_elo": 1500, "player_color": "white"},
                headers={"Authorization": f"Bearer {token}"})
    recs = [r for r in http_log.records if hasattr(r, "http") and r.http.get("path") == "/api/game/start"]
    assert recs
    assert recs[0].http["user_id"] == 77


def test_logs_unauthenticated_request(client, http_log):
    client.get("/api/game/start")  # no token → 401
    rec = next(r for r in http_log.records if hasattr(r, "http") and r.http.get("path") == "/api/game/start")
    assert rec.http["status_code"] == 401
    assert rec.http["user_id"] is None


def test_request_id_unique_per_request(client, http_log):
    client.get("/health")
    client.get("/health")
    ids = [r.http["request_id"] for r in http_log.records if hasattr(r, "http") and r.http.get("path") == "/health"]
    assert len(ids) >= 2
    assert ids[0] != ids[1]


def test_query_param_secret_redacted(client, http_log):
    client.get("/health?token=abc&foo=bar")
    rec = next(r for r in http_log.records if hasattr(r, "http") and r.http.get("path") == "/health")
    assert "abc" not in rec.http["query"]
    assert "token=%5BREDACTED%5D" in rec.http["query"] or "token=[REDACTED]" in rec.http["query"]
    assert "foo=bar" in rec.http["query"]


# ---------------------------------------------------------------------------
# Body logging
# ---------------------------------------------------------------------------


def test_body_logging_disabled_by_default(client, http_log):
    client.post("/api/game/start", json={"engine_elo": 1500, "player_color": "white"},
                headers={"Authorization": "Bearer bad-token"})
    rec = next(r for r in http_log.records if hasattr(r, "http") and r.http.get("path") == "/api/game/start")
    assert "request_body" not in rec.http
    assert "response_body" not in rec.http


def test_body_logged_when_env_enabled(client, http_log, monkeypatch):
    monkeypatch.setenv("LOG_HTTP_BODY", "true")
    token = create_access_token(user_id=1, username="u", is_anonymous=True)
    client.post("/api/game/start", json={"engine_elo": 1500, "player_color": "white"},
                headers={"Authorization": f"Bearer {token}"})
    rec = next(r for r in http_log.records if hasattr(r, "http") and r.http.get("path") == "/api/game/start")
    assert "request_body" in rec.http


def test_request_body_password_redacted(http_log, monkeypatch):
    monkeypatch.setenv("LOG_HTTP_BODY", "true")
    mini = FastAPI()
    mini.add_middleware(HTTPLoggingMiddleware)

    @mini.post("/register")
    async def register(payload: dict):
        return {"ok": True}

    with TestClient(mini) as c:
        c.post("/register", json={"username": "alice", "password": "s3cr3t"})

    rec = next(r for r in http_log.records if hasattr(r, "http") and r.http.get("path") == "/register")
    assert "s3cr3t" not in rec.http.get("request_body", "")
    assert "[REDACTED]" in rec.http.get("request_body", "")


def test_response_body_token_redacted(http_log, monkeypatch):
    monkeypatch.setenv("LOG_HTTP_BODY", "true")
    mini = FastAPI()
    mini.add_middleware(HTTPLoggingMiddleware)

    @mini.post("/login")
    async def login():
        return {"token": "super-secret-jwt", "user": "alice"}

    with TestClient(mini) as c:
        c.post("/login")

    rec = next(r for r in http_log.records if hasattr(r, "http") and r.http.get("path") == "/login")
    assert "super-secret-jwt" not in rec.http.get("response_body", "")
    assert "[REDACTED]" in rec.http.get("response_body", "")


def test_body_truncated_at_cap(http_log, monkeypatch):
    from app.http_logging import BODY_LOG_CAP

    monkeypatch.setenv("LOG_HTTP_BODY", "true")
    mini = FastAPI()
    mini.add_middleware(HTTPLoggingMiddleware)

    @mini.get("/big")
    async def big():
        return {"data": "x" * 2000}

    with TestClient(mini) as c:
        c.get("/big")

    rec = next(r for r in http_log.records if hasattr(r, "http") and r.http.get("path") == "/big")
    body = rec.http.get("response_body", "")
    assert len(body) <= BODY_LOG_CAP


def test_large_body_buffer_ceiling(http_log, monkeypatch):
    from app.http_logging import BODY_BUFFER_CAP

    monkeypatch.setenv("LOG_HTTP_BODY", "true")
    mini = FastAPI()
    mini.add_middleware(HTTPLoggingMiddleware)

    @mini.get("/huge")
    async def huge():
        # Return a body larger than BODY_BUFFER_CAP
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("y" * (BODY_BUFFER_CAP + 100))

    with TestClient(mini) as c:
        c.get("/huge")

    rec = next(r for r in http_log.records if hasattr(r, "http") and r.http.get("path") == "/huge")
    assert rec.http.get("response_body") == "[body too large to log]"


def test_binary_body_skipped(http_log, monkeypatch):
    monkeypatch.setenv("LOG_HTTP_BODY", "true")
    mini = FastAPI()
    mini.add_middleware(HTTPLoggingMiddleware)

    @mini.get("/bin")
    async def binary():
        from fastapi.responses import Response
        return Response(content=bytes(range(256)), media_type="application/octet-stream")

    with TestClient(mini) as c:
        c.get("/bin")

    rec = next(r for r in http_log.records if hasattr(r, "http") and r.http.get("path") == "/bin")
    assert rec.http.get("response_body") == "[binary]"


def test_unhandled_500_logged(http_log):
    crash_app = FastAPI()
    crash_app.add_middleware(HTTPLoggingMiddleware)

    @crash_app.get("/crash")
    def crash():
        raise RuntimeError("boom")

    with TestClient(crash_app, raise_server_exceptions=False) as c:
        c.get("/crash")

    rec = next(r for r in http_log.records if hasattr(r, "http") and r.http.get("path") == "/crash")
    assert rec.http["status_code"] == 500


def test_authorization_header_never_logged(client, http_log):
    token = create_access_token(user_id=1, username="u", is_anonymous=True)
    client.get("/health", headers={"Authorization": f"Bearer {token}"})
    for rec in http_log.records:
        if not hasattr(rec, "http"):
            continue
        for v in rec.http.values():
            assert token not in str(v), f"token value found in log field: {v}"
