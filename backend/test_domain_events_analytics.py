"""PostHog domain-event capture at the API handlers (Phase 4 of g-g9sq).

Each handler calls the ``capture`` helper imported into its own module
namespace; these tests patch that name with a recorder and assert the event
name, distinct_id (``str(user_id)``) and properties for representative
handlers. The real helper no-ops in the suite (POSTHOG_DISABLED=true), so the
recorder is the only thing under test here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models import Blunder, GameSession, Position
from app.opening_roots import OpeningRoot, OpeningRoots
from test_drill_api import _roots_for, _steering_graph, E4_E5_FEN, ROOT_FEN, START_FEN

_API_MODULES = ("auth", "game", "drills", "blunder", "srs", "session")

OPEN_GAME_FEN_FULL = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
KINGS_PAWN_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"


@pytest.fixture
def captured(monkeypatch):
    """Record every ``capture(distinct_id, event, props)`` across the API modules."""
    calls: list[tuple] = []
    recorder = lambda *a, **k: calls.append(a)  # noqa: E731
    for mod in _API_MODULES:
        monkeypatch.setattr(f"app.api.{mod}.capture", recorder)
    return calls


def _one(calls: list[tuple], event: str) -> tuple:
    matches = [a for a in calls if len(a) >= 2 and a[1] == event]
    assert len(matches) == 1, f"expected exactly one {event}, got {len(matches)}"
    return matches[0]


def _drill_roots() -> OpeningRoots:
    root = OpeningRoot(
        opening_key=KINGS_PAWN_FEN,
        opening_name="King's Pawn Game",
        opening_family="King's Pawn Game",
        eco="B00",
        depth=1,
        parent_keys=frozenset(),
        child_keys=frozenset(),
    )
    return OpeningRoots({KINGS_PAWN_FEN: root}, {KINGS_PAWN_FEN: frozenset([KINGS_PAWN_FEN])})


def _create_blunder(db_session, *, user_id: int) -> Blunder:
    position = Position(
        user_id=user_id,
        fen_hash=f"fen-hash-{user_id}",
        fen_raw="8/8/8/8/8/8/8/8 w - - 0 1",
        active_color="white",
    )
    db_session.add(position)
    db_session.flush()
    blunder = Blunder(
        user_id=user_id,
        position_id=position.id,
        bad_move_san="Qh5",
        best_move_san="Nf3",
        eval_loss_cp=120,
        pass_streak=0,
    )
    db_session.add(blunder)
    db_session.commit()
    db_session.refresh(blunder)
    return blunder


# --------------------------------------------------------------------------- auth


def test_register_emits_user_registered(client, captured):
    r = client.post("/api/auth/register", json={"username": "ghost_new", "password": "secret123"})
    assert r.status_code == 201
    did, _event, props = _one(captured, "user_registered")
    assert did == str(r.json()["user_id"])
    assert props == {"is_anonymous": True}


def test_login_emits_user_logged_in(client, captured, create_user):
    create_user("ghost_login", "secret123", is_anonymous=False)
    r = client.post("/api/auth/login", json={"username": "ghost_login", "password": "secret123"})
    assert r.status_code == 200
    did, _event, props = _one(captured, "user_logged_in")
    assert did == str(r.json()["user_id"])
    assert props == {"is_anonymous": False}


def test_claim_emits_user_claimed(client, captured):
    reg = client.post("/api/auth/register", json={"username": "ghost_anon", "password": "secret123"})
    token = reg.json()["token"]
    r = client.post(
        "/api/auth/claim",
        json={"new_username": "claimed_user", "new_password": "newsecret1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    did, _event, props = _one(captured, "user_claimed")
    assert did == str(r.json()["user_id"])
    assert props == {"is_anonymous": False}


# --------------------------------------------------------------------------- game


def test_start_game_emits_game_started(client, auth_headers, captured):
    r = client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=42),
    )
    assert r.status_code == 201
    did, _event, props = _one(captured, "game_started")
    assert did == "42"
    assert props["engine_elo"] == 1500
    assert props["player_color"] == "white"
    assert props["is_rated"] is True


def test_end_game_emits_game_ended(client, auth_headers, captured):
    start = client.post("/api/game/start", json={"engine_elo": 1500}, headers=auth_headers(user_id=42))
    session_id = start.json()["session_id"]
    r = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "checkmate_win",
            "pgn": "1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7#",
        },
        headers=auth_headers(user_id=42),
    )
    assert r.status_code == 200
    did, _event, props = _one(captured, "game_ended")
    assert did == "42"
    assert props["result"] == "checkmate_win"
    assert props["is_rated"] is True
    assert props["ply_count"] == 7
    # Rated checkmate → a rating change is computed; delta is consistent.
    assert props["rating_before"] is not None
    assert props["rating_after"] is not None
    assert props["rating_delta"] == props["rating_after"] - props["rating_before"]


def test_opponent_move_served_engine_fallback(client, auth_headers, captured):
    from app.opponent_move_controller import ControllerMove

    start = client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=123),
    )
    session_id = start.json()["session_id"]
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    fake_move = ControllerMove(uci="e7e5", san="e5", method="maia3_api")
    with patch("app.opponent_move_controller.choose_move", return_value=fake_move):
        r = client.post(
            "/api/game/next-opponent-move",
            json={"session_id": session_id, "fen": fen_after_e4, "moves": ["e2e4"]},
            headers=auth_headers(user_id=123),
        )
    assert r.status_code == 200
    did, _event, props = _one(captured, "opponent_move_served")
    assert did == "123"
    assert props == {"decision_source": "engine", "has_target_blunder": False, "replayed": False}


def test_opponent_move_served_marks_a_replayed_decision(client, auth_headers, captured):
    """A retry replays one stored decision; served counts must be able to exclude it."""
    from app.opponent_move_controller import ControllerMove

    start = client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=123),
    )
    session_id = start.json()["session_id"]
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    fake_move = ControllerMove(uci="e7e5", san="e5", method="maia3_api")
    body = {"session_id": session_id, "fen": fen_after_e4, "moves": ["e2e4"]}
    with patch("app.opponent_move_controller.choose_move", return_value=fake_move):
        assert client.post(
            "/api/game/next-opponent-move", json=body, headers=auth_headers(user_id=123)
        ).status_code == 200
        captured.clear()
        assert client.post(
            "/api/game/next-opponent-move", json=body, headers=auth_headers(user_id=123)
        ).status_code == 200

    _did, _event, props = _one(captured, "opponent_move_served")
    assert props == {
        "decision_source": "engine",
        "has_target_blunder": False,
        "replayed": True,
    }


# --------------------------------------------------------------------------- drills


def test_start_drill_emits_drill_started(client, auth_headers, captured):
    with patch("app.api.drills.get_opening_roots", return_value=_drill_roots()):
        r = client.post(
            "/api/drills/start",
            json={
                "opening_key": KINGS_PAWN_FEN,
                "player_color": "black",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(user_id=55),
        )
    assert r.status_code == 201
    did, _event, props = _one(captured, "drill_started")
    assert did == "55"
    assert props == {
        "opening_key": KINGS_PAWN_FEN,
        "family": "King's Pawn Game",
        "eco": "B00",
        "player_color": "black",
        "engine_elo": 1500,
        "strictness": "standard",
    }


def test_abandon_drill_emits_once_and_is_idempotent(client, auth_headers, captured):
    with patch("app.api.drills.get_opening_roots", return_value=_drill_roots()):
        start = client.post(
            "/api/drills/start",
            json={
                "opening_key": KINGS_PAWN_FEN,
                "player_color": "black",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(user_id=55),
        )
        session_id = start.json()["session_id"]
        first = client.post(f"/api/drills/{session_id}/abandon", headers=auth_headers(user_id=55))
        # Second abandon is a no-op (already ended) and must NOT re-emit.
        second = client.post(f"/api/drills/{session_id}/abandon", headers=auth_headers(user_id=55))
    assert first.status_code == 200
    assert second.status_code == 200
    did, _event, props = _one(captured, "drill_abandoned")
    assert did == "55"
    # NULL terminal_reason == quit mid-drill, the genuine abandon (g-drill-failed-overwrite).
    assert props == {"terminal_reason": None}


def test_abandon_after_failure_emits_both_events_once(client, auth_headers, captured, db_session):
    """A quit-after-failure emits drill_failed AND drill_abandoned, and the
    abandon event carries the reason that drill_state no longer records."""
    session_id = _start_kp_drill(client, auth_headers, user_id=55)
    _force_drill_state(db_session, session_id, "root_reached")
    with patch("app.api.drills.get_opening_roots", return_value=_drill_roots()):
        failed = client.post(
            f"/api/drills/{session_id}/fail",
            json={"terminal_reason": "accuracy"},
            headers=auth_headers(user_id=55),
        )
        abandoned = client.post(f"/api/drills/{session_id}/abandon", headers=auth_headers(user_id=55))
    assert failed.status_code == 200
    assert abandoned.status_code == 200

    assert _one(captured, "drill_failed")[2] == {"reason": "accuracy"}
    assert _one(captured, "drill_abandoned")[2] == {"terminal_reason": "accuracy"}


def _start_kp_drill(client, auth_headers, *, user_id: int) -> str:
    with patch("app.api.drills.get_opening_roots", return_value=_drill_roots()):
        r = client.post(
            "/api/drills/start",
            json={
                "opening_key": KINGS_PAWN_FEN,
                "player_color": "black",
                "engine_elo": 1500,
                "strictness": "standard",
            },
            headers=auth_headers(user_id=user_id),
        )
    assert r.status_code == 201
    return r.json()["session_id"]


def _force_drill_state(db_session, session_id: str, state: str) -> None:
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.drill_state = state
    db_session.commit()


def test_fail_drill_emits_drill_failed_accuracy(client, auth_headers, captured, db_session):
    session_id = _start_kp_drill(client, auth_headers, user_id=55)
    _force_drill_state(db_session, session_id, "root_reached")
    with patch("app.api.drills.get_opening_roots", return_value=_drill_roots()):
        r = client.post(
            f"/api/drills/{session_id}/fail",
            json={"terminal_reason": "accuracy"},
            headers=auth_headers(user_id=55),
        )
    assert r.status_code == 200
    did, _event, props = _one(captured, "drill_failed")
    assert did == "55"
    assert props == {"reason": "accuracy"}


def test_route_check_off_route_emits_drill_failed(client, auth_headers, captured):
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
            headers=auth_headers(user_id=77),
        )
        session_id = start.json()["session_id"]
        r = client.post(
            f"/api/drills/{session_id}/route-check",
            json={"current_fen": d4_fen, "previous_fen": START_FEN, "played_uci": "d2d4"},
            headers=auth_headers(user_id=77),
        )
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    did, _event, props = _one(captured, "drill_failed")
    assert did == "77"
    assert props == {"reason": "off_route"}


def test_natural_end_emits_drill_natural_end(client, auth_headers, captured):
    session_id = _start_kp_drill(client, auth_headers, user_id=55)
    with patch("app.api.drills.get_opening_roots", return_value=_drill_roots()):
        r = client.post(
            f"/api/drills/{session_id}/natural-end",
            json={"result": "checkmate_win"},
            headers=auth_headers(user_id=55),
        )
    assert r.status_code == 200
    did, _event, props = _one(captured, "drill_natural_end")
    assert did == "55"
    assert props == {"result": "checkmate_win"}


def test_continue_emits_drill_continued_once(client, auth_headers, captured, db_session):
    session_id = _start_kp_drill(client, auth_headers, user_id=55)
    _force_drill_state(db_session, session_id, "root_reached")
    with patch("app.api.drills.get_opening_roots", return_value=_drill_roots()):
        first = client.post(
            f"/api/drills/{session_id}/continue",
            json={"current_ply": 1},
            headers=auth_headers(user_id=55),
        )
        # Idempotent repeat (same rated_start_ply) echoes the contract; no re-emit.
        repeat = client.post(
            f"/api/drills/{session_id}/continue",
            json={"current_ply": 1},
            headers=auth_headers(user_id=55),
        )
    assert first.status_code == 200
    assert repeat.status_code == 200
    did, _event, props = _one(captured, "drill_continued")
    assert did == "55"
    assert props == {}


def test_opponent_move_served_drill_route_ghost(client, auth_headers, captured):
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
            headers=auth_headers(user_id=88),
        )
        session_id = start.json()["session_id"]
        r = client.post(
            "/api/game/next-opponent-move",
            json={"session_id": session_id, "fen": START_FEN, "moves": []},
            headers=auth_headers(user_id=88),
        )
    assert r.status_code == 200
    assert r.json()["mode"] == "ghost"
    did, _event, props = _one(captured, "opponent_move_served")
    assert did == "88"
    assert props == {"decision_source": "ghost", "has_target_blunder": False, "replayed": False}


def test_opponent_move_served_ghost_with_target_blunder(
    client, auth_headers, captured, create_game_session, db_session
):
    user_id = 123
    source_session = create_game_session(user_id=user_id, player_color="white")
    blunder_response = client.post(
        "/api/blunder",
        json={
            "session_id": source_session,
            "pgn": "1. e4 e5 2. Qh5",
            "fen": OPEN_GAME_FEN_FULL,
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=user_id),
    )
    assert blunder_response.status_code == 201
    blunder = db_session.get(Blunder, blunder_response.json()["blunder_id"])
    blunder.created_at = datetime.now(timezone.utc) - timedelta(hours=5)
    db_session.commit()

    new_session = create_game_session(user_id=user_id, player_color="white")
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    r = client.post(
        "/api/game/next-opponent-move",
        json={"session_id": new_session, "fen": fen_after_e4},
        headers=auth_headers(user_id=user_id),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] == "ghost"
    assert data["target_blunder_id"] is not None
    did, _event, props = _one(captured, "opponent_move_served")
    assert did == "123"
    assert props == {"decision_source": "ghost", "has_target_blunder": True, "replayed": False}


# --------------------------------------------------------------------------- srs


def test_review_emits_srs_review_recorded(client, auth_headers, captured, create_game_session, db_session):
    session_id = create_game_session(user_id=123, player_color="white")
    blunder = _create_blunder(db_session, user_id=123)
    r = client.post(
        "/api/srs/review",
        json={
            "session_id": session_id,
            "blunder_id": blunder.id,
            "passed": True,
            "user_move": "Nf3",
            "eval_delta": 20,
        },
        headers=auth_headers(user_id=123),
    )
    assert r.status_code == 200
    did, _event, props = _one(captured, "srs_review_recorded")
    assert did == "123"
    assert props == {
        "passed": True,
        "pass_streak": 1,
        "eval_delta": 20,
        "recompute_queued": True,
    }


# --------------------------------------------------------------------------- session


def _one_move() -> list[dict]:
    return [
        {
            "move_number": 1,
            "color": "white",
            "move_san": "e4",
            "fen_after": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            "eval_cp": 20,
            "eval_mate": None,
            "best_move_san": "e4",
            "best_move_eval_cp": 20,
            "eval_delta": 0,
            "classification": "best",
        }
    ]


def test_session_moves_emit_uploaded(client, auth_headers, captured, create_game_session):
    session_id = create_game_session(user_id=123, player_color="white")
    moves = _one_move()
    r = client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": moves},
        headers=auth_headers(user_id=123),
    )
    assert r.status_code == 200
    did, _event, props = _one(captured, "session_moves_uploaded")
    assert did == "123"
    # A plain (incremental-shaped) upload carries no client id and no terminal
    # action; the convenience enrichment (g-upload-observe) is null id, the
    # default finality flag, and the session's mode.
    assert props == {
        "move_count": len(moves),
        "recompute_queued": True,
        "client_request_id": None,
        "recompute_opportunity": True,
        "session_mode": "normal",
    }


def test_empty_session_moves_does_not_emit(client, auth_headers, captured, create_game_session):
    session_id = create_game_session(user_id=123, player_color="white")
    r = client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": []},
        headers=auth_headers(user_id=123),
    )
    assert r.status_code == 200
    assert not any(len(a) >= 2 and a[1] == "session_moves_uploaded" for a in captured)


# --------------------------------------------------------------------------- blunder


def test_record_blunder_emits(client, auth_headers, captured, create_game_session):
    session_id = create_game_session(user_id=123, player_color="white")
    r = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. Qh5",
            "fen": OPEN_GAME_FEN_FULL,
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123),
    )
    assert r.status_code == 201
    did, _event, props = _one(captured, "blunder_recorded")
    assert did == "123"
    assert props["eval_loss_cp"] == 150
    assert "opening_family" in props


def test_record_manual_blunder_emits(client, auth_headers, captured, create_game_session):
    session_id = create_game_session(user_id=123, player_color="white")
    r = client.post(
        "/api/blunder/manual",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. Qh5",
            "fen": OPEN_GAME_FEN_FULL,
            "user_move": "Qh5",
        },
        headers=auth_headers(user_id=123),
    )
    assert r.status_code == 201
    did, _event, props = _one(captured, "blunder_added_manual")
    assert did == "123"
    assert props == {}


def test_already_recorded_blunder_does_not_re_emit(client, auth_headers, captured, create_game_session):
    session_id = create_game_session(user_id=123, player_color="white", blunder_recorded=True)
    # Legacy already-recorded session with no idempotency bookkeeping → 409, no event.
    r = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. Qh5",
            "fen": OPEN_GAME_FEN_FULL,
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123),
    )
    assert r.status_code == 409
    assert not any(len(a) >= 2 and a[1] == "blunder_recorded" for a in captured)
