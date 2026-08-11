"""Cache-only aggregate accuracy reads (g-b-cache-reads).

Release B switched ``/api/stats/summary`` and ``/api/history`` onto
``game_sessions.player_accuracy``, read through the single seam
``app.accuracy.accuracy_for_sessions``. These tests pin:

* both consumers reach accuracy ONLY through that seam, and history reaches it for
  EVERY returned session — the zero-``session_moves`` branch included, which is
  exactly where a map wrongly scoped to the grouped move rows would go unnoticed;
* the served aggregates ARE the cached values: overall and per-color means, at the
  API's rounding, over a fixture whose cached column is set to known numbers;
* the surrounding semantics survive the switch unchanged — ended-only, visible-only,
  the time window, the color split, and the "ended games that SCORED" denominator;
* neither consumer parses a PGN or runs the accuracy algorithm any more, including
  when a visible row's cached accuracy is NULL, and stats issues no ordered
  ``session_moves`` evaluation query at all;
* history narrowed that query's SELECT list but KEPT its ORDER BY: moves inserted in
  scrambled physical order still derive the play-order opening name;
* ``/api/session/{id}/analysis`` still computes live from its own rows, so the
  ply-coordinate guard survives the switch on the one endpoint that computes.

The guard's own endpoint coverage lives in ``test_accuracy_rows_guard.py``, which
stays green across the switch by serving the NULL the guarded hook stamped.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import chess
import pytest
from sqlalchemy import event

from app.accuracy import ACCURACY_ALGO_VERSION
from app.models import GameSession, SessionMove
from app.opening_roots import OpeningRoot, OpeningRoots
from conftest import engine

PGN_TWO_PLY = "1. e4 e5"

# Two plies with real evals, so any live recomputation would produce a REAL number
# and could never be mistaken for the cache's own value.
SCOREABLE_MOVES = [
    {"move_number": 1, "color": "white", "eval_cp": 20},
    {"move_number": 1, "color": "black", "eval_cp": -10},
]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------
def _insert_session(db, **kwargs) -> GameSession:
    """Insert a session row directly with an EXPLICIT cached accuracy.

    Bypassing the write hooks is the point: the cached column is set to a value the
    live algorithm would not produce from these rows, so anything the endpoint
    reports can only have come from the cache.
    """
    defaults = dict(
        id=uuid.uuid4(),
        user_id=123,
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        status="ended",
        result="checkmate_win",
        engine_elo=1500,
        player_color="white",
        session_mode="normal",
        is_rated=True,
        pgn=PGN_TWO_PLY,
        player_accuracy=None,
        player_accuracy_algo_version=ACCURACY_ALGO_VERSION,
    )
    defaults.update(kwargs)
    session = GameSession(**defaults)
    db.add(session)
    db.commit()
    return session


def _add_moves(db, session_id, moves) -> None:
    for m in moves:
        db.add(
            SessionMove(
                session_id=uuid.UUID(str(session_id)),
                move_number=m["move_number"],
                color=m["color"],
                move_san=m.get("move_san", "e4"),
                fen_after=m.get("fen_after", "f"),
                eval_cp=m.get("eval_cp"),
                eval_mate=m.get("eval_mate"),
                eval_delta=m.get("eval_delta"),
                classification=m.get("classification"),
            )
        )
    db.commit()


def _summary(client, auth_headers, user_id=123, **params) -> dict:
    return client.get(
        "/api/stats/summary", params=params, headers=auth_headers(user_id=user_id)
    ).json()


def _history(client, auth_headers, user_id=123) -> list[dict]:
    return client.get("/api/history", headers=auth_headers(user_id=user_id)).json()["games"]


# ---------------------------------------------------------------------------
# 1. The served aggregates ARE the cached values.
# ---------------------------------------------------------------------------
def test_summary_means_are_the_cached_values(client, auth_headers, db_session):
    """Overall and per-color means, at the API's one-decimal rounding.

    The numbers are chosen so the three means differ from each other and none is a
    round integer, so a mean taken over the wrong population would not coincide.
    """
    for color, accuracy in (
        ("white", 90),
        ("white", 81),
        ("black", 60),
        ("black", 55),
    ):
        _insert_session(db_session, player_color=color, player_accuracy=accuracy)

    data = _summary(client, auth_headers)

    assert data["moves"]["accuracy_pct"] == 71.5           # (90+81+60+55)/4
    assert data["colors"]["white"]["accuracy_pct"] == 85.5  # (90+81)/2
    assert data["colors"]["black"]["accuracy_pct"] == 57.5  # (60+55)/2


def test_history_summary_accuracy_is_the_cached_value(client, auth_headers, db_session):
    session = _insert_session(db_session, player_accuracy=42)
    _add_moves(db_session, session.id, SCOREABLE_MOVES)

    games = _history(client, auth_headers)

    assert [g["summary"]["accuracy"] for g in games] == [42]


# ---------------------------------------------------------------------------
# 2. The surrounding aggregate semantics survive unchanged.
# ---------------------------------------------------------------------------
def test_null_cached_accuracy_drops_out_of_the_denominator(client, auth_headers, db_session):
    """"Ended games that SCORED", not "ended games".

    docs/features/stats-metrics.md defines the completed-and-scored-game
    denominator.

    The NULL game carries scoreable move rows, so a read that recomputed would pull
    it INTO the mean and move the number.
    """
    _insert_session(db_session, player_accuracy=80)
    unscored = _insert_session(db_session, player_accuracy=None)
    _add_moves(db_session, unscored.id, SCOREABLE_MOVES)

    data = _summary(client, auth_headers)

    assert data["moves"]["accuracy_pct"] == 80.0
    # ...and the dropped game is still one of the two ended games elsewhere: the
    # guard's fail-closed verdict must not silently shrink OTHER populations.
    assert data["games"]["played"] == 2
    assert data["moves"]["mistake_free_game_rate"] == 100.0


def test_active_sessions_are_excluded(client, auth_headers, db_session):
    _insert_session(db_session, player_accuracy=80)
    # A stamped value on an active row is not reachable through the hooks, but a
    # read that forgot its ended-only filter would happily average it in.
    _insert_session(db_session, status="active", ended_at=None, result=None,
                    player_accuracy=10)

    assert _summary(client, auth_headers)["moves"]["accuracy_pct"] == 80.0


def test_hidden_drills_are_excluded(client, auth_headers, db_session):
    _insert_session(db_session, player_accuracy=80)
    _insert_session(
        db_session,
        session_mode="drill",
        drill_state="failed",
        is_rated=False,
        rated_start_ply=None,
        player_accuracy=10,
    )

    assert _summary(client, auth_headers)["moves"]["accuracy_pct"] == 80.0
    assert [g["summary"]["accuracy"] for g in _history(client, auth_headers)] == [80]


def test_converted_drills_are_included(client, auth_headers, db_session):
    now = datetime.now(timezone.utc)
    _insert_session(db_session, player_accuracy=80)
    _insert_session(
        db_session,
        session_mode="drill",
        drill_state="converted",
        is_rated=True,
        normal_started_at=now,
        converted_at=now,
        rated_start_ply=2,
        player_accuracy=60,
    )

    assert _summary(client, auth_headers)["moves"]["accuracy_pct"] == 70.0


def test_time_window_filter_still_applies(client, auth_headers, db_session):
    now = datetime.now(timezone.utc)
    _insert_session(db_session, started_at=now - timedelta(days=2), player_accuracy=80)
    _insert_session(db_session, started_at=now - timedelta(days=60), player_accuracy=20)

    assert _summary(client, auth_headers, window_days=7)["moves"]["accuracy_pct"] == 80.0
    assert _summary(client, auth_headers, window_days=0)["moves"]["accuracy_pct"] == 50.0


def test_other_users_are_excluded(client, auth_headers, db_session):
    _insert_session(db_session, user_id=123, player_accuracy=80)
    _insert_session(db_session, user_id=999, player_accuracy=10)

    assert _summary(client, auth_headers, user_id=123)["moves"]["accuracy_pct"] == 80.0


def test_empty_color_group_reports_null_not_zero(client, auth_headers, db_session):
    _insert_session(db_session, player_color="white", player_accuracy=80)

    data = _summary(client, auth_headers)
    assert data["colors"]["white"]["accuracy_pct"] == 80.0
    assert data["colors"]["black"]["accuracy_pct"] is None


# ---------------------------------------------------------------------------
# 3. Both consumers reach the shared seam — history for EVERY returned session.
# ---------------------------------------------------------------------------
def test_history_passes_every_returned_session_to_the_seam(
    client, auth_headers, db_session, monkeypatch
):
    """Including an ended-visible session with ZERO session_moves rows.

    That session is absent from history's GROUP BY, so it takes the zero-move
    summary branch. Before the switch that branch shared one hoisted GameSummary
    instance, which cannot carry a per-session accuracy; a map scoped to the grouped
    move rows would regress here and nowhere else.
    """
    with_moves = _insert_session(db_session, player_accuracy=11)
    _add_moves(db_session, with_moves.id, SCOREABLE_MOVES)
    without_moves = _insert_session(db_session, player_accuracy=None)

    seen: dict[str, list] = {}

    def _spy(db, sessions):
        sessions = list(sessions)
        seen["ids"] = {s.id for s in sessions}
        return {s.id: 77 for s in sessions}

    monkeypatch.setattr("app.api.history.accuracy_for_sessions", _spy)

    games = {uuid.UUID(g["session_id"]): g for g in _history(client, auth_headers)}

    assert seen["ids"] == {with_moves.id, without_moves.id}
    # The spy's value reaches BOTH branches of the response comprehension.
    assert games[with_moves.id]["summary"]["accuracy"] == 77
    assert games[without_moves.id]["summary"]["accuracy"] == 77
    # ...and the zero-move branch still reports its other fields as zero/None.
    assert games[without_moves.id]["summary"]["total_moves"] == 0
    assert games[without_moves.id]["summary"]["average_centipawn_loss"] is None


def test_zero_move_session_serves_its_own_cached_accuracy(client, auth_headers, db_session):
    """The unpatched counterpart: two zero-move sessions with DIFFERENT cached values.

    A single shared zero-move summary instance would give them the same accuracy.
    """
    a = _insert_session(db_session, player_accuracy=33)
    b = _insert_session(db_session, player_accuracy=None)

    games = {uuid.UUID(g["session_id"]): g for g in _history(client, auth_headers)}
    assert games[a.id]["summary"]["accuracy"] == 33
    assert games[b.id]["summary"]["accuracy"] is None


def test_stats_reaches_the_seam(client, auth_headers, db_session, monkeypatch):
    ended = _insert_session(db_session, player_accuracy=11)
    _insert_session(db_session, status="active", ended_at=None, result=None,
                    player_accuracy=99)

    seen: dict[str, set] = {}

    def _spy(db, sessions):
        sessions = list(sessions)
        seen["ids"] = {s.id for s in sessions}
        return {s.id: 64 for s in sessions}

    monkeypatch.setattr("app.api.stats.accuracy_for_sessions", _spy)

    data = _summary(client, auth_headers)

    # Only the ENDED session is handed to the seam — the population filter happens
    # before the read, not inside it.
    assert seen["ids"] == {ended.id}
    assert data["moves"]["accuracy_pct"] == 64.0


# ---------------------------------------------------------------------------
# 4. No PGN parsing, no accuracy computation, no ordered eval query.
# ---------------------------------------------------------------------------
@pytest.fixture
def no_accuracy_work(monkeypatch):
    """Explode on any PGN parse or any run of the frozen algorithm.

    Patched at the LOWEST shared point rather than at the consumers' imports, so it
    fires no matter which route a future regression takes back to them.
    """

    def _boom_parse(*args, **kwargs):
        raise AssertionError("aggregate read parsed a PGN")

    def _boom_compute(*args, **kwargs):
        raise AssertionError("aggregate read ran the accuracy algorithm")

    monkeypatch.setattr("chess.pgn.read_game", _boom_parse)
    monkeypatch.setattr("app.accuracy.compute_game_accuracy", _boom_compute)


def test_stats_does_no_pgn_parsing_or_accuracy_computation(
    client, auth_headers, db_session, no_accuracy_work
):
    scored = _insert_session(db_session, player_accuracy=80)
    _add_moves(db_session, scored.id, SCOREABLE_MOVES)
    # A visible row with a NULL cached accuracy is the tempting case: it is exactly
    # where a "just recompute the missing ones" regression would land.
    unscored = _insert_session(db_session, player_accuracy=None)
    _add_moves(db_session, unscored.id, SCOREABLE_MOVES)

    data = _summary(client, auth_headers)
    assert data["moves"]["accuracy_pct"] == 80.0


def test_history_does_no_pgn_parsing_or_accuracy_computation(
    client, auth_headers, db_session, no_accuracy_work
):
    scored = _insert_session(db_session, player_accuracy=80)
    _add_moves(db_session, scored.id, SCOREABLE_MOVES)
    unscored = _insert_session(db_session, player_accuracy=None)
    _add_moves(db_session, unscored.id, SCOREABLE_MOVES)

    accuracies = sorted(
        (g["summary"]["accuracy"] for g in _history(client, auth_headers)),
        key=lambda v: (v is None, v),
    )
    assert accuracies == [80, None]


def _is_ordered_eval_query(statement: str) -> bool:
    sql = " ".join(statement.lower().split())
    return "session_moves" in sql and "eval_cp" in sql and "order by" in sql


@pytest.fixture
def statements():
    captured: list[str] = []

    def _on_cursor(conn, cursor, statement, parameters, context, executemany) -> None:
        captured.append(statement)

    event.listen(engine, "before_cursor_execute", _on_cursor)
    try:
        yield captured
    finally:
        event.remove(engine, "before_cursor_execute", _on_cursor)


def test_stats_issues_no_ordered_evaluation_query(
    client, auth_headers, db_session, statements
):
    session = _insert_session(db_session, player_accuracy=80)
    _add_moves(db_session, session.id, SCOREABLE_MOVES)

    statements.clear()
    assert _summary(client, auth_headers)["moves"]["accuracy_pct"] == 80.0

    offenders = [s for s in statements if _is_ordered_eval_query(s)]
    assert offenders == [], offenders


def test_history_selects_no_eval_columns(client, auth_headers, db_session, statements):
    """History kept its ordered move query — for FENs — but narrowed the select list.

    eval_cp / eval_mate are accuracy inputs and nothing else on this path, so their
    presence in any history statement means the old shape came back.
    """
    session = _insert_session(db_session, player_accuracy=80)
    _add_moves(db_session, session.id, SCOREABLE_MOVES)

    statements.clear()
    _history(client, auth_headers)

    offenders = [
        s
        for s in statements
        if "session_moves" in s.lower()
        and ("eval_cp" in s.lower() or "eval_mate" in s.lower())
    ]
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# 5. History keeps its move ORDERING for opening-name derivation.
# ---------------------------------------------------------------------------
def _four_field(board: chess.Board) -> str:
    return " ".join(board.fen().split()[:4])


def _e4_e5_positions() -> tuple[str, str, str, str]:
    """Full FENs and 4-field opening keys for the positions after 1. e4 and 1... e5."""
    board = chess.Board()
    board.push(chess.Move.from_uci("e2e4"))
    after_e4, key_e4 = board.fen(), _four_field(board)
    board.push(chess.Move.from_uci("e7e5"))
    after_e5, key_e5 = board.fen(), _four_field(board)
    return after_e4, key_e4, after_e5, key_e5


def _two_root_registry(key_shallow: str, key_deep: str) -> OpeningRoots:
    roots = {
        key_shallow: OpeningRoot(
            opening_key=key_shallow,
            opening_name="King's Pawn Game",
            opening_family="King's Pawn Game",
            eco="B00",
            depth=1,
            parent_keys=frozenset(),
            child_keys=frozenset([key_deep]),
        ),
        key_deep: OpeningRoot(
            opening_key=key_deep,
            opening_name="Open Game",
            opening_family="Open Game",
            eco="C20",
            depth=2,
            parent_keys=frozenset([key_shallow]),
            child_keys=frozenset(),
        ),
    }
    ownership = {key: frozenset([key]) for key in roots}
    return OpeningRoots(roots, ownership)


def test_history_derives_the_opening_name_in_play_order(
    client, auth_headers, db_session, monkeypatch
):
    """Insert the moves in SCRAMBLED physical order; the derived name must not move.

    ``deepest_opening_name`` walks the fens in play order and returns the LAST root
    crossed, so a fen walk in insertion order would report the shallow root
    ("King's Pawn Game") instead of the deep one. Dropping the ORDER BY along with
    move_number/color from the select list would fail exactly this way — silently,
    with a plausible-looking opening name — which is why the narrowed query keeps
    ordering on columns it no longer selects.
    """
    after_e4, key_e4, after_e5, key_e5 = _e4_e5_positions()
    session = _insert_session(db_session, player_accuracy=80)
    # Black's ply is inserted FIRST, so rowid order is the reverse of play order.
    _add_moves(
        db_session,
        session.id,
        [
            {"move_number": 1, "color": "black", "move_san": "e5", "fen_after": after_e5},
            {"move_number": 1, "color": "white", "move_san": "e4", "fen_after": after_e4},
        ],
    )

    monkeypatch.setattr(
        "app.api.history.get_opening_roots",
        lambda: _two_root_registry(key_e4, key_e5),
    )

    games = _history(client, auth_headers)
    assert games[0]["opening_name"] == "Open Game"


# ---------------------------------------------------------------------------
# 6. Session analysis still computes LIVE.
# ---------------------------------------------------------------------------
def test_session_analysis_still_computes_live(client, auth_headers, db_session):
    """The one endpoint that did not switch.

    Its rows score to a real number while the cached column says NULL, so a value
    here can only have been computed from the rows — which is what keeps the
    ply-coordinate guard exercised end to end after the read switch.
    """
    session = _insert_session(db_session, player_accuracy=None)
    _add_moves(db_session, session.id, SCOREABLE_MOVES)

    response = client.get(
        f"/api/session/{session.id}/analysis", headers=auth_headers(user_id=123)
    )
    assert response.status_code == 200
    live = response.json()["summary"]["accuracy"]
    assert live is not None

    # ...and the aggregate consumers still say NULL for the same session.
    assert _summary(client, auth_headers)["moves"]["accuracy_pct"] is None
    assert [g["summary"]["accuracy"] for g in _history(client, auth_headers)] == [None]
