"""Cross-implementation parity for the played-opening chain walk (g-a5v3).

The live opening cards are derived CLIENT-side so they render on the same tick
as the move (src/openings/deriveLiveLineage.ts); the persisted lineage is
derived SERVER-side (app/opening_roots.py::played_opening_chain_indexed). Two
implementations of one walk can silently drift, which would desync the live
cards from the history cards.

This module and src/openings/deriveLiveLineage.parity.test.ts consume the SAME
generated fixture (src/openings/__fixtures__/openingChainParity.json, produced
by scripts/gen_opening_chain_parity_fixture.py). If a change makes one side
fail, regenerate the fixture and fix the OTHER side to match.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.opening_roots import OpeningRoot, OpeningRoots, played_opening_chain_indexed

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "openings"
    / "__fixtures__"
    / "openingChainParity.json"
)

_FIXTURE = json.loads(FIXTURE_PATH.read_text())


def _roots_registry() -> OpeningRoots:
    roots = {
        spec["opening_key"]: OpeningRoot(
            opening_key=spec["opening_key"],
            opening_name=spec["opening_name"],
            opening_family=spec["opening_family"],
            eco=spec["eco"],
            depth=spec["depth"],
            parent_keys=frozenset(),
            child_keys=frozenset(),
        )
        for spec in _FIXTURE["roots"]
    }
    return OpeningRoots(roots, {})


@pytest.mark.parametrize(
    "case", _FIXTURE["cases"], ids=[c["name"] for c in _FIXTURE["cases"]]
)
def test_played_chain_matches_shared_parity_fixture(case):
    """played_opening_chain_indexed reproduces the shared expected chain.

    Asserts the (key, crossing index) pairs AND the derived SAN prefix, since
    the prefix is what a card actually displays and is the thing a wrong index
    would corrupt.
    """
    roots = _roots_registry()
    chain = played_opening_chain_indexed(case["fens"], roots)

    actual = [
        {
            "opening_key": root.opening_key,
            "crossing_index": index,
            "moves": case["sans"][: index + 1],
        }
        for root, index in chain
    ]
    expected = [
        {k: e[k] for k in ("opening_key", "crossing_index", "moves")}
        for e in case["expected"]
    ]
    assert actual == expected


def test_consecutive_duplicate_keys_are_deduped():
    """The consecutive-repeat dedupe branch, which no legal move list can reach
    (consecutive plies always differ in side-to-move, so their keys differ).
    Driven with a synthetic duplicated FEN list instead; the frontend suite has
    the mirror of this test.
    """
    roots = _roots_registry()
    root_key = _FIXTURE["roots"][0]["opening_key"]
    # Same position three times running -> a single chain entry at the FIRST index.
    chain = played_opening_chain_indexed([root_key] * 3, roots)
    assert [(r.opening_key, i) for r, i in chain] == [(root_key, 0)]


def test_unknown_and_missing_positions_are_skipped():
    """Positions absent from the registry, and null FENs, are skipped rather
    than aborting the walk (mirrors the frontend's falsy-fen guard)."""
    roots = _roots_registry()
    root_key = _FIXTURE["roots"][0]["opening_key"]
    chain = played_opening_chain_indexed(
        [None, "not-a-fen", root_key], roots
    )
    assert [(r.opening_key, i) for r, i in chain] == [(root_key, 2)]
