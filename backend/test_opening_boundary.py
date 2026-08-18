from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import chess
from sqlalchemy import update

from app.models import GameSession
from app.game_phase import Division
from app.opening_cache import current_evidence_seq
from app.opening_boundary import (
    claim_opening_boundary_shadow_terminal,
    opening_boundary_shadow_properties,
)
from app.opening_score_delta import _conditional_store_baseline


BOUNDARY_SANS = [
    "e3",
    "a5",
    "Qh5",
    "Ra6",
    "Qxa5",
    "h5",
    "Qxc7",
    "Rah6",
    "h4",
    "f6",
    "Qxd7+",
    "Kf7",
    "Qxb7",
    "Qd3",
    "Qxb8",
    "Qh7",
    "Qxc8",
]


def _line(*sans: str) -> list[dict]:
    board = chess.Board()
    rows: list[dict] = []
    for index, san in enumerate(sans):
        fen_before = board.fen()
        move = board.parse_san(san)
        board.push(move)
        rows.append(
            {
                "move_number": index // 2 + 1,
                "color": "white" if index % 2 == 0 else "black",
                "move_san": san,
                "fen_before": fen_before,
                "fen_after": board.fen(),
                "move_uci": move.uci(),
            }
        )
    return rows


def _session(db, session_id: str) -> GameSession:
    return db.get(GameSession, uuid.UUID(session_id))


def test_boundary_hint_and_exact_proof_stay_shadow_only(
    client, auth_headers, create_game_session, db_session
):
    user_id = 8301
    session_id = create_game_session(user_id=user_id)
    headers = auth_headers(user_id=user_id)
    evidence_seq_before = current_evidence_seq(db_session, user_id, "white")
    uploaded = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": _line(*BOUNDARY_SANS),
            "line_revision": 0,
            "opening_phase_protocol_version": 1,
        },
        headers=headers,
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json() == {
        "moves_inserted": 17,
        "line_revision": 0,
        "opening_phase_protocol_version": 1,
        "opening_phase_probe_ply": 17,
        "opening_phase_exhausted": False,
    }

    too_short = client.post(
        f"/api/session/{session_id}/opening-boundary",
        json={"line_revision": 0, "probe_ply": 16},
        headers=headers,
    )
    assert too_short.status_code == 200
    assert too_short.json()["state"] == "probe_not_ready"

    proven = client.post(
        f"/api/session/{session_id}/opening-boundary",
        json={"line_revision": 0, "probe_ply": 17},
        headers=headers,
    )
    assert proven.status_code == 200, proven.text
    assert proven.json() == {
        "line_revision": 0,
        "probe_ply": 17,
        "opening_middle_candidate_ply": 17,
        "exhausted": False,
        "state": "baseline_pending",
        "proof_verdict": "passed",
    }
    db_session.expire_all()
    row = _session(db_session, session_id)
    assert row.opening_middle_candidate_ply == 17
    assert row.opening_middle_ready_at is None
    assert row.opening_middle_ply is None

    # A baseline arriving after proof makes the shadow candidate measurable but
    # still cannot publish an evidence horizon in observation mode.
    row.opening_score_baseline = '{"schema_version":2,"scores":{}}'
    db_session.commit()
    ready = client.post(
        f"/api/session/{session_id}/opening-boundary",
        json={"line_revision": 0, "probe_ply": 17},
        headers=headers,
    )
    assert ready.status_code == 200
    assert ready.json()["state"] == "shadow_ready"
    db_session.expire_all()
    row = _session(db_session, session_id)
    assert row.opening_middle_ready_at is not None
    assert row.opening_middle_ply is None
    assert current_evidence_seq(db_session, user_id, "white") == evidence_seq_before


def test_takeback_clears_revision_observation_but_retains_protocol(
    client, auth_headers, create_game_session, db_session
):
    user_id = 8302
    session_id = create_game_session(user_id=user_id)
    headers = auth_headers(user_id=user_id)
    row = _session(db_session, session_id)
    row.is_rated = False
    row.opening_phase_protocol_version = 1
    row.opening_phase_probe_ply = 17
    row.opening_phase_probe_verdict = "passed"
    row.opening_middle_candidate_ply = 17
    row.opening_score_baseline = '{"schema_version":2,"scores":{}}'
    row.opening_middle_ready_at = datetime.now(timezone.utc)
    db_session.commit()

    response = client.post(
        f"/api/session/{session_id}/moves/truncate",
        json={"line_revision": 0, "after_ply": 0},
        headers=headers,
    )
    assert response.status_code == 200
    db_session.expire_all()
    row = _session(db_session, session_id)
    assert row.move_line_revision == 1
    assert row.opening_phase_protocol_version == 1
    assert row.opening_phase_probe_ply is None
    assert row.opening_phase_probe_verdict is None
    assert row.opening_middle_candidate_ply is None
    assert row.opening_middle_ready_at is None
    assert row.opening_middle_ply is None
    assert row.opening_phase_exhausted is False


def test_larger_raw_hint_still_returns_the_first_true_middle_ply(
    client, auth_headers, create_game_session
):
    user_id = 8304
    session_id = create_game_session(user_id=user_id)
    headers = auth_headers(user_id=user_id)
    line = _line(*BOUNDARY_SANS, "Kg6")
    prefix = client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": line[:17], "line_revision": 0},
        headers=headers,
    )
    assert prefix.status_code == 200
    # Only the later board is inspected by the versioned request, so 18 is the
    # scheduling hint even though replay discovers the true first middle at 17.
    later = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [line[17]],
            "line_revision": 0,
            "opening_phase_protocol_version": 1,
        },
        headers=headers,
    )
    assert later.status_code == 200, later.text
    assert later.json()["opening_phase_probe_ply"] == 18

    proven = client.post(
        f"/api/session/{session_id}/opening-boundary",
        json={"line_revision": 0, "probe_ply": 18},
        headers=headers,
    )
    assert proven.status_code == 200, proven.text
    assert proven.json()["opening_middle_candidate_ply"] == 17
    assert proven.json()["proof_verdict"] == "passed"


def test_probe_cap_is_durable_terminal_fallback(
    client, auth_headers, create_game_session, db_session
):
    user_id = 8305
    session_id = create_game_session(user_id=user_id)
    row = _session(db_session, session_id)
    row.opening_phase_protocol_version = 1
    row.opening_phase_probe_ply = 81
    db_session.commit()

    response = client.post(
        f"/api/session/{session_id}/opening-boundary",
        json={"line_revision": 0, "probe_ply": 81},
        headers=auth_headers(user_id=user_id),
    )
    assert response.status_code == 200
    assert response.json()["state"] == "capped"
    assert response.json()["proof_verdict"] == "capped"
    db_session.expire_all()
    row = _session(db_session, session_id)
    assert row.opening_middle_candidate_ply is None
    assert row.opening_middle_ply is None


def test_simultaneous_middle_end_becomes_exhausted_without_candidate(
    client, auth_headers, create_game_session, db_session
):
    user_id = 8308
    session_id = create_game_session(user_id=user_id)
    headers = auth_headers(user_id=user_id)
    uploaded = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": _line(*BOUNDARY_SANS),
            "line_revision": 0,
            "opening_phase_protocol_version": 1,
        },
        headers=headers,
    )
    assert uploaded.status_code == 200

    with patch(
        "app.api.session.divide",
        return_value=Division(middle=None, end=17, plies=18),
    ):
        response = client.post(
            f"/api/session/{session_id}/opening-boundary",
            json={"line_revision": 0, "probe_ply": 17},
            headers=headers,
        )
    assert response.status_code == 200
    assert response.json() == {
        "line_revision": 0,
        "exhausted": True,
        "state": "exhausted",
        "proof_verdict": "exhausted",
    }
    db_session.expire_all()
    row = _session(db_session, session_id)
    assert row.opening_phase_probe_ply is None
    assert row.opening_middle_candidate_ply is None
    assert row.opening_middle_ready_at is None
    assert row.opening_middle_ply is None
    assert row.opening_phase_exhausted is True


def test_unknown_boundary_protocol_is_rejected_before_writes(
    client, auth_headers, create_game_session, db_session
):
    user_id = 8303
    session_id = create_game_session(user_id=user_id)
    response = client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": [], "opening_phase_protocol_version": 2},
        headers=auth_headers(user_id=user_id),
    )
    assert response.status_code == 422
    db_session.expire_all()
    assert _session(db_session, session_id).opening_phase_protocol_version is None


def test_baseline_linearization_stamps_shadow_ready_in_same_transaction(
    create_game_session, db_session
):
    session_id = create_game_session(user_id=8307)
    row = _session(db_session, session_id)
    assert row.baseline_watermark_seq is not None
    row.opening_phase_protocol_version = 1
    row.opening_phase_probe_ply = 17
    row.opening_phase_probe_verdict = "passed"
    row.opening_middle_candidate_ply = 17
    db_session.commit()

    baseline = '{"schema_version":2,"scores":{}}'
    assert _conditional_store_baseline(db_session, row, baseline) is True
    assert row.opening_score_baseline == baseline
    assert row.opening_middle_ready_at is not None
    assert row.opening_middle_ply is None
    db_session.commit()


def test_baseline_linearization_reads_candidate_from_db_when_worker_row_is_stale(
    create_game_session, db_session
):
    session_id = create_game_session(user_id=8309)
    row = _session(db_session, session_id)
    assert row.baseline_watermark_seq is not None
    assert row.opening_middle_candidate_ply is None

    # Model the boundary transaction committing while the worker computes its
    # baseline. Disable identity-map synchronization so ``row`` retains exactly
    # the stale no-candidate snapshot that the real worker would hold.
    db_session.execute(
        update(GameSession)
        .where(GameSession.id == row.id)
        .values(
            opening_phase_protocol_version=1,
            opening_phase_probe_ply=17,
            opening_phase_probe_verdict="passed",
            opening_middle_candidate_ply=17,
        )
        .execution_options(synchronize_session=False)
    )
    assert row.opening_middle_candidate_ply is None

    baseline = '{"schema_version":2,"scores":{}}'
    assert _conditional_store_baseline(db_session, row, baseline) is True
    assert row.opening_score_baseline == baseline
    assert row.opening_middle_ready_at is not None

    db_session.expire(row)
    assert row.opening_middle_candidate_ply == 17
    assert row.opening_middle_ready_at is not None
    assert row.opening_middle_ply is None
    terminal_at = row.opening_middle_ready_at + timedelta(seconds=1)
    properties = opening_boundary_shadow_properties(
        row,
        terminal_trigger="game_end",
        terminal_at=terminal_at,
    )
    assert properties["baseline_ready_at_transition"] is True
    assert properties["would_have_published"] is True
    assert properties["reason"] == "would_publish"


def test_unknown_terminal_branch_discards_boundary_observation(
    client, auth_headers, create_game_session, db_session
):
    user_id = 8306
    session_id = create_game_session(user_id=user_id)
    row = _session(db_session, session_id)
    row.is_rated = False
    row.move_line_revision = 1
    row.opening_phase_protocol_version = 1
    row.opening_phase_probe_ply = 17
    row.opening_phase_probe_verdict = "passed"
    row.opening_middle_candidate_ply = 17
    row.opening_score_baseline = '{"schema_version":2,"scores":{}}'
    row.opening_middle_ready_at = datetime.now(timezone.utc)
    db_session.commit()

    ended = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "draw",
            "pgn": "",
            "is_rated": False,
            "line_revision": None,
            "discard_move_evidence": True,
        },
        headers=auth_headers(user_id=user_id),
    )
    assert ended.status_code == 200, ended.text
    db_session.expire_all()
    row = _session(db_session, session_id)
    assert row.move_line_revision == 2
    assert row.opening_phase_protocol_version == 1
    assert row.opening_phase_probe_ply is None
    assert row.opening_middle_candidate_ply is None
    assert row.opening_middle_ready_at is None
    assert row.opening_middle_ply is None


def test_shadow_terminal_projection_is_closed_and_content_free():
    ready_at = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
    session = GameSession(
        id=uuid.uuid4(),
        user_id=99,
        started_at=ready_at - timedelta(minutes=5),
        status="active",
        engine_elo=1500,
        player_color="white",
        session_mode="normal",
        opening_score_baseline="{}",
        move_line_revision=0,
        opening_phase_protocol_version=1,
        opening_phase_probe_ply=17,
        opening_phase_probe_verdict="passed",
        opening_middle_candidate_ply=17,
        opening_middle_ready_at=ready_at,
        opening_phase_exhausted=False,
    )
    properties = opening_boundary_shadow_properties(
        session,
        terminal_trigger="game_end",
        terminal_at=ready_at + timedelta(seconds=12.345),
    )
    assert properties == {
        "protocol_version": 1,
        "session_mode": "normal",
        "terminal_trigger": "game_end",
        "raw_candidate_seen": True,
        "proof_verdict": "passed",
        "baseline_ready_at_transition": True,
        "would_have_published": True,
        "reason": "would_publish",
        "line_revision_zero": True,
        "ready_to_terminal_lead_ms": 12345,
    }
    assert not {"session_id", "user_id", "fen", "score", "grade"} & properties.keys()
    assert claim_opening_boundary_shadow_terminal(
        session,
        terminal_at=ready_at + timedelta(seconds=12.345),
    )
    assert not claim_opening_boundary_shadow_terminal(
        session,
        terminal_at=ready_at + timedelta(minutes=1),
    )
