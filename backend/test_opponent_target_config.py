"""Validate the targeting-source rollout switch before the API accepts traffic."""

from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from app.main import app


@pytest.mark.parametrize("source", ["Facts", "fact", ""])
def test_invalid_target_source_prevents_startup(monkeypatch, source):
    monkeypatch.setenv("OPPONENT_TARGET_SOURCE", source)
    with patch("app.main.engine") as engine:
        with pytest.raises(ValueError, match="OPPONENT_TARGET_SOURCE must be decisions or facts"):
            with TestClient(app):
                pytest.fail("invalid source allowed API startup")
        engine.connect.assert_not_called()


@pytest.mark.parametrize("source", [None, "decisions", "facts"])
def test_valid_target_source_allows_startup(monkeypatch, request, source):
    if source is None:
        monkeypatch.delenv("OPPONENT_TARGET_SOURCE", raising=False)
    else:
        monkeypatch.setenv("OPPONENT_TARGET_SOURCE", source)
    client = request.getfixturevalue("client")
    assert client.get("/health").status_code == 200
