from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import chess

from app.models import AnalysisCache, Blunder, GameSession, RatingHistory, SessionMove
from app.opening_graph import OpeningGraph, OpeningGraphNode
from app.opening_roots import OpeningRoot, OpeningRoots

ROOT_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
E4_E5_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -"


def _push_fen(board: chess.Board, uci: str) -> str:
    board.push(chess.Move.from_uci(uci))
    return " ".join(board.fen().split()[:4])


def _steering_graph() -> OpeningGraph:
    board = chess.Board()
    start = " ".join(board.fen().split()[:4])
    e4 = _push_fen(board, "e2e4")
    e5 = _push_fen(board, "e7e5")

    nodes = {
        start: OpeningGraphNode(start, "white"),
        e4: OpeningGraphNode(e4, "black"),
        e5: OpeningGraphNode(e5, "white"),
    }
    nodes[start].children["e2e4"] = e4
    nodes[e4].parents.add((start, "e2e4"))
    nodes[e4].children["e7e5"] = e5
    nodes[e5].parents.add((e4, "e7e5"))
    graph = OpeningGraph(nodes, start)
    graph.freeze()
    return graph


def _roots() -> OpeningRoots:
    root = OpeningRoot(
        opening_key=ROOT_FEN,
        opening_name="King's Pawn Game",
        opening_family="King's Pawn Game",
        eco="B00",
        depth=1,
        parent_keys=frozenset(),
        child_keys=frozenset(),
    )
    return OpeningRoots({ROOT_FEN: root}, {ROOT_FEN: frozenset([ROOT_FEN])})


def _roots_for(root_fen: str) -> OpeningRoots:
    root = OpeningRoot(
        opening_key=root_fen,
        opening_name="Test Root",
        opening_family="Test Root",
        eco="T00",
        depth=1,
        parent_keys=frozenset(),
        child_keys=frozenset(),
    )
    return OpeningRoots({root_fen: root}, {root_fen: frozenset([root_fen])})


def _start_drill(client, auth_headers, *, user_id: int = 123):
    with patch("app.api.drills.get_opening_roots", return_value=_roots()):
        return client.post(
            "/api/drills/start",
            json={
                "opening_key": ROOT_FEN,
                "player_color": "black",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(user_id=user_id),
        )


def test_start_drill_persists_contract(client, auth_headers, db_session):
    response = _start_drill(client, auth_headers)

    assert response.status_code == 201
    data = response.json()
    assert data["mode"] == "drill"
    assert data["drill_state"] == "active"
    assert data["opening_key"] == ROOT_FEN
    assert data["opening_name"] == "King's Pawn Game"
    assert data["player_color"] == "black"
    assert data["engine_elo"] == 1500
    assert data["strictness"] == "standard"
    assert data["is_rated"] is False
    assert data["rated_start_ply"] is None

    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(data["session_id"])).one()
    assert session.session_mode == "drill"
    assert session.drill_state == "active"
    assert session.drill_opening_key == ROOT_FEN
    assert session.drill_strictness == "standard"
    assert session.is_rated is False


def test_start_drill_unknown_opening_returns_404(client, auth_headers):
    with patch("app.api.drills.get_opening_roots", return_value=_roots()):
        response = client.post(
            "/api/drills/start",
            json={
                "opening_key": "missing",
                "player_color": "black",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(),
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown opening root"


def test_route_check_marks_root_reached_by_user_move(client, auth_headers, db_session):
    graph = _steering_graph()
    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(ROOT_FEN)),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = client.post(
            "/api/drills/start",
            json={
                "opening_key": ROOT_FEN,
                "player_color": "white",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(),
        )
        assert start.status_code == 201
        session_id = start.json()["session_id"]
        response = client.post(
            f"/api/drills/{session_id}/route-check",
            json={
                "current_fen": ROOT_FEN,
                "previous_fen": START_FEN,
                "played_uci": "e2e4",
            },
            headers=auth_headers(),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "root_reached"
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    assert session.drill_state == "root_reached"


def test_route_check_fails_only_when_no_path_remains(client, auth_headers, db_session):
    graph = _steering_graph()
    d4_fen = "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -"
    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(E4_E5_FEN)),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = client.post(
            "/api/drills/start",
            json={
                "opening_key": E4_E5_FEN,
                "player_color": "white",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(),
        )
        assert start.status_code == 201
        session_id = start.json()["session_id"]
        response = client.post(
            f"/api/drills/{session_id}/route-check",
            json={
                "current_fen": d4_fen,
                "previous_fen": START_FEN,
                "played_uci": "d2d4",
            },
            headers=auth_headers(),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "failed"
    assert data["failure"]["correction_fen"] == START_FEN
    assert [suggestion["uci"] for suggestion in data["suggestions"]] == ["e2e4"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    assert session.drill_state == "failed"


def test_next_opponent_move_steers_active_drill_and_persists_root_reached(
    client,
    auth_headers,
    db_session,
):
    graph = _steering_graph()
    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(ROOT_FEN)),
        patch("app.api.game.get_opening_graph", return_value=graph),
    ):
        start = client.post(
            "/api/drills/start",
            json={
                "opening_key": ROOT_FEN,
                "player_color": "black",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(),
        )
        assert start.status_code == 201
        session_id = start.json()["session_id"]
        response = client.post(
            "/api/game/next-opponent-move",
            json={"session_id": session_id, "fen": START_FEN, "moves": []},
            headers=auth_headers(),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "ghost"
    assert data["decision_source"] == "ghost_path"
    assert data["move"] == {"uci": "e2e4", "san": "e4"}
    assert data["target_blunder_id"] is None
    assert data["drill_route"]["status"] == "root_reached"
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    assert session.drill_state == "root_reached"


def test_continue_drill_sets_boundary_and_resegments(client, auth_headers, db_session):
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]
    session_uuid = uuid.UUID(session_id)
    session = db_session.query(GameSession).filter(GameSession.id == session_uuid).one()
    session.drill_state = "root_reached"
    db_session.commit()

    upload = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": "fen-after-e4",
                },
                {
                    "move_number": 1,
                    "color": "black",
                    "move_san": "e5",
                    "fen_after": "fen-after-e5",
                },
            ]
        },
        headers=auth_headers(),
    )
    assert upload.status_code == 200
    assert {row.segment for row in db_session.query(SessionMove).filter(SessionMove.session_id == session_uuid)} == {"drill"}

    with patch("app.api.drills.get_opening_roots", return_value=_roots()):
        response = client.post(
            f"/api/drills/{session_id}/continue",
            json={"current_ply": 1},
            headers=auth_headers(),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["drill_state"] == "converted"
    assert data["is_rated"] is True
    assert data["rated_start_ply"] == 1
    assert data["normal_started_at"] is not None
    assert data["converted_at"] is not None

    rows = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == session_uuid)
        .order_by(SessionMove.move_number, SessionMove.color)
        .all()
    )
    assert [(row.color, row.segment) for row in rows] == [("black", "normal"), ("white", "drill")]

    with patch("app.api.drills.get_opening_roots", return_value=_roots()):
        repeated = client.post(
            f"/api/drills/{session_id}/continue",
            json={"current_ply": 1},
            headers=auth_headers(),
        )
        conflict = client.post(
            f"/api/drills/{session_id}/continue",
            json={"current_ply": 2},
            headers=auth_headers(),
        )
    assert repeated.status_code == 200
    assert conflict.status_code == 409


def test_converted_drill_next_opponent_move_uses_ghost_srs_metadata(
    client,
    auth_headers,
    create_game_session,
    db_session,
):
    user_id = 123
    source_session_id = create_game_session(user_id=user_id, player_color="white")
    fen_before_blunder = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    blunder_response = client.post(
        "/api/blunder",
        json={
            "session_id": source_session_id,
            "pgn": "1. e4 e5 2. Qh5",
            "fen": fen_before_blunder,
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=user_id),
    )
    assert blunder_response.status_code == 201
    blunder_id = blunder_response.json()["blunder_id"]
    blunder = db_session.get(Blunder, blunder_id)
    blunder.created_at = datetime.now(timezone.utc) - timedelta(hours=5)
    db_session.commit()

    with patch("app.api.drills.get_opening_roots", return_value=_roots()):
        start = client.post(
            "/api/drills/start",
            json={
                "opening_key": ROOT_FEN,
                "player_color": "white",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(user_id=user_id),
        )
    assert start.status_code == 201
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    now = datetime.now(timezone.utc)
    session.drill_state = "converted"
    session.is_rated = True
    session.rated_start_ply = 1
    session.normal_started_at = now
    session.converted_at = now
    db_session.commit()

    response = client.post(
        "/api/game/next-opponent-move",
        json={
            "session_id": session_id,
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            "moves": ["e2e4"],
        },
        headers=auth_headers(user_id=user_id),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "ghost"
    assert data["decision_source"] == "ghost_path"
    assert data["move"] == {"uci": "e7e5", "san": "e5"}
    assert data["target_blunder_id"] == blunder_id
    assert data["target_blunder_srs"]["pass_streak"] == 0


def test_converted_drill_next_opponent_move_uses_backend_engine_fallback(
    client,
    auth_headers,
    db_session,
):
    from app.opponent_move_controller import ControllerMove

    user_id = 123
    with patch("app.api.drills.get_opening_roots", return_value=_roots()):
        start = client.post(
            "/api/drills/start",
            json={
                "opening_key": ROOT_FEN,
                "player_color": "white",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(user_id=user_id),
        )
    assert start.status_code == 201
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    now = datetime.now(timezone.utc)
    session.drill_state = "converted"
    session.is_rated = True
    session.rated_start_ply = 1
    session.normal_started_at = now
    session.converted_at = now
    db_session.commit()

    fake_move = ControllerMove(uci="e7e5", san="e5", method="maia3_api")
    with patch("app.opponent_move_controller.choose_move", return_value=fake_move):
        response = client.post(
            "/api/game/next-opponent-move",
            json={
                "session_id": session_id,
                "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
                "moves": ["e2e4"],
            },
            headers=auth_headers(user_id=user_id),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "engine"
    assert data["decision_source"] == "backend_engine"
    assert data["move"] == {"uci": "e7e5", "san": "e5"}
    assert data["target_blunder_id"] is None


def test_continue_drill_requires_root_reached(client, auth_headers):
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]

    active = client.post(
        f"/api/drills/{session_id}/continue",
        json={"current_ply": 0},
        headers=auth_headers(),
    )

    failed = client.post(
        f"/api/drills/{session_id}/fail",
        headers=auth_headers(),
    )
    assert failed.status_code == 200
    failed_continue = client.post(
        f"/api/drills/{session_id}/continue",
        json={"current_ply": 0},
        headers=auth_headers(),
    )

    assert active.status_code == 400
    assert active.json()["detail"] == "Drill root must be reached before continuing"
    assert failed_continue.status_code == 400
    assert failed_continue.json()["detail"] == "Drill root must be reached before continuing"


def test_unconverted_drill_rejects_game_end_and_stays_unrated(client, auth_headers, db_session):
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]

    response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "checkmate_win",
            "pgn": "1. e4 e5",
            "is_rated": True,
        },
        headers=auth_headers(),
    )

    assert response.status_code == 400
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    assert session.status == "active"
    assert session.is_rated is False
    assert db_session.query(RatingHistory).filter(RatingHistory.game_session_id == session.id).count() == 0


def test_converted_drill_ignores_request_is_rated_false(client, auth_headers, db_session):
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.drill_state = "root_reached"
    db_session.commit()

    with patch("app.api.drills.get_opening_roots", return_value=_roots()):
        converted = client.post(
            f"/api/drills/{session_id}/continue",
            json={"current_ply": 0},
            headers=auth_headers(),
        )
    assert converted.status_code == 200

    ended = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "checkmate_win",
            "pgn": "1. e4 e5",
            "is_rated": False,
        },
        headers=auth_headers(),
    )

    assert ended.status_code == 200
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    assert session.is_rated is True
    assert db_session.query(RatingHistory).filter(RatingHistory.game_session_id == session.id).count() == 1


def test_converted_drill_abandon_preserves_rated_session_without_rating_history(client, auth_headers, db_session):
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.drill_state = "root_reached"
    db_session.commit()

    with patch("app.api.drills.get_opening_roots", return_value=_roots()):
        converted = client.post(
            f"/api/drills/{session_id}/continue",
            json={"current_ply": 0},
            headers=auth_headers(),
        )
    assert converted.status_code == 200

    ended = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "abandon",
            "pgn": "1. e4 e5",
            "is_rated": False,
        },
        headers=auth_headers(),
    )

    assert ended.status_code == 200
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    assert session.is_rated is True
    assert session.result == "abandon"
    assert db_session.query(RatingHistory).filter(RatingHistory.game_session_id == session.id).count() == 0


def test_abandoned_drill_hidden_from_history_stats_and_analysis(client, auth_headers, db_session):
    start = _start_drill(client, auth_headers)
    session_id = start.json()["session_id"]
    with patch("app.api.drills.get_opening_roots", return_value=_roots()):
        abandoned = client.post(f"/api/drills/{session_id}/abandon", headers=auth_headers())
    assert abandoned.status_code == 200

    assert client.get("/api/history", headers=auth_headers()).json()["games"] == []
    assert client.get("/api/stats/summary", headers=auth_headers()).json()["games"]["played"] == 0
    assert client.get(f"/api/session/{session_id}/analysis", headers=auth_headers()).status_code == 404

    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    assert session.result == "drill_abandon"
    assert session.is_rated is False


# ---------------------------------------------------------------------------
# strictness_cp tests
# ---------------------------------------------------------------------------

D4_FEN = "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -"


def _start_drill_cp(client, auth_headers, strictness_cp: int, *, root_fen: str = ROOT_FEN, user_id: int = 123):
    with patch("app.api.drills.get_opening_roots", return_value=_roots_for(root_fen)):
        return client.post(
            "/api/drills/start",
            json={
                "opening_key": root_fen,
                "player_color": "white",
                "engine_elo": 1500,
                "strictness": "standard",
                "strictness_cp": strictness_cp,
            },
            headers=auth_headers(user_id=user_id),
        )


def _seed_cache(db_session, fen_before: str, move_uci: str, *, eval_delta: int | None, played_eval: int | None = None, best_eval: int | None = None) -> None:
    entry = AnalysisCache(
        fen_before=fen_before,
        move_uci=move_uci,
        move_san="x",
        eval_delta=eval_delta,
        played_eval=played_eval,
        best_eval=best_eval,
    )
    db_session.add(entry)
    db_session.commit()


def test_start_drill_persists_strictness_cp(client, auth_headers, db_session):
    response = _start_drill_cp(client, auth_headers, strictness_cp=10)
    assert response.status_code == 201
    data = response.json()
    assert data["strictness_cp"] == 10

    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(data["session_id"])).one()
    assert session.drill_strictness_cp == 10


def test_start_drill_null_strictness_cp_persists(client, auth_headers, db_session):
    # No strictness_cp passed → null in DB, null in response
    with patch("app.api.drills.get_opening_roots", return_value=_roots()):
        response = client.post(
            "/api/drills/start",
            json={
                "opening_key": ROOT_FEN,
                "player_color": "black",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(),
        )
    assert response.status_code == 201
    data = response.json()
    assert data["strictness_cp"] is None

    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(data["session_id"])).one()
    assert session.drill_strictness_cp is None


def test_route_check_accuracy_failure(client, auth_headers, db_session):
    graph = _steering_graph()
    _seed_cache(db_session, START_FEN, "e2e4", eval_delta=30)

    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(E4_E5_FEN)),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = _start_drill_cp(client, auth_headers, strictness_cp=15, root_fen=E4_E5_FEN)
        assert start.status_code == 201
        session_id = start.json()["session_id"]

        response = client.post(
            f"/api/drills/{session_id}/route-check",
            json={"current_fen": ROOT_FEN, "previous_fen": START_FEN, "played_uci": "e2e4"},
            headers=auth_headers(),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "failed"
    assert data["failure"]["reason"] == "accuracy"
    assert data["failure"]["played_move_uci"] == "e2e4"
    assert data["failure"]["played_move_san"] == "e4"
    assert data["failure"]["correction_fen"] == START_FEN

    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    assert session.drill_state == "failed"


def test_route_check_accuracy_failure_before_root_reached(client, auth_headers, db_session):
    # Target-reaching move that also exceeds threshold should fail with reason=accuracy
    graph = _steering_graph()
    _seed_cache(db_session, START_FEN, "e2e4", eval_delta=20)

    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(ROOT_FEN)),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = _start_drill_cp(client, auth_headers, strictness_cp=10, root_fen=ROOT_FEN)
        session_id = start.json()["session_id"]

        response = client.post(
            f"/api/drills/{session_id}/route-check",
            json={"current_fen": ROOT_FEN, "previous_fen": START_FEN, "played_uci": "e2e4"},
            headers=auth_headers(),
        )

    data = response.json()
    assert data["status"] == "failed"
    assert data["failure"]["reason"] == "accuracy"


def test_route_check_cache_miss_best_effort_pass(client, auth_headers, db_session):
    # No cache entry → fall through to route check → on_route
    graph = _steering_graph()

    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(E4_E5_FEN)),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = _start_drill_cp(client, auth_headers, strictness_cp=0, root_fen=E4_E5_FEN)
        session_id = start.json()["session_id"]

        response = client.post(
            f"/api/drills/{session_id}/route-check",
            json={"current_fen": ROOT_FEN, "previous_fen": START_FEN, "played_uci": "e2e4"},
            headers=auth_headers(),
        )

    data = response.json()
    assert data["status"] == "on_route"


def test_route_check_tier_fallback_strict(client, auth_headers, db_session):
    # Without strictness_cp, strict tier threshold = 15
    graph = _steering_graph()
    _seed_cache(db_session, START_FEN, "e2e4", eval_delta=20)

    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(E4_E5_FEN)),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        with patch("app.api.drills.get_opening_roots", return_value=_roots_for(E4_E5_FEN)):
            start = client.post(
                "/api/drills/start",
                json={
                    "opening_key": E4_E5_FEN,
                    "player_color": "white",
                    "engine_elo": 1500,
                    "strictness": "strict",
                },
                headers=auth_headers(),
            )
        assert start.status_code == 201
        session_id = start.json()["session_id"]

        response = client.post(
            f"/api/drills/{session_id}/route-check",
            json={"current_fen": ROOT_FEN, "previous_fen": START_FEN, "played_uci": "e2e4"},
            headers=auth_headers(),
        )

    data = response.json()
    # eval_delta=20 > strict threshold=15 → accuracy failure
    assert data["status"] == "failed"
    assert data["failure"]["reason"] == "accuracy"


def test_route_check_onthefly_eval_both_evals_present(client, auth_headers, db_session):
    # eval_delta=None, played_eval=100, best_eval=130 (white to move → delta=30)
    graph = _steering_graph()
    _seed_cache(db_session, START_FEN, "e2e4", eval_delta=None, played_eval=100, best_eval=130)

    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(E4_E5_FEN)),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = _start_drill_cp(client, auth_headers, strictness_cp=20, root_fen=E4_E5_FEN)
        session_id = start.json()["session_id"]

        response = client.post(
            f"/api/drills/{session_id}/route-check",
            json={"current_fen": ROOT_FEN, "previous_fen": START_FEN, "played_uci": "e2e4"},
            headers=auth_headers(),
        )

    data = response.json()
    # on-the-fly delta=30 > threshold=20 → failure
    assert data["status"] == "failed"
    assert data["failure"]["reason"] == "accuracy"


def test_route_check_onthefly_eval_null_fallthrough_both_null(client, auth_headers, db_session):
    # Both played_eval and best_eval null → mate position → best-effort pass
    graph = _steering_graph()
    _seed_cache(db_session, START_FEN, "e2e4", eval_delta=None, played_eval=None, best_eval=None)

    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(E4_E5_FEN)),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = _start_drill_cp(client, auth_headers, strictness_cp=0, root_fen=E4_E5_FEN)
        session_id = start.json()["session_id"]

        response = client.post(
            f"/api/drills/{session_id}/route-check",
            json={"current_fen": ROOT_FEN, "previous_fen": START_FEN, "played_uci": "e2e4"},
            headers=auth_headers(),
        )

    data = response.json()
    assert data["status"] == "on_route"


def test_route_check_onthefly_eval_null_fallthrough_asymmetric(client, auth_headers, db_session):
    # played_eval present, best_eval null → cannot compute delta → best-effort pass
    graph = _steering_graph()
    _seed_cache(db_session, START_FEN, "e2e4", eval_delta=None, played_eval=50, best_eval=None)

    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(E4_E5_FEN)),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = _start_drill_cp(client, auth_headers, strictness_cp=0, root_fen=E4_E5_FEN)
        session_id = start.json()["session_id"]

        response = client.post(
            f"/api/drills/{session_id}/route-check",
            json={"current_fen": ROOT_FEN, "previous_fen": START_FEN, "played_uci": "e2e4"},
            headers=auth_headers(),
        )

    data = response.json()
    assert data["status"] == "on_route"


def test_route_check_suggestion_filtering_threshold_keeps_passing_move(client, auth_headers, db_session):
    # Off-route failure; suggestion e2e4 has delta=5 (passes threshold=20), is returned
    graph = _steering_graph()
    _seed_cache(db_session, START_FEN, "e2e4", eval_delta=5)

    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(E4_E5_FEN)),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = _start_drill_cp(client, auth_headers, strictness_cp=20, root_fen=E4_E5_FEN)
        session_id = start.json()["session_id"]

        response = client.post(
            f"/api/drills/{session_id}/route-check",
            json={"current_fen": D4_FEN, "previous_fen": START_FEN, "played_uci": "d2d4"},
            headers=auth_headers(),
        )

    data = response.json()
    assert data["status"] == "failed"
    suggestion_ucis = [s["uci"] for s in data["suggestions"]]
    assert suggestion_ucis == ["e2e4"]


def test_route_check_suggestion_filtering_all_exceed_returns_unfiltered(client, auth_headers, db_session):
    # Off-route failure; only suggestion e2e4 has delta=60 (exceeds threshold=20)
    # All suggestions exceed → unfiltered fallback → e2e4 still returned
    graph = _steering_graph()
    _seed_cache(db_session, START_FEN, "e2e4", eval_delta=60)

    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(E4_E5_FEN)),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = _start_drill_cp(client, auth_headers, strictness_cp=20, root_fen=E4_E5_FEN)
        session_id = start.json()["session_id"]

        response = client.post(
            f"/api/drills/{session_id}/route-check",
            json={"current_fen": D4_FEN, "previous_fen": START_FEN, "played_uci": "d2d4"},
            headers=auth_headers(),
        )

    data = response.json()
    assert data["status"] == "failed"
    suggestion_ucis = [s["uci"] for s in data["suggestions"]]
    assert suggestion_ucis == ["e2e4"]


def test_route_check_accuracy_failure_populates_all_failure_fields(client, auth_headers, db_session):
    graph = _steering_graph()
    _seed_cache(db_session, START_FEN, "e2e4", eval_delta=30)

    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(E4_E5_FEN)),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = _start_drill_cp(client, auth_headers, strictness_cp=15, root_fen=E4_E5_FEN)
        session_id = start.json()["session_id"]

        response = client.post(
            f"/api/drills/{session_id}/route-check",
            json={"current_fen": ROOT_FEN, "previous_fen": START_FEN, "played_uci": "e2e4"},
            headers=auth_headers(),
        )

    data = response.json()
    failure = data["failure"]
    assert failure["played_move_uci"] is not None
    assert failure["played_move_san"] is not None
    assert failure["correction_fen"] is not None
    assert failure["reason"] == "accuracy"


def test_route_check_off_route_failure_has_off_route_reason(client, auth_headers, db_session):
    graph = _steering_graph()

    with (
        patch("app.api.drills.get_opening_roots", return_value=_roots_for(E4_E5_FEN)),
        patch("app.api.drills.get_opening_graph", return_value=graph),
    ):
        start = _start_drill_cp(client, auth_headers, strictness_cp=50, root_fen=E4_E5_FEN)
        session_id = start.json()["session_id"]

        response = client.post(
            f"/api/drills/{session_id}/route-check",
            json={"current_fen": D4_FEN, "previous_fen": START_FEN, "played_uci": "d2d4"},
            headers=auth_headers(),
        )

    data = response.json()
    assert data["status"] == "failed"
    assert data["failure"]["reason"] == "off_route"
