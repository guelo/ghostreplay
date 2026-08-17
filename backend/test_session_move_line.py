from __future__ import annotations

import uuid
from unittest.mock import patch

import chess

from app.models import (
    GameSession,
    SessionMove,
    SessionMoveTruncationReceipt,
    SessionUploadReceipt,
)


def _line(*sans: str) -> list[dict]:
    board = chess.Board()
    moves: list[dict] = []
    for index, san in enumerate(sans):
        fen_before = board.fen()
        move = board.parse_san(san)
        board.push(move)
        moves.append(
            {
                "move_number": index // 2 + 1,
                "color": "white" if index % 2 == 0 else "black",
                "move_san": san,
                "fen_before": fen_before,
                "fen_after": board.fen(),
                "move_uci": move.uci(),
                "eval_cp": index,
                "eval_mate": None,
                "best_move_san": san,
                "best_move_eval_cp": index,
                "eval_delta": 0,
                "classification": "best",
                "best_move_uci": move.uci(),
                "decision_source": "local_fallback",
                "target_blunder_id": None,
            }
        )
    return moves


def _session(db, session_id: str) -> GameSession:
    return db.get(GameSession, uuid.UUID(session_id))


def _make_unrated(db, session_id: str) -> None:
    row = _session(db, session_id)
    row.is_rated = False
    db.commit()


def _post_moves(client, headers, session_id: str, moves: list[dict], **extra):
    return client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": moves, **extra},
        headers=headers,
    )


def test_start_contracts_expose_zero_revision(
    client, auth_headers, create_game_session
):
    normal_id = create_game_session(user_id=8101)
    assert normal_id
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "black"},
        headers=auth_headers(user_id=8101),
    )
    assert response.status_code == 201
    assert response.json()["move_line_revision"] == 0


def test_truncation_deletes_tail_once_and_fences_stale_uploads(
    client, auth_headers, create_game_session, db_session
):
    user_id = 8102
    session_id = create_game_session(user_id=user_id)
    _make_unrated(db_session, session_id)
    headers = auth_headers(user_id=user_id)
    original = _line("e4", "e5", "Nf3", "Nc6")
    uploaded = _post_moves(client, headers, session_id, original, line_revision=0)
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["line_revision"] == 0

    request_id = str(uuid.uuid4())
    truncated = client.post(
        f"/api/session/{session_id}/moves/truncate",
        json={
            "client_request_id": request_id,
            "line_revision": 0,
            "after_ply": 2,
        },
        headers=headers,
    )
    assert truncated.status_code == 200, truncated.text
    assert truncated.json() == {
        "client_request_id": request_id,
        "from_revision": 0,
        "to_revision": 1,
        "line_revision": 1,
        "after_ply": 2,
        "deleted_move_count": 2,
        "evidence_changed": False,
    }
    db_session.expire_all()
    assert db_session.query(SessionMove).count() == 2

    replacement = _post_moves(
        client,
        headers,
        session_id,
        _line("e4", "e5", "d4"),
        line_revision=1,
    )
    assert replacement.status_code == 200, replacement.text
    db_session.expire_all()
    assert [
        row.move_san for row in db_session.query(SessionMove).order_by(SessionMove.id)
    ] == [
        "e4",
        "e5",
        "d4",
    ]

    reused_id = client.post(
        f"/api/session/{session_id}/moves/truncate",
        json={
            "client_request_id": request_id,
            "line_revision": 0,
            "after_ply": 1,
        },
        headers=headers,
    )
    assert reused_id.status_code == 409
    assert (
        reused_id.json()["error"]["details"]["error_code"]
        == "TRUNCATION_IDEMPOTENCY_CONFLICT"
    )

    stale_truncation = client.post(
        f"/api/session/{session_id}/moves/truncate",
        json={
            "client_request_id": str(uuid.uuid4()),
            "line_revision": 0,
            "after_ply": 0,
        },
        headers=headers,
    )
    assert stale_truncation.status_code == 409
    db_session.expire_all()
    assert _session(db_session, session_id).move_line_revision == 1
    assert db_session.query(SessionMoveTruncationReceipt).count() == 1
    assert db_session.query(SessionMove).count() == 3
    assert _session(db_session, session_id).move_line_revision == 1

    retried = client.post(
        f"/api/session/{session_id}/moves/truncate",
        json={
            "client_request_id": request_id,
            "line_revision": 0,
            "after_ply": 2,
        },
        headers=headers,
    )
    assert retried.status_code == 200
    assert retried.json()["deleted_move_count"] == 2
    assert db_session.query(SessionMoveTruncationReceipt).count() == 1
    db_session.expire_all()
    assert _session(db_session, session_id).move_line_revision == 1

    stale = _post_moves(client, headers, session_id, original)
    assert stale.status_code == 409
    assert stale.json()["error"]["details"] == {
        "error_code": "FOREIGN_BRANCH_REVISION",
        "current_revision": 1,
    }
    assert db_session.query(SessionMove).count() == 3


def test_empty_truncation_still_advances_revision(
    client, auth_headers, create_game_session, db_session
):
    user_id = 8103
    session_id = create_game_session(user_id=user_id)
    _make_unrated(db_session, session_id)
    response = client.post(
        f"/api/session/{session_id}/moves/truncate",
        json={
            "client_request_id": str(uuid.uuid4()),
            "line_revision": 0,
            "after_ply": 0,
        },
        headers=auth_headers(user_id=user_id),
    )
    assert response.status_code == 200, response.text
    assert response.json()["deleted_move_count"] == 0
    assert response.json()["line_revision"] == 1


def test_versioned_upload_keeps_line_identity_but_allows_eval_enrichment(
    client, auth_headers, create_game_session, db_session
):
    user_id = 8104
    session_id = create_game_session(user_id=user_id)
    headers = auth_headers(user_id=user_id)
    move = _line("e4")
    first = _post_moves(client, headers, session_id, move, line_revision=0)
    assert first.status_code == 200, first.text

    enriched = [{**move[0], "eval_cp": 55, "eval_delta": 12}]
    response = _post_moves(client, headers, session_id, enriched, line_revision=0)
    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert db_session.query(SessionMove).one().eval_cp == 55

    changed = _line("d4")
    conflict = _post_moves(client, headers, session_id, changed, line_revision=0)
    assert conflict.status_code == 409
    assert (
        conflict.json()["error"]["details"]["error_code"]
        == "MOVE_LINE_IDENTITY_CONFLICT"
    )
    db_session.expire_all()
    assert db_session.query(SessionMove).one().move_san == "e4"


def test_failed_final_full_proof_commits_rows_and_receipt_but_suppresses_evidence(
    client, auth_headers, create_game_session, db_session
):
    user_id = 8105
    session_id = create_game_session(user_id=user_id)
    headers = auth_headers(user_id=user_id)
    incomplete = _line("e4")
    nonstandard = chess.Board()
    nonstandard.push_san("d4")
    incomplete[0]["fen_before"] = nonstandard.fen()
    session = _session(db_session, session_id)
    session.status = "ended"
    session.pgn = "1. e4 *"
    db_session.commit()
    client_id = str(uuid.uuid4())
    headers["X-Client-Request-ID"] = client_id

    with (
        patch("app.api.session.enqueue_session_evidence") as enqueue,
        patch("app.api.session.bump_evidence_seq") as bump,
    ):
        response = _post_moves(
            client,
            headers,
            session_id,
            incomplete,
            line_revision=0,
            line_sync_verdict="synchronized",
            terminal_action="game_end",
            recompute_opportunity=True,
        )
    assert response.status_code == 200, response.text
    assert response.json()["line_proof_verdict"] == "nonstandard_start"
    enqueue.assert_not_called()
    bump.assert_not_called()
    db_session.expire_all()
    assert db_session.query(SessionMove).count() == 1
    stored_session = _session(db_session, session_id)
    assert stored_session.player_accuracy is not None
    assert stored_session.player_accuracy_algo_version is not None
    receipt = db_session.query(SessionUploadReceipt).one()
    assert receipt.move_line_revision == 0
    assert receipt.line_proof_verdict == "nonstandard_start"
    assert receipt.line_sync_verdict == "synchronized"


def test_passing_final_full_proof_retains_evidence_enqueue(
    client, auth_headers, create_game_session, db_session
):
    user_id = 8106
    session_id = create_game_session(user_id=user_id)
    headers = auth_headers(user_id=user_id)
    headers["X-Client-Request-ID"] = str(uuid.uuid4())
    with patch("app.api.session.enqueue_session_evidence") as enqueue:
        response = _post_moves(
            client,
            headers,
            session_id,
            _line("e4", "e5"),
            line_revision=0,
            line_sync_verdict="deadline_expired",
            terminal_action="game_end",
        )
    assert response.status_code == 200, response.text
    assert response.json()["line_proof_verdict"] == "passed"
    enqueue.assert_called_once()
    db_session.expire_all()
    receipt = db_session.query(SessionUploadReceipt).one()
    assert receipt.line_sync_verdict == "deadline_expired"


def test_empty_final_full_noop_proves_the_persisted_line(
    client, auth_headers, create_game_session, db_session
):
    user_id = 8111
    session_id = create_game_session(user_id=user_id)
    headers = auth_headers(user_id=user_id)
    uploaded = _post_moves(
        client,
        headers,
        session_id,
        _line("e4", "e5"),
        line_revision=0,
    )
    assert uploaded.status_code == 200, uploaded.text

    headers["X-Client-Request-ID"] = str(uuid.uuid4())
    no_op = _post_moves(
        client,
        headers,
        session_id,
        [],
        line_revision=0,
        line_sync_verdict="synchronized",
        terminal_action="game_end",
    )
    assert no_op.status_code == 200, no_op.text
    assert no_op.json()["line_proof_verdict"] == "passed"
    db_session.expire_all()
    assert db_session.query(SessionUploadReceipt).one().line_proof_verdict == "passed"


def test_generic_dialect_final_full_proof_commits_and_returns_typed_verdict(
    client, auth_headers, create_game_session, db_session, monkeypatch
):
    user_id = 8112
    session_id = create_game_session(user_id=user_id)
    headers = auth_headers(user_id=user_id)
    headers["X-Client-Request-ID"] = str(uuid.uuid4())
    monkeypatch.setattr(db_session.bind.dialect, "name", "generic")

    with patch("app.api.session.enqueue_session_evidence") as enqueue:
        response = _post_moves(
            client,
            headers,
            session_id,
            _line("e4", "e5"),
            line_revision=0,
            line_sync_verdict="synchronized",
            terminal_action="game_end",
        )

    assert response.status_code == 200, response.text
    assert response.json()["line_proof_verdict"] == "passed"
    enqueue.assert_called_once()
    db_session.expire_all()
    assert db_session.query(SessionMove).count() == 2
    assert db_session.query(SessionUploadReceipt).one().line_proof_verdict == "passed"


def test_line_sync_verdict_is_terminal_observability_only(
    client, auth_headers, create_game_session, db_session
):
    session_id = create_game_session(user_id=8109)
    response = _post_moves(
        client,
        auth_headers(user_id=8109),
        session_id,
        _line("e4"),
        line_revision=0,
        line_sync_verdict="permanent_conflict",
    )
    assert response.status_code == 400
    assert db_session.query(SessionMove).count() == 0


def test_eligible_truncation_bumps_evidence_once_after_deleting_a_row(
    client, auth_headers, create_game_session, db_session
):
    user_id = 8110
    session_id = create_game_session(user_id=user_id)
    headers = auth_headers(user_id=user_id)
    _make_unrated(db_session, session_id)
    uploaded = _post_moves(client, headers, session_id, _line("e4", "e5"))
    assert uploaded.status_code == 200, uploaded.text

    session = _session(db_session, session_id)
    session.session_mode = "drill"
    session.drill_state = "failed"
    session.drill_terminal_reason = "accuracy"
    db_session.commit()

    with (
        patch("app.api.session.bump_evidence_seq") as bump,
        patch("app.api.session.request_recompute") as recompute,
    ):
        response = client.post(
            f"/api/session/{session_id}/moves/truncate",
            json={
                "client_request_id": str(uuid.uuid4()),
                "line_revision": 0,
                "after_ply": 1,
            },
            headers=headers,
        )
    assert response.status_code == 200, response.text
    assert response.json()["deleted_move_count"] == 1
    assert response.json()["evidence_changed"] is True
    bump.assert_called_once()
    recompute.assert_called_once()
    db_session.expire_all()
    assert db_session.query(SessionMoveTruncationReceipt).one().evidence_changed is True


def test_truncation_rejects_rated_and_foreign_sessions(
    client, auth_headers, create_game_session, db_session
):
    owner = 8107
    session_id = create_game_session(user_id=owner)
    body = {
        "client_request_id": str(uuid.uuid4()),
        "line_revision": 0,
        "after_ply": 0,
    }
    rated = client.post(
        f"/api/session/{session_id}/moves/truncate",
        json=body,
        headers=auth_headers(user_id=owner),
    )
    assert rated.status_code == 409
    foreign = client.post(
        f"/api/session/{session_id}/moves/truncate",
        json=body,
        headers=auth_headers(user_id=8108),
    )
    assert foreign.status_code == 403

    session = _session(db_session, session_id)
    session.is_rated = False
    session.status = "ended"
    db_session.commit()
    ended = client.post(
        f"/api/session/{session_id}/moves/truncate",
        json={**body, "client_request_id": str(uuid.uuid4())},
        headers=auth_headers(user_id=owner),
    )
    assert ended.status_code == 409
