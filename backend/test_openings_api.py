from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.opening_evidence import EvidenceOverlay, NodeEvidence, EdgeEvidence
from app.opening_graph import OpeningGraph, OpeningGraphNode
from app.opening_roots import OpeningRoot, OpeningRoots
from app.fen import active_color

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

ROOT_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"
CHILD_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -"


def _make_graph() -> OpeningGraph:
    root_node = OpeningGraphNode(ROOT_FEN, active_color(ROOT_FEN))
    root_node.name = "King's Pawn Game"
    child_node = OpeningGraphNode(CHILD_FEN, active_color(CHILD_FEN))
    child_node.name = "King's Pawn Game"
    root_node.children["e5"] = CHILD_FEN
    child_node.parents.add((ROOT_FEN, "e5"))
    return OpeningGraph(
        {ROOT_FEN: root_node, CHILD_FEN: child_node},
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -",
    )


def _make_roots() -> OpeningRoots:
    root = OpeningRoot(
        opening_key=ROOT_FEN,
        opening_name="King's Pawn Game",
        opening_family="King's Pawn Game",
        eco="B00",
        depth=1,
        parent_keys=frozenset(),
        child_keys=frozenset(),
    )
    return OpeningRoots(
        {ROOT_FEN: root},
        {ROOT_FEN: frozenset([ROOT_FEN]), CHILD_FEN: frozenset([ROOT_FEN])},
    )


def _empty_overlay() -> EvidenceOverlay:
    return EvidenceOverlay(user_id=123, player_color="black")


def _overlay_with_evidence() -> EvidenceOverlay:
    overlay = EvidenceOverlay(user_id=123, player_color="black")
    overlay.nodes[ROOT_FEN] = NodeEvidence(
        fen=ROOT_FEN,
        live_attempts=5,
        live_passes=4,
        live_fails=1,
        last_live_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    overlay.edges[(ROOT_FEN, CHILD_FEN)] = EdgeEvidence(
        parent_fen=ROOT_FEN,
        child_fen=CHILD_FEN,
        uci="e7e5",
        live_attempts=5,
        live_passes=4,
    )
    return overlay


# Patch targets — these are looked up in the openings module's namespace
_PATCH_GRAPH = "app.api.openings.get_opening_graph"
_PATCH_ROOTS = "app.api.openings.get_opening_roots"
_PATCH_OVERLAY = "app.api.openings.overlay_evidence"


@pytest.fixture(autouse=True)
def _mock_singletons():
    with (
        patch(_PATCH_GRAPH, return_value=_make_graph()),
        patch(_PATCH_ROOTS, return_value=_make_roots()),
    ):
        yield


# ---------------------------------------------------------------------------
# POST /api/openings/score
# ---------------------------------------------------------------------------


def test_score_unknown_root_returns_404(client, auth_headers):
    with patch(_PATCH_OVERLAY, return_value=_empty_overlay()):
        resp = client.post(
            "/api/openings/score",
            json={"opening_key": "unknown/fen", "player_color": "black"},
            headers=auth_headers(),
        )
    assert resp.status_code == 404
    assert "Unknown opening root" in resp.json()["detail"]


def test_score_valid_root_no_evidence(client, auth_headers):
    with patch(_PATCH_OVERLAY, return_value=_empty_overlay()):
        resp = client.post(
            "/api/openings/score",
            json={"opening_key": ROOT_FEN, "player_color": "black"},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["opening_key"] == ROOT_FEN
    assert data["opening_name"] == "King's Pawn Game"
    assert data["opening_family"] == "King's Pawn Game"
    assert data["player_color"] == "black"
    assert 0 <= data["opening_score"] <= 100
    assert data["debug_nodes"] == []


def test_score_with_evidence(client, auth_headers):
    with patch(_PATCH_OVERLAY, return_value=_empty_overlay()):
        resp_no_ev = client.post(
            "/api/openings/score",
            json={"opening_key": ROOT_FEN, "player_color": "black"},
            headers=auth_headers(),
        )

    with patch(_PATCH_OVERLAY, return_value=_overlay_with_evidence()):
        resp_ev = client.post(
            "/api/openings/score",
            json={"opening_key": ROOT_FEN, "player_color": "black"},
            headers=auth_headers(),
        )

    assert resp_no_ev.status_code == 200
    assert resp_ev.status_code == 200
    # Evidence should change the score
    assert resp_ev.json()["opening_score"] != resp_no_ev.json()["opening_score"]


def test_score_debug_flag(client, auth_headers):
    with patch(_PATCH_OVERLAY, return_value=_empty_overlay()):
        resp = client.post(
            "/api/openings/score?debug=true",
            json={"opening_key": ROOT_FEN, "player_color": "black"},
            headers=auth_headers(),
        )
    assert resp.status_code == 200
    assert len(resp.json()["debug_nodes"]) > 0
    node = resp.json()["debug_nodes"][0]
    assert "fen" in node
    assert "p_n" in node
    assert "raw_score" in node


def test_score_invalid_color_returns_422(client, auth_headers):
    resp = client.post(
        "/api/openings/score",
        json={"opening_key": ROOT_FEN, "player_color": "red"},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_score_no_auth_returns_401(client):
    resp = client.post(
        "/api/openings/score",
        json={"opening_key": ROOT_FEN, "player_color": "black"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/openings/roots
# ---------------------------------------------------------------------------


def test_roots_list(client, auth_headers):
    resp = client.get("/api/openings/roots", headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_families"] == 1
    assert data["total_roots"] == 1
    fam = data["families"][0]
    assert fam["family_name"] == "King's Pawn Game"
    root = fam["roots"][0]
    assert root["opening_key"] == ROOT_FEN
    assert root["eco"] == "B00"


def test_roots_list_family_filter(client, auth_headers):
    resp = client.get(
        "/api/openings/roots",
        params={"family": "King's Pawn Game"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["total_families"] == 1

    resp_miss = client.get(
        "/api/openings/roots",
        params={"family": "Nonexistent Family"},
        headers=auth_headers(),
    )
    assert resp_miss.status_code == 200
    assert resp_miss.json()["total_families"] == 0
    assert resp_miss.json()["total_roots"] == 0


def test_roots_no_auth_returns_401(client):
    resp = client.get("/api/openings/roots")
    assert resp.status_code == 401
