"""Error-envelope contracts, plus who owns the traceback of an unhandled 500.

Starlette routes the ``Exception`` (== 500) handler to ``ServerErrorMiddleware`` —
the OUTERMOST middleware — not to the inner ``ExceptionMiddleware``
(``applications.py``: ``if key in (500, Exception): error_handler = value``). That
middleware sends the handler's response and then UNCONDITIONALLY re-raises, so the
ASGI server always receives the exception and logs it.

That makes the ownership rule easy to state and easy to get wrong in the obvious
direction: the app must NOT log the traceback in its own handler, because the server
already does and every 500 would be recorded twice. The last three tests pin both
halves — the envelope the caller sees, and the fact that the exception still escapes
to the server — so a future "let's log it here too" fails a test instead of silently
doubling every production 500 (g-rating-serialize-flake).
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.main import app


def test_http_exception_returns_standard_error_envelope(client, auth_headers):
    response = client.post(
        "/api/game/end",
        json={
            "session_id": "00000000-0000-0000-0000-000000000000",
            "result": "resign",
            "pgn": "1. e4 e5",
        },
        headers=auth_headers(),
    )

    assert response.status_code == 404
    data = response.json()

    assert data["detail"] == "Game session not found"
    assert data["error"]["code"] == "http_404"
    assert data["error"]["message"] == "Game session not found"
    assert data["error"]["retryable"] is False


def test_validation_error_returns_standard_error_envelope(client, auth_headers):
    response = client.post(
        "/api/game/start",
        json={},
        headers=auth_headers(),
    )

    assert response.status_code == 422
    data = response.json()

    assert data["detail"] == "Validation error"
    assert data["error"]["code"] == "validation_error"
    assert data["error"]["message"] == "Validation error"
    assert data["error"]["retryable"] is False
    assert isinstance(data["error"]["details"], list)


# ---------------------------------------------------------------------------
# Unhandled exceptions: the envelope, and the single logging owner.
# ---------------------------------------------------------------------------


@pytest.fixture
def crashing_route():
    """A route on the REAL app that raises, removed again on teardown.

    Driven WITHOUT ``with TestClient(...)`` below: the context manager is what runs
    the lifespan, and the lifespan opens ``app.main.engine`` and starts the real
    scheduler daemons. These tests need neither, and a raw client over the real app
    is exactly how a daemon bound to the configured database escapes into the rest of
    the run (g-rating-serialize-flake). No lifespan, no engine, no daemons.
    """

    @app.get("/__test_crash__")
    def _crash() -> None:
        raise RuntimeError("boom")

    yield "/__test_crash__"
    app.router.routes[:] = [
        r for r in app.router.routes if getattr(r, "path", None) != "/__test_crash__"
    ]
    app.openapi_schema = None


def test_unhandled_exception_returns_standard_error_envelope(crashing_route, auth_headers):
    """The caller gets the envelope, and the cause never leaks into the body."""
    response = TestClient(app, raise_server_exceptions=False).get(
        crashing_route, headers=auth_headers()
    )

    assert response.status_code == 500
    data = response.json()

    assert data["detail"] == "Internal server error"
    assert data["error"]["code"] == "internal_error"
    assert data["error"]["retryable"] is True
    assert "boom" not in response.text


def test_unhandled_exception_still_escapes_to_the_asgi_server(crashing_route, auth_headers):
    """ServerErrorMiddleware re-raises AFTER the handler returns its response.

    This is what makes the server the traceback's owner — and what makes logging it
    in the handler a duplicate rather than the only record.
    """
    with pytest.raises(RuntimeError, match="boom"):
        TestClient(app, raise_server_exceptions=True).get(
            crashing_route, headers=auth_headers()
        )


def test_app_does_not_log_the_traceback_of_an_unhandled_exception(
    crashing_route, auth_headers, caplog
):
    """Exactly one logging owner for a 500, and it is not this application."""
    with caplog.at_level(logging.ERROR):
        TestClient(app, raise_server_exceptions=False).get(
            crashing_route, headers=auth_headers()
        )

    offenders = [
        record for record in caplog.records
        if record.exc_info is not None and record.name.startswith("app.")
    ]
    assert offenders == [], (
        "the app logged the traceback of an unhandled 500; ServerErrorMiddleware "
        "re-raises so the ASGI server already logs it, and this doubles every "
        f"production 500: {[record.getMessage() for record in offenders]}"
    )
