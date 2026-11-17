"""The exact-boundary migration changes the delete guards' boundary and nothing else.

``20261116_01`` re-creates both SRS delete-guard functions from one template. If
the template drifted from ``20260919_05`` anywhere but the boundary, the upgrade
would silently rewrite guard logic and the downgrade would not restore the
schema it claims to. The behaviour of the exact boundary itself is proven against
PostgreSQL in test_opportunity_lifecycle_pg.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

VERSIONS = Path(__file__).parent / "alembic" / "versions"


def _migration(filename: str):
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"),
                                                  VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ORIGINAL = _migration("20260919_05_add_opportunity_lifecycle_guards.py")
EXACT = _migration("20261116_01_exact_lifecycle_guard_boundary.py")
PAIRS = [
    (EXACT._SESSION_GUARD, ORIGINAL.SESSION_GUARD),
    (EXACT._EVENT_GUARD, ORIGINAL.EVENT_GUARD),
]


def test_downgrade_restores_the_original_guard_functions_exactly():
    for template, original in PAIRS:
        assert EXACT.render(template, EXACT.CALENDAR_BOUNDARY) == original


def test_upgrade_changes_only_the_boundary_expression():
    for template, original in PAIRS:
        upgraded = EXACT.render(template, EXACT.EXACT_BOUNDARY)
        assert "days =>" not in upgraded
        assert upgraded == original.replace(EXACT.CALENDAR_BOUNDARY,
                                            EXACT.EXACT_BOUNDARY)
        assert upgraded != original
