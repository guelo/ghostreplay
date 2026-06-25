"""Tests for GET /api/session/{session_id}/openings — the opening lineage
endpoint backing the opening cards on the /history analysis page (g-uo2n)."""

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import chess
import pytest

from app.api.openings import build_opening_children
from app.opening_aggregate import _snapshot_cached_rows
from app.opening_cache import opening_score_inputs_fingerprint
from app.opening_graph import OpeningGraph, OpeningGraphNode, _fen_from_board
from app.opening_roots import OpeningRoot, OpeningRoots
from app.models import GameSession, OpeningScoreBatch, SessionMove, UserOpeningScore

PATCH_ROOTS = "app.api.session.get_opening_roots"
PATCH_GRAPH = "app.api.session.get_opening_graph"

# A tiny stub graph so the endpoint's stale-batch fingerprint check runs without
# building the real (~30s) opening graph. Its only role is a stable fingerprint.
_STUB_ROOT_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"


def _stub_graph() -> OpeningGraph:
    node = OpeningGraphNode(_STUB_ROOT_FEN, "white")
    return OpeningGraph({_STUB_ROOT_FEN: node}, _STUB_ROOT_FEN)


_STUB_GRAPH = _stub_graph()


@pytest.fixture(autouse=True)
def _stub_singletons():
    # The lineage reader goes through opening_cache.load_cached_rows, which lazy-
    # imports the scheduler funcs from app.opening_score_scheduler. Stub them there
    # (warm-path request_recompute + cold-path refresh_now) so unit tests serve
    # seeded rows directly without spawning the worker thread.
    with (
        patch("app.opening_score_scheduler.request_recompute", return_value=None),
        patch("app.opening_score_scheduler.refresh_now", return_value=False),
    ):
        yield


def _matching_fingerprint(roots: OpeningRoots) -> str:
    """Fingerprint the endpoint will compute for a stale-batch check, so a
    seeded batch is treated as fresh (no recompute that would wipe seeded rows)."""
    return opening_score_inputs_fingerprint(_STUB_GRAPH, roots)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fens_after(sans: list[str]) -> list[str]:
    """Return the normalized 4-field FEN after each SAN move."""
    board = chess.Board()
    out: list[str] = []
    for san in sans:
        board.push_san(san)
        out.append(_fen_from_board(board))
    return out


def _full_fens_after(sans: list[str]) -> list[str]:
    """Return the full 6-field FEN after each SAN move (as the client uploads)."""
    board = chess.Board()
    out: list[str] = []
    for san in sans:
        board.push_san(san)
        out.append(board.fen())
    return out


def _insert_moves(db_session, session_id: str, sans: list[str]) -> None:
    full_fens = _full_fens_after(sans)
    for index, (san, fen_after) in enumerate(zip(sans, full_fens)):
        move_number = index // 2 + 1
        color = "white" if index % 2 == 0 else "black"
        db_session.add(
            SessionMove(
                session_id=uuid.UUID(session_id),
                move_number=move_number,
                color=color,
                move_san=san,
                fen_after=fen_after,
                segment="normal",
            )
        )
    db_session.commit()


def _make_roots(specs: dict[str, dict]) -> OpeningRoots:
    """Build an OpeningRoots registry from {key: {name, family, eco, depth, parents}}."""
    roots: dict[str, OpeningRoot] = {}
    child_map: dict[str, set[str]] = {key: set() for key in specs}
    for key, spec in specs.items():
        for parent in spec.get("parents", []):
            child_map[parent].add(key)
    for key, spec in specs.items():
        roots[key] = OpeningRoot(
            opening_key=key,
            opening_name=spec["name"],
            opening_family=spec["family"],
            eco=spec.get("eco"),
            depth=spec["depth"],
            parent_keys=frozenset(spec.get("parents", [])),
            child_keys=frozenset(child_map[key]),
        )
    ownership = {key: frozenset({key}) for key in specs}
    return OpeningRoots(roots, ownership)


# Ruy Lopez: Morphy Defense line and its three boundary roots.
RUY_SANS = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"]
_RUY_FENS = _fens_after(RUY_SANS)
KP_KEY = _RUY_FENS[0]       # after 1. e4
RUY_KEY = _RUY_FENS[4]      # after 3. Bb5
MORPHY_KEY = _RUY_FENS[5]   # after 3... a6


def _ruy_roots() -> OpeningRoots:
    return _make_roots({
        KP_KEY: {"name": "King's Pawn Game", "family": "King's Pawn Game", "eco": "B00", "depth": 1, "parents": []},
        RUY_KEY: {"name": "Ruy Lopez", "family": "Ruy Lopez", "eco": "C60", "depth": 5, "parents": [KP_KEY]},
        MORPHY_KEY: {"name": "Ruy Lopez: Morphy Defense", "family": "Ruy Lopez", "eco": "C70", "depth": 6, "parents": [RUY_KEY]},
    })


def _add_score_row(db_session, *, batch_id, opening_key, opening_name, opening_family,
                   opening_score, confidence=0.5, coverage=0.5, sample_size=5,
                   player_color="white"):
    db_session.add(UserOpeningScore(
        batch_id=batch_id, user_id=123, player_color=player_color,
        opening_key=opening_key, opening_name=opening_name, opening_family=opening_family,
        opening_score=opening_score, confidence=confidence, coverage=coverage,
        weighted_depth=1.0, sample_size=sample_size,
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    ))


def _make_batch(db_session, roots: OpeningRoots, player_color="white") -> int:
    batch = OpeningScoreBatch(
        user_id=123, player_color=player_color, generation=1,
        registry_fingerprint=_matching_fingerprint(roots),
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    db_session.add(batch)
    db_session.flush()
    return batch.id


# ---------------------------------------------------------------------------
# Visibility guards
# ---------------------------------------------------------------------------

def test_openings_session_not_found(client, auth_headers):
    resp = client.get(
        "/api/session/00000000-0000-0000-0000-000000000000/openings",
        headers=auth_headers(user_id=123),
    )
    assert resp.status_code == 404


def test_openings_wrong_user_forbidden(client, auth_headers, create_game_session):
    session_id = create_game_session(user_id=999, player_color="white")
    resp = client.get(
        f"/api/session/{session_id}/openings",
        headers=auth_headers(user_id=123),
    )
    assert resp.status_code == 403


def test_openings_active_drill_returns_lineage(client, auth_headers, create_game_session, db_session):
    """An in-progress (not-yet-converted) drill is not "visible" in history, but
    the owner is actively playing it and must see its live opening lineage
    (g-8nke). The endpoint serves drill sessions for their owner."""
    session_id = create_game_session(user_id=123, player_color="white")
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.session_mode = "drill"
    session.drill_state = "active"
    session.is_rated = False
    session.rated_start_ply = None
    db_session.commit()
    _insert_moves(db_session, session_id, RUY_SANS)

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        resp = client.get(
            f"/api/session/{session_id}/openings",
            headers=auth_headers(user_id=123),
        )

    assert resp.status_code == 200
    keys = [item["opening_key"] for item in resp.json()["lineage"]]
    assert keys == [KP_KEY, RUY_KEY, MORPHY_KEY]


# ---------------------------------------------------------------------------
# Lineage derivation
# ---------------------------------------------------------------------------

def test_openings_ordered_lineage(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123, player_color="white")
    _insert_moves(db_session, session_id, RUY_SANS)

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        resp = client.get(f"/api/session/{session_id}/openings", headers=auth_headers(user_id=123))

    assert resp.status_code == 200
    data = resp.json()
    assert data["player_color"] == "white"
    keys = [item["opening_key"] for item in data["lineage"]]
    assert keys == [KP_KEY, RUY_KEY, MORPHY_KEY]
    names = [item["opening_name"] for item in data["lineage"]]
    assert names == ["King's Pawn Game", "Ruy Lopez", "Ruy Lopez: Morphy Defense"]
    depths = [item["depth"] for item in data["lineage"]]
    assert depths == [1, 5, 6]
    # Each item's path is its played ancestors, broadest -> deepest.
    assert data["lineage"][0]["path"] == []
    assert data["lineage"][1]["path"] == [KP_KEY]
    assert data["lineage"][2]["path"] == [KP_KEY, RUY_KEY]


def test_openings_lineage_canonicalizes_to_itself(client, auth_headers, create_game_session, db_session):
    """The played chain is a valid DAG route: each item links to its own node."""
    from app.api.openings import canonicalize_children_route

    session_id = create_game_session(user_id=123, player_color="white")
    _insert_moves(db_session, session_id, RUY_SANS)
    roots = _ruy_roots()

    with patch(PATCH_ROOTS, return_value=roots):
        resp = client.get(f"/api/session/{session_id}/openings", headers=auth_headers(user_id=123))

    for item in resp.json()["lineage"]:
        canonical_key, canonical_path, _ = canonicalize_children_route(
            item["opening_key"], item["path"], roots
        )
        assert canonical_key == item["opening_key"]
        assert canonical_path == item["path"]


def test_openings_transposition_played_chain_only(client, auth_headers, create_game_session, db_session):
    """A multi-parent root surfaces only the parent actually played."""
    # Build a synthetic line where the deep root has two possible parents but
    # only one is crossed in this game.
    sans = ["e4", "c5", "Nf3"]
    fens = _fens_after(sans)
    a_key = fens[0]      # after 1. e4 (played parent)
    deep_key = fens[2]   # after 2. Nf3 (multi-parent root)
    other_parent = "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -"  # d4 — not played

    roots = _make_roots({
        a_key: {"name": "King's Pawn Game", "family": "King's Pawn Game", "depth": 1, "parents": []},
        other_parent: {"name": "Queen's Pawn Game", "family": "Queen's Pawn Game", "depth": 1, "parents": []},
        deep_key: {"name": "Shared Transposition", "family": "Shared Transposition", "depth": 2, "parents": [a_key, other_parent]},
    })

    session_id = create_game_session(user_id=123, player_color="white")
    _insert_moves(db_session, session_id, sans)

    with patch(PATCH_ROOTS, return_value=roots):
        resp = client.get(f"/api/session/{session_id}/openings", headers=auth_headers(user_id=123))

    data = resp.json()
    keys = [item["opening_key"] for item in data["lineage"]]
    assert keys == [a_key, deep_key]
    # Only the played parent appears in the path, never the unplayed one.
    assert data["lineage"][1]["path"] == [a_key]
    assert other_parent not in data["lineage"][1]["path"]


def test_openings_empty_when_no_recognized_opening(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123, player_color="white")
    _insert_moves(db_session, session_id, RUY_SANS)
    # Registry has an unrelated root that the game never reaches.
    roots = _make_roots({
        "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -": {
            "name": "Queen's Pawn Game", "family": "Queen's Pawn Game", "depth": 1, "parents": []
        },
    })
    with patch(PATCH_ROOTS, return_value=roots):
        resp = client.get(f"/api/session/{session_id}/openings", headers=auth_headers(user_id=123))
    assert resp.status_code == 200
    assert resp.json()["lineage"] == []


def test_openings_black_player_color(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123, player_color="black")
    _insert_moves(db_session, session_id, RUY_SANS)
    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        resp = client.get(f"/api/session/{session_id}/openings", headers=auth_headers(user_id=123))
    assert resp.status_code == 200
    assert resp.json()["player_color"] == "black"
    assert len(resp.json()["lineage"]) == 3


# ---------------------------------------------------------------------------
# Subtree score semantics
# ---------------------------------------------------------------------------

def test_openings_fully_unscored_lineage_null(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123, player_color="white")
    _insert_moves(db_session, session_id, RUY_SANS)
    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        resp = client.get(f"/api/session/{session_id}/openings", headers=auth_headers(user_id=123))
    for item in resp.json()["lineage"]:
        assert item["score"] is None
        assert item["confidence"] is None
        assert item["coverage"] is None
        assert item["sample_size"] is None


def test_openings_ancestor_without_own_row_is_unscored(client, auth_headers, create_game_session, db_session):
    """Direct-row semantics: an ancestor without its own cached row is unscored,
    even when a descendant is scored (no descendant rollup)."""
    session_id = create_game_session(user_id=123, player_color="white")
    _insert_moves(db_session, session_id, RUY_SANS)

    batch_id = _make_batch(db_session, _ruy_roots())
    # Only the deepest node (Morphy) has a cached row.
    _add_score_row(
        db_session, batch_id=batch_id, opening_key=MORPHY_KEY,
        opening_name="Ruy Lopez: Morphy Defense", opening_family="Ruy Lopez",
        opening_score=70.0, confidence=0.6, coverage=0.5, sample_size=8,
    )
    db_session.commit()

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        resp = client.get(f"/api/session/{session_id}/openings", headers=auth_headers(user_id=123))

    lineage = {item["opening_key"]: item for item in resp.json()["lineage"]}
    # Only Morphy (its own direct row) is scored; ancestors are unscored.
    assert lineage[MORPHY_KEY]["score"] == pytest.approx(70.0)
    assert lineage[MORPHY_KEY]["sample_size"] == 8
    assert lineage[RUY_KEY]["score"] is None
    assert lineage[KP_KEY]["score"] is None


def test_openings_score_parity_with_build_opening_children(client, auth_headers, create_game_session, db_session):
    """Each item's score equals what build_opening_children reports for that node."""
    session_id = create_game_session(user_id=123, player_color="white")
    _insert_moves(db_session, session_id, RUY_SANS)
    roots = _ruy_roots()

    batch_id = _make_batch(db_session, roots)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY,
                   opening_name="Ruy Lopez", opening_family="Ruy Lopez",
                   opening_score=40.0, confidence=0.5, coverage=0.5, sample_size=4)
    _add_score_row(db_session, batch_id=batch_id, opening_key=MORPHY_KEY,
                   opening_name="Ruy Lopez: Morphy Defense", opening_family="Ruy Lopez",
                   opening_score=80.0, confidence=0.7, coverage=0.6, sample_size=6)
    db_session.commit()

    with patch(PATCH_ROOTS, return_value=roots):
        resp = client.get(f"/api/session/{session_id}/openings", headers=auth_headers(user_id=123))
    lineage = {item["opening_key"]: item for item in resp.json()["lineage"]}

    # Reconstruct rows_by_key the way both endpoints do and compare to the
    # /openings children builder (the card the user lands on).
    rows = (
        db_session.query(UserOpeningScore)
        .filter(UserOpeningScore.batch_id == batch_id)
        .all()
    )
    rows_by_key = {row.opening_key: row for row in _snapshot_cached_rows(rows)}

    # build_opening_children for parent KP_KEY yields the RUY child subtree score.
    kp_children = {c.opening_key: c for c in build_opening_children(rows_by_key, KP_KEY, roots)}
    assert lineage[RUY_KEY]["score"] == pytest.approx(kp_children[RUY_KEY].subtree_score)

    ruy_children = {c.opening_key: c for c in build_opening_children(rows_by_key, RUY_KEY, roots)}
    assert lineage[MORPHY_KEY]["score"] == pytest.approx(ruy_children[MORPHY_KEY].subtree_score)


def test_openings_lineage_warm_serves_cached_and_schedules_background(client, auth_headers, create_game_session, db_session):
    """Warm cache: the lineage reader serves the cached batch and schedules a
    background recompute via request_recompute — it never blocks on refresh_now
    and does no reader-side recompute (the worker repairs staleness)."""
    session_id = create_game_session(user_id=123, player_color="white")
    _insert_moves(db_session, session_id, RUY_SANS)
    roots = _ruy_roots()

    batch = OpeningScoreBatch(
        user_id=123, player_color="white", generation=1,
        registry_fingerprint="stale-fingerprint",
        computed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    db_session.add(batch)
    db_session.flush()
    db_session.commit()

    fresh_rows = [
        UserOpeningScore(
            batch_id=batch.id, user_id=123, player_color="white",
            opening_key=MORPHY_KEY, opening_name="Ruy Lopez: Morphy Defense",
            opening_family="Ruy Lopez", opening_score=90.0, confidence=0.6,
            coverage=0.5, weighted_depth=1.0, sample_size=8,
            computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
    ]

    with (
        patch(PATCH_ROOTS, return_value=roots),
        patch("app.opening_score_scheduler.request_recompute") as mock_recompute,
        patch("app.opening_score_scheduler.refresh_now") as mock_refresh,
        patch("app.opening_cache.list_cached_opening_scores", return_value=(batch, fresh_rows)),
    ):
        resp = client.get(f"/api/session/{session_id}/openings", headers=auth_headers(user_id=123))

    assert resp.status_code == 200
    mock_recompute.assert_called_once_with(123, "white")
    mock_refresh.assert_not_called()
    lineage = {item["opening_key"]: item for item in resp.json()["lineage"]}
    # Score reflects the cached batch.
    assert lineage[MORPHY_KEY]["score"] == pytest.approx(90.0)
