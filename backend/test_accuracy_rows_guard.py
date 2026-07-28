"""Frozen ply-coordinate guard in front of accuracy v1 (g-22t8.6).

``compute_game_accuracy`` derives the MOVER from ply parity
(``accuracy_v1.py:200``) but the eval SIGN from ``move.color``
(``_white_relative_cp``, ``accuracy_v1.py:133``). Those are independent axes: on a
mis-shaped row list they disagree and the accuracy is silently WRONG rather than
None. After Release B (g-aeq8) that wrong number is PERSISTED in
``game_sessions.player_accuracy`` and served from cache forever, so the guard has
to be the live write path before Release B ships.

These tests pin:

* the unit contract of ``app.accuracy_rows_v1.ply_coordinates_intact``, including
  identical verdicts for plain-``str`` and ORM-Enum colours;
* the ONE colour rule — ``ply_color`` feeds both the validator and the
  ``AccuracyMove`` construction, so the guard can never validate a grid the
  algorithm then reads differently;
* the guard wired into the live WRITE hook (``recompute_session_accuracy``) and
  into ``/api/session/{id}/analysis``, the one endpoint that still COMPUTES after
  Release B's read switch. ``/api/history`` and ``/api/stats/summary`` now prove
  the guard's verdict TRANSITIVELY: they serve ``game_sessions.player_accuracy``,
  so the NULL the guard stamped is what reaches the wire. That is why their
  fixtures below call ``_recompute_cache`` — the cache has to hold the guard's own
  decision about these rows, not an empty-rowset artifact;
* the surplus-row scope boundary in BOTH directions: a coordinate-contiguous
  surplus still scores, and does so SILENTLY (frozen v1 accepts ``n > expected``,
  and g-i6st measured that length is not a defect signal — the surplus population
  is dominated by truncated PGNs whose rows are the fuller record), while a
  coordinate-BREAKING surplus fails closed;
* the /stats blast radius — a guard-nulled game leaves ``accuracy_pct``'s
  population but STAYS in ``mistake_free_game_rate``'s (SPEC §18.3);
* the static wiring pin: no module under ``app/api/`` may reference
  ``compute_game_accuracy``.
"""

from __future__ import annotations

import ast
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.accuracy import (
    ACCURACY_ALGO_VERSION,
    game_accuracy_for_rows,
    ply_color,
    ply_coordinates_intact,
    recompute_session_accuracy,
)
from app.api.session import MoveColor
from app.models import GameSession, SessionMove

PGN_TWO_PLY = "1. e4 e5"


class _Row:
    """Minimal stand-in for a SessionMove row / query tuple."""

    def __init__(self, move_number, color, eval_cp=15, eval_mate=None):
        self.move_number = move_number
        self.color = color
        self.eval_cp = eval_cp
        self.eval_mate = eval_mate


def _grid(n: int, color_type=str) -> list[_Row]:
    """The first ``n`` plies of a well-formed mainline coordinate grid."""
    rows = []
    for i in range(n):
        name = "white" if i % 2 == 0 else "black"
        color = name if color_type is str else MoveColor(name)
        rows.append(_Row(i // 2 + 1, color, eval_cp=15 if i % 2 == 0 else -15))
    return rows


# ===========================================================================
# 1. Unit: ply_coordinates_intact.
# ===========================================================================
def test_intact_on_well_formed_grid():
    assert ply_coordinates_intact(_grid(4)) is True


def test_intact_on_empty_rows():
    # Vacuously the grid. Frozen v1 owns the empty-list rejection (n == 0 -> None);
    # the guard's job is coordinates, and it must not duplicate that rule.
    assert ply_coordinates_intact([]) is True


def test_broken_on_white_white_adjacency():
    # 1w, 2w: index 1 must be move 1 black, so both axes disagree.
    assert ply_coordinates_intact([_Row(1, "white"), _Row(2, "white")]) is False


def test_broken_on_move_number_gap():
    # 1w, 1b, 3w: move 2 is missing entirely.
    rows = [_Row(1, "white"), _Row(1, "black"), _Row(3, "white")]
    assert ply_coordinates_intact(rows) is False


def test_broken_on_missing_leading_ply():
    # A game whose rows start at black: parity would call it white.
    assert ply_coordinates_intact([_Row(1, "black")]) is False


def test_intact_on_contiguous_surplus():
    # The grid simply continues past the PGN's last ply — coordinates are fine.
    assert ply_coordinates_intact(_grid(3)) is True


def test_verdicts_identical_for_str_and_orm_enum_colors():
    # session.py hands the guard ORM rows; history/stats hand it query tuples with
    # plain strings. A guard that read the two differently would be no guard.
    assert ply_coordinates_intact(_grid(4, color_type=MoveColor)) is True
    assert ply_coordinates_intact(
        [_Row(1, MoveColor.WHITE), _Row(2, MoveColor.WHITE)]
    ) is False


def test_ply_color_normalizes_both_row_shapes():
    assert ply_color(_Row(1, "white")) == "white"
    assert ply_color(_Row(1, MoveColor.WHITE)) == "white"
    assert ply_color(_Row(1, MoveColor.BLACK)) == "black"


# ===========================================================================
# 2. One colour rule: the validator and the algorithm's input agree.
# ===========================================================================
def test_enum_rows_validate_and_score(monkeypatch):
    """An ORM-Enum row set must validate AND reach frozen v1 as plain "white"/"black".

    ``MoveColor`` is a ``str`` Enum, so a naive ``str(move.color)`` yields
    "MoveColor.WHITE" — which ``_white_relative_cp`` (accuracy_v1.py:133) compares
    against "black" and silently treats as WHITE. Pin that both sides go through
    ``ply_color`` instead.
    """
    seen: dict = {}

    import app.accuracy as accuracy_mod

    def _spy(moves, player_color, expected_total_moves):
        seen["moves"] = moves
        return 100

    monkeypatch.setattr(accuracy_mod, "compute_game_accuracy", _spy)

    result = game_accuracy_for_rows(
        _grid(2, color_type=MoveColor), player_color="white", expected_total_moves=2
    )
    assert result == 100
    assert [m.color for m in seen["moves"]] == ["white", "black"]


# ===========================================================================
# 3. The surplus-row boundary, both directions.
# ===========================================================================
def test_contiguous_surplus_scores_silently(caplog):
    """n == expected + 1 with the grid continuing: frozen v1 accepts n > expected
    (accuracy_v1.py:152 rejects only n < expected) and the guard does not
    second-guess it.

    Silently, and that is the MEASURED outcome of g-i6st, not an unexamined
    default. Against the 2026-07-24 production dump the surplus population's
    dominant shape is a truncated PGN — the extra rows replay as a legal
    continuation, so the rows are the fuller record — and rejecting on length
    would have nulled three correct scores while missing 16 of the 19 sessions
    that actually serve a wrong number. The warning this used to assert was
    removed with that finding: it fired on the benign shape and stayed silent on
    the harmful one.
    """
    with caplog.at_level("WARNING"):
        accuracy = game_accuracy_for_rows(
            _grid(3), player_color="white", expected_total_moves=2
        )
    assert accuracy is not None, "a contiguous surplus must still score"
    assert caplog.text == "", "length alone is not a defect signal (g-i6st)"


def test_coordinate_breaking_surplus_fails_closed(caplog):
    # An extra row that leaves a move_number gap -> not the grid -> None.
    rows = _grid(2) + [_Row(3, "white")]
    with caplog.at_level("WARNING"):
        accuracy = game_accuracy_for_rows(
            rows, player_color="white", expected_total_moves=2
        )
    assert accuracy is None
    assert "non-mainline ply coordinates" in caplog.text


def test_well_formed_rows_score_without_warning(caplog):
    with caplog.at_level("WARNING"):
        accuracy = game_accuracy_for_rows(
            _grid(2), player_color="white", expected_total_moves=2
        )
    assert accuracy is not None
    assert caplog.text == ""


# ===========================================================================
# 4. Behavioral: the guard is wired into Release A's WRITE hook.
#
# Helper-only tests pass even if a call site still imports compute_game_accuracy,
# so these go through recompute_session_accuracy and the endpoints.
# ===========================================================================
def _insert_session(db, **kwargs) -> GameSession:
    defaults = dict(
        id=uuid.uuid4(),
        user_id=123,
        started_at=datetime.now(timezone.utc),
        status="ended",
        engine_elo=1500,
        player_color="white",
        session_mode="normal",
        is_rated=True,
        pgn=PGN_TWO_PLY,
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
            )
        )
    db.commit()


# Every broken fixture below is deliberately SCOREABLE by frozen v1 if the guard
# is removed — n >= expected, and white's own eval tanks so the unguarded result is
# a low number rather than 100 or None. Without that, these tests would pass on a
# missing guard, because v1 already nulls n < expected and already scores a
# monotonically-improving white to 100.

# 1w then 2w: no 1b, so parity and move.color name different sides from index 1 on.
BROKEN_WHITE_WHITE = [
    {"move_number": 1, "color": "white", "fen_after": "a", "eval_cp": -300},
    {"move_number": 2, "color": "white", "fen_after": "b", "eval_cp": 30},
]
# 1w, 1b, 3w: move 2 missing. Three rows against a two-ply PGN, so v1's n < expected
# rule does NOT fire (it rejects only n < expected) and the guard is the only reason.
BROKEN_GAP = [
    {"move_number": 1, "color": "white", "fen_after": "a", "eval_cp": -300},
    {"move_number": 1, "color": "black", "fen_after": "b", "eval_cp": -10},
    {"move_number": 3, "color": "white", "fen_after": "c", "eval_cp": 25},
]
CLEAN_TWO_PLY = [
    {"move_number": 1, "color": "white", "fen_after": "a", "eval_cp": 15},
    {"move_number": 1, "color": "black", "fen_after": "b", "eval_cp": -15},
]


def test_broken_fixtures_would_score_without_the_guard():
    """The guard must be the ONLY reason the fixtures below come back None.

    Frozen v1 nulls n < expected and scores a monotonically-improving white to 100
    on its own, so a carelessly-built "broken" fixture yields None/100 anyway and
    every test using it passes on a REMOVED guard. Pin that unguarded v1 returns a
    real, non-100 number for both — then None downstream can only be the guard.
    """
    from app.accuracy_v1 import AccuracyMove, compute_game_accuracy

    for fixture in (BROKEN_WHITE_WHITE, BROKEN_GAP):
        unguarded = compute_game_accuracy(
            [
                AccuracyMove(color=m["color"], eval_cp=m.get("eval_cp"), eval_mate=m.get("eval_mate"))
                for m in fixture
            ],
            player_color="white",
            expected_total_moves=2,  # what PGN_TWO_PLY yields; n >= expected for both
        )
        assert unguarded is not None and unguarded != 100, fixture


def test_hook_nulls_accuracy_on_white_white_adjacency(db_session):
    session = _insert_session(db_session)
    _add_moves(db_session, session.id, BROKEN_WHITE_WHITE)

    recompute_session_accuracy(db_session, session)

    assert session.player_accuracy is None
    # Still stamped: v1 ATTEMPTED the computation and its input contract rejected
    # the inputs — exactly what docs/session-accuracy-versioning.md:8-13 says a
    # stamped NULL means. The algorithm did not change, only the contract in front.
    assert session.player_accuracy_algo_version == ACCURACY_ALGO_VERSION


def test_hook_nulls_accuracy_on_move_number_gap(db_session):
    session = _insert_session(db_session, pgn=PGN_TWO_PLY)
    _add_moves(db_session, session.id, BROKEN_GAP)

    recompute_session_accuracy(db_session, session)

    assert session.player_accuracy is None
    assert session.player_accuracy_algo_version == ACCURACY_ALGO_VERSION


def test_hook_still_scores_a_clean_grid(db_session):
    """The converse — without it the tests above would pass on a guard that
    nulled everything."""
    session = _insert_session(db_session)
    _add_moves(db_session, session.id, CLEAN_TWO_PLY)

    recompute_session_accuracy(db_session, session)

    assert session.player_accuracy is not None
    assert session.player_accuracy_algo_version == ACCURACY_ALGO_VERSION


# ===========================================================================
# 5. Endpoint coverage for every read path that computes accuracy.
# ===========================================================================
def _end_game(client, auth_headers, session_id, user_id=123, pgn=PGN_TWO_PLY):
    return client.post(
        "/api/game/end",
        json={"session_id": session_id, "result": "checkmate_win", "pgn": pgn},
        headers=auth_headers(user_id=user_id),
    )


def _recompute_cache(db_session, session_id) -> GameSession:
    """Run Release A's write hook over the seeded rows, and commit.

    REQUIRED after ``_add_moves`` in any endpoint fixture, because of an ordering
    trap: ``_end_game`` runs the hook while the session has NO move rows, so it
    caches ``None`` (stamped version 1) from an EMPTY rowset, and the direct
    inserts afterwards bypass the hook entirely. That leaves the cache holding a
    NULL the guard never produced.

    Now that history/stats READ ``game_sessions.player_accuracy`` (g-b-cache-reads),
    this is the whole test: without it a broken-grid case would pass on a REMOVED
    guard (the cache says None either way), and the /stats case would report
    accuracy_pct = None for the wrong reason. Recompute so the cached value is what
    the guard actually decided about these rows.
    """
    db_session.expire_all()
    session = (
        db_session.query(GameSession)
        .filter(GameSession.id == uuid.UUID(str(session_id)))
        .one()
    )
    recompute_session_accuracy(db_session, session)
    db_session.commit()
    return session


def test_history_accuracy_is_none_on_broken_grid(
    client, auth_headers, create_game_session, db_session
):
    session_id = create_game_session(user_id=123)
    _end_game(client, auth_headers, session_id)
    _add_moves(db_session, session_id, BROKEN_WHITE_WHITE)
    session = _recompute_cache(db_session, session_id)

    # The CACHE holds the guard's own verdict, not an empty-rowset artifact — so
    # this pins the value the read switch serves.
    assert session.player_accuracy is None
    assert session.player_accuracy_algo_version == ACCURACY_ALGO_VERSION

    response = client.get("/api/history", headers=auth_headers(user_id=123))
    game = response.json()["games"][0]
    assert game["session_id"] == session_id
    assert game["summary"]["accuracy"] is None


def test_session_analysis_accuracy_is_none_on_broken_grid(
    client, auth_headers, create_game_session, db_session
):
    # No _recompute_cache here, deliberately: g-aeq8 keeps /analysis computing LIVE
    # from the rows rather than reading the cached column, so this path exercises
    # the guard directly both before and after the read switch.
    session_id = create_game_session(user_id=123)
    _end_game(client, auth_headers, session_id)
    _add_moves(db_session, session_id, BROKEN_WHITE_WHITE)

    response = client.get(
        f"/api/session/{session_id}/analysis", headers=auth_headers(user_id=123)
    )
    assert response.status_code == 200
    assert response.json()["summary"]["accuracy"] is None


def test_stats_summary_drops_broken_game_from_accuracy_but_keeps_mistake_free(
    client, auth_headers, create_game_session, db_session
):
    """The guard's /stats blast radius, pinned in both halves (SPEC §18.3).

    accuracy_pct is eval-coordinate-grain and drops the broken game from its
    denominator. mistake_free_game_rate is classification-grain — a broken ply grid
    says nothing about whether the rows carry a "blunder" classification — so the
    SAME game stays in ITS denominator. That is intended: the guard must fail
    accuracy closed WITHOUT silently shrinking the mistake-free population.
    """
    clean = create_game_session(user_id=123, player_color="white")
    broken = create_game_session(user_id=123, player_color="white")
    _end_game(client, auth_headers, clean)
    _end_game(client, auth_headers, broken)

    # Flat 15cp white-relative -> accuracy 100 for the clean game.
    _add_moves(db_session, clean, CLEAN_TWO_PLY)
    _add_moves(db_session, broken, BROKEN_WHITE_WHITE)
    clean_session = _recompute_cache(db_session, clean)
    broken_session = _recompute_cache(db_session, broken)

    # Pin the CACHE both ways first: without this the fixture caches None for BOTH
    # games (empty rowset at game-end), and since /stats now serves that column the
    # accuracy_pct assertion below would read None for the wrong reason.
    assert clean_session.player_accuracy == 100
    assert broken_session.player_accuracy is None
    assert clean_session.player_accuracy_algo_version == ACCURACY_ALGO_VERSION
    assert broken_session.player_accuracy_algo_version == ACCURACY_ALGO_VERSION

    data = client.get("/api/stats/summary", headers=auth_headers(user_id=123)).json()

    # Only the clean game reaches _mean_accuracy's population.
    assert data["moves"]["accuracy_pct"] == 100.0
    # ...but the broken game is still a mistake-free game: neither row is a blunder.
    assert data["moves"]["mistake_free_game_rate"] == 100.0


# ===========================================================================
# 6. Static wiring pin.
# ===========================================================================
def test_no_api_module_references_compute_game_accuracy():
    """Every live API caller must go through game_accuracy_for_rows.

    AST-based, so the surviving prose mention in a stats.py docstring does not
    trip it — only a real import or name reference does.
    """
    api_dir = Path(__file__).resolve().parent / "app" / "api"
    offenders: list[str] = []
    for path in sorted(api_dir.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if any(a.name == "compute_game_accuracy" for a in node.names):
                    offenders.append(f"{path.name}:{node.lineno} imports it")
            elif isinstance(node, ast.Name) and node.id == "compute_game_accuracy":
                offenders.append(f"{path.name}:{node.lineno} references it")
            elif isinstance(node, ast.Attribute) and node.attr == "compute_game_accuracy":
                offenders.append(f"{path.name}:{node.lineno} references it")
    assert offenders == [], f"API modules must use game_accuracy_for_rows: {offenders}"
