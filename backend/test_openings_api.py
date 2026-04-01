from __future__ import annotations

import uuid
from datetime import datetime, timezone
from urllib.parse import quote
from unittest.mock import patch

import pytest
from sqlalchemy import event

from app.models import (
    GameSession,
    OpeningScoreBatch,
    OpeningScoreCursor,
    SessionMove,
    UserOpeningScore,
)
from app.opening_cache import opening_score_inputs_fingerprint
from app.opening_evidence import EvidenceOverlay, NodeEvidence, EdgeEvidence
from app.opening_graph import OpeningGraph, OpeningGraphNode
from app.opening_rootcalc import BranchSummary, RootScore
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


# ---------------------------------------------------------------------------
# GET /api/openings/families/scores
# ---------------------------------------------------------------------------

_FAMILIES_URL = "/api/openings/families/scores"

# Patch targets for cache functions in the openings module namespace
_PATCH_ENSURE = "app.api.openings.ensure_opening_scores"
_PATCH_LIST_CACHED = "app.api.openings.list_cached_opening_scores"
_PATCH_RECOMPUTE = "app.api.openings.recompute_opening_scores"


def _make_batch(batch_id: int = 1, user_id: int = 123, player_color: str = "white",
                generation: int = 1, computed_at: datetime | None = None,
                registry_fingerprint: str | None = None):
    from app.models import OpeningScoreBatch
    batch = OpeningScoreBatch(
        id=batch_id,
        user_id=user_id,
        player_color=player_color,
        generation=generation,
        registry_fingerprint=registry_fingerprint,
    )
    if computed_at is not None:
        batch.computed_at = computed_at
    else:
        batch.computed_at = datetime(2026, 3, 1, tzinfo=timezone.utc)
    return batch


def _make_batch_for_roots(
    roots: OpeningRoots,
    *,
    graph: OpeningGraph | None = None,
    batch_id: int = 1,
    user_id: int = 123,
    player_color: str = "white",
    generation: int = 1,
    computed_at: datetime | None = None,
):
    if graph is None:
        graph = _make_graph()
    return _make_batch(
        batch_id=batch_id,
        user_id=user_id,
        player_color=player_color,
        generation=generation,
        computed_at=computed_at,
        registry_fingerprint=opening_score_inputs_fingerprint(graph, roots),
    )


def _make_row(batch_id: int = 1, user_id: int = 123, player_color: str = "white",
              opening_key: str = "key-a", opening_name: str = "Root A",
              opening_family: str = "Family A", opening_score: float = 60.0,
              confidence: float = 0.8, coverage: float = 0.5,
              weighted_depth: float = 3.0, sample_size: int = 10,
              last_practiced_at: datetime | None = None,
              strongest_branch_name: str | None = None,
              strongest_branch_key: str | None = None,
              strongest_branch_score: float | None = None,
              weakest_branch_name: str | None = None,
              weakest_branch_key: str | None = None,
              weakest_branch_score: float | None = None,
              underexposed_branch_name: str | None = None,
              underexposed_branch_key: str | None = None,
              underexposed_branch_value: float | None = None,
              computed_at: datetime | None = None):
    from app.models import UserOpeningScore
    row = UserOpeningScore(
        batch_id=batch_id,
        user_id=user_id,
        player_color=player_color,
        opening_key=opening_key,
        opening_name=opening_name,
        opening_family=opening_family,
        opening_score=opening_score,
        confidence=confidence,
        coverage=coverage,
        weighted_depth=weighted_depth,
        sample_size=sample_size,
        last_practiced_at=last_practiced_at,
        strongest_branch_name=strongest_branch_name,
        strongest_branch_key=strongest_branch_key,
        strongest_branch_score=strongest_branch_score,
        weakest_branch_name=weakest_branch_name,
        weakest_branch_key=weakest_branch_key,
        weakest_branch_score=weakest_branch_score,
        underexposed_branch_name=underexposed_branch_name,
        underexposed_branch_key=underexposed_branch_key,
        underexposed_branch_value=underexposed_branch_value,
    )
    if computed_at is not None:
        row.computed_at = computed_at
    return row


DRILL_FAMILY_RUY = "Ruy Lopez"
DRILL_FAMILY_SICILIAN = "Sicilian Defense"
DRILL_FAMILY_QGD = "Queen's Gambit Declined"

DRILL_KEY_RUY_MORPHY = "ruy-morphy"
DRILL_KEY_RUY_BERLIN = "ruy-berlin"
DRILL_KEY_RUY_EXCHANGE = "ruy-exchange"
DRILL_KEY_SICILIAN_NAJDORF = "sicilian-najdorf"
DRILL_KEY_QGD_ORTHODOX = "qgd-orthodox"


def _make_drill_roots() -> OpeningRoots:
    roots = {
        DRILL_KEY_RUY_MORPHY: OpeningRoot(
            opening_key=DRILL_KEY_RUY_MORPHY,
            opening_name="Ruy Lopez: Morphy Defense",
            opening_family=DRILL_FAMILY_RUY,
            eco="C60",
            depth=3,
            parent_keys=frozenset(),
            child_keys=frozenset(),
        ),
        DRILL_KEY_RUY_BERLIN: OpeningRoot(
            opening_key=DRILL_KEY_RUY_BERLIN,
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
            eco="C65",
            depth=4,
            parent_keys=frozenset(),
            child_keys=frozenset(),
        ),
        DRILL_KEY_RUY_EXCHANGE: OpeningRoot(
            opening_key=DRILL_KEY_RUY_EXCHANGE,
            opening_name="Ruy Lopez: Exchange Variation",
            opening_family=DRILL_FAMILY_RUY,
            eco="C68",
            depth=4,
            parent_keys=frozenset(),
            child_keys=frozenset(),
        ),
        DRILL_KEY_SICILIAN_NAJDORF: OpeningRoot(
            opening_key=DRILL_KEY_SICILIAN_NAJDORF,
            opening_name="Sicilian Defense: Najdorf Variation",
            opening_family=DRILL_FAMILY_SICILIAN,
            eco="B90",
            depth=3,
            parent_keys=frozenset(),
            child_keys=frozenset(),
        ),
        DRILL_KEY_QGD_ORTHODOX: OpeningRoot(
            opening_key=DRILL_KEY_QGD_ORTHODOX,
            opening_name="Queen's Gambit Declined: Orthodox Defense",
            opening_family=DRILL_FAMILY_QGD,
            eco="D63",
            depth=3,
            parent_keys=frozenset(),
            child_keys=frozenset(),
        ),
    }
    return OpeningRoots(roots, {})


def _drill_url(family_name: str) -> str:
    return f"/api/openings/families/{quote(family_name, safe='')}/scores"


def _roots_for_rows(*rows: UserOpeningScore) -> OpeningRoots:
    roots: dict[str, OpeningRoot] = {}
    ownership: dict[str, frozenset[str]] = {}
    for row in rows:
        roots[row.opening_key] = OpeningRoot(
            opening_key=row.opening_key,
            opening_name=row.opening_name,
            opening_family=row.opening_family,
            eco=None,
            depth=1,
            parent_keys=frozenset(),
            child_keys=frozenset(),
        )
        ownership[row.opening_key] = frozenset([row.opening_key])
    return OpeningRoots(roots, ownership)


CHILD_KEY_POLISH = "child-polish"
CHILD_KEY_POLISH_E6 = "child-polish-e6"
CHILD_KEY_POLISH_BB2 = "child-polish-bb2"
CHILD_KEY_POLISH_ALT = "child-polish-alt"
CHILD_KEY_SHARED = "child-shared"
CHILD_KEY_ENGLISH = "child-english"
CHILD_KEY_BIRD = "child-bird"


def _make_children_roots() -> OpeningRoots:
    roots = {
        CHILD_KEY_POLISH: OpeningRoot(
            opening_key=CHILD_KEY_POLISH,
            opening_name="Polish Opening",
            opening_family="Polish Opening",
            eco="A00",
            depth=1,
            parent_keys=frozenset(),
            child_keys=frozenset({CHILD_KEY_POLISH_E6, CHILD_KEY_POLISH_ALT}),
        ),
        CHILD_KEY_POLISH_E6: OpeningRoot(
            opening_key=CHILD_KEY_POLISH_E6,
            opening_name="Polish Opening, 1...e6",
            opening_family="Polish Opening",
            eco="A00",
            depth=2,
            parent_keys=frozenset({CHILD_KEY_POLISH}),
            child_keys=frozenset({CHILD_KEY_POLISH_BB2, CHILD_KEY_SHARED}),
        ),
        CHILD_KEY_POLISH_BB2: OpeningRoot(
            opening_key=CHILD_KEY_POLISH_BB2,
            opening_name="Polish Opening, 1...e6 2. Bb2",
            opening_family="Polish Opening",
            eco="A00",
            depth=3,
            parent_keys=frozenset({CHILD_KEY_POLISH_E6}),
            child_keys=frozenset(),
        ),
        CHILD_KEY_POLISH_ALT: OpeningRoot(
            opening_key=CHILD_KEY_POLISH_ALT,
            opening_name="Polish",
            opening_family="Polish",
            eco="A00",
            depth=2,
            parent_keys=frozenset({CHILD_KEY_POLISH}),
            child_keys=frozenset({CHILD_KEY_SHARED}),
        ),
        CHILD_KEY_SHARED: OpeningRoot(
            opening_key=CHILD_KEY_SHARED,
            opening_name="Polish Shared Node",
            opening_family="Polish Shared Node",
            eco="A00",
            depth=3,
            parent_keys=frozenset({CHILD_KEY_POLISH_E6, CHILD_KEY_POLISH_ALT, CHILD_KEY_ENGLISH}),
            child_keys=frozenset(),
        ),
        CHILD_KEY_ENGLISH: OpeningRoot(
            opening_key=CHILD_KEY_ENGLISH,
            opening_name="English Opening",
            opening_family="English Opening",
            eco="A10",
            depth=1,
            parent_keys=frozenset(),
            child_keys=frozenset({CHILD_KEY_SHARED}),
        ),
        CHILD_KEY_BIRD: OpeningRoot(
            opening_key=CHILD_KEY_BIRD,
            opening_name="Bird Opening",
            opening_family="Bird Opening",
            eco="A02",
            depth=1,
            parent_keys=frozenset(),
            child_keys=frozenset(),
        ),
    }
    ownership = {opening_key: frozenset({opening_key}) for opening_key in roots}
    return OpeningRoots(roots, ownership)


def _children_url() -> str:
    return "/api/openings/children"


# Case 1: auth required
def test_family_scores_no_auth_returns_401(client):
    resp = client.get(_FAMILIES_URL, params={"player_color": "white"})
    assert resp.status_code == 401


# Case 2: invalid player_color returns 422
def test_family_scores_invalid_color_returns_422(client, auth_headers):
    resp = client.get(_FAMILIES_URL, params={"player_color": "red"}, headers=auth_headers())
    assert resp.status_code == 422


# Case 3: cache miss with historical evidence bootstraps batch
def test_family_scores_bootstrap_on_cache_miss(client, auth_headers, db_session):
    # Seed historical evidence: a completed game session with moves for user 123, black
    session = GameSession(
        id=uuid.uuid4(),
        user_id=123,
        started_at=datetime.now(timezone.utc),
        status="completed",
        result="win",
        engine_elo=1500,
        player_color="black",
    )
    db_session.add(session)
    db_session.commit()
    db_session.add_all([
        SessionMove(
            session_id=session.id, move_number=1, color="white", move_san="e4",
            fen_before="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            fen_after="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            eval_delta=0,
        ),
        SessionMove(
            session_id=session.id, move_number=1, color="black", move_san="e5",
            fen_before="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            fen_after="rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            eval_delta=0,
        ),
    ])
    db_session.commit()

    # No batch exists yet — verify
    assert db_session.query(OpeningScoreBatch).filter_by(user_id=123, player_color="black").first() is None

    # Patch graph/roots on the cache module so recompute uses our test graph
    with (
        patch("app.opening_cache.get_opening_graph", return_value=_make_graph()),
        patch("app.opening_cache.get_opening_roots", return_value=_make_roots()),
    ):
        resp = client.get(_FAMILIES_URL, params={"player_color": "black"}, headers=auth_headers())

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["families"]) >= 1
    assert data["computed_at"] is not None

    # A batch row should now exist in the database
    batch = db_session.query(OpeningScoreBatch).filter_by(user_id=123, player_color="black").first()
    assert batch is not None


# Case 4: true no-evidence returns empty
def test_family_scores_no_evidence(client, auth_headers):
    with patch(_PATCH_ENSURE, return_value=(None, [])):
        resp = client.get(_FAMILIES_URL, params={"player_color": "white"}, headers=auth_headers())

    assert resp.status_code == 200
    data = resp.json()
    assert data["families"] == []
    assert data["computed_at"] is None


# Case 5: empty batch returns empty families with non-null computed_at
def test_family_scores_empty_batch(client, auth_headers):
    batch = _make_batch(
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        registry_fingerprint=opening_score_inputs_fingerprint(_make_graph(), _make_roots()),
    )
    with patch(_PATCH_ENSURE, return_value=(batch, [])):
        resp = client.get(_FAMILIES_URL, params={"player_color": "white"}, headers=auth_headers())

    assert resp.status_code == 200
    data = resp.json()
    assert data["families"] == []
    assert data["computed_at"] is not None


# Case 6: weighted aggregation
def test_family_scores_weighted_aggregation(client, auth_headers):
    rows = [
        _make_row(opening_key="k1", opening_name="Root 1", opening_family="Fam",
                  opening_score=80.0, confidence=0.6, coverage=0.4, sample_size=5),
        _make_row(opening_key="k2", opening_name="Root 2", opening_family="Fam",
                  opening_score=40.0, confidence=0.4, coverage=0.6, sample_size=15),
    ]
    roots = _roots_for_rows(*rows)
    batch = _make_batch_for_roots(roots)

    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(_FAMILIES_URL, params={"player_color": "white"}, headers=auth_headers())

    data = resp.json()
    fam = data["families"][0]
    # Weighted: (80*0.6 + 40*0.4) / (0.6+0.4) = (48+16)/1.0 = 64.0
    assert fam["family_score"] == pytest.approx(64.0)
    assert fam["root_count"] == 2


# Case 7: all-zero confidence falls back to simple average
def test_family_scores_zero_confidence_fallback(client, auth_headers):
    rows = [
        _make_row(opening_key="k1", opening_name="R1", opening_family="F",
                  opening_score=30.0, confidence=0.0),
        _make_row(opening_key="k2", opening_name="R2", opening_family="F",
                  opening_score=70.0, confidence=0.0),
    ]
    roots = _roots_for_rows(*rows)
    batch = _make_batch_for_roots(roots)

    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(_FAMILIES_URL, params={"player_color": "white"}, headers=auth_headers())

    fam = resp.json()["families"][0]
    assert fam["family_score"] == pytest.approx(50.0)


# Case 8: confidence and coverage are arithmetic means
def test_family_scores_confidence_coverage_means(client, auth_headers):
    rows = [
        _make_row(opening_key="k1", opening_name="R1", opening_family="F",
                  confidence=0.3, coverage=0.2),
        _make_row(opening_key="k2", opening_name="R2", opening_family="F",
                  confidence=0.9, coverage=0.8),
    ]
    roots = _roots_for_rows(*rows)
    batch = _make_batch_for_roots(roots)

    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(_FAMILIES_URL, params={"player_color": "white"}, headers=auth_headers())

    fam = resp.json()["families"][0]
    assert fam["family_confidence"] == pytest.approx(0.6)
    assert fam["family_coverage"] == pytest.approx(0.5)


# Case 9: root_sample_size_sum is a straight sum
def test_family_scores_sample_size_sum(client, auth_headers):
    rows = [
        _make_row(opening_key="k1", opening_name="R1", opening_family="F", sample_size=7),
        _make_row(opening_key="k2", opening_name="R2", opening_family="F", sample_size=13),
    ]
    roots = _roots_for_rows(*rows)
    batch = _make_batch_for_roots(roots)

    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(_FAMILIES_URL, params={"player_color": "white"}, headers=auth_headers())

    fam = resp.json()["families"][0]
    assert fam["root_sample_size_sum"] == 20


# Case 10: last_practiced_at picks most recent non-null
def test_family_scores_last_practiced_at(client, auth_headers):
    rows = [
        _make_row(opening_key="k1", opening_name="R1", opening_family="F",
                  last_practiced_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        _make_row(opening_key="k2", opening_name="R2", opening_family="F",
                  last_practiced_at=datetime(2026, 3, 15, tzinfo=timezone.utc)),
        _make_row(opening_key="k3", opening_name="R3", opening_family="F",
                  last_practiced_at=None),
    ]
    roots = _roots_for_rows(*rows)
    batch = _make_batch_for_roots(roots)

    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(_FAMILIES_URL, params={"player_color": "white"}, headers=auth_headers())

    fam = resp.json()["families"][0]
    assert fam["last_practiced_at"] is not None
    assert "2026-03-15" in fam["last_practiced_at"]


# Case 11: weakest-root tie-break is deterministic
def test_family_scores_weakest_root_tiebreak(client, auth_headers):
    # Same score, same confidence — tie-break on opening_name ascending
    rows = [
        _make_row(opening_key="k-z", opening_name="Zulu Root", opening_family="F",
                  opening_score=50.0, confidence=0.5),
        _make_row(opening_key="k-a", opening_name="Alpha Root", opening_family="F",
                  opening_score=50.0, confidence=0.5),
    ]
    roots = _roots_for_rows(*rows)
    batch = _make_batch_for_roots(roots)

    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(_FAMILIES_URL, params={"player_color": "white"}, headers=auth_headers())

    fam = resp.json()["families"][0]
    assert fam["weakest_root_name"] == "Alpha Root"
    assert fam["weakest_root_score"] == pytest.approx(50.0)


# Case 12: family ordering matches weakest-root sort
def test_family_scores_ordering(client, auth_headers):
    rows = [
        _make_row(opening_key="k1", opening_name="Strong Root", opening_family="Strong Family",
                  opening_score=90.0),
        _make_row(opening_key="k2", opening_name="Weak Root", opening_family="Weak Family",
                  opening_score=20.0),
        _make_row(opening_key="k3", opening_name="Mid Root", opening_family="Mid Family",
                  opening_score=50.0),
    ]
    roots = _roots_for_rows(*rows)
    batch = _make_batch_for_roots(roots)

    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(_FAMILIES_URL, params={"player_color": "white"}, headers=auth_headers())

    names = [f["family_name"] for f in resp.json()["families"]]
    assert names == ["Weak Family", "Mid Family", "Strong Family"]


# Case 13: two batches don't mix — only latest batch rows returned
def test_family_scores_single_batch_semantics(client, auth_headers, db_session):
    # Seed two batches for user 123, white — generation 1 and generation 2
    cursor = OpeningScoreCursor(user_id=123, player_color="white", latest_generation=2)
    db_session.add(cursor)
    db_session.flush()
    roots = _roots_for_rows(_make_row(opening_key="new-key", opening_name="New Root", opening_family="Fam"))

    batch1 = OpeningScoreBatch(user_id=123, player_color="white", generation=1,
                               registry_fingerprint=opening_score_inputs_fingerprint(_make_graph(), roots),
                               computed_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    batch2 = OpeningScoreBatch(user_id=123, player_color="white", generation=2,
                               registry_fingerprint=opening_score_inputs_fingerprint(_make_graph(), roots),
                               computed_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    db_session.add_all([batch1, batch2])
    db_session.flush()

    # Batch 1 has "Old Root" with score 10
    db_session.add(UserOpeningScore(
        batch_id=batch1.id, user_id=123, player_color="white",
        opening_key="old-key", opening_name="Old Root", opening_family="Fam",
        opening_score=10.0, confidence=0.5, coverage=0.5,
        weighted_depth=1.0, sample_size=5,
        computed_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    ))
    # Batch 2 has "New Root" with score 80
    db_session.add(UserOpeningScore(
        batch_id=batch2.id, user_id=123, player_color="white",
        opening_key="new-key", opening_name="New Root", opening_family="Fam",
        opening_score=80.0, confidence=0.9, coverage=0.7,
        weighted_depth=2.0, sample_size=20,
        computed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ))
    db_session.commit()

    with patch(_PATCH_ROOTS, return_value=roots):
        resp = client.get(_FAMILIES_URL, params={"player_color": "white"}, headers=auth_headers())

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["families"]) == 1
    fam = data["families"][0]
    # Only batch 2 rows should appear — "New Root", not "Old Root"
    assert fam["weakest_root_name"] == "New Root"
    assert fam["root_count"] == 1
    assert "2026" in data["computed_at"]


# Case 14: cache-only read path — calculator functions must not be called
def test_family_scores_cache_only_read_path(client, auth_headers, db_session):
    # Pre-seed a batch so the cache hit path is exercised
    cursor = OpeningScoreCursor(user_id=123, player_color="white", latest_generation=1)
    db_session.add(cursor)
    db_session.flush()

    roots = _roots_for_rows(_make_row(opening_key="cached-key", opening_name="Cached Root", opening_family="Cached Fam"))
    batch = OpeningScoreBatch(user_id=123, player_color="white", generation=1,
                              registry_fingerprint=opening_score_inputs_fingerprint(_make_graph(), roots),
                              computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc))
    db_session.add(batch)
    db_session.flush()

    db_session.add(UserOpeningScore(
        batch_id=batch.id, user_id=123, player_color="white",
        opening_key="cached-key", opening_name="Cached Root", opening_family="Cached Fam",
        opening_score=55.0, confidence=0.7, coverage=0.6,
        weighted_depth=2.0, sample_size=12,
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    ))
    db_session.commit()

    # Patch calculator functions in both namespaces — the router imports these symbols,
    # but ensure_opening_scores resolves recompute_opening_scores in its own module.
    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch("app.api.openings.recompute_opening_scores", side_effect=AssertionError("should not recompute")),
        patch("app.opening_cache.recompute_opening_scores", side_effect=AssertionError("should not recompute via cache")),
        patch("app.api.openings.overlay_evidence", side_effect=AssertionError("should not overlay")),
        patch("app.api.openings.compute_root_score", side_effect=AssertionError("should not compute")),
    ):
        resp = client.get(_FAMILIES_URL, params={"player_color": "white"}, headers=auth_headers())

    assert resp.status_code == 200
    data = resp.json()
    assert data["families"][0]["family_name"] == "Cached Fam"
    assert data["families"][0]["weakest_root_name"] == "Cached Root"


# Case 15: computed_at comes from batch, not rows or now()
def test_family_scores_computed_at_from_batch(client, auth_headers, db_session):
    batch_ts = datetime(2020, 1, 1, tzinfo=timezone.utc)
    row_ts = datetime(2024, 6, 15, tzinfo=timezone.utc)

    cursor = OpeningScoreCursor(user_id=123, player_color="white", latest_generation=1)
    db_session.add(cursor)
    db_session.flush()

    roots = _roots_for_rows(_make_row(opening_key="ts-key", opening_name="TS Root", opening_family="TS Fam"))
    batch = OpeningScoreBatch(user_id=123, player_color="white", generation=1,
                              registry_fingerprint=opening_score_inputs_fingerprint(_make_graph(), roots),
                              computed_at=batch_ts)
    db_session.add(batch)
    db_session.flush()

    db_session.add(UserOpeningScore(
        batch_id=batch.id, user_id=123, player_color="white",
        opening_key="ts-key", opening_name="TS Root", opening_family="TS Fam",
        opening_score=50.0, confidence=0.5, coverage=0.5,
        weighted_depth=1.0, sample_size=5,
        last_practiced_at=row_ts,
        computed_at=batch_ts,
    ))
    db_session.commit()

    with patch(_PATCH_ROOTS, return_value=roots):
        resp = client.get(_FAMILIES_URL, params={"player_color": "white"}, headers=auth_headers())

    data = resp.json()
    assert "2020-01-01" in data["computed_at"]
    assert "2024" not in data["computed_at"]


def test_family_scores_mismatched_fingerprint_triggers_recompute(client, auth_headers):
    roots = _make_drill_roots()
    stale_batch = _make_batch(
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        registry_fingerprint="stale-fingerprint",
    )
    fresh_batch = _make_batch_for_roots(
        roots,
        batch_id=2,
        generation=2,
        computed_at=datetime(2026, 3, 2, tzinfo=timezone.utc),
    )
    stale_rows = [
        _make_row(
            opening_key=DRILL_KEY_RUY_BERLIN,
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
            opening_score=48.0,
        )
    ]
    fresh_rows = [
        _make_row(
            batch_id=2,
            opening_key=DRILL_KEY_RUY_BERLIN,
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
            opening_score=48.0,
        )
    ]

    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(stale_batch, stale_rows)),
        patch(_PATCH_RECOMPUTE) as recompute_mock,
        patch(_PATCH_LIST_CACHED, return_value=(fresh_batch, fresh_rows)),
    ):
        resp = client.get(_FAMILIES_URL, params={"player_color": "white"}, headers=auth_headers())

    assert resp.status_code == 200
    recompute_mock.assert_called_once()
    data = resp.json()
    assert "2026-03-02" in data["computed_at"]
    assert data["families"][0]["family_name"] == DRILL_FAMILY_RUY


def test_family_scores_empty_batch_with_evidence_triggers_recompute(client, auth_headers):
    roots = _make_drill_roots()
    stale_batch = _make_batch(
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        registry_fingerprint="stale-fingerprint",
    )
    fresh_batch = _make_batch_for_roots(
        roots,
        batch_id=2,
        generation=2,
        computed_at=datetime(2026, 3, 2, tzinfo=timezone.utc),
    )
    fresh_rows = [
        _make_row(
            batch_id=2,
            opening_key=DRILL_KEY_RUY_BERLIN,
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
            opening_score=61.0,
        )
    ]

    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(stale_batch, [])),
        patch(_PATCH_RECOMPUTE) as recompute_mock,
        patch(_PATCH_LIST_CACHED, return_value=(fresh_batch, fresh_rows)),
    ):
        resp = client.get(_FAMILIES_URL, params={"player_color": "white"}, headers=auth_headers())

    assert resp.status_code == 200
    recompute_mock.assert_called_once()
    data = resp.json()
    assert "2026-03-02" in data["computed_at"]
    assert data["families"][0]["family_name"] == DRILL_FAMILY_RUY


def test_family_scores_empty_batch_with_current_registry_fingerprint_is_cache_hit(client, auth_headers):
    roots = _make_drill_roots()
    batch = _make_batch(
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        registry_fingerprint=opening_score_inputs_fingerprint(_make_graph(), roots),
    )
    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, [])),
        patch(_PATCH_RECOMPUTE, side_effect=AssertionError("should not recompute")),
    ):
        resp = client.get(_FAMILIES_URL, params={"player_color": "white"}, headers=auth_headers())

    assert resp.status_code == 200
    data = resp.json()
    assert data["families"] == []
    assert "2026-03-01" in data["computed_at"]


def test_family_drill_registry_drift_unknown_cached_root_triggers_recompute(client, auth_headers):
    roots = _make_drill_roots()
    stale_batch = _make_batch(
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        registry_fingerprint="stale-fingerprint",
    )
    fresh_batch = _make_batch_for_roots(
        roots,
        batch_id=2,
        generation=2,
        computed_at=datetime(2026, 3, 2, tzinfo=timezone.utc),
    )
    stale_rows = [
        _make_row(
            opening_key="stale-ruy-key",
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
            opening_score=55.0,
        )
    ]
    fresh_rows = [
        _make_row(
            batch_id=2,
            opening_key=DRILL_KEY_RUY_BERLIN,
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
            opening_score=55.0,
        )
    ]

    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(stale_batch, stale_rows)),
        patch(_PATCH_RECOMPUTE) as recompute_mock,
        patch(_PATCH_LIST_CACHED, return_value=(fresh_batch, fresh_rows)),
    ):
        resp = client.get(
            _drill_url(DRILL_FAMILY_RUY),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    assert resp.status_code == 200
    recompute_mock.assert_called_once()
    data = resp.json()
    assert "2026-03-02" in data["computed_at"]
    assert data["scored_roots"] == 1
    assert data["roots"][0]["opening_key"] == DRILL_KEY_RUY_BERLIN


def test_family_drill_empty_batch_with_evidence_triggers_recompute(client, auth_headers):
    roots = _make_drill_roots()
    stale_batch = _make_batch(
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        registry_fingerprint="stale-fingerprint",
    )
    fresh_batch = _make_batch_for_roots(
        roots,
        batch_id=2,
        generation=2,
        computed_at=datetime(2026, 3, 2, tzinfo=timezone.utc),
    )
    fresh_rows = [
        _make_row(
            batch_id=2,
            opening_key=DRILL_KEY_RUY_BERLIN,
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
            opening_score=55.0,
        )
    ]

    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(stale_batch, [])),
        patch(_PATCH_RECOMPUTE) as recompute_mock,
        patch(_PATCH_LIST_CACHED, return_value=(fresh_batch, fresh_rows)),
    ):
        resp = client.get(
            _drill_url(DRILL_FAMILY_RUY),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    assert resp.status_code == 200
    recompute_mock.assert_called_once()
    data = resp.json()
    assert "2026-03-02" in data["computed_at"]
    assert data["scored_roots"] == 1
    assert data["roots"][0]["opening_key"] == DRILL_KEY_RUY_BERLIN


def test_family_drill_empty_batch_with_current_registry_fingerprint_is_cache_hit(client, auth_headers):
    roots = _make_drill_roots()
    batch = _make_batch(
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        registry_fingerprint=opening_score_inputs_fingerprint(_make_graph(), roots),
    )
    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, [])),
        patch(_PATCH_RECOMPUTE, side_effect=AssertionError("should not recompute")),
    ):
        resp = client.get(
            _drill_url(DRILL_FAMILY_RUY),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["scored_roots"] == 0
    assert "2026-03-01" in data["computed_at"]


# ---------------------------------------------------------------------------
# GET /api/openings/families/{family_name}/scores
# ---------------------------------------------------------------------------


def test_family_drill_no_auth_returns_401(client):
    resp = client.get(_drill_url(DRILL_FAMILY_RUY), params={"player_color": "white"})
    assert resp.status_code == 401


def test_family_drill_invalid_color_returns_422(client, auth_headers):
    with patch(_PATCH_ROOTS, return_value=_make_drill_roots()):
        resp = client.get(
            _drill_url(DRILL_FAMILY_RUY),
            params={"player_color": "red"},
            headers=auth_headers(),
        )
    assert resp.status_code == 422


def test_family_drill_unknown_family_returns_404(client, auth_headers):
    with patch(_PATCH_ROOTS, return_value=_make_drill_roots()):
        resp = client.get(
            _drill_url("Unknown Family"),
            params={"player_color": "white"},
            headers=auth_headers(),
        )
    assert resp.status_code == 404
    assert "Unknown opening family" in resp.json()["detail"]


def test_family_drill_no_evidence_returns_all_family_roots_with_null_scores(client, auth_headers):
    with (
        patch(_PATCH_ROOTS, return_value=_make_drill_roots()),
        patch(_PATCH_ENSURE, return_value=(None, [])),
    ):
        resp = client.get(
            _drill_url(DRILL_FAMILY_RUY),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["family_name"] == DRILL_FAMILY_RUY
    assert data["computed_at"] is None
    assert data["total_roots"] == 3
    assert data["scored_roots"] == 0
    assert [root["opening_key"] for root in data["roots"]] == [
        DRILL_KEY_RUY_BERLIN,
        DRILL_KEY_RUY_EXCHANGE,
        DRILL_KEY_RUY_MORPHY,
    ]
    assert all(root["opening_score"] is None for root in data["roots"])


def test_family_drill_empty_batch_keeps_family_roots_and_batch_timestamp(client, auth_headers):
    roots = _make_drill_roots()
    batch = _make_batch(
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        registry_fingerprint=opening_score_inputs_fingerprint(_make_graph(), roots),
    )
    rows = [
        _make_row(
            opening_key=DRILL_KEY_SICILIAN_NAJDORF,
            opening_name="Sicilian Defense: Najdorf Variation",
            opening_family=DRILL_FAMILY_SICILIAN,
        )
    ]
    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(
            _drill_url(DRILL_FAMILY_RUY),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["scored_roots"] == 0
    assert "2026-03-01" in data["computed_at"]
    assert all(root["opening_score"] is None for root in data["roots"])


def test_family_drill_single_scored_root_keeps_unscored_roots(client, auth_headers):
    roots = _make_drill_roots()
    batch = _make_batch_for_roots(roots)
    rows = [
        _make_row(
            opening_key=DRILL_KEY_RUY_BERLIN,
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
            opening_score=42.0,
            confidence=0.7,
            coverage=0.3,
            weighted_depth=2.5,
            sample_size=9,
        )
    ]
    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(
            _drill_url(DRILL_FAMILY_RUY),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    data = resp.json()
    assert data["total_roots"] == 3
    assert data["scored_roots"] == 1
    assert data["roots"][0]["opening_key"] == DRILL_KEY_RUY_BERLIN
    assert data["roots"][0]["opening_name"] == "Ruy Lopez: Berlin Defense"
    assert data["roots"][0]["opening_family"] == DRILL_FAMILY_RUY
    assert data["roots"][0]["opening_score"] == pytest.approx(42.0)
    assert data["roots"][1]["opening_score"] is None
    assert data["roots"][2]["opening_score"] is None


def test_family_drill_sorts_scored_roots_before_unscored(client, auth_headers):
    roots = _make_drill_roots()
    batch = _make_batch_for_roots(roots)
    rows = [
        _make_row(
            opening_key=DRILL_KEY_RUY_BERLIN,
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
            opening_score=30.0,
        ),
        _make_row(
            opening_key=DRILL_KEY_RUY_EXCHANGE,
            opening_name="Ruy Lopez: Exchange Variation",
            opening_family=DRILL_FAMILY_RUY,
            opening_score=10.0,
        ),
    ]
    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(
            _drill_url(DRILL_FAMILY_RUY),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    assert resp.status_code == 200
    assert [root["opening_key"] for root in resp.json()["roots"]] == [
        DRILL_KEY_RUY_EXCHANGE,
        DRILL_KEY_RUY_BERLIN,
        DRILL_KEY_RUY_MORPHY,
    ]


def test_family_drill_resolves_branch_metadata_from_registry(client, auth_headers):
    roots = _make_drill_roots()
    batch = _make_batch_for_roots(roots)
    rows = [
        _make_row(
            opening_key=DRILL_KEY_RUY_BERLIN,
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
            strongest_branch_name="Sicilian Defense: Najdorf Variation",
            strongest_branch_key=DRILL_KEY_SICILIAN_NAJDORF,
            strongest_branch_score=18.0,
        )
    ]
    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(
            _drill_url(DRILL_FAMILY_RUY),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    branch = resp.json()["roots"][0]["strongest_branch"]
    assert branch == {
        "opening_key": DRILL_KEY_SICILIAN_NAJDORF,
        "opening_name": "Sicilian Defense: Najdorf Variation",
        "opening_family": DRILL_FAMILY_SICILIAN,
        "value": 18.0,
    }


def test_family_drill_null_branches_stay_null(client, auth_headers):
    roots = _make_drill_roots()
    batch = _make_batch_for_roots(roots)
    rows = [
        _make_row(
            opening_key=DRILL_KEY_RUY_BERLIN,
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
        )
    ]
    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(
            _drill_url(DRILL_FAMILY_RUY),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    item = resp.json()["roots"][0]
    assert item["strongest_branch"] is None
    assert item["weakest_branch"] is None
    assert item["underexposed_branch"] is None


def test_family_drill_unknown_branch_key_triggers_recompute(client, auth_headers):
    roots = _make_drill_roots()
    stale_batch = _make_batch(
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        registry_fingerprint="stale-fingerprint",
    )
    fresh_batch = _make_batch_for_roots(
        roots,
        batch_id=2,
        generation=2,
        computed_at=datetime(2026, 3, 2, tzinfo=timezone.utc),
    )
    stale_rows = [
        _make_row(
            opening_key=DRILL_KEY_RUY_BERLIN,
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
            strongest_branch_key="missing-root",
            strongest_branch_score=11.0,
        )
    ]
    fresh_rows = [
        _make_row(
            batch_id=2,
            opening_key=DRILL_KEY_RUY_BERLIN,
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
            strongest_branch_key=DRILL_KEY_SICILIAN_NAJDORF,
            strongest_branch_score=11.0,
        )
    ]
    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(stale_batch, stale_rows)),
        patch(_PATCH_RECOMPUTE) as recompute_mock,
        patch(_PATCH_LIST_CACHED, return_value=(fresh_batch, fresh_rows)),
    ):
        resp = client.get(
            _drill_url(DRILL_FAMILY_RUY),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    assert resp.status_code == 200
    recompute_mock.assert_called_once()
    data = resp.json()
    assert "2026-03-02" in data["computed_at"]
    assert data["roots"][0]["strongest_branch"]["opening_key"] == DRILL_KEY_SICILIAN_NAJDORF


def test_family_drill_lazily_enriches_missing_branch_summaries(client, auth_headers):
    graph = _make_graph()
    graph.has_position = lambda fen: fen == DRILL_KEY_RUY_BERLIN  # type: ignore[method-assign]
    roots = _make_drill_roots()
    batch = _make_batch_for_roots(roots)
    rows = [
        _make_row(
            opening_key=DRILL_KEY_RUY_BERLIN,
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
            opening_score=55.0,
        )
    ]
    lazy_score = RootScore(
        opening_key=DRILL_KEY_RUY_BERLIN,
        opening_name="Ruy Lopez: Berlin Defense",
        opening_family=DRILL_FAMILY_RUY,
        player_color="white",
        opening_score=55.0,
        confidence=0.8,
        coverage=0.7,
        weighted_depth=3.0,
        sample_size=12,
        last_practiced_at=None,
        strongest_branch=BranchSummary(
            opening_key=DRILL_KEY_RUY_EXCHANGE,
            opening_name="Ruy Lopez: Exchange Variation",
            value=61.0,
        ),
        weakest_branch=BranchSummary(
            opening_key=DRILL_KEY_RUY_MORPHY,
            opening_name="Ruy Lopez: Morphy Defense",
            value=48.0,
        ),
        underexposed_branch=BranchSummary(
            opening_key=DRILL_KEY_RUY_EXCHANGE,
            opening_name="Ruy Lopez: Exchange Variation",
            value=0.35,
        ),
        computed_at=datetime(2026, 3, 2, tzinfo=timezone.utc),
        debug_nodes=[],
    )
    with (
        patch(_PATCH_GRAPH, return_value=graph),
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
        patch(_PATCH_OVERLAY, return_value=_empty_overlay()),
        patch("app.api.openings.compute_root_score", return_value=lazy_score) as score_mock,
    ):
        resp = client.get(
            _drill_url(DRILL_FAMILY_RUY),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    assert resp.status_code == 200
    score_mock.assert_called_once()
    item = resp.json()["roots"][0]
    assert item["strongest_branch"]["opening_key"] == DRILL_KEY_RUY_EXCHANGE
    assert item["weakest_branch"]["opening_key"] == DRILL_KEY_RUY_MORPHY
    assert item["underexposed_branch"]["opening_key"] == DRILL_KEY_RUY_EXCHANGE


def test_family_drill_lazy_enrichment_does_not_reload_expired_cache_rows(
    client,
    auth_headers,
    db_session,
):
    roots = _make_drill_roots()
    graph = _make_graph()
    graph.has_position = lambda fen: fen == DRILL_KEY_RUY_BERLIN  # type: ignore[method-assign]

    batch_ts = datetime(2026, 3, 1, tzinfo=timezone.utc)
    row_ts = datetime(2026, 3, 2, tzinfo=timezone.utc)
    batch = OpeningScoreBatch(
        user_id=123,
        player_color="white",
        generation=1,
        registry_fingerprint=opening_score_inputs_fingerprint(graph, roots),
        computed_at=batch_ts,
    )
    db_session.add(batch)
    db_session.flush()
    db_session.add(
        UserOpeningScore(
            batch_id=batch.id,
            user_id=123,
            player_color="white",
            opening_key=DRILL_KEY_RUY_BERLIN,
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
            opening_score=55.0,
            confidence=0.7,
            coverage=0.6,
            weighted_depth=2.0,
            sample_size=12,
            last_practiced_at=row_ts,
            computed_at=batch_ts,
        )
    )
    db_session.commit()

    lazy_score = RootScore(
        opening_key=DRILL_KEY_RUY_BERLIN,
        opening_name="Ruy Lopez: Berlin Defense",
        opening_family=DRILL_FAMILY_RUY,
        player_color="white",
        opening_score=55.0,
        confidence=0.8,
        coverage=0.7,
        weighted_depth=3.0,
        sample_size=12,
        last_practiced_at=None,
        strongest_branch=BranchSummary(
            opening_key=DRILL_KEY_RUY_EXCHANGE,
            opening_name="Ruy Lopez: Exchange Variation",
            value=61.0,
        ),
        weakest_branch=BranchSummary(
            opening_key=DRILL_KEY_RUY_MORPHY,
            opening_name="Ruy Lopez: Morphy Defense",
            value=48.0,
        ),
        underexposed_branch=BranchSummary(
            opening_key=DRILL_KEY_RUY_EXCHANGE,
            opening_name="Ruy Lopez: Exchange Variation",
            value=0.35,
        ),
        computed_at=datetime(2026, 3, 3, tzinfo=timezone.utc),
        debug_nodes=[],
    )

    cache_selects: list[str] = []

    def _capture_cache_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select") and (
            " from opening_score_batches " in normalized
            or " from user_opening_scores " in normalized
        ):
            cache_selects.append(normalized)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _capture_cache_selects)
    try:
        with (
            patch(_PATCH_GRAPH, return_value=graph),
            patch(_PATCH_ROOTS, return_value=roots),
            patch(_PATCH_OVERLAY, return_value=_empty_overlay()),
            patch("app.api.openings.compute_root_score", return_value=lazy_score),
        ):
            resp = client.get(
                _drill_url(DRILL_FAMILY_RUY),
                params={"player_color": "white"},
                headers=auth_headers(),
            )
    finally:
        event.remove(engine, "before_cursor_execute", _capture_cache_selects)

    assert resp.status_code == 200
    assert len(cache_selects) == 2
    assert any(" from opening_score_batches " in statement for statement in cache_selects)
    assert any(" from user_opening_scores " in statement for statement in cache_selects)


def test_family_drill_underexposed_branch_uses_value_field(client, auth_headers):
    roots = _make_drill_roots()
    batch = _make_batch_for_roots(roots)
    rows = [
        _make_row(
            opening_key=DRILL_KEY_RUY_MORPHY,
            opening_name="Ruy Lopez: Morphy Defense",
            opening_family=DRILL_FAMILY_RUY,
            underexposed_branch_name="Ruy Lopez: Exchange Variation",
            underexposed_branch_key=DRILL_KEY_RUY_EXCHANGE,
            underexposed_branch_value=0.35,
        )
    ]
    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(
            _drill_url(DRILL_FAMILY_RUY),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    branch = resp.json()["roots"][0]["underexposed_branch"]
    assert branch["opening_key"] == DRILL_KEY_RUY_EXCHANGE
    assert branch["opening_name"] == "Ruy Lopez: Exchange Variation"
    assert branch["opening_family"] == DRILL_FAMILY_RUY
    assert branch["value"] == pytest.approx(0.35)


def test_family_drill_uses_registry_for_membership_name_depth_and_eco(client, auth_headers):
    roots = _make_drill_roots()
    batch = _make_batch_for_roots(roots)
    rows = [
        _make_row(
            opening_key=DRILL_KEY_RUY_MORPHY,
            opening_name="Ruy Lopez: Morphy Defense",
            opening_family=DRILL_FAMILY_RUY,
            opening_score=77.0,
        )
    ]
    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(
            _drill_url(DRILL_FAMILY_RUY),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    item = next(root for root in resp.json()["roots"] if root["opening_key"] == DRILL_KEY_RUY_MORPHY)
    assert item["opening_name"] == "Ruy Lopez: Morphy Defense"
    assert item["opening_family"] == DRILL_FAMILY_RUY
    assert item["depth"] == 3
    assert item["eco"] == "C60"
    assert item["opening_score"] == pytest.approx(77.0)


def test_family_drill_stale_batches_trigger_recompute(client, auth_headers):
    roots = _make_drill_roots()
    stale_batch = _make_batch(
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        registry_fingerprint=opening_score_inputs_fingerprint(_make_graph(), roots),
    )
    fresh_batch = _make_batch_for_roots(
        roots,
        batch_id=2,
        generation=2,
        computed_at=datetime(2026, 3, 2, tzinfo=timezone.utc),
    )
    stale_rows = [
        _make_row(
            opening_key=DRILL_KEY_RUY_BERLIN,
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
            strongest_branch_name="Sicilian Defense: Najdorf Variation",
            strongest_branch_key=None,
            strongest_branch_score=21.0,
        )
    ]
    fresh_rows = [
        _make_row(
            batch_id=2,
            opening_key=DRILL_KEY_RUY_BERLIN,
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
            strongest_branch_key=DRILL_KEY_SICILIAN_NAJDORF,
            strongest_branch_score=21.0,
        )
    ]
    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(stale_batch, stale_rows)),
        patch(_PATCH_RECOMPUTE) as recompute_mock,
        patch(_PATCH_LIST_CACHED, return_value=(fresh_batch, fresh_rows)),
    ):
        resp = client.get(
            _drill_url(DRILL_FAMILY_RUY),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    recompute_mock.assert_called_once()
    data = resp.json()
    assert "2026-03-02" in data["computed_at"]
    assert data["roots"][0]["strongest_branch"]["opening_key"] == DRILL_KEY_SICILIAN_NAJDORF


def test_family_drill_cache_only_read_path(client, auth_headers, db_session):
    cursor = OpeningScoreCursor(user_id=123, player_color="white", latest_generation=1)
    db_session.add(cursor)
    db_session.flush()
    roots = _make_drill_roots()

    batch_ts = datetime(2020, 1, 1, tzinfo=timezone.utc)
    row_ts = datetime(2024, 6, 15, tzinfo=timezone.utc)
    batch = OpeningScoreBatch(
        user_id=123,
        player_color="white",
        generation=1,
        registry_fingerprint=opening_score_inputs_fingerprint(_make_graph(), roots),
        computed_at=batch_ts,
    )
    db_session.add(batch)
    db_session.flush()

    db_session.add(
        UserOpeningScore(
            batch_id=batch.id,
            user_id=123,
            player_color="white",
            opening_key=DRILL_KEY_RUY_BERLIN,
            opening_name="Ruy Lopez: Berlin Defense",
            opening_family=DRILL_FAMILY_RUY,
            opening_score=55.0,
            confidence=0.7,
            coverage=0.6,
            weighted_depth=2.0,
            sample_size=12,
            last_practiced_at=row_ts,
            strongest_branch_key=DRILL_KEY_SICILIAN_NAJDORF,
            strongest_branch_score=9.0,
            computed_at=batch_ts,
        )
    )
    db_session.commit()

    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch("app.api.openings.recompute_opening_scores", side_effect=AssertionError("should not recompute")),
        patch("app.opening_cache.recompute_opening_scores", side_effect=AssertionError("should not recompute via cache")),
        patch("app.api.openings.overlay_evidence", side_effect=AssertionError("should not overlay")),
        patch("app.opening_cache.overlay_evidence", side_effect=AssertionError("should not overlay via cache")),
        patch("app.api.openings.compute_root_score", side_effect=AssertionError("should not compute")),
    ):
        resp = client.get(
            _drill_url(DRILL_FAMILY_RUY),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    data = resp.json()
    assert resp.status_code == 200
    assert "2020-01-01" in data["computed_at"]
    assert data["scored_roots"] == 1
    assert data["roots"][0]["opening_key"] == DRILL_KEY_RUY_BERLIN
    assert data["roots"][0]["last_practiced_at"].startswith("2024-06-15")
    assert data["roots"][0]["strongest_branch"]["opening_family"] == DRILL_FAMILY_SICILIAN


def test_family_drill_url_encoded_family_names_work(client, auth_headers):
    with (
        patch(_PATCH_ROOTS, return_value=_make_drill_roots()),
        patch(_PATCH_ENSURE, return_value=(None, [])),
    ):
        resp = client.get(
            _drill_url(DRILL_FAMILY_QGD),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["family_name"] == DRILL_FAMILY_QGD
    assert data["total_roots"] == 1
    assert data["roots"][0]["opening_key"] == DRILL_KEY_QGD_ORTHODOX


def test_children_top_level_returns_structural_roots_without_scores(client, auth_headers):
    roots = _make_children_roots()
    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(None, [])),
    ):
        resp = client.get(
            _children_url(),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["parent_key"] is None
    assert data["parent_name"] is None
    assert data["computed_at"] is None
    assert data["total_children"] == 3
    assert [child["opening_key"] for child in data["children"]] == [
        CHILD_KEY_BIRD,
        CHILD_KEY_ENGLISH,
        CHILD_KEY_POLISH,
    ]
    assert all(child["subtree_score"] is None for child in data["children"])
    assert all(child["subtree_root_count"] == 0 for child in data["children"])


def test_children_parent_key_returns_404_for_unknown_root(client, auth_headers):
    with patch(_PATCH_ROOTS, return_value=_make_children_roots()):
        resp = client.get(
            _children_url(),
            params={"player_color": "white", "parent_key": "missing-root"},
            headers=auth_headers(),
        )

    assert resp.status_code == 404
    assert "Unknown opening root" in resp.json()["detail"]


def test_children_drill_down_returns_immediate_children(client, auth_headers):
    roots = _make_children_roots()
    batch = _make_batch_for_roots(roots)
    rows = [
        _make_row(
            opening_key=CHILD_KEY_POLISH_E6,
            opening_name="Polish Opening, 1...e6",
            opening_family="Polish Opening",
            opening_score=20.0,
            confidence=0.3,
            coverage=0.3,
            sample_size=4,
            last_practiced_at=datetime(2026, 3, 2, tzinfo=timezone.utc),
        ),
        _make_row(
            opening_key=CHILD_KEY_POLISH_BB2,
            opening_name="Polish Opening, 1...e6 2. Bb2",
            opening_family="Polish Opening",
            opening_score=40.0,
            confidence=0.2,
            coverage=0.4,
            sample_size=6,
            last_practiced_at=datetime(2026, 3, 3, tzinfo=timezone.utc),
        ),
        _make_row(
            opening_key=CHILD_KEY_SHARED,
            opening_name="Polish Shared Node",
            opening_family="Polish Shared Node",
            opening_score=80.0,
            confidence=0.1,
            coverage=0.8,
            sample_size=8,
            last_practiced_at=datetime(2026, 3, 4, tzinfo=timezone.utc),
        ),
        _make_row(
            opening_key=CHILD_KEY_POLISH_ALT,
            opening_name="Polish",
            opening_family="Polish",
            opening_score=50.0,
            confidence=0.2,
            coverage=0.5,
            sample_size=2,
            last_practiced_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        ),
    ]
    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(
            _children_url(),
            params={"player_color": "white", "parent_key": CHILD_KEY_POLISH},
            headers=auth_headers(),
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["parent_key"] == CHILD_KEY_POLISH
    assert data["parent_name"] == "Polish Opening"
    assert [child["opening_key"] for child in data["children"]] == [
        CHILD_KEY_POLISH_ALT,
        CHILD_KEY_POLISH_E6,
    ]


def test_children_subtree_aggregation_deduplicates_shared_descendants(client, auth_headers):
    roots = _make_children_roots()
    batch = _make_batch_for_roots(roots)
    rows = [
        _make_row(
            opening_key=CHILD_KEY_POLISH,
            opening_name="Polish Opening",
            opening_family="Polish Opening",
            opening_score=60.0,
            confidence=0.4,
            coverage=0.6,
            sample_size=6,
            last_practiced_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        ),
        _make_row(
            opening_key=CHILD_KEY_POLISH_E6,
            opening_name="Polish Opening, 1...e6",
            opening_family="Polish Opening",
            opening_score=20.0,
            confidence=0.3,
            coverage=0.3,
            sample_size=4,
            last_practiced_at=datetime(2026, 3, 2, tzinfo=timezone.utc),
        ),
        _make_row(
            opening_key=CHILD_KEY_POLISH_ALT,
            opening_name="Polish",
            opening_family="Polish",
            opening_score=40.0,
            confidence=0.2,
            coverage=0.4,
            sample_size=2,
            last_practiced_at=datetime(2026, 3, 3, tzinfo=timezone.utc),
        ),
        _make_row(
            opening_key=CHILD_KEY_SHARED,
            opening_name="Polish Shared Node",
            opening_family="Polish Shared Node",
            opening_score=80.0,
            confidence=0.1,
            coverage=0.8,
            sample_size=8,
            last_practiced_at=datetime(2026, 3, 4, tzinfo=timezone.utc),
        ),
    ]
    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(
            _children_url(),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    assert resp.status_code == 200
    polish_item = next(
        child for child in resp.json()["children"] if child["opening_key"] == CHILD_KEY_POLISH
    )
    assert polish_item["child_count"] == 2
    assert polish_item["subtree_root_count"] == 4
    assert polish_item["subtree_sample_size"] == 20
    assert polish_item["subtree_score"] == pytest.approx(46.0)
    assert polish_item["subtree_confidence"] == pytest.approx(0.25)
    assert polish_item["subtree_coverage"] == pytest.approx(0.525)
    assert polish_item["weakest_root_key"] == CHILD_KEY_POLISH_E6
    assert polish_item["weakest_root_name"] == "Polish Opening, 1...e6"
    assert polish_item["weakest_root_family"] == "Polish Opening"
    assert polish_item["weakest_root_score"] == pytest.approx(20.0)
    assert polish_item["last_practiced_at"].startswith("2026-03-04")


def test_children_sorts_scored_before_unscored_with_null_last(client, auth_headers):
    roots = _make_children_roots()
    batch = _make_batch_for_roots(roots)
    rows = [
        _make_row(
            opening_key=CHILD_KEY_POLISH,
            opening_name="Polish Opening",
            opening_family="Polish Opening",
            opening_score=60.0,
            confidence=0.4,
        ),
        _make_row(
            opening_key=CHILD_KEY_POLISH_E6,
            opening_name="Polish Opening, 1...e6",
            opening_family="Polish Opening",
            opening_score=20.0,
            confidence=0.3,
        ),
        _make_row(
            opening_key=CHILD_KEY_POLISH_ALT,
            opening_name="Polish",
            opening_family="Polish",
            opening_score=40.0,
            confidence=0.2,
        ),
        _make_row(
            opening_key=CHILD_KEY_SHARED,
            opening_name="Polish Shared Node",
            opening_family="Polish Shared Node",
            opening_score=80.0,
            confidence=0.1,
        ),
    ]
    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(
            _children_url(),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    assert resp.status_code == 200
    children = resp.json()["children"]
    assert [child["opening_key"] for child in children] == [
        CHILD_KEY_ENGLISH,
        CHILD_KEY_POLISH,
        CHILD_KEY_BIRD,
    ]
    assert children[-1]["subtree_score"] is None
    assert children[-1]["opening_key"] == CHILD_KEY_BIRD


def test_children_sorts_by_subtree_score_descending_before_weakest_root_tiebreak(
    client, auth_headers
):
    roots = _make_children_roots()
    batch = _make_batch_for_roots(roots)
    rows = [
        _make_row(
            opening_key=CHILD_KEY_POLISH,
            opening_name="Polish Opening",
            opening_family="Polish Opening",
            opening_score=60.0,
            confidence=0.4,
        ),
        _make_row(
            opening_key=CHILD_KEY_POLISH_E6,
            opening_name="Polish Opening, 1...e6",
            opening_family="Polish Opening",
            opening_score=20.0,
            confidence=0.3,
        ),
        _make_row(
            opening_key=CHILD_KEY_POLISH_ALT,
            opening_name="Polish",
            opening_family="Polish",
            opening_score=40.0,
            confidence=0.2,
        ),
        _make_row(
            opening_key=CHILD_KEY_SHARED,
            opening_name="Polish Shared Node",
            opening_family="Polish Shared Node",
            opening_score=80.0,
            confidence=0.1,
        ),
        _make_row(
            opening_key=CHILD_KEY_ENGLISH,
            opening_name="English Opening",
            opening_family="English Opening",
            opening_score=52.0,
            confidence=0.5,
        ),
        _make_row(
            opening_key=CHILD_KEY_BIRD,
            opening_name="Bird Opening",
            opening_family="Bird Opening",
            opening_score=49.0,
            confidence=0.5,
        ),
    ]
    with (
        patch(_PATCH_ROOTS, return_value=roots),
        patch(_PATCH_ENSURE, return_value=(batch, rows)),
    ):
        resp = client.get(
            _children_url(),
            params={"player_color": "white"},
            headers=auth_headers(),
        )

    assert resp.status_code == 200
    children = resp.json()["children"]
    assert [child["opening_key"] for child in children] == [
        CHILD_KEY_ENGLISH,
        CHILD_KEY_BIRD,
        CHILD_KEY_POLISH,
    ]
