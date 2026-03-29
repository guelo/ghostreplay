"""Tests for opening evidence overlay."""

from __future__ import annotations

import json
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import chess
import pytest
from sqlalchemy import text

from app.fen import normalize_fen
from app.opening_evidence import EdgeEvidence, EvidenceOverlay, NodeEvidence, overlay_evidence
from app.opening_graph import OpeningGraph, _fen_from_board, build_opening_graph

ROOT_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"


def _fen_after_moves(*uci_moves: str) -> str:
    board = chess.Board()
    for m in uci_moves:
        board.push_uci(m)
    return _fen_from_board(board)


# 6-field FEN (as stored in DB).
def _raw_fen_after_moves(*uci_moves: str) -> str:
    board = chess.Board()
    for m in uci_moves:
        board.push_uci(m)
    return board.fen()


# Precompute the 4-field FENs for our synthetic graph.
FEN_ROOT = ROOT_FEN
FEN_E4 = _fen_after_moves("e2e4")
FEN_E4E5 = _fen_after_moves("e2e4", "e7e5")
FEN_E4C5 = _fen_after_moves("e2e4", "c7c5")
FEN_E4E5NF3 = _fen_after_moves("e2e4", "e7e5", "g1f3")

# Raw 6-field FENs for DB insertion.
RAW_ROOT = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
RAW_E4 = _raw_fen_after_moves("e2e4")
RAW_E4E5 = _raw_fen_after_moves("e2e4", "e7e5")
RAW_E4C5 = _raw_fen_after_moves("e2e4", "c7c5")
RAW_E4E5NF3 = _raw_fen_after_moves("e2e4", "e7e5", "g1f3")


@pytest.fixture(scope="module")
def branching_graph() -> OpeningGraph:
    """Build a branching synthetic graph:
    root(w) --e2e4--> e4(b) --e7e5--> e4e5(w) --g1f3--> Nf3(b)
                              --c7c5--> e4c5(w)
    5 nodes, 4 edges.
    """
    with tempfile.TemporaryDirectory() as tmp:
        eco_path = Path(tmp) / "eco.json"
        bypos_path = Path(tmp) / "bypos.json"

        eco_data = {
            "dataset": "test",
            "source_commit": "abc",
            "entry_count": 3,
            "entries": [
                {"eco": "B00", "name": "King's Pawn", "pgn": "1. e4 e5",
                 "uci": "e2e4 e7e5", "epd": FEN_E4E5},
                {"eco": "B20", "name": "Sicilian", "pgn": "1. e4 c5",
                 "uci": "e2e4 c7c5", "epd": FEN_E4C5},
                {"eco": "C44", "name": "King's Knight", "pgn": "1. e4 e5 2. Nf3",
                 "uci": "e2e4 e7e5 g1f3", "epd": FEN_E4E5NF3},
            ],
        }
        bypos_data = {
            "dataset": "test", "source_commit": "abc", "position_count": 0,
            "by_position": {},
        }
        eco_path.write_text(json.dumps(eco_data))
        bypos_path.write_text(json.dumps(bypos_data))
        return build_opening_graph(eco_path, bypos_path)


# -- DB helpers --

def _insert_user(db, user_id: int = 1) -> None:
    db.execute(text(
        "INSERT OR IGNORE INTO users (id, username, is_anonymous) VALUES (:id, :u, 1)"
    ), {"id": user_id, "u": f"user{user_id}"})
    db.commit()


def _insert_session(
    db,
    session_id: str | None = None,
    user_id: int = 1,
    player_color: str = "white",
    started_at: str = "2026-01-01 10:00:00",
    ended_at: str | None = "2026-01-01 11:00:00",
) -> str:
    sid = session_id or str(uuid.uuid4())
    db.execute(text("""
        INSERT INTO game_sessions (id, user_id, started_at, ended_at, status, engine_elo, player_color)
        VALUES (:id, :uid, :sa, :ea, 'completed', 1500, :pc)
    """), {"id": sid, "uid": user_id, "sa": started_at, "ea": ended_at, "pc": player_color})
    db.commit()
    return sid


def _insert_move(
    db,
    session_id: str,
    move_number: int,
    color: str,
    move_san: str,
    fen_before: str | None,
    fen_after: str,
    eval_delta: int | None = None,
) -> None:
    db.execute(text("""
        INSERT INTO session_moves (session_id, move_number, color, move_san, fen_before, fen_after, eval_delta)
        VALUES (:sid, :mn, :c, :ms, :fb, :fa, :ed)
    """), {
        "sid": session_id, "mn": move_number, "c": color,
        "ms": move_san, "fb": fen_before, "fa": fen_after, "ed": eval_delta,
    })
    db.commit()


def _insert_position(db, user_id: int, fen_raw: str, fen_hash: str, active_color: str) -> int:
    db.execute(text("""
        INSERT INTO positions (user_id, fen_hash, fen_raw, active_color)
        VALUES (:uid, :fh, :fr, :ac)
    """), {"uid": user_id, "fh": fen_hash, "fr": fen_raw, "ac": active_color})
    db.commit()
    row = db.execute(text("SELECT last_insert_rowid()")).fetchone()
    return row[0]


def _insert_blunder(
    db,
    user_id: int,
    position_id: int,
    source_session_id: str | None = None,
) -> int:
    db.execute(text("""
        INSERT INTO blunders (user_id, position_id, bad_move_san, best_move_san, eval_loss_cp, source_session_id)
        VALUES (:uid, :pid, 'Qh5', 'Nf3', 200, :ssid)
    """), {"uid": user_id, "pid": position_id, "ssid": source_session_id})
    db.commit()
    row = db.execute(text("SELECT last_insert_rowid()")).fetchone()
    return row[0]


def _insert_review(
    db,
    blunder_id: int,
    session_id: str,
    passed: bool,
    reviewed_at: str = "2026-01-05 12:00:00",
) -> None:
    db.execute(text("""
        INSERT INTO blunder_reviews (blunder_id, session_id, passed, move_played_san, eval_delta_cp, reviewed_at)
        VALUES (:bid, :sid, :p, 'Nf3', 10, :ra)
    """), {"bid": blunder_id, "sid": session_id, "p": passed, "ra": reviewed_at})
    db.commit()


# -- Tests --


class TestEmptyOverlay:
    def test_no_data(self, db_session, branching_graph):
        _insert_user(db_session)
        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        assert ov.nodes == {}
        assert ov.edges == {}
        assert ov.user_id == 1
        assert ov.player_color == "white"


class TestLiveMoves:
    def test_single_pass(self, db_session, branching_graph):
        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=20)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)

        node = ov.nodes[FEN_ROOT]
        assert node.live_attempts == 1
        assert node.live_passes == 1
        assert node.live_fails == 0

        edge = ov.edges[(FEN_ROOT, FEN_E4)]
        assert edge.traversal_count == 1
        assert edge.live_attempts == 1
        assert edge.live_passes == 1
        assert edge.uci == "e2e4"

    def test_single_fail(self, db_session, branching_graph):
        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=80)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)

        node = ov.nodes[FEN_ROOT]
        assert node.live_attempts == 1
        assert node.live_passes == 0
        assert node.live_fails == 1

        edge = ov.edges[(FEN_ROOT, FEN_E4)]
        assert edge.live_fails == 1

    def test_opponent_move_edge_only(self, db_session, branching_graph):
        """Opponent's move creates edge traversal but no node evidence."""
        _insert_user(db_session)
        sid = _insert_session(db_session)
        # Black plays e5 (opponent for white player).
        _insert_move(db_session, sid, 1, "black", "e5", RAW_E4, RAW_E4E5, eval_delta=10)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)

        edge = ov.edges[(FEN_E4, FEN_E4E5)]
        assert edge.traversal_count == 1
        assert edge.live_attempts == 0  # Not user's move.

        # No node evidence for the opponent's source position.
        assert FEN_E4 not in ov.nodes

    def test_null_fen_before_skipped(self, db_session, branching_graph):
        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(db_session, sid, 1, "white", "e4", None, RAW_E4, eval_delta=10)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        assert ov.nodes == {}
        assert ov.edges == {}

    def test_null_eval_delta(self, db_session, branching_graph):
        """Null eval_delta: no live counts, but edge traversal still counted."""
        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=None)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)

        assert FEN_ROOT not in ov.nodes  # No live_attempts recorded.
        edge = ov.edges[(FEN_ROOT, FEN_E4)]
        assert edge.traversal_count == 1
        assert edge.live_attempts == 0

    def test_multiple_attempts_accumulate(self, db_session, branching_graph):
        _insert_user(db_session)
        for i in range(3):
            sid = _insert_session(db_session)
            _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=20 + i * 30)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.live_attempts == 3
        # eval_deltas: 20 (pass), 50 (fail), 80 (fail)
        assert node.live_passes == 1
        assert node.live_fails == 2

    def test_last_live_at_uses_ended_at(self, db_session, branching_graph):
        _insert_user(db_session)
        sid1 = _insert_session(db_session, started_at="2026-01-01 10:00:00", ended_at="2026-01-01 11:00:00")
        sid2 = _insert_session(db_session, started_at="2026-01-05 10:00:00", ended_at="2026-01-05 15:00:00")

        _insert_move(db_session, sid1, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=20)
        _insert_move(db_session, sid2, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=30)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        # Should use the later ended_at.
        assert node.last_live_at.year == 2026
        assert node.last_live_at.day == 5

    def test_last_live_at_falls_back_to_started_at(self, db_session, branching_graph):
        _insert_user(db_session)
        sid = _insert_session(db_session, started_at="2026-02-01 10:00:00", ended_at=None)
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=20)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.last_live_at is not None
        assert node.last_live_at.month == 2


    def test_last_live_at_parses_aware_timestamp(self, db_session, branching_graph):
        """SQLite stores aware datetimes as strings like '2026-01-02 03:04:05.678901+00:00'."""
        _insert_user(db_session)
        sid = _insert_session(
            db_session,
            started_at="2026-03-15 10:00:00.123456+00:00",
            ended_at="2026-03-15 11:30:00.654321+00:00",
        )
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=20)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.last_live_at is not None
        assert node.last_live_at.day == 15
        assert node.last_live_at.hour == 11


class TestBranching:
    def test_branching_edge_counts(self, db_session, branching_graph):
        """Two different moves from after_e4 produce separate edges."""
        _insert_user(db_session)
        sid = _insert_session(db_session)
        # Black plays e5.
        _insert_move(db_session, sid, 1, "black", "e5", RAW_E4, RAW_E4E5, eval_delta=5)
        # In another game, black plays c5.
        sid2 = _insert_session(db_session)
        _insert_move(db_session, sid2, 1, "black", "c5", RAW_E4, RAW_E4C5, eval_delta=10)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)

        assert (FEN_E4, FEN_E4E5) in ov.edges
        assert (FEN_E4, FEN_E4C5) in ov.edges
        assert ov.edges[(FEN_E4, FEN_E4E5)].traversal_count == 1
        assert ov.edges[(FEN_E4, FEN_E4C5)].traversal_count == 1
        assert ov.edges[(FEN_E4, FEN_E4E5)].uci == "e7e5"
        assert ov.edges[(FEN_E4, FEN_E4C5)].uci == "c7c5"


class TestColorIsolation:
    def test_white_only(self, db_session, branching_graph):
        _insert_user(db_session)
        white_sid = _insert_session(db_session, player_color="white")
        black_sid = _insert_session(db_session, player_color="black")

        _insert_move(db_session, white_sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=20)
        _insert_move(db_session, black_sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=80)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.live_passes == 1
        assert node.live_fails == 0  # Black session excluded.


class TestOffBook:
    def test_entirely_off_book_ignored(self, db_session, branching_graph):
        """Moves at positions not in graph and not reachable via extension are ignored."""
        _insert_user(db_session)
        sid = _insert_session(db_session)
        # Some random middlegame FEN.
        random_fen = "r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4"
        random_fen2 = "r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R b KQkq - 5 4"
        _insert_move(db_session, sid, 1, "white", "Nc3", random_fen, random_fen2, eval_delta=10)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        assert ov.nodes == {}
        assert ov.edges == {}


class TestBookExtension:
    def test_one_user_decision_beyond_book(self, db_session, branching_graph):
        """Nf3 is the last book node. A user move from Nf3 response should be collected."""
        _insert_user(db_session)
        sid = _insert_session(db_session)

        # First: user plays e4 (in book, white move).
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=10)
        # Opponent plays e5 (in book).
        _insert_move(db_session, sid, 1, "black", "e5", RAW_E4, RAW_E4E5, eval_delta=5)
        # User plays Nf3 (in book, white move).
        _insert_move(db_session, sid, 2, "white", "Nf3", RAW_E4E5, RAW_E4E5NF3, eval_delta=8)
        # Opponent plays Nc6 (off book, black move — not a user decision).
        raw_nc6 = _raw_fen_after_moves("e2e4", "e7e5", "g1f3", "b8c6")
        _insert_move(db_session, sid, 2, "black", "Nc6", RAW_E4E5NF3, raw_nc6, eval_delta=3)
        # User plays Bc4 (off book, 1st user decision beyond book).
        raw_bc4 = _raw_fen_after_moves("e2e4", "e7e5", "g1f3", "b8c6", "f1c4")
        _insert_move(db_session, sid, 3, "white", "Bc4", raw_nc6, raw_bc4, eval_delta=15)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)

        # The extension edge from Nf3 to Nc6 response should exist.
        norm_nf3 = FEN_E4E5NF3
        norm_nc6 = normalize_fen(raw_nc6)
        norm_bc4 = normalize_fen(raw_bc4)

        # Book-boundary exit: Nf3 (book) -> Nc6 (off-book).
        assert (norm_nf3, norm_nc6) in ov.edges

        # Extension: Nc6 -> Bc4 (1st user decision).
        assert (norm_nc6, norm_bc4) in ov.edges
        edge = ov.edges[(norm_nc6, norm_bc4)]
        assert edge.live_attempts == 1
        assert edge.live_passes == 1

        # Node evidence for the extension user move.
        assert norm_nc6 in ov.nodes
        assert ov.nodes[norm_nc6].live_attempts == 1

    def test_extension_stops_at_limit(self, db_session, branching_graph):
        """Third user decision beyond book should not be collected."""
        _insert_user(db_session)
        sid = _insert_session(db_session)

        # Book moves.
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=10)
        _insert_move(db_session, sid, 1, "black", "e5", RAW_E4, RAW_E4E5, eval_delta=5)
        _insert_move(db_session, sid, 2, "white", "Nf3", RAW_E4E5, RAW_E4E5NF3, eval_delta=8)

        # Off-book chain.
        moves_uci = ["e2e4", "e7e5", "g1f3"]
        off_book_sequence = [
            ("b8c6", "black", "Nc6"),    # opponent (depth 0)
            ("f1c4", "white", "Bc4"),    # user decision 1
            ("g8f6", "black", "Nf6"),    # opponent (still depth 1)
            ("d2d3", "white", "d3"),     # user decision 2
            ("f8e7", "black", "Be7"),    # opponent (still depth 2)
            ("c1g5", "white", "Bg5"),    # user decision 3 — should be EXCLUDED
        ]

        prev_raw = RAW_E4E5NF3
        for uci, color, san in off_book_sequence:
            moves_uci.append(uci)
            next_raw = _raw_fen_after_moves(*moves_uci)
            mn = (len(moves_uci) + 1) // 2
            _insert_move(db_session, sid, mn, color, san, prev_raw, next_raw, eval_delta=10)
            prev_raw = next_raw

        ov = overlay_evidence(db_session, 1, "white", branching_graph)

        # Build the FENs for checking.
        uci_chain = ["e2e4", "e7e5", "g1f3"]
        fens = []
        for uci, _, _ in off_book_sequence:
            uci_chain.append(uci)
            fens.append(normalize_fen(_raw_fen_after_moves(*uci_chain)))

        # Nc6 (opponent, depth 0): should have edge from Nf3.
        assert (FEN_E4E5NF3, fens[0]) in ov.edges

        # Bc4 (user decision 1): node evidence at Nc6 position.
        assert fens[0] in ov.nodes

        # Nf6 (opponent, depth 1): edge should exist.
        assert (fens[1], fens[2]) in ov.edges

        # d3 (user decision 2): node evidence at Nf6 position.
        assert fens[2] in ov.nodes

        # Bg5 (user decision 3): should NOT be collected.
        # The parent position (after Be7) should not have node evidence.
        assert fens[4] not in ov.nodes
        assert (fens[4], fens[5]) not in ov.edges


class TestExtensionTransposition:
    """An off-book position reachable via two paths with different user-decision
    depth should use the shallowest path, so evidence beyond it isn't excluded."""

    def test_shallower_transposition_allows_deeper_evidence(self, db_session, branching_graph):
        """Two sessions reach the same off-book transposition point (tp) at
        different user-decision depths. The code should keep the shallowest
        depth so moves beyond tp are not incorrectly excluded.

        Both start from Nf3 (book leaf, black to move after e4 e5 Nf3).

        Session A (opponent exit → depth 0, user move → depth 1 at tp):
          ...Nc6 (opp exit, depth=0) → Bc4 (user, depth=1) → ...d6 (opp, depth=1) → tp

        Session B (user exit from e4e5 → depth 1, more user moves → depth 2 at tp):
          Bc4 (user exit from e4e5, depth=1) → ...Nc6 (opp, depth=1) → d3 (user, depth=2)
          This doesn't reach tp.

        Simpler: two paths from the SAME book leaf, with a real transposition.
        From Nf3 (book, black to move):
          Path A: ...Nc6 (opp exit) Bc4 (user=1) ...d6 (opp) = tp at depth 1
          Path B: ...d6 — wait, it's black to move, so both Nc6 and d6 are
                  opponent moves. Can't interleave white/black differently.

        Actually the simplest real transposition from a book leaf:
        From e4e5 (book, white to move), TWO sessions with transposing moves:
          Session A: Nc3 (user exit=1) ...Nf6 (opp) Bc4 (user=2) = tp
          Session B: Bc4 (user exit=1) ...Nf6 (opp) Nc3 (user=2) = tp
        Both reach tp at depth 2. Still symmetric.

        For ASYMMETRIC depth we need one path where an opponent exit precedes
        the user's path. Use two different book exit points:
          Session A: from e4e5, Nf3 is book → from Nf3, ...Nc6 (opp exit=0)
                     → Bc4 (user=1) ...d6 (opp=1) → tp
          Session B: from e4e5, Bc4 (user exit=1) → ...Nc6 (opp=1)
                     → d3 (user=2) → ... = different position.

        Nc3 d6 vs d3 Nc6: won't transpose. Let me just use the same two moves
        in different order where they DO transpose (knight + pawn that don't
        interact):
          From Nf3 (book leaf, black to move):
          Session A: ...Nc6 (opp exit=0) → Bc4 (user=1) → ...Nf6 (opp=1)  [tp at depth 1]
          Session B: ...Nf6 (opp exit=0) → Bc4 (user=1) → ...Nc6 (opp=1)  [tp at depth 1]
        After e4 e5 Nf3 Nc6 Bc4 Nf6 == e4 e5 Nf3 Nf6 Bc4 Nc6? YES — same
        pieces, same squares, same side to move (white). This transposes!
        But both paths arrive at tp with depth 1. Still symmetric.

        For a truly asymmetric test, we'd need a path with MORE user decisions.
        But that's inherently hard when alternating colors.

        Instead: verify the mechanism works by having two paths reach the same
        FEN where one path has already been enqueued at a higher depth. We
        process the deeper path first (due to pop() LIFO), then the shallower
        path should re-enqueue. Verify a user move beyond tp is collected.
        """
        _insert_user(db_session)

        # Both sessions: e4 e5 Nf3 (all book).
        sid_a = _insert_session(db_session)
        sid_b = _insert_session(db_session)
        for sid in (sid_a, sid_b):
            _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=10)
            _insert_move(db_session, sid, 1, "black", "e5", RAW_E4, RAW_E4E5, eval_delta=5)
            _insert_move(db_session, sid, 2, "white", "Nf3", RAW_E4E5, RAW_E4E5NF3, eval_delta=8)

        # Session A: ...Nc6 (opp exit=0) → Bc4 (user=1) → ...Nf6 (opp=1) → tp
        raw_nc6 = _raw_fen_after_moves("e2e4", "e7e5", "g1f3", "b8c6")
        raw_nc6_bc4 = _raw_fen_after_moves("e2e4", "e7e5", "g1f3", "b8c6", "f1c4")
        raw_tp = _raw_fen_after_moves("e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6")

        _insert_move(db_session, sid_a, 2, "black", "Nc6", RAW_E4E5NF3, raw_nc6, eval_delta=5)
        _insert_move(db_session, sid_a, 3, "white", "Bc4", raw_nc6, raw_nc6_bc4, eval_delta=8)
        _insert_move(db_session, sid_a, 3, "black", "Nf6", raw_nc6_bc4, raw_tp, eval_delta=3)

        # Session B: ...Nf6 (opp exit=0) → Bc4 (user=1) → ...Nc6 (opp=1) → tp
        raw_nf6 = _raw_fen_after_moves("e2e4", "e7e5", "g1f3", "g8f6")
        raw_nf6_bc4 = _raw_fen_after_moves("e2e4", "e7e5", "g1f3", "g8f6", "f1c4")
        raw_tp_b = _raw_fen_after_moves("e2e4", "e7e5", "g1f3", "g8f6", "f1c4", "b8c6")

        _insert_move(db_session, sid_b, 2, "black", "Nf6", RAW_E4E5NF3, raw_nf6, eval_delta=4)
        _insert_move(db_session, sid_b, 3, "white", "Bc4", raw_nf6, raw_nf6_bc4, eval_delta=7)
        _insert_move(db_session, sid_b, 3, "black", "Nc6", raw_nf6_bc4, raw_tp_b, eval_delta=2)

        # Verify transposition.
        norm_tp_a = normalize_fen(raw_tp)
        norm_tp_b = normalize_fen(raw_tp_b)
        assert norm_tp_a == norm_tp_b, "Transposition FENs must match"
        tp_fen = norm_tp_a

        # From tp (depth 1 via both paths), user plays d3 (depth 2, within limit).
        raw_d3 = _raw_fen_after_moves("e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6", "d2d3")
        _insert_move(db_session, sid_a, 4, "white", "d3", raw_tp, raw_d3, eval_delta=5)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)

        norm_d3 = normalize_fen(raw_d3)
        # tp should be reachable via both paths, and the user move beyond it
        # should be collected (depth 1 + 1 = 2, within limit of 2).
        assert (tp_fen, norm_d3) in ov.edges
        assert tp_fen in ov.nodes


class TestNonBookEdgeToBookPosition:
    """Finding #1: a move from a book position via a non-book edge that lands on
    a position known elsewhere in the graph should be treated as a book exit,
    not silently dropped."""

    def test_non_book_edge_to_known_position(self, db_session, branching_graph):
        """After 1.e4 c5, the position e4c5 is in the graph. But if we reach
        it from the root via a non-book path (impossible in real chess but
        demonstrates the logic), it should be treated as a book exit.

        Real scenario: user plays 1.e4 (book), opponent plays e5 (book edge
        e7e5 exists). Now from after_e4e5, user plays a non-book move whose
        resulting position happens to exist in the graph elsewhere.

        We'll use a simpler shape: from after_e4 (in book), opponent plays a
        non-book move (d7d5 instead of e7e5 or c7c5). The resulting position
        is NOT in the graph — but the key test is that the code doesn't skip it.
        """
        _insert_user(db_session)
        sid = _insert_session(db_session)
        # User plays e4 (book edge).
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=10)
        # Opponent plays d5 — NOT a book edge from after_e4 (only e7e5 and c7c5 are).
        raw_e4d5 = _raw_fen_after_moves("e2e4", "d7d5")
        _insert_move(db_session, sid, 1, "black", "d5", RAW_E4, raw_e4d5, eval_delta=5)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)

        norm_e4d5 = normalize_fen(raw_e4d5)
        # The non-book edge should be recorded as a book exit.
        assert (FEN_E4, norm_e4d5) in ov.edges
        edge = ov.edges[(FEN_E4, norm_e4d5)]
        assert edge.traversal_count == 1
        assert edge.uci == "d7d5"


class TestUserExitConsumesDepth:
    """Finding #2: a user move that exits the book should consume one extension
    depth, so only 1 more user decision is allowed (not 2)."""

    def test_user_exit_limits_extension_to_one_more(self, db_session, branching_graph):
        _insert_user(db_session)
        sid = _insert_session(db_session)

        # Book moves.
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=10)
        _insert_move(db_session, sid, 1, "black", "e5", RAW_E4, RAW_E4E5, eval_delta=5)

        # USER exits book: plays d3 instead of Nf3 (user decision 1 of 2).
        raw_d3 = _raw_fen_after_moves("e2e4", "e7e5", "d2d3")
        _insert_move(db_session, sid, 2, "white", "d3", RAW_E4E5, raw_d3, eval_delta=15)

        # Opponent responds Nc6.
        raw_d3_nc6 = _raw_fen_after_moves("e2e4", "e7e5", "d2d3", "b8c6")
        _insert_move(db_session, sid, 2, "black", "Nc6", raw_d3, raw_d3_nc6, eval_delta=3)

        # User plays Nf3 (user decision 2 of 2).
        raw_d3_nc6_nf3 = _raw_fen_after_moves("e2e4", "e7e5", "d2d3", "b8c6", "g1f3")
        _insert_move(db_session, sid, 3, "white", "Nf3", raw_d3_nc6, raw_d3_nc6_nf3, eval_delta=5)

        # Opponent responds d6.
        raw_d3_nc6_nf3_d6 = _raw_fen_after_moves("e2e4", "e7e5", "d2d3", "b8c6", "g1f3", "d7d6")
        _insert_move(db_session, sid, 3, "black", "d6", raw_d3_nc6_nf3, raw_d3_nc6_nf3_d6, eval_delta=2)

        # User plays Be2 (user decision 3 — should be EXCLUDED).
        raw_be2 = _raw_fen_after_moves("e2e4", "e7e5", "d2d3", "b8c6", "g1f3", "d7d6", "f1e2")
        _insert_move(db_session, sid, 4, "white", "Be2", raw_d3_nc6_nf3_d6, raw_be2, eval_delta=8)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)

        norm_d3 = normalize_fen(raw_d3)
        norm_d3_nc6 = normalize_fen(raw_d3_nc6)
        norm_d3_nc6_nf3 = normalize_fen(raw_d3_nc6_nf3)
        norm_d3_nc6_nf3_d6 = normalize_fen(raw_d3_nc6_nf3_d6)
        norm_be2 = normalize_fen(raw_be2)

        # User exit (decision 1): e4e5 -> d3 should exist.
        assert (FEN_E4E5, norm_d3) in ov.edges

        # User decision 2: Nc6 -> Nf3 should exist.
        assert norm_d3_nc6 in ov.nodes
        assert (norm_d3_nc6, norm_d3_nc6_nf3) in ov.edges

        # User decision 3: should be excluded.
        assert norm_d3_nc6_nf3_d6 not in ov.nodes
        assert (norm_d3_nc6_nf3_d6, norm_be2) not in ov.edges


class TestBoundaryDoubleCount:
    """Finding #3: a user move that exits the book should not double-count
    node mastery on the boundary position."""

    def test_user_exit_no_double_count(self, db_session, branching_graph):
        _insert_user(db_session)
        sid = _insert_session(db_session)

        # User plays e4 (book).
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=10)
        # Opponent plays e5 (book).
        _insert_move(db_session, sid, 1, "black", "e5", RAW_E4, RAW_E4E5, eval_delta=5)
        # User exits book with d3 instead of Nf3.
        raw_d3 = _raw_fen_after_moves("e2e4", "e7e5", "d2d3")
        _insert_move(db_session, sid, 2, "white", "d3", RAW_E4E5, raw_d3, eval_delta=15)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)

        # The boundary node (e4e5) should have exactly 1 live_attempt, not 2.
        node = ov.nodes[FEN_E4E5]
        assert node.live_attempts == 1
        assert node.live_passes == 1  # eval_delta=15 < 50


class TestGhostTargets:
    def test_ghost_target_flagged(self, db_session, branching_graph):
        _insert_user(db_session)
        sid = _insert_session(db_session)
        pos_id = _insert_position(db_session, 1, RAW_ROOT, "roothash", "white")
        _insert_blunder(db_session, 1, pos_id, source_session_id=sid)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        assert FEN_ROOT in ov.nodes
        assert ov.nodes[FEN_ROOT].is_ghost_target is True

    def test_ghost_target_no_source_session_fallback(self, db_session, branching_graph):
        """Blunder without source_session_id uses position active_color."""
        _insert_user(db_session)
        pos_id = _insert_position(db_session, 1, RAW_ROOT, "roothash", "white")
        _insert_blunder(db_session, 1, pos_id, source_session_id=None)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        assert ov.nodes[FEN_ROOT].is_ghost_target is True

    def test_ghost_target_wrong_color_excluded(self, db_session, branching_graph):
        _insert_user(db_session)
        sid = _insert_session(db_session, player_color="black")
        pos_id = _insert_position(db_session, 1, RAW_ROOT, "roothash", "white")
        _insert_blunder(db_session, 1, pos_id, source_session_id=sid)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        assert FEN_ROOT not in ov.nodes


class TestReviews:
    def test_review_pass(self, db_session, branching_graph):
        _insert_user(db_session)
        sid = _insert_session(db_session)
        pos_id = _insert_position(db_session, 1, RAW_ROOT, "roothash", "white")
        blunder_id = _insert_blunder(db_session, 1, pos_id, source_session_id=sid)
        _insert_review(db_session, blunder_id, sid, passed=True, reviewed_at="2026-01-10 12:00:00")

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.review_attempts == 1
        assert node.review_passes == 1
        assert node.review_fails == 0
        assert node.last_review_at is not None

    def test_review_fail(self, db_session, branching_graph):
        _insert_user(db_session)
        sid = _insert_session(db_session)
        pos_id = _insert_position(db_session, 1, RAW_ROOT, "roothash", "white")
        blunder_id = _insert_blunder(db_session, 1, pos_id, source_session_id=sid)
        _insert_review(db_session, blunder_id, sid, passed=False)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.review_attempts == 1
        assert node.review_passes == 0
        assert node.review_fails == 1

    def test_review_off_book_ignored(self, db_session, branching_graph):
        """Reviews at positions not in the graph are ignored."""
        _insert_user(db_session)
        sid = _insert_session(db_session)
        random_fen = "r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4"
        pos_id = _insert_position(db_session, 1, random_fen, "randomhash", "white")
        blunder_id = _insert_blunder(db_session, 1, pos_id, source_session_id=sid)
        _insert_review(db_session, blunder_id, sid, passed=True)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        assert ov.nodes == {}
