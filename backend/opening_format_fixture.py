"""Storage-format parametrization for the opening-score reader tests.

``recompute_opening_scores`` resolves its format in the BODY through
``opening_cache.default_storage_format()``, so patching that one module attribute
switches every batch a test publishes between legacy and current storage without
threading a keyword through the writer's callers.

This lives outside ``conftest.py`` only because that file is being edited by a
concurrent session; fold it in later. Register it from a test module::

    pytest_plugins = ("opening_format_fixture",)
"""

from __future__ import annotations

import pytest

from app import opening_cache
from app.opening_score_storage import StorageFormat


@pytest.fixture(
    params=[StorageFormat.LEGACY, StorageFormat.CURRENT],
    ids=["legacy", "current"],
)
def storage_format(request, monkeypatch):
    """Publish this test's batches in one storage format; yields the format."""
    monkeypatch.setattr(
        opening_cache, "default_storage_format", lambda: request.param
    )
    return request.param
