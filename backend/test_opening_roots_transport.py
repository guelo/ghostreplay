from __future__ import annotations

import gzip
import json

import pytest

from app.opening_roots import OpeningRoots, get_opening_roots
from app.opening_roots_transport import (
    choose_content_encoding,
    opening_roots_representations,
)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, "identity"),
        ("", "identity"),
        ("br", "identity"),
        ("gzip", "gzip"),
        ("br, gzip, deflate", "gzip"),
        ("gzip;q=0", "identity"),
        ("gzip;q=0.5", "gzip"),
        ("gzip;q=0.5, identity;q=0.8", "identity"),
        ("gzip;q=0.8, identity;q=0.5", "gzip"),
        ("*;q=1, identity;q=0", "gzip"),
        ("gzip;q=0, identity;q=0", None),
        ("*;q=0", None),
    ],
)
def test_choose_content_encoding(header, expected):
    assert choose_content_encoding(header) == expected


def _expected_payload(roots: OpeningRoots) -> dict:
    families = [
        {
            "family_name": family_name,
            "roots": [
                {
                    "opening_key": root.opening_key,
                    "opening_name": root.opening_name,
                    "opening_family": root.opening_family,
                    "eco": root.eco,
                    "depth": root.depth,
                }
                for root in roots.get_family(family_name)
            ],
        }
        for family_name in roots.get_families()
    ]
    return {
        "families": families,
        "total_roots": roots.root_count,
        "total_families": roots.family_count,
    }


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_production_roots_round_trip_and_compression_budget():
    roots = get_opening_roots()
    representations = opening_roots_representations(roots, None)
    raw = representations.identity.body
    decoded_bytes = gzip.decompress(representations.gzip.body)
    decoded = json.loads(decoded_bytes)

    assert roots.root_count >= 10_000
    assert len(raw) >= 2_000_000
    assert len(representations.gzip.body) <= len(raw) * 0.15
    assert decoded_bytes == raw
    assert decoded == _expected_payload(roots)
    assert representations.gzip.etag != representations.identity.etag

    assert b"\x00" not in raw
    assert all(byte >= 0x20 for byte in raw)
    assert all(
        all(ord(character) >= 0x20 for character in value)
        for value in _strings(decoded)
    )


def test_missing_family_filters_share_the_empty_cached_representation():
    roots = get_opening_roots()

    first = opening_roots_representations(roots, "not-a-family")
    second = opening_roots_representations(roots, "also-not-a-family")

    assert second is first
    assert json.loads(first.identity.body) == {
        "families": [],
        "total_roots": 0,
        "total_families": 0,
    }
