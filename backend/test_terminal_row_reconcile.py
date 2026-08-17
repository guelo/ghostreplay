"""Terminal row reconcile (g-short-move-rows).

An ended session must not silently persist fewer canonical move rows than its
terminal PGN describes. ``POST /api/game/end`` derives verified missing tail
or interior rows from the client PGN inside the terminal transaction; the
historical backfill deliberately replays the narrower prefix-only decision over
the row-short cohort, dry-run first.

Fail-closed branches pinned here: unparseable PGN, a stored prefix that
disagrees with the PGN mainline (g-discard-branch-rows shape), surplus rows
over a truncated PGN (the measured g-i6st non-defect), and a PGN over the
derivation ceilings (an unbounded client PGN must not expand into unbounded
terminal INSERTs). The backfill's aggregate-only privacy contract — no session
UUIDs or chess.pgn parse errors on the log stream — is pinned here too.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import chess
import chess.pgn
import pytest

from app.models import GameSession, SessionMove, SessionUploadReceipt, User
from app.short_move_row_backfill import run_backfill
from app.terminal_row_reconcile import (
    MAX_DERIVABLE_PLIES,
    MAX_TERMINAL_PGN_BYTES,
    OUTCOME_COMPLETE,
    OUTCOME_DERIVED,
    OUTCOME_OVER_CEILING,
    OUTCOME_PGN_UNKNOWN,
    OUTCOME_PREFIX_MISMATCH,
    reconcile_terminal_move_rows,
)
from conftest import TestingSessionLocal


FOOLS_MATE = ["f3", "e5", "g4", "Qh4#"]


def _replayed(sans):
    """Real per-ply records (SAN/FEN chain) for a game from the start position."""
    board = chess.Board()
    plies = []
    for index, san in enumerate(sans):
        fen_before = board.fen()
        move = board.parse_san(san)
        canonical_san = board.san(move)
        board.push(move)
        plies.append(
            {
                "move_number": index // 2 + 1,
                "color": "white" if index % 2 == 0 else "black",
                "move_san": canonical_san,
                "fen_before": fen_before,
                "fen_after": board.fen(),
            }
        )
    game = chess.pgn.Game.from_board(board)
    pgn = game.accept(
        chess.pgn.StringExporter(headers=False, variations=False, comments=False)
    ).strip()
    return plies, pgn


def _move_payload(ply, eval_cp=20):
    return {
        "move_number": ply["move_number"],
        "color": ply["color"],
        "move_san": ply["move_san"],
        "fen_before": ply["fen_before"],
        "fen_after": ply["fen_after"],
        "eval_cp": eval_cp,
        "eval_mate": None,
    }


def _upload(client, auth_headers, session_id, plies, **kwargs):
    response = client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": [_move_payload(p, **kwargs) for p in plies]},
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 200
    return response


def _end(client, auth_headers, session_id, pgn, result="checkmate_loss"):
    return client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": result,
            "pgn": pgn,
            "is_rated": False,
        },
        headers=auth_headers(user_id=123),
    )


def _ordered_rows(db, session_id):
    return (
        db.query(SessionMove)
        .filter(SessionMove.session_id == uuid.UUID(str(session_id)))
        .order_by(SessionMove.move_number, SessionMove.color.desc())
        .all()
    )


@pytest.fixture
def game_ended_props(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.api.game.capture", lambda *a, **k: calls.append(a)
    )
    def _props():
        matches = [a for a in calls if len(a) >= 3 and a[1] == "game_ended"]
        assert len(matches) == 1
        return matches[0][2]
    return _props


def test_end_with_short_prefix_derives_the_pgn_tail(
    client, auth_headers, create_game_session, db_session, game_ended_props
):
    plies, pgn = _replayed(FOOLS_MATE)
    session_id = create_game_session(user_id=123, player_color="white")
    _upload(client, auth_headers, session_id, plies[:2])

    assert _end(client, auth_headers, session_id, pgn).status_code == 200

    rows = _ordered_rows(db_session, session_id)
    assert [(r.move_number, r.color, r.move_san) for r in rows] == [
        (p["move_number"], p["color"], p["move_san"]) for p in plies
    ]
    # The derived tail carries the PGN's own record and nothing guessed.
    for row, ply in zip(rows[2:], plies[2:]):
        assert row.fen_before == ply["fen_before"]
        assert row.fen_after == ply["fen_after"]
        assert row.eval_cp is None and row.eval_mate is None
        assert row.classification is None
        assert row.segment == "normal"
    # Strict-null accuracy now refuses on the eval gap, not on missing rows,
    # and the terminal write never manufactures a final-upload receipt.
    session = db_session.query(GameSession).filter(
        GameSession.id == uuid.UUID(session_id)
    ).one()
    assert session.player_accuracy is None
    assert session.player_accuracy_algo_version == 1
    assert db_session.query(SessionUploadReceipt).count() == 0
    # The durable marker records the derivation; the analytics props below
    # carry the same verdict but are best-effort only.
    assert session.derived_tail_rows == 2
    assert session.terminal_line_reconciled is True

    props = game_ended_props()
    assert props["row_reconcile_outcome"] == OUTCOME_DERIVED
    assert props["stored_move_rows"] == 2
    assert props["derived_tail_rows"] == 2


def test_end_with_verified_interior_holes_derives_the_full_grid(
    client, auth_headers, create_game_session, db_session, game_ended_props
):
    plies, pgn = _replayed(FOOLS_MATE)
    session_id = create_game_session(user_id=123, player_color="white")
    # The pre-2026-06-25 writer shape: resolved-only incremental uploads kept
    # absolute coordinates 1w and 2w while unresolved 1b (and the terminal 2b)
    # never reached /moves. The terminal PGN still describes all four plies.
    _upload(client, auth_headers, session_id, [plies[0], plies[2]])

    assert _end(client, auth_headers, session_id, pgn).status_code == 200

    rows = _ordered_rows(db_session, session_id)
    assert [(r.move_number, r.color) for r in rows] == [
        (1, "white"),
        (1, "black"),
        (2, "white"),
        (2, "black"),
    ]
    # Existing evaluations stay attached to their declared canonical plies;
    # missing coordinates are represented honestly, with no invented eval.
    assert [row.eval_cp for row in rows] == [20, None, 20, None]
    assert [(r.move_san, r.fen_before, r.fen_after) for r in rows] == [
        (p["move_san"], p["fen_before"], p["fen_after"]) for p in plies
    ]
    session = db_session.query(GameSession).filter(
        GameSession.id == uuid.UUID(session_id)
    ).one()
    assert session.player_accuracy is None
    assert session.player_accuracy_algo_version == 1
    # Compatibility column name: the durable count covers all rows derived by
    # terminal reconciliation, including these two non-tail coordinates.
    assert session.derived_tail_rows == 2
    props = game_ended_props()
    assert props["row_reconcile_outcome"] == OUTCOME_DERIVED
    assert props["stored_move_rows"] == 2
    assert props["derived_tail_rows"] == 2


def test_sparse_policy_is_live_only_not_historical_backfill(
    client, auth_headers, create_game_session, db_session
):
    plies, pgn = _replayed(FOOLS_MATE)
    session_id = create_game_session(user_id=123, player_color="white")
    _upload(client, auth_headers, session_id, [plies[0], plies[2]])
    session = db_session.query(GameSession).filter(
        GameSession.id == uuid.UUID(session_id)
    ).one()
    session.pgn = pgn

    historical = reconcile_terminal_move_rows(db_session, session, stage=False)
    serving = reconcile_terminal_move_rows(
        db_session,
        session,
        stage=False,
        allow_sparse=True,
    )

    # Prefix-only remains the default used by short_move_row_backfill, so this
    # bead's historical ten cannot be absorbed by that repair. Only an active
    # /game/end opts into sparse completion.
    assert historical.outcome == OUTCOME_PREFIX_MISMATCH
    assert historical.derived_rows == 0
    assert serving.outcome == OUTCOME_DERIVED
    assert serving.derived_rows == 2
    assert len(_ordered_rows(db_session, session_id)) == 2


def test_end_with_no_rows_derives_the_full_game(
    client, auth_headers, create_game_session, db_session
):
    plies, pgn = _replayed(FOOLS_MATE)
    session_id = create_game_session(user_id=123, player_color="white")

    assert _end(client, auth_headers, session_id, pgn).status_code == 200

    rows = _ordered_rows(db_session, session_id)
    assert [(r.move_number, r.color, r.move_san) for r in rows] == [
        (p["move_number"], p["color"], p["move_san"]) for p in plies
    ]


def test_end_with_complete_rows_derives_nothing(
    client,
    auth_headers,
    create_game_session,
    db_session,
    game_ended_props,
    monkeypatch,
):
    plies, pgn = _replayed(FOOLS_MATE)
    session_id = create_game_session(user_id=123, player_color="white")
    _upload(client, auth_headers, session_id, plies)
    # An exact canonical key set needs no derivation, so it bypasses both the
    # expansion ceiling and the costly SAN/FEN identity replay. Identity-only
    # divergence at intact coordinates belongs to g-discard-branch-rows.
    monkeypatch.setattr(
        "app.terminal_row_reconcile.MAX_DERIVABLE_PLIES",
        len(plies) - 1,
    )
    monkeypatch.setattr(
        "app.terminal_row_reconcile.stored_subset_matches",
        lambda *_: pytest.fail("complete grids must not replay row identity"),
    )

    assert _end(client, auth_headers, session_id, pgn).status_code == 200

    rows = _ordered_rows(db_session, session_id)
    assert len(rows) == len(plies)
    assert all(row.eval_cp == 20 for row in rows)
    session = db_session.query(GameSession).filter(
        GameSession.id == uuid.UUID(session_id)
    ).one()
    assert session.derived_tail_rows is None
    props = game_ended_props()
    assert props["row_reconcile_outcome"] == OUTCOME_COMPLETE
    assert props["derived_tail_rows"] == 0


def test_end_with_surplus_rows_over_truncated_pgn_is_untouched(
    client, auth_headers, create_game_session, db_session, game_ended_props
):
    # The measured g-i6st shape: rows are the fuller record, the PGN lags them.
    plies, _ = _replayed(FOOLS_MATE)
    _, short_pgn = _replayed(FOOLS_MATE[:2])
    session_id = create_game_session(user_id=123, player_color="white")
    _upload(client, auth_headers, session_id, plies)

    assert _end(client, auth_headers, session_id, short_pgn).status_code == 200

    assert len(_ordered_rows(db_session, session_id)) == len(plies)
    assert game_ended_props()["row_reconcile_outcome"] == OUTCOME_COMPLETE


def test_end_with_diverging_prefix_fails_closed(
    client, auth_headers, create_game_session, db_session, game_ended_props
):
    # Stored ply 2 is a move the PGN's game never played (g-discard-branch-rows
    # shape): identity fails, so nothing is derived against a contradicted PGN.
    diverged, _ = _replayed(["f3", "e6"])
    _, pgn = _replayed(FOOLS_MATE)
    session_id = create_game_session(user_id=123, player_color="white")
    _upload(client, auth_headers, session_id, diverged)

    assert _end(client, auth_headers, session_id, pgn).status_code == 200

    rows = _ordered_rows(db_session, session_id)
    assert [(r.move_san) for r in rows] == ["f3", "e6"]
    props = game_ended_props()
    assert props["row_reconcile_outcome"] == OUTCOME_PREFIX_MISMATCH
    assert props["derived_tail_rows"] == 0


def test_end_with_sparse_wrong_identity_fails_closed(
    client, auth_headers, create_game_session, db_session, game_ended_props
):
    plies, pgn = _replayed(FOOLS_MATE)
    diverged, _ = _replayed(["f3", "e5", "e4"])
    session_id = create_game_session(user_id=123, player_color="white")
    # Both stored keys are real PGN coordinates, but the row declared at 2w is
    # e4 rather than the terminal PGN's g4. Sparse mode must bind identity at
    # the declared coordinate before deriving either absent row.
    _upload(client, auth_headers, session_id, [plies[0], diverged[2]])

    assert _end(client, auth_headers, session_id, pgn).status_code == 200

    rows = _ordered_rows(db_session, session_id)
    assert [(r.move_number, r.color, r.move_san) for r in rows] == [
        (1, "white", "f3"),
        (2, "white", "e4"),
    ]
    session = db_session.query(GameSession).filter(
        GameSession.id == uuid.UUID(session_id)
    ).one()
    assert session.derived_tail_rows is None
    props = game_ended_props()
    assert props["row_reconcile_outcome"] == OUTCOME_PREFIX_MISMATCH
    assert props["derived_tail_rows"] == 0


def test_end_with_missing_plus_extra_exact_count_grid_fails_closed(
    client, auth_headers, create_game_session, db_session, game_ended_props
):
    plies, pgn = _replayed(FOOLS_MATE)
    session_id = create_game_session(user_id=123, player_color="white")
    extra = {**plies[3], "move_number": 3, "color": "white"}
    # Four stored rows and four PGN plies, but 2b is absent and an impossible
    # 3w coordinate takes its place. Count equality must not bypass sparse-grid
    # verification or be reported as complete.
    _upload(
        client,
        auth_headers,
        session_id,
        [plies[0], plies[1], plies[2], extra],
    )

    assert _end(client, auth_headers, session_id, pgn).status_code == 200

    rows = _ordered_rows(db_session, session_id)
    assert [(r.move_number, r.color) for r in rows] == [
        (1, "white"),
        (1, "black"),
        (2, "white"),
        (3, "white"),
    ]
    props = game_ended_props()
    assert props["row_reconcile_outcome"] == OUTCOME_PREFIX_MISMATCH
    assert props["derived_tail_rows"] == 0


def test_over_ceiling_missing_plus_extra_skips_identity_verification(
    client,
    auth_headers,
    create_game_session,
    db_session,
    game_ended_props,
    monkeypatch,
):
    plies, pgn = _replayed(FOOLS_MATE)
    session_id = create_game_session(user_id=123, player_color="white")
    extra = {**plies[3], "move_number": 3, "color": "white"}
    _upload(
        client,
        auth_headers,
        session_id,
        [plies[0], plies[1], plies[2], extra],
    )
    # Simulate an exact-count divergent grid above the real 600-ply ceiling.
    # Its missing key prevents the complete fast path, but the ceiling must
    # refuse it before any SAN/FEN identity replay.
    monkeypatch.setattr(
        "app.terminal_row_reconcile.MAX_DERIVABLE_PLIES",
        len(plies) - 1,
    )
    monkeypatch.setattr(
        "app.terminal_row_reconcile.stored_subset_matches",
        lambda *_: pytest.fail("over-ceiling grids must not replay row identity"),
    )

    assert _end(client, auth_headers, session_id, pgn).status_code == 200

    coordinates = [
        (row.move_number, row.color)
        for row in _ordered_rows(db_session, session_id)
    ]
    assert coordinates == [
        (1, "white"),
        (1, "black"),
        (2, "white"),
        (3, "white"),
    ]
    props = game_ended_props()
    assert props["row_reconcile_outcome"] == OUTCOME_OVER_CEILING
    assert props["derived_tail_rows"] == 0


def test_end_with_malformed_legacy_rows_fails_closed_not_500(
    client, auth_headers, create_game_session, db_session, game_ended_props
):
    # Legacy rows can carry junk FENs; identity verification must refuse them
    # without breaking the terminal write.
    _, pgn = _replayed(FOOLS_MATE)
    session_id = create_game_session(user_id=123, player_color="white")
    db_session.add(
        SessionMove(
            session_id=uuid.UUID(session_id),
            move_number=1,
            color="white",
            move_san="f3",
            fen_after="not-a-fen",
        )
    )
    db_session.commit()

    assert _end(client, auth_headers, session_id, pgn).status_code == 200

    assert len(_ordered_rows(db_session, session_id)) == 1
    props = game_ended_props()
    assert props["row_reconcile_outcome"] == OUTCOME_PREFIX_MISMATCH
    assert props["derived_tail_rows"] == 0


def test_end_with_unparseable_pgn_derives_nothing(
    client, auth_headers, create_game_session, db_session, game_ended_props
):
    plies, _ = _replayed(FOOLS_MATE)
    session_id = create_game_session(user_id=123, player_color="white")
    _upload(client, auth_headers, session_id, plies[:1])

    assert _end(client, auth_headers, session_id, "not a pgn").status_code == 200

    assert len(_ordered_rows(db_session, session_id)) == 1
    assert game_ended_props()["row_reconcile_outcome"] == OUTCOME_PGN_UNKNOWN


def test_late_final_upload_overwrites_derived_rows_and_heals_accuracy(
    client, auth_headers, create_game_session, db_session
):
    plies, pgn = _replayed(FOOLS_MATE)
    session_id = create_game_session(user_id=123, player_color="white")
    _upload(client, auth_headers, session_id, plies[:2])
    assert _end(client, auth_headers, session_id, pgn).status_code == 200

    # The delayed final upload lands after the terminal write; ON CONFLICT
    # replaces the derived placeholders with the client's richer rows.
    _upload(client, auth_headers, session_id, plies)

    db_session.expire_all()
    rows = _ordered_rows(db_session, session_id)
    assert len(rows) == len(plies)
    assert all(row.eval_cp == 20 for row in rows)
    session = db_session.query(GameSession).filter(
        GameSession.id == uuid.UUID(session_id)
    ).one()
    assert session.player_accuracy is not None
    # The marker records that derivation FIRED at the terminal write, not the
    # provenance of the current rows — the overwrite does not clear it.
    assert session.derived_tail_rows == 2


def test_end_with_over_ceiling_ply_count_fails_closed(
    client, auth_headers, create_game_session, db_session, game_ended_props
):
    # A legal knight shuffle exceeding MAX_DERIVABLE_PLIES: an unbounded client
    # PGN must not expand into unbounded INSERTs under the terminal lock. The
    # session keeps its (empty) rows and strict-NULL accuracy.
    shuffle = ["Nf3", "Nf6", "Ng1", "Ng8"] * (MAX_DERIVABLE_PLIES // 4 + 1)
    _, pgn = _replayed(shuffle)
    session_id = create_game_session(user_id=123, player_color="white")

    assert _end(client, auth_headers, session_id, pgn, result="abandon").status_code == 200

    assert _ordered_rows(db_session, session_id) == []
    session = db_session.query(GameSession).filter(
        GameSession.id == uuid.UUID(session_id)
    ).one()
    assert session.player_accuracy is None
    assert session.derived_tail_rows is None
    props = game_ended_props()
    assert props["row_reconcile_outcome"] == OUTCOME_OVER_CEILING
    assert props["derived_tail_rows"] == 0
    # The ply ceiling had to parse once to learn the count; that one parse's
    # verdict is what analytics reports — never a reparse.
    assert props["ply_count"] == len(shuffle)


def test_end_with_oversized_pgn_never_parses_it(
    client, auth_headers, create_game_session, db_session, game_ended_props,
    monkeypatch,
):
    # The size gate's refusal must be total across the terminal path: the
    # reconcile, the accuracy recompute, and the analytics props all reuse the
    # one ReconcileResult, so a refused PGN costs ZERO parses — otherwise the
    # gate saves the INSERTs but still pays unbounded parse work twice.
    parses = []
    real_read_game = chess.pgn.read_game
    monkeypatch.setattr(
        chess.pgn,
        "read_game",
        lambda *args, **kwargs: parses.append(1) or real_read_game(*args, **kwargs),
    )
    _, pgn = _replayed(FOOLS_MATE)
    padded = pgn + " " * (MAX_TERMINAL_PGN_BYTES + 1)
    session_id = create_game_session(user_id=123, player_color="white")

    assert _end(client, auth_headers, session_id, padded).status_code == 200

    assert parses == []
    assert _ordered_rows(db_session, session_id) == []
    props = game_ended_props()
    assert props["row_reconcile_outcome"] == OUTCOME_OVER_CEILING
    assert props["ply_count"] is None


def test_end_with_short_prefix_parses_the_pgn_exactly_once(
    client, auth_headers, create_game_session, db_session, monkeypatch
):
    # Successful derivation must not pay a second parse either: the expected
    # count, prefix verification, derivation records, accuracy recompute, and
    # analytics all come from the reconcile's one replay_pgn_mainline call.
    plies, pgn = _replayed(FOOLS_MATE)
    session_id = create_game_session(user_id=123, player_color="white")
    _upload(client, auth_headers, session_id, plies[:2])

    parses = []
    real_read_game = chess.pgn.read_game
    monkeypatch.setattr(
        chess.pgn,
        "read_game",
        lambda *args, **kwargs: parses.append(1) or real_read_game(*args, **kwargs),
    )
    assert _end(client, auth_headers, session_id, pgn).status_code == 200

    assert len(parses) == 1
    assert len(_ordered_rows(db_session, session_id)) == len(plies)


def _ghost_session(pgn):
    """A detached session object: the size gate must refuse before any parse,
    so the (nonexistent) database row is never needed."""
    return GameSession(
        id=uuid.uuid4(),
        user_id=123,
        started_at=datetime.now(timezone.utc),
        status="ended",
        result="abandon",
        engine_elo=1500,
        player_color="white",
        session_mode="normal",
        is_rated=False,
        pgn=pgn,
    )


@pytest.mark.parametrize(
    "pgn",
    [
        # Over the ceiling in code points alone: the O(1) pre-gate rejects
        # without measuring bytes (code points never exceed UTF-8 bytes).
        "\ud800" * (MAX_TERMINAL_PGN_BYTES + 1),
        # Passes the code-point pre-gate but exceeds the ceiling in strict
        # UTF-8 bytes: 20k two-byte characters is ~40 KiB. A character count
        # alone would wave this through.
        "é" * 20_000,
        # Short but not encodable UTF-8 at all: an unpaired surrogate smuggled
        # in via a JSON escape. encode(errors="ignore") would silently drop it
        # from a byte count; strict sizing refuses outright.
        "1. e4 e5 \ud800",
    ],
)
def test_size_gate_enforces_strict_utf8_bytes(db_session, pgn):
    result = reconcile_terminal_move_rows(db_session, _ghost_session(pgn))

    assert result.outcome == OUTCOME_OVER_CEILING
    assert result.expected_plies is None
    assert result.derived_rows == 0


def test_converted_drill_derived_rows_get_segmented(db_session):
    plies, pgn = _replayed(FOOLS_MATE)
    db_session.add(User(id=321, username="drill-owner", is_anonymous=True))
    now = datetime.now(timezone.utc)
    session = GameSession(
        id=uuid.uuid4(),
        user_id=321,
        started_at=now,
        normal_started_at=now,
        converted_at=now,
        status="ended",
        result="checkmate_loss",
        engine_elo=1500,
        player_color="white",
        session_mode="drill",
        drill_state="converted",
        rated_start_ply=2,
        is_rated=True,
        pgn=pgn,
    )
    db_session.add(session)
    db_session.commit()

    result = reconcile_terminal_move_rows(db_session, session)
    db_session.commit()

    assert result.outcome == OUTCOME_DERIVED
    rows = _ordered_rows(db_session, session.id)
    assert [row.segment for row in rows] == ["drill", "drill", "normal", "normal"]


def _seed_ended_session(db, *, plies, pgn, stored, user_id=123):
    session_id = uuid.uuid4()
    db.add(
        GameSession(
            id=session_id,
            user_id=user_id,
            started_at=datetime.now(timezone.utc),
            status="ended",
            result="checkmate_loss",
            engine_elo=1500,
            player_color="white",
            session_mode="normal",
            is_rated=True,
            pgn=pgn,
            player_accuracy=None,
            player_accuracy_algo_version=1,
        )
    )
    for ply in stored:
        db.add(
            SessionMove(
                session_id=session_id,
                move_number=ply["move_number"],
                color=ply["color"],
                move_san=ply["move_san"],
                fen_before=ply["fen_before"],
                fen_after=ply["fen_after"],
                eval_cp=20,
            )
        )
    db.commit()
    return session_id


def test_backfill_dry_run_classifies_then_apply_repairs_idempotently(db_session):
    db_session.add(User(id=123, username="short-row-owner", is_anonymous=True))
    db_session.commit()
    plies, pgn = _replayed(FOOLS_MATE)
    diverged, _ = _replayed(["f3", "e6"])
    short_id = _seed_ended_session(db_session, plies=plies, pgn=pgn, stored=plies[:2])
    _seed_ended_session(db_session, plies=plies, pgn=pgn, stored=diverged)
    _seed_ended_session(db_session, plies=plies, pgn=pgn, stored=plies)

    dry = run_backfill(TestingSessionLocal, apply=False)
    assert dry.plan.sessions_rows_short == 2
    assert dry.plan.missing_tail_rows == 4
    assert dry.plan.verified_sessions == 1
    assert dry.plan.unverifiable_sessions == 1
    assert dry.outcome.rows_inserted_actual == 0
    assert (
        db_session.query(SessionMove).count() == 2 + 2 + len(plies)
    ), "dry run must not write"

    applied = run_backfill(TestingSessionLocal, apply=True)
    assert applied.outcome.sessions_attempted == 1
    assert applied.outcome.rows_inserted_actual == 2
    assert applied.outcome.evidence_sessions_bumped == 1
    # Derived rows carry NULL evals, so the session stays strict-NULL until an
    # eval repair with its own evidence rules runs.
    assert applied.outcome.sessions_still_none == 1
    db_session.expire_all()
    repaired = _ordered_rows(db_session, short_id)
    assert [(r.move_san, r.eval_cp) for r in repaired] == [
        ("f3", 20),
        ("e5", 20),
        ("g4", None),
        ("Qh4#", None),
    ]
    # The repair stamps the same durable marker as the forward path, so the 47
    # repaired sessions stay distinguishable from organically complete ones.
    marked = db_session.query(GameSession).filter(
        GameSession.id == short_id
    ).one()
    assert marked.derived_tail_rows == 2

    # Idempotent: the repaired session is complete; the contradicted one stays
    # fail-closed residue and is never guessed at.
    again = run_backfill(TestingSessionLocal, apply=True)
    assert again.plan.sessions_rows_short == 1
    assert again.plan.verified_sessions == 0
    assert again.plan.unverifiable_sessions == 1
    assert again.outcome.rows_inserted_actual == 0


def test_backfill_logs_no_session_or_move_data(db_session, caplog):
    # Aggregate-only privacy contract: planning over a malformed PGN and
    # applying a repair must put NOTHING identifying on the log stream. The
    # leak vectors are real — chess.pgn logs the offending SAN, board FEN, and
    # PGN headers for illegal movetext, and the reconcile/accuracy helpers log
    # session UUIDs at WARNING — so the backfill disables logging around both
    # phases.
    db_session.add(User(id=123, username="short-row-owner", is_anonymous=True))
    db_session.commit()
    plies, pgn = _replayed(FOOLS_MATE)
    short_id = _seed_ended_session(db_session, plies=plies, pgn=pgn, stored=plies[:2])
    _seed_ended_session(
        db_session, plies=plies, pgn="1. e4 e4 1-0", stored=plies[:1]
    )

    with caplog.at_level(logging.INFO):
        run = run_backfill(TestingSessionLocal, apply=True)

    assert run.outcome.rows_inserted_actual == 2
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert str(short_id) not in logged
    assert "while parsing" not in logged
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
