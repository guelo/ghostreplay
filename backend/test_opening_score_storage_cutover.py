"""Deployment selection reaches real publications without changing score identity."""

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import event

from app import opening_cache as oc
from app.opening_score_storage import StorageFormat
from test_opening_cache import (  # noqa: F401
    _mock_opening_cache_singletons,
    _seed_black_opening_session,
)


def test_invalid_selector_rejects_startup_before_database_or_workers(monkeypatch):
    from app.main import app
    monkeypatch.setenv("OPENING_SCORE_STORAGE_FORMAT", "current")
    with patch("app.main.engine") as engine, patch("app.main.get_scheduler") as scheduler:
        with pytest.raises(ValueError, match="OPENING_SCORE_STORAGE_FORMAT"):
            with TestClient(app):
                pytest.fail("invalid selector allowed startup")
        engine.connect.assert_not_called()
        scheduler.assert_not_called()


@pytest.mark.parametrize("value", [None, "legacy", "current-b50-v1"])
def test_startup_logs_effective_selector(monkeypatch, request, caplog, value):
    if value is None:
        monkeypatch.delenv("OPENING_SCORE_STORAGE_FORMAT", raising=False)
    else:
        monkeypatch.setenv("OPENING_SCORE_STORAGE_FORMAT", value)
    with caplog.at_level("INFO", logger="app.main"):
        client = request.getfixturevalue("client")
    assert client.get("/health").status_code == 200
    assert f"OPENING_SCORE_STORAGE_FORMAT={value or 'legacy'}" in caplog.text


@pytest.mark.parametrize("value", [None, "legacy", "current-b50-v1"])
def test_selector(value, monkeypatch):
    if value is None:
        monkeypatch.delenv("OPENING_SCORE_STORAGE_FORMAT", raising=False)
    else:
        monkeypatch.setenv("OPENING_SCORE_STORAGE_FORMAT", value)
    assert oc.default_storage_format() == StorageFormat(value or "legacy")


@pytest.mark.parametrize("value", ["", "current", "CURRENT-B50-V1", " legacy "])
def test_invalid_selector_performs_no_database_work(db_session, monkeypatch, value):
    monkeypatch.setenv("OPENING_SCORE_STORAGE_FORMAT", value)
    statements = []

    def record(*args):
        statements.append(args[2])

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        with pytest.raises(ValueError, match="OPENING_SCORE_STORAGE_FORMAT"):
            oc.recompute_opening_scores(db_session, 123, "black")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements == []


def test_switch_converts_on_rebuild_and_preserves_identity(db_session, monkeypatch):
    _seed_black_opening_session(db_session)
    monkeypatch.delenv("OPENING_SCORE_STORAGE_FORMAT", raising=False)
    first = oc.recompute_opening_scores(db_session, 123, "black")
    first_id = first.id
    fingerprint = first.registry_fingerprint
    _, roots = oc.list_cached_opening_scores(db_session, 123, "black")
    root_keys = {row.opening_key for row in roots}
    assert root_keys

    monkeypatch.setenv("OPENING_SCORE_STORAGE_FORMAT", "current-b50-v1")
    # A configuration change does not convert an existing publication.
    view, _ = oc.list_cached_opening_scores(db_session, 123, "black")
    assert (view.id, view.storage_format) == (first_id, "legacy")
    current = oc.recompute_opening_scores(db_session, 123, "black")
    current_id = current.id
    view, roots = oc.list_cached_opening_scores(db_session, 123, "black")
    assert (view.id, view.storage_format) == (current_id, "current-b50-v1")
    assert {row.opening_key for row in roots} == root_keys
    assert current.registry_fingerprint == fingerprint

    monkeypatch.setenv("OPENING_SCORE_STORAGE_FORMAT", "legacy")
    view, _ = oc.list_cached_opening_scores(db_session, 123, "black")
    assert view.storage_format == "current-b50-v1"
    restored = oc.recompute_opening_scores(db_session, 123, "black")
    view, roots = oc.list_cached_opening_scores(db_session, 123, "black")
    assert (view.id, view.storage_format) == (restored.id, "legacy")
    assert {row.opening_key for row in roots} == root_keys
    assert restored.registry_fingerprint == fingerprint


@pytest.mark.parametrize("target", list(StorageFormat))
def test_explicit_maintenance_format_overrides_selector(db_session, monkeypatch, target):
    _seed_black_opening_session(db_session)
    monkeypatch.setenv("OPENING_SCORE_STORAGE_FORMAT", "invalid")
    batch = oc.recompute_opening_scores(db_session, 123, "black", storage_format=target)
    assert batch.storage_format == target.value
