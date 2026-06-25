"""Tests for end-of-session opening-score deltas (g-xanz).

Covers the shared helper (snapshot_opening_baseline + compute_opening_score_delta)
and the endpoint wiring that surfaces ``opening_score_changes`` on game end, drill
natural-end, drill accuracy-fail, and the off-route route-check failure path.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import chess
import pytest

from app.models import GameSession, OpeningScoreBatch, SessionMove, UserOpeningScore
from app.opening_graph import OpeningGraph, OpeningGraphNode, _fen_from_board
from app.opening_roots import OpeningRoot, OpeningRoots
from app.opening_score_delta import (
    compute_opening_score_delta,
    snapshot_opening_baseline,
)

# The helper imports get_opening_roots / load_cached_rows into its own namespace,
# and lazy-imports the scheduler funcs from app.opening_score_scheduler.
PATCH_ROOTS = "app.opening_score_delta.get_opening_roots"


@pytest.fixture(autouse=True)
def _stub_scheduler():
    """Serve seeded rows directly: stub the scheduler so no worker thread spawns
    and seeded batches are never recomputed away. refresh_now returns True
    (recompute confirmed fresh) so the baseline snapshot treats the seeded batch
    as current; tests that need a failed refresh override it locally."""
    with (
        patch("app.opening_score_scheduler.request_recompute", return_value=None),
        patch("app.opening_score_scheduler.refresh_now", return_value=True),
    ):
        yield


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _fens_after(sans: list[str]) -> list[str]:
    board = chess.Board()
    out: list[str] = []
    for san in sans:
        board.push_san(san)
        out.append(_fen_from_board(board))
    return out


def _full_fens_after(sans: list[str]) -> list[str]:
    board = chess.Board()
    out: list[str] = []
    for san in sans:
        board.push_san(san)
        out.append(board.fen())
    return out


def _insert_moves(db_session, session_id: str, sans: list[str]) -> None:
    for index, (san, fen_after) in enumerate(zip(sans, _full_fens_after(sans))):
        db_session.add(
            SessionMove(
                session_id=uuid.UUID(str(session_id)),
                move_number=index // 2 + 1,
                color="white" if index % 2 == 0 else "black",
                move_san=san,
                fen_after=fen_after,
                segment="normal",
            )
        )
    db_session.commit()


def _make_roots(specs: dict[str, dict]) -> OpeningRoots:
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


def _make_batch(db_session, *, user_id=123, player_color="white", generation=1) -> int:
    batch = OpeningScoreBatch(
        user_id=user_id, player_color=player_color, generation=generation,
        registry_fingerprint="fp",
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    db_session.add(batch)
    db_session.flush()
    return batch.id


def _add_score_row(db_session, *, batch_id, opening_key, opening_score,
                   user_id=123, player_color="white", opening_name="x",
                   opening_family="x"):
    db_session.add(UserOpeningScore(
        batch_id=batch_id, user_id=user_id, player_color=player_color,
        opening_key=opening_key, opening_name=opening_name,
        opening_family=opening_family, opening_score=opening_score,
        confidence=0.5, coverage=0.5, weighted_depth=1.0, sample_size=5,
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    ))


def _make_session(db_session, *, user_id=123, player_color="white",
                  baseline=None) -> GameSession:
    sid = uuid.uuid4()
    db_session.add(GameSession(
        id=sid, user_id=user_id,
        started_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        status="ended", result="checkmate_win", engine_elo=1500,
        player_color=player_color, session_mode="normal",
        opening_score_baseline=baseline,
    ))
    db_session.commit()
    return db_session.query(GameSession).filter(GameSession.id == sid).one()


# ---------------------------------------------------------------------------
# snapshot_opening_baseline
# ---------------------------------------------------------------------------

def test_snapshot_returns_json_score_map(db_session):
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=41.0)
    _add_score_row(db_session, batch_id=batch_id, opening_key=MORPHY_KEY, opening_score=75.0)
    db_session.commit()

    import json
    snap = snapshot_opening_baseline(db_session, 123, "white")
    assert json.loads(snap) == {RUY_KEY: 41.0, MORPHY_KEY: 75.0}


def test_snapshot_empty_map_when_no_batch(db_session):
    # No batch + stubbed refresh_now -> a valid empty baseline ("{}"), NOT None,
    # so the session's first openings later read as new rather than unknown.
    assert snapshot_opening_baseline(db_session, 123, "white") == "{}"


def test_snapshot_none_on_failure(db_session):
    with patch("app.opening_score_delta.load_cached_rows", side_effect=RuntimeError("boom")):
        assert snapshot_opening_baseline(db_session, 123, "white") is None


def test_snapshot_forces_refresh_before_reading(db_session):
    # The baseline must reflect ALL evidence as of session start, so a bounded
    # refresh_now runs before the (otherwise warm-stale) read.
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=41.0)
    db_session.commit()

    with patch(
        "app.opening_score_scheduler.refresh_now", return_value=True
    ) as mock_refresh:
        snap = snapshot_opening_baseline(db_session, 123, "white")

    mock_refresh.assert_called_once_with(123, "white")
    import json
    assert json.loads(snap) == {RUY_KEY: 41.0}


def test_snapshot_rolls_back_on_failure(db_session):
    # A failed read can abort the transaction (Postgres); snapshot must roll back
    # so the caller's session-create commit is not poisoned.
    with (
        patch("app.opening_score_delta.load_cached_rows", side_effect=RuntimeError("boom")),
        patch.object(db_session, "rollback") as mock_rollback,
    ):
        result = snapshot_opening_baseline(db_session, 123, "white")
    assert result is None
    mock_rollback.assert_called_once()


def test_snapshot_skips_when_refresh_fails(db_session):
    # Warm cache holds a (possibly stale) batch, but refresh_now reports it could
    # NOT confirm freshness (timeout/failure/shutdown). Snapshotting the warm rows
    # anyway would persist a stale "before" -> end-of-session misattribution. The
    # snapshot must be skipped (NULL baseline) instead.
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=41.0)
    db_session.commit()

    with patch("app.opening_score_scheduler.refresh_now", return_value=False):
        result = snapshot_opening_baseline(db_session, 123, "white")

    assert result is None


def test_delta_serves_cached_when_final_refresh_fails(db_session):
    # The END-of-session recompute is best-effort: if refresh_now fails there, the
    # delta still serves the cached after-scores (compute ignores the bool).
    import json
    session = _make_session(db_session, baseline=json.dumps({RUY_KEY: 41.0}))
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    db_session.commit()

    with (
        patch("app.opening_score_scheduler.refresh_now", return_value=False),
        patch(PATCH_ROOTS, return_value=_ruy_roots()),
    ):
        items = compute_opening_score_delta(db_session, session)

    by_key = {item.opening_key: item for item in items}
    assert by_key[RUY_KEY].after == pytest.approx(44.0)
    assert by_key[RUY_KEY].delta == pytest.approx(3.0)


def test_game_start_succeeds_when_baseline_snapshot_fails(client, auth_headers, db_session):
    # Endpoint contract: a snapshot failure degrades the baseline to NULL but the
    # game must still start.
    with patch(
        "app.opening_score_delta.load_cached_rows", side_effect=RuntimeError("db boom")
    ):
        resp = client.post(
            "/api/game/start",
            json={"engine_elo": 1500, "player_color": "white"},
            headers=auth_headers(user_id=123),
        )
    assert resp.status_code == 201
    session = db_session.query(GameSession).filter(
        GameSession.id == uuid.UUID(resp.json()["session_id"])
    ).one()
    assert session.opening_score_baseline is None


# ---------------------------------------------------------------------------
# compute_opening_score_delta
# ---------------------------------------------------------------------------

def test_delta_numeric_when_baseline_has_key(db_session):
    import json
    session = _make_session(
        db_session,
        baseline=json.dumps({RUY_KEY: 41.0, MORPHY_KEY: 75.0}),
    )
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    _add_score_row(db_session, batch_id=batch_id, opening_key=MORPHY_KEY, opening_score=80.0)
    db_session.commit()

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        items = compute_opening_score_delta(db_session, session)

    by_key = {item.opening_key: item for item in items}
    assert [i.opening_key for i in items] == [KP_KEY, RUY_KEY, MORPHY_KEY]
    # RUY: present in baseline and batch -> numeric delta.
    assert by_key[RUY_KEY].before == pytest.approx(41.0)
    assert by_key[RUY_KEY].after == pytest.approx(44.0)
    assert by_key[RUY_KEY].delta == pytest.approx(3.0)
    assert by_key[RUY_KEY].is_new is False
    assert by_key[MORPHY_KEY].delta == pytest.approx(5.0)
    # KP: missing from baseline AND batch -> brand-new opening, no after score.
    assert by_key[KP_KEY].is_new is True
    assert by_key[KP_KEY].before is None
    assert by_key[KP_KEY].after is None
    assert by_key[KP_KEY].delta is None


def test_delta_is_new_when_baseline_lacks_key(db_session):
    # Empty baseline ("{}") -> every crossed opening is new; after-scores shown,
    # no numeric delta.
    session = _make_session(db_session, baseline="{}")
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=30.0)
    db_session.commit()

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        items = compute_opening_score_delta(db_session, session)

    by_key = {item.opening_key: item for item in items}
    assert by_key[RUY_KEY].is_new is True
    assert by_key[RUY_KEY].before is None
    assert by_key[RUY_KEY].after == pytest.approx(30.0)
    assert by_key[RUY_KEY].delta is None


def test_delta_null_baseline_shows_after_only(db_session):
    # Pre-feature session (baseline NULL): can't claim "new"; show after, no delta.
    session = _make_session(db_session, baseline=None)
    _insert_moves(db_session, session.id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=50.0)
    db_session.commit()

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        items = compute_opening_score_delta(db_session, session)

    by_key = {item.opening_key: item for item in items}
    assert by_key[RUY_KEY].is_new is False
    assert by_key[RUY_KEY].before is None
    assert by_key[RUY_KEY].after == pytest.approx(50.0)
    assert by_key[RUY_KEY].delta is None


def test_delta_empty_when_no_opening_crossed(db_session):
    session = _make_session(db_session, baseline="{}")
    _insert_moves(db_session, session.id, RUY_SANS)
    # Registry has only an unrelated root the game never reaches.
    other = _make_roots({
        "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -": {
            "name": "Queen's Pawn", "family": "Queen's Pawn", "depth": 1, "parents": []
        }
    })
    with patch(PATCH_ROOTS, return_value=other):
        assert compute_opening_score_delta(db_session, session) == []


def test_delta_after_none_when_opening_unscored(db_session):
    # Opening crossed but no cached row yet (after unknown) and baseline empty.
    session = _make_session(db_session, baseline="{}")
    _insert_moves(db_session, session.id, RUY_SANS)
    _make_batch(db_session)  # empty batch, no rows
    db_session.commit()

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        items = compute_opening_score_delta(db_session, session)

    for item in items:
        assert item.after is None
        assert item.delta is None
        assert item.is_new is True


def test_delta_never_raises_on_internal_failure(db_session):
    session = _make_session(db_session, baseline="{}")
    _insert_moves(db_session, session.id, RUY_SANS)
    with patch(PATCH_ROOTS, side_effect=RuntimeError("boom")):
        assert compute_opening_score_delta(db_session, session) == []


# ---------------------------------------------------------------------------
# Endpoint wiring: game start populates baseline, game end returns deltas
# ---------------------------------------------------------------------------

def test_game_start_populates_baseline(client, auth_headers, db_session):
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=41.0)
    db_session.commit()

    resp = client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=123),
    )
    assert resp.status_code == 201
    session = db_session.query(GameSession).filter(
        GameSession.id == uuid.UUID(resp.json()["session_id"])
    ).one()
    import json
    assert json.loads(session.opening_score_baseline) == {RUY_KEY: 41.0}


def test_game_end_returns_opening_score_changes(client, auth_headers, db_session):
    import json
    start = client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=123),
    )
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.opening_score_baseline = json.dumps({RUY_KEY: 41.0, MORPHY_KEY: 75.0})
    db_session.commit()
    _insert_moves(db_session, session_id, RUY_SANS)

    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    _add_score_row(db_session, batch_id=batch_id, opening_key=MORPHY_KEY, opening_score=80.0)
    db_session.commit()

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        resp = client.post(
            "/api/game/end",
            json={"session_id": session_id, "result": "checkmate_win", "pgn": "1. e4", "is_rated": False},
            headers=auth_headers(user_id=123),
        )

    assert resp.status_code == 200
    changes = {c["opening_key"]: c for c in resp.json()["opening_score_changes"]}
    assert changes[RUY_KEY]["delta"] == pytest.approx(3.0)
    assert changes[MORPHY_KEY]["delta"] == pytest.approx(5.0)
    assert changes[KP_KEY]["is_new"] is True


# ---------------------------------------------------------------------------
# Endpoint wiring: drill terminal paths
# ---------------------------------------------------------------------------

DRILL_ROOT_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"


def _drill_roots() -> OpeningRoots:
    root = OpeningRoot(
        opening_key=DRILL_ROOT_FEN, opening_name="King's Pawn Game",
        opening_family="King's Pawn Game", eco="B00", depth=1,
        parent_keys=frozenset(), child_keys=frozenset(),
    )
    return OpeningRoots({DRILL_ROOT_FEN: root}, {DRILL_ROOT_FEN: frozenset([DRILL_ROOT_FEN])})


def _start_drill(client, auth_headers, *, user_id=123):
    with patch("app.api.drills.get_opening_roots", return_value=_drill_roots()):
        return client.post(
            "/api/drills/start",
            json={"opening_key": DRILL_ROOT_FEN, "player_color": "white",
                  "engine_elo": 1500, "strictness": "standard"},
            headers=auth_headers(user_id=user_id),
        )


def _seed_drill_after_scores(db_session):
    batch_id = _make_batch(db_session, player_color="white")
    _add_score_row(db_session, batch_id=batch_id, opening_key=KP_KEY, opening_score=60.0)
    db_session.commit()


def test_drill_start_populates_baseline(client, auth_headers, db_session):
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=KP_KEY, opening_score=33.0)
    db_session.commit()

    start = _start_drill(client, auth_headers)
    assert start.status_code == 201
    session = db_session.query(GameSession).filter(
        GameSession.id == uuid.UUID(start.json()["session_id"])
    ).one()
    import json
    assert json.loads(session.opening_score_baseline) == {KP_KEY: 33.0}


def test_drill_natural_end_returns_opening_score_changes(client, auth_headers, db_session):
    import json
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.opening_score_baseline = json.dumps({KP_KEY: 40.0})
    db_session.commit()
    _insert_moves(db_session, session_id, RUY_SANS)
    _seed_drill_after_scores(db_session)

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        resp = client.post(
            f"/api/drills/{session_id}/natural-end",
            json={"result": "checkmate_win", "pgn": "1. e4"},
            headers=auth_headers(user_id=123),
        )

    assert resp.status_code == 200
    changes = {c["opening_key"]: c for c in resp.json()["opening_score_changes"]}
    assert changes[KP_KEY]["before"] == pytest.approx(40.0)
    assert changes[KP_KEY]["after"] == pytest.approx(60.0)
    assert changes[KP_KEY]["delta"] == pytest.approx(20.0)


def test_drill_accuracy_fail_returns_opening_score_changes(client, auth_headers, db_session):
    import json
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.drill_state = "root_reached"
    session.opening_score_baseline = json.dumps({KP_KEY: 40.0})
    db_session.commit()
    _insert_moves(db_session, session_id, RUY_SANS)
    _seed_drill_after_scores(db_session)

    with patch(PATCH_ROOTS, return_value=_ruy_roots()):
        resp = client.post(
            f"/api/drills/{session_id}/fail",
            json={"terminal_reason": "accuracy"},
            headers=auth_headers(user_id=123),
        )

    assert resp.status_code == 200
    changes = {c["opening_key"]: c for c in resp.json()["opening_score_changes"]}
    assert changes[KP_KEY]["delta"] == pytest.approx(20.0)


def test_drill_endpoints_omit_changes_when_not_terminal(client, auth_headers, db_session):
    # start / get must NOT carry deltas (no recompute on those paths).
    start = _start_drill(client, auth_headers)
    assert start.json()["opening_score_changes"] is None
    session_id = start.json()["session_id"]
    with patch("app.api.drills.get_opening_roots", return_value=_drill_roots()):
        got = client.get(f"/api/drills/{session_id}", headers=auth_headers(user_id=123))
    assert got.json()["opening_score_changes"] is None


# --- off-route route-check failure path -----------------------------------

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"
E4_E5_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -"
D4_FEN = "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -"


def _steering_graph() -> OpeningGraph:
    nodes = {
        START_FEN: OpeningGraphNode(START_FEN, "white"),
        E4_FEN: OpeningGraphNode(E4_FEN, "black"),
        E4_E5_FEN: OpeningGraphNode(E4_E5_FEN, "white"),
    }
    nodes[START_FEN].children["e2e4"] = E4_FEN
    nodes[E4_FEN].parents.add((START_FEN, "e2e4"))
    nodes[E4_FEN].children["e7e5"] = E4_E5_FEN
    nodes[E4_E5_FEN].parents.add((E4_FEN, "e7e5"))
    graph = OpeningGraph(nodes, START_FEN)
    graph.freeze()
    return graph


def test_drill_offroute_route_check_omits_opening_score_changes(
    client, auth_headers, db_session
):
    # Off-route fail no longer carries a delta: route-check is a speculative
    # per-move call that can't be upload-barriered, and going off-route means the
    # target opening was never reached. The response must not expose the field.
    import json
    graph = _steering_graph()
    offroute_roots = _make_roots({
        E4_E5_FEN: {"name": "Open Game", "family": "Open Game", "depth": 2, "parents": []},
    })
    with (
        patch("app.api.drills.get_opening_roots", return_value=offroute_roots),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = client.post(
            "/api/drills/start",
            json={"opening_key": E4_E5_FEN, "player_color": "white",
                  "engine_elo": 1500, "strictness": "standard"},
            headers=auth_headers(user_id=123),
        )
        assert start.status_code == 201
        session_id = start.json()["session_id"]

        session = db_session.query(GameSession).filter(
            GameSession.id == uuid.UUID(session_id)
        ).one()
        session.opening_score_baseline = json.dumps({KP_KEY: 40.0})
        db_session.commit()
        _insert_moves(db_session, session_id, RUY_SANS)
        _seed_drill_after_scores(db_session)

        with patch(PATCH_ROOTS, return_value=_ruy_roots()):
            resp = client.post(
                f"/api/drills/{session_id}/route-check",
                json={"current_fen": D4_FEN, "previous_fen": START_FEN,
                      "played_uci": "d2d4"},
                headers=auth_headers(user_id=123),
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "failed"
    assert "opening_score_changes" not in data


def test_route_check_on_route_omits_opening_score_changes(client, auth_headers, db_session):
    # The hot (non-terminal) route-check branch must not carry deltas either.
    graph = _steering_graph()
    roots = _make_roots({
        E4_E5_FEN: {"name": "Open Game", "family": "Open Game", "depth": 2, "parents": []},
    })
    with (
        patch("app.api.drills.get_opening_roots", return_value=roots),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = client.post(
            "/api/drills/start",
            json={"opening_key": E4_E5_FEN, "player_color": "white",
                  "engine_elo": 1500, "strictness": "standard"},
            headers=auth_headers(user_id=123),
        )
        session_id = start.json()["session_id"]
        # Playing the on-route first move (1. e4) stays on route.
        resp = client.post(
            f"/api/drills/{session_id}/route-check",
            json={"current_fen": E4_FEN, "previous_fen": START_FEN, "played_uci": "e2e4"},
            headers=auth_headers(user_id=123),
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "on_route"
    assert "opening_score_changes" not in resp.json()


# --- abandon gate (P3) ----------------------------------------------------

def test_game_end_abandon_skips_opening_score_changes(client, auth_headers, db_session):
    import json
    start = client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=123),
    )
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.opening_score_baseline = json.dumps({RUY_KEY: 41.0})
    db_session.commit()
    _insert_moves(db_session, session_id, RUY_SANS)
    batch_id = _make_batch(db_session)
    _add_score_row(db_session, batch_id=batch_id, opening_key=RUY_KEY, opening_score=44.0)
    db_session.commit()

    # Even with baseline + moves + scores that would otherwise yield a delta, an
    # abandon end must skip the (synchronous) recompute entirely.
    with patch(
        "app.api.game.compute_opening_score_delta"
    ) as mock_compute:
        resp = client.post(
            "/api/game/end",
            json={"session_id": session_id, "result": "abandon", "pgn": "1. e4", "is_rated": False},
            headers=auth_headers(user_id=123),
        )

    assert resp.status_code == 200
    assert resp.json()["opening_score_changes"] is None
    mock_compute.assert_not_called()
