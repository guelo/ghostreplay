"""Tests for opening evidence overlay."""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import chess
import pytest
from sqlalchemy import bindparam, text

import app.game_phase as game_phase
import app.opening_evidence as opening_evidence
from app.analysis_profiles import CANONICAL_PROFILE_ID, IDENTITY_FIELDS, get_profile
from app.fen import normalize_fen
from app.models import AnalysisCache, PositionAnalysisRow
from app.opening_evidence import (
    OPENING_EVIDENCE_INPUTS_VERSION,
    EdgeEvidence,
    EvidenceOverlay,
    observed_off_book_fens,
    overlay_evidence,
    raw_evidence_inputs_digest,
    reset_session_evidence_cache,
    session_evidence_cache_eviction_count,
)
from app.opening_graph import (
    OpeningGraph,
    OpeningGraphNode,
    _fen_from_board,
    build_opening_graph,
)
from app.opening_rootcalc import RootCalcConfig, _SharedCalculator
from app.opening_roots import OpeningRoot, OpeningRoots

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
    status: str = "ended",
    session_mode: str = "normal",
    drill_state: str | None = None,
    drill_terminal_reason: str | None = None,
    normal_started_at: str | None = None,
    converted_at: str | None = None,
    rated_start_ply: int | None = None,
    is_rated: bool = True,
) -> str:
    sid = session_id or str(uuid.uuid4())
    db.execute(text("""
        INSERT INTO game_sessions (
            id, user_id, started_at, ended_at, status, engine_elo, player_color,
            is_rated, session_mode, drill_state, drill_terminal_reason,
            normal_started_at, converted_at, rated_start_ply
        )
        VALUES (
            :id, :uid, :sa, :ea, :status, 1500, :pc,
            :is_rated, :session_mode, :drill_state, :drill_terminal_reason,
            :normal_started_at, :converted_at, :rated_start_ply
        )
    """), {
        "id": sid,
        "uid": user_id,
        "sa": started_at,
        "ea": ended_at,
        "status": status,
        "pc": player_color,
        "is_rated": is_rated,
        "session_mode": session_mode,
        "drill_state": drill_state,
        "drill_terminal_reason": drill_terminal_reason,
        "normal_started_at": normal_started_at,
        "converted_at": converted_at,
        "rated_start_ply": rated_start_ply,
    })
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
    eval_cp: int | None = None,
    best_move_eval_cp: int | None = None,
) -> None:
    db.execute(text("""
        INSERT INTO session_moves (session_id, move_number, color, move_san, fen_before, fen_after,
                                   eval_delta, eval_cp, best_move_eval_cp)
        VALUES (:sid, :mn, :c, :ms, :fb, :fa, :ed, :ec, :bc)
    """), {
        "sid": session_id, "mn": move_number, "c": color,
        "ms": move_san, "fb": fen_before, "fa": fen_after, "ed": eval_delta,
        "ec": eval_cp, "bc": best_move_eval_cp,
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

    def test_active_drill_excluded_from_evidence(self, db_session, branching_graph):
        # In-progress sessions feed no evidence: session_moves are upserted
        # per-move during live play, so including them would flip the freshness
        # digest on every move (g-dmd1).
        _insert_user(db_session)
        sid = _insert_session(
            db_session, status="active", ended_at=None,
            session_mode="drill", drill_state="active", is_rated=False,
        )
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=20)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)

        assert ov.nodes == {}
        assert ov.edges == {}

    def test_ended_unconverted_drill_contributes_evidence(self, db_session, branching_graph):
        # Amended drill policy (2026-06-01): drill uploads feed the same regular
        # opening-evidence path as normal games once the session has ended —
        # rated or not.
        _insert_user(db_session)
        sid = _insert_session(
            db_session, session_mode="drill", drill_state="abandoned", is_rated=False
        )
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=20)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)

        node = ov.nodes[FEN_ROOT]
        assert node.live_attempts == 1
        assert node.live_passes == 1

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

    def test_last_live_at_uses_started_at(self, db_session, branching_graph):
        _insert_user(db_session)
        sid1 = _insert_session(db_session, started_at="2026-01-01 10:00:00", ended_at="2026-01-01 11:00:00")
        sid2 = _insert_session(db_session, started_at="2026-01-05 10:00:00", ended_at="2026-01-05 15:00:00")

        _insert_move(db_session, sid1, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=20)
        _insert_move(db_session, sid2, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=30)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        # Evidence timestamps are anchored to the normal-play start boundary.
        assert node.last_live_at.year == 2026
        assert node.last_live_at.day == 5

    def test_last_live_at_uses_converted_drill_started_at(self, db_session, branching_graph):
        # Amended drill policy (2026-06-01): a converted drill is one full normal
        # game anchored to the drill's actual started_at, not conversion time.
        _insert_user(db_session)
        sid = _insert_session(
            db_session,
            started_at="2026-01-01 10:00:00",
            ended_at="2026-01-10 12:00:00",
            session_mode="drill",
            drill_state="converted",
            normal_started_at="2026-01-08 09:30:00",
            converted_at="2026-01-08 09:30:00",
            rated_start_ply=0,
            is_rated=True,
        )
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=20)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.last_live_at is not None
        assert node.last_live_at.day == 1
        assert node.last_live_at.hour == 10

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
        assert node.last_live_at.hour == 10


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

    def test_no_user_decision_cutoff_within_opening(self, db_session, branching_graph):
        """Off-book user decisions are collected for as long as the line is in
        the opening phase: there is no fixed two-user-decision cutoff (the divider
        is the only horizon)."""
        _insert_user(db_session)
        sid = _insert_session(db_session)

        # Book moves.
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=10)
        _insert_move(db_session, sid, 1, "black", "e5", RAW_E4, RAW_E4E5, eval_delta=5)
        _insert_move(db_session, sid, 2, "white", "Nf3", RAW_E4E5, RAW_E4E5NF3, eval_delta=8)

        # Off-book chain — all still a full-piece opening (no middlegame trigger).
        moves_uci = ["e2e4", "e7e5", "g1f3"]
        off_book_sequence = [
            ("b8c6", "black", "Nc6"),    # opponent
            ("f1c4", "white", "Bc4"),    # user decision 1
            ("g8f6", "black", "Nf6"),    # opponent
            ("d2d3", "white", "d3"),     # user decision 2
            ("f8e7", "black", "Be7"),    # opponent
            ("c1g5", "white", "Bg5"),    # user decision 3 — still opening, included
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

        # User decisions 1 and 2 are collected as before.
        assert fens[0] in ov.nodes
        assert fens[2] in ov.nodes
        # User decision 3 (Bg5) is now also collected: it is still in the opening
        # phase, so the old two-decision cap no longer drops it.
        assert fens[4] in ov.nodes
        assert (fens[4], fens[5]) in ov.edges


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

    def test_user_exit_extends_through_opening_phase(self, db_session, branching_graph):
        """A user move that exits the book no longer burns a decision budget;
        every opening-phase user decision after it is still collected."""
        _insert_user(db_session)
        sid = _insert_session(db_session)

        # Book moves.
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=10)
        _insert_move(db_session, sid, 1, "black", "e5", RAW_E4, RAW_E4E5, eval_delta=5)

        # USER exits book: plays d3 instead of Nf3 (user decision 1).
        raw_d3 = _raw_fen_after_moves("e2e4", "e7e5", "d2d3")
        _insert_move(db_session, sid, 2, "white", "d3", RAW_E4E5, raw_d3, eval_delta=15)

        # Opponent responds Nc6.
        raw_d3_nc6 = _raw_fen_after_moves("e2e4", "e7e5", "d2d3", "b8c6")
        _insert_move(db_session, sid, 2, "black", "Nc6", raw_d3, raw_d3_nc6, eval_delta=3)

        # User plays Nf3 (user decision 2).
        raw_d3_nc6_nf3 = _raw_fen_after_moves("e2e4", "e7e5", "d2d3", "b8c6", "g1f3")
        _insert_move(db_session, sid, 3, "white", "Nf3", raw_d3_nc6, raw_d3_nc6_nf3, eval_delta=5)

        # Opponent responds d6.
        raw_d3_nc6_nf3_d6 = _raw_fen_after_moves("e2e4", "e7e5", "d2d3", "b8c6", "g1f3", "d7d6")
        _insert_move(db_session, sid, 3, "black", "d6", raw_d3_nc6_nf3, raw_d3_nc6_nf3_d6, eval_delta=2)

        # User plays Be2 (user decision 3 — still opening, now included).
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

        # User decision 3 (Be2): now collected — no fixed cutoff inside the opening.
        assert norm_d3_nc6_nf3_d6 in ov.nodes
        assert (norm_d3_nc6_nf3_d6, norm_be2) in ov.edges


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

    def test_ghost_target_from_active_session_excluded_until_ended(
        self, db_session, branching_graph
    ):
        # Blunders are recorded mid-game, so a live session's blunder must not
        # become a ghost target (or flip the digest) until the session ends.
        _insert_user(db_session)
        empty_digest = raw_evidence_inputs_digest(db_session, 1, "white")
        sid = _insert_session(db_session, status="active", ended_at=None)
        pos_id = _insert_position(db_session, 1, RAW_ROOT, "roothash", "white")
        _insert_blunder(db_session, 1, pos_id, source_session_id=sid)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        assert FEN_ROOT not in ov.nodes
        assert raw_evidence_inputs_digest(db_session, 1, "white") == empty_digest

        db_session.execute(
            text("UPDATE game_sessions SET status='ended', ended_at='2026-01-01 11:00:00'"
                 " WHERE id = :sid"),
            {"sid": sid},
        )
        db_session.commit()

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        assert ov.nodes[FEN_ROOT].is_ghost_target is True
        assert raw_evidence_inputs_digest(db_session, 1, "white") != empty_digest

    def test_manual_ghost_target_in_digest_without_any_session(
        self, db_session, branching_graph
    ):
        # Manually-seeded blunders (no source session) are always eligible.
        _insert_user(db_session)
        empty_digest = raw_evidence_inputs_digest(db_session, 1, "white")
        pos_id = _insert_position(db_session, 1, RAW_ROOT, "roothash", "white")
        _insert_blunder(db_session, 1, pos_id, source_session_id=None)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        assert ov.nodes[FEN_ROOT].is_ghost_target is True
        assert raw_evidence_inputs_digest(db_session, 1, "white") != empty_digest


class TestSessionEligibilityParity:
    """In-progress sessions affect NEITHER the digest NOR the overlay, and BOTH
    once terminal (g-dmd1). The digest is the freshness proxy for the overlay's
    inputs, so the two selections must always agree."""

    def test_inputs_version_bumped_for_eligibility_narrowing(self):
        # The eligibility gate changed the digest's row selection (raw-v4),
        # g-jact moved the version fold into the registry fingerprint (raw-v5),
        # g-no51 normalized the opening-quality eval_delta read (raw-v6), and
        # g-v21l changed WHICH rows the cache fallback selects (the
        # OPENING_EVIDENCE grant), which of those a given user may read (submitter
        # scoping), and which PAIRS upgrade (the coherent-tuple requirement) —
        # raw-v7. Pre-change batches must self-heal via a version mismatch, not
        # serve as fresh.
        assert OPENING_EVIDENCE_INPUTS_VERSION == "raw-v7"

    def test_in_progress_session_affects_neither_digest_nor_overlay(
        self, db_session, branching_graph
    ):
        _insert_user(db_session)
        empty_digest = raw_evidence_inputs_digest(db_session, 1, "white")

        sid = _insert_session(db_session, status="active", ended_at=None)
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=20)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        assert ov.nodes == {}
        assert ov.edges == {}
        assert raw_evidence_inputs_digest(db_session, 1, "white") == empty_digest

        db_session.execute(
            text("UPDATE game_sessions SET status='ended', ended_at='2026-01-01 11:00:00'"
                 " WHERE id = :sid"),
            {"sid": sid},
        )
        db_session.commit()

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        assert ov.nodes[FEN_ROOT].live_attempts == 1
        assert raw_evidence_inputs_digest(db_session, 1, "white") != empty_digest

    def test_accuracy_failed_drill_counts_while_still_active(
        self, db_session, branching_graph
    ):
        # POST /api/drills/{id}/fail (accuracy) computes an opening-score delta
        # WITHOUT ending the session (so /continue stays possible); its quiescent
        # played chain must be included or the end-of-drill delta reads empty.
        _insert_user(db_session)
        empty_digest = raw_evidence_inputs_digest(db_session, 1, "white")
        sid = _insert_session(
            db_session, status="active", ended_at=None, session_mode="drill",
            drill_state="failed", drill_terminal_reason="accuracy", is_rated=False,
        )
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=20)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        assert ov.nodes[FEN_ROOT].live_attempts == 1
        assert raw_evidence_inputs_digest(db_session, 1, "white") != empty_digest

    def test_off_route_failed_drill_excluded_while_active(
        self, db_session, branching_graph
    ):
        # Off-route fail computes no delta and its final move may not be durable
        # yet; the drill folds in only once the session properly ends.
        _insert_user(db_session)
        empty_digest = raw_evidence_inputs_digest(db_session, 1, "white")
        sid = _insert_session(
            db_session, status="active", ended_at=None, session_mode="drill",
            drill_state="failed", drill_terminal_reason="off_route", is_rated=False,
        )
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=20)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        assert ov.nodes == {}
        assert raw_evidence_inputs_digest(db_session, 1, "white") == empty_digest


class TestContinuousQuality:
    def test_primary_session_evals_set_quality(self, db_session, branching_graph):
        """A user move with mover-relative best/played evals scores continuous
        quality without consulting eval_delta."""
        from app.opening_quality import (
            SOURCE_SESSION_EVAL,
            quality_from_win_chance_loss,
        )

        _insert_user(db_session)
        sid = _insert_session(db_session)
        # Best line was even (0cp); played dropped to -60cp (mover perspective).
        _insert_move(
            db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4,
            eval_delta=60, eval_cp=-60, best_move_eval_cp=0,
        )

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.quality_count == 1
        assert node.quality_sum == pytest.approx(quality_from_win_chance_loss(0, -60))
        # Telemetry attributes the observation to the primary source.
        assert ov.source_counts[SOURCE_SESSION_EVAL] == 1

        edge = ov.edges[(FEN_ROOT, FEN_E4)]
        assert edge.quality_count == 1

    def test_eval_delta_fallback_source(self, db_session, branching_graph):
        """A user move with only eval_delta uses the deterministic fallback and
        is attributed to the eval_delta source."""
        from app.opening_quality import SOURCE_EVAL_DELTA, quality_from_eval_delta

        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=120)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.quality_count == 1
        assert node.quality_sum == pytest.approx(quality_from_eval_delta(120))
        assert ov.source_counts[SOURCE_EVAL_DELTA] == 1

    def test_historical_eval_delta_capped_before_quality(self, db_session, branching_graph):
        """A historical uncapped eval_delta (>1000) is normalized through the
        shared centipawn-loss cap before feeding the EVAL_DELTA quality source
        (g-no51). An old session_moves row with eval_delta=10000 must therefore
        yield quality_from_eval_delta(1000) = e^-10, NOT the uncapped
        quality_from_eval_delta(10000) = e^-100. The raw eval_delta stays
        uncapped, so its binary PASS_THRESHOLD read is unaffected (a fail)."""
        from app.opening_quality import SOURCE_EVAL_DELTA, quality_from_eval_delta

        _insert_user(db_session)
        sid = _insert_session(db_session)
        # Only eval_delta present (no eval_cp / best_move_eval_cp) and no matching
        # analysis_cache row for RAW_ROOT, so move_quality falls through to the
        # EVAL_DELTA branch. Exactly one move at FEN_ROOT keeps quality_sum
        # unambiguous.
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=10000)
        # Historical uncapped row, set directly on the stored column.
        db_session.execute(
            text("UPDATE session_moves SET eval_delta = 10000 WHERE session_id = :sid"),
            {"sid": sid},
        )
        db_session.commit()

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.quality_count == 1
        assert ov.source_counts[SOURCE_EVAL_DELTA] == 1
        # Capped: e^-10 (~4.54e-5), NOT the uncapped e^-100.
        assert node.quality_sum == pytest.approx(quality_from_eval_delta(1000))
        assert node.quality_sum != pytest.approx(quality_from_eval_delta(10000))
        # The stored/raw eval_delta remains uncapped, so its binary read still fails.
        assert node.live_fails == 1

    def test_no_eval_signal_no_quality(self, db_session, branching_graph):
        """A user move with no eval at all yields no quality observation."""
        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=None)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        assert FEN_ROOT not in ov.nodes
        assert ov.source_counts == Counter()

    def test_broken_continuity_excludes_session(self, db_session, branching_graph):
        """A session whose move list is not a continuous board line is dropped
        and counted, rather than guessed across."""
        _insert_user(db_session)
        sid = _insert_session(db_session)
        # fen_after of move 1 does not match fen_before of move 2.
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=10)
        bogus_before = _raw_fen_after_moves("d2d4")  # discontinuous jump
        _insert_move(db_session, sid, 1, "black", "d5", bogus_before,
                     _raw_fen_after_moves("d2d4", "d7d5"), eval_delta=10)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        assert ov.excluded_sessions == 1
        assert ov.nodes == {}


class TestDedupOnce:
    """A move reachable by both the pass-2 book-exit and pass-3 extension routes
    must contribute exactly one mastery observation (regression for the
    transposition double-count)."""

    def test_transposed_move_recorded_once(self, db_session, branching_graph):
        _insert_user(db_session)

        # Two sessions transpose to the same off-book position tp after one user
        # decision past the book leaf Nf3, exercising both traversal routes.
        sid_a = _insert_session(db_session)
        sid_b = _insert_session(db_session)
        for sid in (sid_a, sid_b):
            _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=10)
            _insert_move(db_session, sid, 1, "black", "e5", RAW_E4, RAW_E4E5, eval_delta=5)
            _insert_move(db_session, sid, 2, "white", "Nf3", RAW_E4E5, RAW_E4E5NF3, eval_delta=8)

        # Session A: ...Nc6 (opp exit) Bc4 (user) ...Nf6 (opp) → tp
        raw_nc6 = _raw_fen_after_moves("e2e4", "e7e5", "g1f3", "b8c6")
        raw_nc6_bc4 = _raw_fen_after_moves("e2e4", "e7e5", "g1f3", "b8c6", "f1c4")
        raw_tp = _raw_fen_after_moves("e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6")
        _insert_move(db_session, sid_a, 2, "black", "Nc6", RAW_E4E5NF3, raw_nc6, eval_delta=5)
        # The shared user decision Bc4 from raw_nc6 — give it primary evals.
        _insert_move(db_session, sid_a, 3, "white", "Bc4", raw_nc6, raw_nc6_bc4,
                     eval_delta=8, eval_cp=10, best_move_eval_cp=15)
        _insert_move(db_session, sid_a, 3, "black", "Nf6", raw_nc6_bc4, raw_tp, eval_delta=3)

        # Session B reaches raw_nc6 via a different route order (Nf6 first) so the
        # Bc4 user node at raw_nc6 can also be queued from the extension BFS.
        raw_nf6 = _raw_fen_after_moves("e2e4", "e7e5", "g1f3", "g8f6")
        raw_nf6_bc4 = _raw_fen_after_moves("e2e4", "e7e5", "g1f3", "g8f6", "f1c4")
        raw_tp_b = _raw_fen_after_moves("e2e4", "e7e5", "g1f3", "g8f6", "f1c4", "b8c6")
        _insert_move(db_session, sid_b, 2, "black", "Nf6", RAW_E4E5NF3, raw_nf6, eval_delta=4)
        _insert_move(db_session, sid_b, 3, "white", "Bc4", raw_nf6, raw_nf6_bc4,
                     eval_delta=7, eval_cp=12, best_move_eval_cp=14)
        _insert_move(db_session, sid_b, 3, "black", "Nc6", raw_nf6_bc4, raw_tp_b, eval_delta=2)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)

        # Each session's Bc4 is a distinct identity, so two observations total at
        # the (distinct) source positions — but neither is counted twice.
        norm_nc6 = normalize_fen(raw_nc6)
        norm_nf6 = normalize_fen(raw_nf6)
        assert ov.nodes[norm_nc6].quality_count == 1
        assert ov.nodes[norm_nf6].quality_count == 1
        # Edge live attempts are not double-counted across passes either.
        assert ov.edges[(norm_nc6, normalize_fen(raw_nc6_bc4))].live_attempts == 1


_UNSET = object()


def _identity_fields(profile_id: str = CANONICAL_PROFILE_ID) -> dict:
    """Every IDENTITY_FIELDS column for ``profile_id`` so the read-time trust gate
    (_effectively_authoritative) verifies the row's identity against the profile."""
    profile = get_profile(profile_id)
    return {f: getattr(profile, f) for f in IDENTITY_FIELDS}


def _v2_eval_delta(fen_before: str, played_eval: int, best_eval: int) -> int:
    """White-relative best/played -> side-to-move delta clamped >=0.

    Mirrors the resolver-complete-v2 invariant so a canonical row validates on the
    move grain (white: best-played, black: played-best)."""
    board = chess.Board(fen_before)
    delta = best_eval - played_eval if board.turn == chess.WHITE else played_eval - best_eval
    return max(delta, 0)


def _insert_analysis_cache(
    db,
    fen_before: str,
    move_uci: str,
    move_san: str,
    played_eval: int | None = None,
    played_eval_mate: int | None = None,
    best_eval: int | None = None,
    best_eval_mate: int | None = None,
    *,
    normalized_fen_before=_UNSET,
    source: str = "game",
    best_move_uci: str | None = None,
    best_move_san: str | None = None,
    best_line_uci: str | None = None,
    classification: str | None = None,
    eval_delta: int | None = None,
    analysis_profile_id: str | None = None,
    evidence_contract_id: str | None = None,
    identity: dict | None = None,
) -> AnalysisCache:
    """Low-level analysis_cache insert. Defaults produce an UNTRUSTED row (no
    profile/identity/contract); pass the trust columns to make it trusted."""
    row = AnalysisCache(
        fen_before=fen_before,
        normalized_fen_before=(
            normalize_fen(fen_before)
            if normalized_fen_before is _UNSET
            else normalized_fen_before
        ),
        move_uci=move_uci,
        move_san=move_san,
        played_eval=played_eval,
        played_eval_mate=played_eval_mate,
        best_eval=best_eval,
        best_eval_mate=best_eval_mate,
        best_move_uci=best_move_uci,
        best_move_san=best_move_san,
        best_line_uci=best_line_uci,
        classification=classification,
        eval_delta=eval_delta,
        source=source,
        analysis_profile_id=analysis_profile_id,
        evidence_contract_id=evidence_contract_id,
        **(identity or {}),
    )
    db.add(row)
    db.commit()
    return row


def _insert_canonical_cache(
    db,
    fen_before: str,
    move_uci: str,
    move_san: str,
    played_eval: int,
    best_eval: int,
    *,
    best_move_uci: str,
    best_line_uci: str,
    classification: str = "inaccuracy",
    profile_id: str = CANONICAL_PROFILE_ID,
    source: str = "precomputed",
) -> AnalysisCache:
    """A full resolver-complete-v2 canonical row, trusted on BOTH grains.

    One such row backs both ``resolve_trusted_positions`` (position grain) and the
    ``move_trust_flags`` gate (move grain), so a single row covers the happy path.
    ``eval_delta`` is derived to satisfy the v2 cross-grain invariant.
    """
    return _insert_analysis_cache(
        db,
        fen_before,
        move_uci,
        move_san,
        played_eval=played_eval,
        best_eval=best_eval,
        best_move_uci=best_move_uci,
        best_move_san=best_move_uci,
        best_line_uci=best_line_uci,
        classification=classification,
        eval_delta=_v2_eval_delta(fen_before, played_eval, best_eval),
        source=source,
        analysis_profile_id=profile_id,
        evidence_contract_id="resolver-complete-v2",
        identity=_identity_fields(profile_id),
    )


def _insert_position_analysis(
    db,
    fen: str,
    *,
    best_move_uci: str,
    best_line_uci: str,
    best_eval: int | None = None,
    best_eval_mate: int | None = None,
    profile_id: str = CANONICAL_PROFILE_ID,
) -> PositionAnalysisRow:
    """A trusted position-complete-v1 storage winner (resolve_trusted_positions
    tier 1)."""
    row = PositionAnalysisRow(
        normalized_fen=normalize_fen(fen),
        fen=fen,
        best_move_uci=best_move_uci,
        best_move_san=best_move_uci,
        best_line_uci=best_line_uci,
        best_eval=best_eval,
        best_eval_mate=best_eval_mate,
        source="precomputed",
        analysis_profile_id=profile_id,
        evidence_contract_id="position-complete-v1",
        **_identity_fields(profile_id),
    )
    db.add(row)
    db.commit()
    return row


class TestAnalysisCacheFallback:
    def test_cache_reconstructs_quality_and_telemetry(self, db_session, branching_graph):
        """A user move lacking primary session evals is rescored from a matching
        analysis_cache row, attributed to the analysis_cache source."""
        from app.opening_quality import (
            SOURCE_ANALYSIS_CACHE,
            quality_from_win_chance_loss,
        )

        _insert_user(db_session)
        sid = _insert_session(db_session)
        # No eval_cp / best_move_eval_cp on the move → triggers cache lookup.
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=40)
        # White to move at the root, so white-relative evals are mover-relative. A
        # full canonical resolver-complete-v2 row is trusted on both grains, so the
        # trusted position best (20) pairs with the move-trusted played eval (-30).
        _insert_canonical_cache(
            db_session, RAW_ROOT, "e2e4", "e4",
            played_eval=-30, best_eval=20,
            best_move_uci="d2d4", best_line_uci="d2d4 d7d5",
        )

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.quality_count == 1
        assert node.quality_sum == pytest.approx(quality_from_win_chance_loss(20, -30))
        assert ov.source_counts[SOURCE_ANALYSIS_CACHE] == 1

    def test_cache_miss_falls_through_to_eval_delta(self, db_session, branching_graph):
        from app.opening_quality import SOURCE_EVAL_DELTA, quality_from_eval_delta

        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=40)
        # No cache row inserted → deterministic eval_delta fallback.

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.quality_sum == pytest.approx(quality_from_eval_delta(40))
        assert ov.source_counts[SOURCE_EVAL_DELTA] == 1


class TestCacheFallbackTrust:
    """The cache fallback consumes a TRUSTED position best paired with a
    move-trusted played eval — never an untrusted/duplicated best_eval."""

    def test_browser_sibling_best_eval_does_not_drive_quality(
        self, db_session, branching_graph
    ):
        """At one normalized FEN a trusted canonical row and an untrusted browser
        sibling (different best_eval) coexist. Quality must derive from the trusted
        position best, NOT the browser sibling's best_eval."""
        from app.opening_quality import (
            SOURCE_ANALYSIS_CACHE,
            quality_from_win_chance_loss,
        )

        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=40)
        # Trusted canonical row for the played move: best_eval=20 is position truth.
        _insert_canonical_cache(
            db_session, RAW_ROOT, "e2e4", "e4",
            played_eval=-30, best_eval=20,
            best_move_uci="d2d4", best_line_uci="d2d4 d7d5",
        )
        # Untrusted browser sibling at the SAME position with a DIFFERENT best_eval
        # (+900). If it leaked in as position truth, quality would change.
        _insert_analysis_cache(
            db_session, RAW_ROOT, "b1c3", "Nc3",
            played_eval=-30, best_eval=900,
            best_move_uci="b1c3", best_move_san="Nc3",
            best_line_uci="b1c3 b8c6", classification="good",
            source="game", analysis_profile_id="browser-game-v1",
            evidence_contract_id="resolver-complete-v1",
        )

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        # (a) quality comes from the trusted best (20), not the browser best (900),
        # AND (b) the positive outcome: it equals the trusted-pairing win-chance.
        assert node.quality_sum == pytest.approx(quality_from_win_chance_loss(20, -30))
        assert node.quality_sum != pytest.approx(quality_from_win_chance_loss(900, -30))
        assert ov.source_counts[SOURCE_ANALYSIS_CACHE] == 1

    def test_browser_only_declines_to_eval_delta(self, db_session, branching_graph):
        """A browser-only row (no trusted position, not move-trusted) does NOT
        upgrade: quality stays eval_delta-sourced."""
        from app.opening_quality import (
            SOURCE_ANALYSIS_CACHE,
            SOURCE_EVAL_DELTA,
            quality_from_eval_delta,
        )

        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=40)
        _insert_analysis_cache(
            db_session, RAW_ROOT, "e2e4", "e4",
            played_eval=-30, best_eval=20,
            best_move_uci="e2e4", best_move_san="e4",
            best_line_uci="e2e4 e7e5", classification="good",
            source="game", analysis_profile_id="browser-game-v1",
            evidence_contract_id="resolver-complete-v1",
        )

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.quality_sum == pytest.approx(quality_from_eval_delta(40))
        assert ov.source_counts[SOURCE_EVAL_DELTA] == 1
        assert ov.source_counts[SOURCE_ANALYSIS_CACHE] == 0

    def test_storage_position_winner_pairs_with_move_row(
        self, db_session, branching_graph
    ):
        """resolve_trusted_positions tier 1: the trusted position best comes from a
        position_analysis storage winner, paired with a move-trusted cache row."""
        from app.opening_quality import (
            SOURCE_ANALYSIS_CACHE,
            quality_from_win_chance_loss,
        )

        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=40)
        # Storage winner supplies the position best (35); the move row supplies the
        # played eval (-30). The move row's OWN best_eval (999) must be ignored.
        _insert_position_analysis(
            db_session, RAW_ROOT,
            best_move_uci="d2d4", best_line_uci="d2d4 d7d5", best_eval=35,
        )
        _insert_canonical_cache(
            db_session, RAW_ROOT, "e2e4", "e4",
            played_eval=-30, best_eval=999,
            best_move_uci="e2e4", best_line_uci="e2e4 e7e5",
        )

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.quality_sum == pytest.approx(quality_from_win_chance_loss(35, -30))
        assert node.quality_sum != pytest.approx(quality_from_win_chance_loss(999, -30))
        assert ov.source_counts[SOURCE_ANALYSIS_CACHE] == 1

    def test_search_strength_mismatch_declines(
        self, db_session, branching_graph, monkeypatch
    ):
        """A trusted position from a different-strength profile than the move row
        cannot be paired (cross-strength subtraction is invalid) → eval_delta."""
        import dataclasses

        from app import analysis_profiles, evidence_policy
        from app.opening_quality import SOURCE_EVAL_DELTA, quality_from_eval_delta

        # A second authoritative profile at a DEEPER search limit (so it is
        # comparable to canonical but NOT equal strength).
        base = get_profile(CANONICAL_PROFILE_ID)
        deep = dataclasses.replace(
            base, profile_id="canonical-sf18-depth30-test", search_limit_value=30
        )
        monkeypatch.setitem(analysis_profiles._REGISTRY, deep.profile_id, deep)
        # A synthetic authoritative+active profile must also be granted (g-v21l):
        # `_assert_registry_consistent` pins every authoritative+active profile to
        # ALL_CAPABILITIES at import, and a monkeypatched registry entry bypasses
        # that load-time check. Without the grant the row would be excluded by the
        # CAPABILITY gate rather than by the strength guard this test is about.
        monkeypatch.setitem(
            evidence_policy.CAPABILITY_GRANTS,
            deep.profile_id,
            evidence_policy.ALL_CAPABILITIES,
        )

        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=40)
        # Position winner from the DEEPER profile; move row from canonical → the two
        # are EQUAL-strength-guard mismatched, so the pairing must decline.
        _insert_position_analysis(
            db_session, RAW_ROOT,
            best_move_uci="d2d4", best_line_uci="d2d4 d7d5", best_eval=35,
            profile_id=deep.profile_id,
        )
        _insert_canonical_cache(
            db_session, RAW_ROOT, "e2e4", "e4",
            played_eval=-30, best_eval=20,
            best_move_uci="e2e4", best_line_uci="e2e4 e7e5",
        )

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.quality_sum == pytest.approx(quality_from_eval_delta(40))
        assert ov.source_counts[SOURCE_EVAL_DELTA] == 1


class TestRolloutTelemetry:
    """Aggregated calibration telemetry on the overlay: a single mixed fixture
    exercising every quality source, the excluded-session counter, and the
    per-session phase-horizon samples together."""

    def test_mixed_fixture_aggregates_sources_excluded_and_phase_samples(
        self, db_session, branching_graph
    ):
        from app.opening_quality import (
            SOURCE_ANALYSIS_CACHE,
            SOURCE_EVAL_DELTA,
            SOURCE_SESSION_EVAL,
        )

        _insert_user(db_session)

        # Session 1 (clean opening line): primary session evals on e4, and a
        # cache-reconstructed Nf3 (no primary evals → analysis_cache fallback).
        sid1 = _insert_session(db_session)
        _insert_move(
            db_session, sid1, 1, "white", "e4", RAW_ROOT, RAW_E4,
            eval_delta=30, eval_cp=-30, best_move_eval_cp=20,
        )
        _insert_move(db_session, sid1, 1, "black", "e5", RAW_E4, RAW_E4E5, eval_delta=5)
        _insert_move(db_session, sid1, 2, "white", "Nf3", RAW_E4E5, RAW_E4E5NF3, eval_delta=40)
        _insert_canonical_cache(
            db_session, RAW_E4E5, "g1f3", "Nf3", played_eval=-20, best_eval=10,
            best_move_uci="g1f3", best_line_uci="g1f3 b8c6",
        )

        # Session 2 (clean opening line): eval_delta-only user move → deterministic
        # fallback source.
        sid2 = _insert_session(db_session)
        _insert_move(db_session, sid2, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=120)

        # Session 3: broken board continuity → excluded, contributes no sample.
        sid3 = _insert_session(db_session)
        _insert_move(db_session, sid3, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=10)
        _insert_move(
            db_session, sid3, 1, "black", "d5", _raw_fen_after_moves("d2d4"),
            _raw_fen_after_moves("d2d4", "d7d5"), eval_delta=10,
        )

        ov = overlay_evidence(db_session, 1, "white", branching_graph)

        # All three quality sources show up once each in the aggregated counter.
        assert ov.source_counts == Counter({
            SOURCE_SESSION_EVAL: 1,
            SOURCE_ANALYSIS_CACHE: 1,
            SOURCE_EVAL_DELTA: 1,
        })
        # The discontinuous session is dropped and counted, not guessed across.
        assert ov.excluded_sessions == 1
        # One phase sample per non-excluded session; the excluded one has none.
        assert len(ov.phase_samples) == 2
        for sample in ov.phase_samples:
            # Short book lines never reach a middlegame boundary, so the opening
            # interval spans the whole line and middle/end stay None.
            assert sample.opening_interval_len > 0
            assert sample.middle_ply is None
            assert sample.end_ply is None


class TestPhaseExclusion:
    """Moves whose pre-move position is at or beyond the divider's middlegame
    boundary are excluded from opening evidence."""

    def test_post_middlegame_moves_excluded(self, db_session, branching_graph):
        from app.game_phase import divide, is_opening_premove, reconstruct_board_sequence

        # A development line that thins White's back rank into the middlegame.
        uci_line = [
            "e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6", "b1c3", "f8c5",
            "d2d3", "d7d6", "c1g5", "c8g4", "d1d2", "d8d7", "e1c1", "e8c8",
        ]
        board = chess.Board()
        rows = []  # (move_number, color, san, fen_before, fen_after)
        for ply, uci in enumerate(uci_line):
            move = chess.Move.from_uci(uci)
            san = board.san(move)
            fen_before = board.fen()
            color = "white" if board.turn == chess.WHITE else "black"
            board.push(move)
            rows.append((ply // 2 + 1, color, san, fen_before, board.fen()))

        # Confirm the line actually reaches a middlegame boundary.
        boards = reconstruct_board_sequence([(r[3], r[4], r[2]) for r in rows])
        division = divide(boards)
        assert division.middle is not None

        _insert_user(db_session)
        sid = _insert_session(db_session)
        for move_number, color, san, fen_before, fen_after in rows:
            _insert_move(db_session, sid, move_number, color, san, fen_before, fen_after, eval_delta=10)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)

        # Every white (user) move is recorded iff its pre-move ply is inside the
        # opening interval; nothing at/after the middlegame boundary leaks in.
        for ply, (_, color, _, fen_before, _) in enumerate(rows):
            if color != "white":
                continue
            norm_before = normalize_fen(fen_before)
            in_opening = is_opening_premove(division, ply)
            assert (norm_before in ov.nodes) == in_opening, (
                f"ply {ply} opening={in_opening} but node present="
                f"{norm_before in ov.nodes}"
            )
        # At least one white move is on each side of the boundary, so this test
        # exercises both inclusion and exclusion.
        white_plies = [p for p, r in enumerate(rows) if r[1] == "white"]
        assert any(p < division.middle for p in white_plies)
        assert any(p >= division.middle for p in white_plies)

    def test_terminal_opponent_edge_at_phase_horizon_earns_exposure(
        self, db_session, branching_graph
    ):
        from app.game_phase import divide, reconstruct_board_sequence

        uci_line = [
            "e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6", "b1c3", "f8c5",
            "d2d3", "d7d6", "c1g5", "c8g4", "d1d2", "d8d7", "e1c1", "e8c8",
        ]
        board = chess.Board()
        rows = []
        for ply, uci in enumerate(uci_line):
            move = chess.Move.from_uci(uci)
            san = board.san(move)
            fen_before = board.fen()
            color = "white" if board.turn == chess.WHITE else "black"
            board.push(move)
            rows.append((ply // 2 + 1, color, san, fen_before, board.fen()))

        boards = reconstruct_board_sequence([(r[3], r[4], r[2]) for r in rows])
        division = divide(boards)
        assert division.middle == 13

        _insert_user(db_session)
        sid = _insert_session(db_session, player_color="black")
        for move_number, color, san, fen_before, fen_after in rows:
            _insert_move(
                db_session,
                sid,
                move_number,
                color,
                san,
                fen_before,
                fen_after,
                eval_delta=10,
            )

        overlay = overlay_evidence(db_session, 1, "black", branching_graph)
        terminal_parent = normalize_fen(rows[12][3])
        terminal_child = normalize_fen(rows[12][4])
        excluded_child = normalize_fen(rows[13][4])
        terminal_edge = overlay.edges[(terminal_parent, terminal_child)]
        assert rows[12][1] == "white"
        assert terminal_edge.traversal_count > 0
        assert (terminal_child, excluded_child) not in overlay.edges

        root = OpeningRoot(
            opening_key=terminal_parent,
            opening_name="Pre-Qd2 horizon",
            opening_family="Synthetic",
            eco=None,
            depth=0,
            parent_keys=frozenset(),
            child_keys=frozenset(),
        )
        roots = OpeningRoots(
            {terminal_parent: root},
            {terminal_parent: frozenset({terminal_parent})},
        )
        calculator = _SharedCalculator(
            "black",
            branching_graph,
            overlay,
            roots,
            RootCalcConfig(),
            datetime(2026, 8, 10, tzinfo=timezone.utc),
            seeds=[terminal_parent],
        )

        assert calculator._coverage_opportunity_mass(
            terminal_parent
        ) == pytest.approx(
            (1.0 + RootCalcConfig().coverage_depth_decay,) * 2
        )
        assert calculator._coverage_fraction(terminal_parent) == pytest.approx(1.0)


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
        # Reviews never add a mastery (quality) observation.
        assert node.quality_count == 0
        assert ov.source_counts == Counter()

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


# ---------------------------------------------------------------------------
# observed_off_book_fens: explicit contract for the tree position-score model.
# ---------------------------------------------------------------------------


def _two_node_graph() -> OpeningGraph:
    nodes = {
        FEN_ROOT: OpeningGraphNode(FEN_ROOT, "white"),
        FEN_E4: OpeningGraphNode(FEN_E4, "black"),
    }
    nodes[FEN_ROOT].children["e2e4"] = FEN_E4
    nodes[FEN_E4].parents.add((FEN_ROOT, "e2e4"))
    graph = OpeningGraph(nodes, FEN_ROOT)
    graph.freeze()
    return graph


def test_observed_off_book_fens_returns_only_off_book_endpoints():
    graph = _two_node_graph()  # FEN_ROOT and FEN_E4 are in-book
    off_book = FEN_E4C5  # not in the graph
    overlay = EvidenceOverlay(1, "white")
    # An in-book edge (both endpoints in graph) contributes nothing.
    overlay.edges[(FEN_ROOT, FEN_E4)] = EdgeEvidence(FEN_ROOT, FEN_E4, "e2e4")
    # An observed continuation off the book surfaces its off-book endpoint.
    overlay.edges[(FEN_E4, off_book)] = EdgeEvidence(FEN_E4, off_book, "c7c5")

    assert observed_off_book_fens(overlay, graph) == {off_book}


def test_observed_off_book_fens_includes_both_off_book_endpoints():
    graph = _two_node_graph()
    off_book_parent = FEN_E4C5
    off_book_child = FEN_E4E5NF3  # reuse another non-graph FEN for the deeper node
    overlay = EvidenceOverlay(1, "white")
    overlay.edges[(off_book_parent, off_book_child)] = EdgeEvidence(
        off_book_parent, off_book_child, "g1f3"
    )

    assert observed_off_book_fens(overlay, graph) == {off_book_parent, off_book_child}


def test_observed_off_book_fens_empty_when_all_edges_in_book():
    graph = _two_node_graph()
    overlay = EvidenceOverlay(1, "white")
    overlay.edges[(FEN_ROOT, FEN_E4)] = EdgeEvidence(FEN_ROOT, FEN_E4, "e2e4")

    assert observed_off_book_fens(overlay, graph) == set()


# ---------------------------------------------------------------------------
# g-25mp: incremental per-session opening-evidence REPLAY cache.
# ---------------------------------------------------------------------------


class _ReplayCounter:
    """Wraps ``reconstruct_board_sequence`` to count how many sessions replay."""

    def __init__(self, real):
        self._real = real
        self.count = 0

    def __call__(self, moves):
        self.count += 1
        return self._real(moves)


def _insert_line(db, sid, uci_moves, *, eval_delta=10, eval_cp=None, best_move_eval_cp=None):
    """Insert a continuous, legal session line as session_moves rows."""
    board = chess.Board()
    for ply, uci in enumerate(uci_moves):
        move = chess.Move.from_uci(uci)
        san = board.san(move)
        fen_before = board.fen()
        color = "white" if board.turn == chess.WHITE else "black"
        board.push(move)
        _insert_move(
            db, sid, ply // 2 + 1, color, san, fen_before, board.fen(),
            eval_delta=eval_delta, eval_cp=eval_cp, best_move_eval_cp=best_move_eval_cp,
        )


def _extend_line(db, sid, prefix_ucis, extra_ucis, *, eval_delta=10):
    """Append LATER plies to a session already holding ``prefix_ucis``.

    The /moves-lands-late shape: row_count GROWS on a session an earlier build
    already cached, rather than a new session appearing or existing values being
    rewritten. The continuation replays the prefix first, so the fen chain stays
    continuous and the session is not excluded.
    """
    board = chess.Board()
    for uci in prefix_ucis:
        board.push(chess.Move.from_uci(uci))
    for offset, uci in enumerate(extra_ucis):
        ply = len(prefix_ucis) + offset
        move = chess.Move.from_uci(uci)
        san = board.san(move)
        fen_before = board.fen()
        color = "white" if board.turn == chess.WHITE else "black"
        board.push(move)
        _insert_move(
            db, sid, ply // 2 + 1, color, san, fen_before, board.fen(),
            eval_delta=eval_delta,
        )


def _insert_discontinuous_session(db, sid):
    """Two white moves whose fens don't chain → ContinuityError on replay."""
    _insert_move(db, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=10)
    # move 2's fen_before (after 1.e4 e5) does not equal move 1's fen_after (RAW_E4).
    _insert_move(db, sid, 2, "white", "Nf3", RAW_E4E5, RAW_E4E5NF3, eval_delta=10)


def _assert_overlay_equal(a, b):
    assert a.nodes == b.nodes
    assert a.edges == b.edges
    assert a.source_counts == b.source_counts
    assert a.excluded_sessions == b.excluded_sessions
    assert a.phase_samples == b.phase_samples


class TestIncrementalReplayCache:
    def test_append_one_session_replays_only_that_session(
        self, db_session, branching_graph, monkeypatch
    ):
        """Primary acceptance: first build replays every session; after appending
        one finished session, the next build replays ONLY the new one."""
        _insert_user(db_session)
        for _ in range(3):
            sid = _insert_session(db_session)
            _insert_line(db_session, sid, ["e2e4", "e7e5", "g1f3", "b8c6"])

        counter = _ReplayCounter(opening_evidence.reconstruct_board_sequence)
        monkeypatch.setattr(opening_evidence, "reconstruct_board_sequence", counter)

        overlay_evidence(db_session, 1, "white", branching_graph)
        assert counter.count == 3  # cold build replays all N

        counter.count = 0
        new_sid = _insert_session(db_session)
        _insert_line(db_session, new_sid, ["e2e4", "c7c5"])
        overlay_evidence(db_session, 1, "white", branching_graph)
        assert counter.count == 1  # only the appended session replayed

    def test_unchanged_rebuild_replays_nothing(
        self, db_session, branching_graph, monkeypatch
    ):
        _insert_user(db_session)
        for _ in range(2):
            sid = _insert_session(db_session)
            _insert_line(db_session, sid, ["e2e4", "e7e5", "g1f3", "b8c6"])
        overlay_evidence(db_session, 1, "white", branching_graph)

        counter = _ReplayCounter(opening_evidence.reconstruct_board_sequence)
        monkeypatch.setattr(opening_evidence, "reconstruct_board_sequence", counter)
        overlay_evidence(db_session, 1, "white", branching_graph)
        assert counter.count == 0  # every session served from cache


class TestExcludedSessionWarnOnce:
    def test_excluded_warns_once_across_builds(self, db_session, branching_graph, caplog):
        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_discontinuous_session(db_session, sid)

        with caplog.at_level(logging.WARNING, logger="app.opening_evidence"):
            ov1 = overlay_evidence(db_session, 1, "white", branching_graph)
            ov2 = overlay_evidence(db_session, 1, "white", branching_graph)

        warns = [r for r in caplog.records if "excluding session" in r.getMessage()]
        assert len(warns) == 1  # logged once per content, not once per rebuild
        assert ov1.excluded_sessions == 1
        assert ov2.excluded_sessions == 1

        # Changing the session content → new content hash → warns again.
        caplog.clear()
        db_session.execute(
            text(
                "UPDATE session_moves SET eval_delta = 99 "
                "WHERE session_id = :sid AND move_number = 1"
            ),
            {"sid": sid},
        )
        db_session.commit()
        with caplog.at_level(logging.WARNING, logger="app.opening_evidence"):
            ov3 = overlay_evidence(db_session, 1, "white", branching_graph)
        warns2 = [r for r in caplog.records if "excluding session" in r.getMessage()]
        assert len(warns2) == 1
        assert ov3.excluded_sessions == 1


class TestDifferentialParity:
    """incremental (cache warm) overlay == from-scratch overlay, across scenarios."""

    def test_parity_new_session_appended(self, db_session, branching_graph):
        _insert_user(db_session)
        sid1 = _insert_session(db_session)
        _insert_line(db_session, sid1, ["e2e4", "e7e5", "g1f3", "b8c6"])
        overlay_evidence(db_session, 1, "white", branching_graph)  # warm

        sid2 = _insert_session(db_session)
        _insert_line(db_session, sid2, ["e2e4", "c7c5"])
        incr = overlay_evidence(db_session, 1, "white", branching_graph)

        reset_session_evidence_cache()
        scratch = overlay_evidence(db_session, 1, "white", branching_graph)
        _assert_overlay_equal(incr, scratch)

    def test_parity_late_rows_appended_to_a_cached_session(
        self, db_session, branching_graph, monkeypatch
    ):
        """row_count GROWTH on an already-cached session — distinct from a new
        session appearing (covered above) and from existing values being rewritten
        (covered below). Only the extended session may re-derive, and the result
        must equal a clean rebuild."""
        _insert_user(db_session)
        stable = _insert_session(db_session)
        _insert_line(db_session, stable, ["e2e4", "e7e5", "g1f3", "b8c6"])
        late = _insert_session(db_session)
        prefix = ["e2e4", "e7e5"]
        _insert_line(db_session, late, prefix)
        before = overlay_evidence(db_session, 1, "white", branching_graph)  # warm both

        # 2.Nf3 is in book from FEN_E4E5, so the appended plies really do add
        # evidence — otherwise "unchanged output" would pass vacuously.
        _extend_line(db_session, late, prefix, ["g1f3", "b8c6"])

        counter = _ReplayCounter(opening_evidence.reconstruct_board_sequence)
        monkeypatch.setattr(opening_evidence, "reconstruct_board_sequence", counter)
        incr = overlay_evidence(db_session, 1, "white", branching_graph)
        assert counter.count == 1  # the extended session only; `stable` still cached

        node = incr.nodes[FEN_E4E5]
        assert node.live_attempts == 2  # was 1 (from `stable`) before the append
        assert late in node.session_ids

        reset_session_evidence_cache()
        scratch = overlay_evidence(db_session, 1, "white", branching_graph)
        _assert_overlay_equal(incr, scratch)
        with pytest.raises(AssertionError):
            _assert_overlay_equal(incr, before)  # the append was observable

    def test_parity_eligibility_round_trip_failed_converted_ended(
        self, db_session, branching_graph, monkeypatch
    ):
        """Eligibility ROUND TRIP: eligible → ineligible → eligible again.

        An accuracy-failed drill is evidence-eligible while its status is still
        'active'; /continue flips ``drill_state`` to 'converted' → ineligible; the
        session later ENDS → eligible again. (There is no converted → failed edge
        to test: ``ck_game_sessions_drill_rating_boundary`` requires is_rated=true
        for 'converted' and is_rated=false for 'failed', so a converted session
        regains eligibility only by ending.)

        Eligibility is enforced by the PROBE — the session is simply not returned —
        and is deliberately NOT part of the content digest, so the cached replay
        product survives the excursion and the return re-derives nothing. Each
        state must also equal its own clean rebuild.
        """
        _insert_user(db_session)
        normal = _insert_session(db_session)
        _insert_line(db_session, normal, ["e2e4", "e7e5"])
        drill = _insert_session(
            db_session, status="active", ended_at=None, session_mode="drill",
            drill_state="failed", drill_terminal_reason="accuracy", is_rated=False,
        )
        _insert_line(db_session, drill, ["e2e4", "c7c5"])

        def _update(**cols):
            sets = ", ".join(f"{c} = :{c}" for c in cols)
            db_session.execute(
                text(f"UPDATE game_sessions SET {sets} WHERE id = :sid"),
                {**cols, "sid": drill},
            )
            db_session.commit()

        eligible = overlay_evidence(db_session, 1, "white", branching_graph)
        assert drill in eligible.nodes[FEN_ROOT].session_ids

        counter = _ReplayCounter(opening_evidence.reconstruct_board_sequence)
        monkeypatch.setattr(opening_evidence, "reconstruct_board_sequence", counter)

        _update(
            drill_state="converted", is_rated=True,
            normal_started_at="2026-01-01 10:30:00",
            converted_at="2026-01-01 10:30:00", rated_start_ply=2,
        )
        converted = overlay_evidence(db_session, 1, "white", branching_graph)
        assert drill not in converted.nodes[FEN_ROOT].session_ids
        assert normal in converted.nodes[FEN_ROOT].session_ids
        assert counter.count == 0  # dropped by the probe, not re-derived

        # The converted game ends with no further plies uploaded: same rows, same
        # started_at, so the digest is unchanged and the entry is still valid.
        _update(status="ended", ended_at="2026-01-01 11:00:00")
        restored = overlay_evidence(db_session, 1, "white", branching_graph)
        assert counter.count == 0  # the surviving entry is legitimately reused
        _assert_overlay_equal(restored, eligible)  # round trip is lossless

        reset_session_evidence_cache()
        _assert_overlay_equal(
            restored, overlay_evidence(db_session, 1, "white", branching_graph)
        )

        # ...and the ineligible middle state equals its own clean rebuild too.
        _update(status="active", ended_at=None)
        reset_session_evidence_cache()
        _assert_overlay_equal(
            converted, overlay_evidence(db_session, 1, "white", branching_graph)
        )

    def test_parity_eval_backfill_invalidates(
        self, db_session, branching_graph, monkeypatch
    ):
        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_line(db_session, sid, ["e2e4", "e7e5", "g1f3", "b8c6"])
        overlay_evidence(db_session, 1, "white", branching_graph)  # warm (eval_delta)

        # Backfill primary evals on the existing rows → content hash flips.
        db_session.execute(
            text(
                "UPDATE session_moves SET eval_cp = 30, best_move_eval_cp = 55, "
                "eval_delta = 25 WHERE session_id = :sid"
            ),
            {"sid": sid},
        )
        db_session.commit()

        counter = _ReplayCounter(opening_evidence.reconstruct_board_sequence)
        monkeypatch.setattr(opening_evidence, "reconstruct_board_sequence", counter)
        incr = overlay_evidence(db_session, 1, "white", branching_graph)
        assert counter.count == 1  # the backfilled session re-derived

        reset_session_evidence_cache()
        scratch = overlay_evidence(db_session, 1, "white", branching_graph)
        # If the cache had served the pre-backfill value, quality source would
        # differ (eval_delta vs session_eval) and this would fail.
        _assert_overlay_equal(incr, scratch)

    def test_parity_with_excluded_session(self, db_session, branching_graph):
        _insert_user(db_session)
        sid_ok = _insert_session(db_session)
        _insert_line(db_session, sid_ok, ["e2e4", "e7e5", "g1f3", "b8c6"])
        sid_bad = _insert_session(db_session)
        _insert_discontinuous_session(db_session, sid_bad)
        overlay_evidence(db_session, 1, "white", branching_graph)  # warm

        incr = overlay_evidence(db_session, 1, "white", branching_graph)
        assert incr.excluded_sessions == 1

        reset_session_evidence_cache()
        scratch = overlay_evidence(db_session, 1, "white", branching_graph)
        _assert_overlay_equal(incr, scratch)

    def test_parity_divider_version_bump(
        self, db_session, branching_graph, monkeypatch
    ):
        _insert_user(db_session)
        for _ in range(2):
            sid = _insert_session(db_session)
            _insert_line(db_session, sid, ["e2e4", "e7e5", "g1f3", "b8c6"])
        overlay_evidence(db_session, 1, "white", branching_graph)  # warm

        counter = _ReplayCounter(opening_evidence.reconstruct_board_sequence)
        monkeypatch.setattr(opening_evidence, "reconstruct_board_sequence", counter)
        monkeypatch.setattr(game_phase, "DIVIDER_VERSION", "divider-test-bump")

        incr = overlay_evidence(db_session, 1, "white", branching_graph)
        assert counter.count == 2  # version bump forced full re-derivation

        reset_session_evidence_cache()
        scratch = overlay_evidence(db_session, 1, "white", branching_graph)
        _assert_overlay_equal(incr, scratch)

    def test_parity_inputs_version_bump(
        self, db_session, branching_graph, monkeypatch
    ):
        _insert_user(db_session)
        for _ in range(2):
            sid = _insert_session(db_session)
            _insert_line(db_session, sid, ["e2e4", "e7e5", "g1f3", "b8c6"])
        overlay_evidence(db_session, 1, "white", branching_graph)  # warm

        counter = _ReplayCounter(opening_evidence.reconstruct_board_sequence)
        monkeypatch.setattr(opening_evidence, "reconstruct_board_sequence", counter)
        monkeypatch.setattr(
            opening_evidence, "OPENING_EVIDENCE_INPUTS_VERSION", "raw-test-bump"
        )

        incr = overlay_evidence(db_session, 1, "white", branching_graph)
        assert counter.count == 2

        reset_session_evidence_cache()
        scratch = overlay_evidence(db_session, 1, "white", branching_graph)
        _assert_overlay_equal(incr, scratch)

    def test_fallback_applies_after_cache_hit(self, db_session, branching_graph):
        """analysis_cache fallback runs OUTSIDE the cache: it applies on the miss
        AND on a later hit, proving cached rows aren't mutated and fallbacks
        re-run over the merged rows each build."""
        from app.opening_quality import (
            SOURCE_ANALYSIS_CACHE,
            quality_from_win_chance_loss,
        )

        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=40)
        _insert_canonical_cache(
            db_session, RAW_ROOT, "e2e4", "e4",
            played_eval=-30, best_eval=20,
            best_move_uci="d2d4", best_line_uci="d2d4 d7d5",
        )

        expected = quality_from_win_chance_loss(20, -30)
        ov1 = overlay_evidence(db_session, 1, "white", branching_graph)  # miss
        db_session.commit()
        reset_session_evidence_cache()
        ov2 = overlay_evidence(db_session, 1, "white", branching_graph)  # L2 hit
        for ov in (ov1, ov2):
            assert ov.nodes[FEN_ROOT].quality_sum == pytest.approx(expected)
            assert ov.source_counts[SOURCE_ANALYSIS_CACHE] == 1
        assert ov2.replay_cache_stats.l2_hits == 1
        assert ov2.replay_cache_stats.raw_derivations == 0

    def test_quality_recomputed_on_cache_hit(
        self, db_session, branching_graph, monkeypatch
    ):
        """Finding-1 guard: quality is recomputed on copy-out, not served from a
        cached derived value. Patching ``move_quality`` after warming the cache
        changes the output on a HIT even though the replay never re-runs."""
        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(
            db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4,
            eval_cp=30, best_move_eval_cp=55,
        )
        ov1 = overlay_evidence(db_session, 1, "white", branching_graph)  # warm
        base_q = ov1.nodes[FEN_ROOT].quality_sum
        assert ov1.nodes[FEN_ROOT].quality_count == 1

        counter = _ReplayCounter(opening_evidence.reconstruct_board_sequence)
        monkeypatch.setattr(opening_evidence, "reconstruct_board_sequence", counter)
        real_move_quality = opening_evidence.move_quality
        SHIFT = 0.25

        def _shifted(**kwargs):
            q, source = real_move_quality(**kwargs)
            return (None if q is None else q + SHIFT, source)

        monkeypatch.setattr(opening_evidence, "move_quality", _shifted)

        ov2 = overlay_evidence(db_session, 1, "white", branching_graph)  # cache hit
        assert counter.count == 0  # replay untouched — served from cache
        assert ov2.nodes[FEN_ROOT].quality_sum == pytest.approx(base_q + SHIFT)


class TestCacheMutationAliasing:
    def test_cached_entry_holds_unresolved_replay_product(
        self, db_session, branching_graph
    ):
        """The cache stores the RAW replay product; ``_apply_cache_fallbacks``
        upgrades only the fresh copies, never the frozen cached rows."""
        from app.opening_quality import SOURCE_ANALYSIS_CACHE

        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=40)
        _insert_canonical_cache(
            db_session, RAW_ROOT, "e2e4", "e4",
            played_eval=-30, best_eval=20,
            best_move_uci="d2d4", best_line_uci="d2d4 d7d5",
        )

        ov1 = overlay_evidence(db_session, 1, "white", branching_graph)  # miss + fallback
        assert ov1.source_counts[SOURCE_ANALYSIS_CACHE] == 1

        _, cached_session = opening_evidence._SESSION_EVIDENCE_CACHE[sid]
        assert cached_session.moves
        for cm in cached_session.moves:
            # Raw evals unchanged; the frozen row has no quality field to poison.
            assert cm.eval_cp is None and cm.best_move_eval_cp is None
            assert not hasattr(cm, "quality")

        ov2 = overlay_evidence(db_session, 1, "white", branching_graph)  # hit
        assert ov2.source_counts[SOURCE_ANALYSIS_CACHE] == 1


class TestDegradedUnderBudget:
    def test_thrash_stays_correct(self, db_session, branching_graph, monkeypatch, caplog):
        """With the row budget below the working set, entries evict mid-build and
        later hydrate from durable L2 — output stays correct and L1 evictions are
        still counted/warned without falsely implying another board replay."""
        _insert_user(db_session)
        for _ in range(4):
            sid = _insert_session(db_session)
            _insert_line(db_session, sid, ["e2e4", "e7e5", "g1f3", "b8c6"])

        monkeypatch.setattr(opening_evidence, "_SESSION_CACHE_MAX_ROWS", 3)

        with caplog.at_level(logging.WARNING, logger="app.opening_evidence"):
            incr = overlay_evidence(db_session, 1, "white", branching_graph)
        assert session_evidence_cache_eviction_count() > 0
        assert any("cache evicted" in r.getMessage() for r in caplog.records)

        reset_session_evidence_cache()
        scratch = overlay_evidence(db_session, 1, "white", branching_graph)
        _assert_overlay_equal(incr, scratch)  # correctness holds under thrash

        counter = _ReplayCounter(opening_evidence.reconstruct_board_sequence)
        monkeypatch.setattr(opening_evidence, "reconstruct_board_sequence", counter)
        new_sid = _insert_session(db_session)
        _insert_line(db_session, new_sid, ["e2e4", "c7c5"])
        rebuilt = overlay_evidence(db_session, 1, "white", branching_graph)
        assert counter.count == 1  # only the genuinely new session replays
        assert rebuilt.replay_cache_stats.l2_hits > 0
        assert rebuilt.replay_cache_stats.raw_derivations == 1


# --------------------------------------------------------------------------- #
# g-overlay-evidence-reuse: the cached UCI, the DB-computed replay digest, and
# the probe/fetch race.
# --------------------------------------------------------------------------- #

# (fen_before, move_san) pairs where a raw-FEN parse could plausibly diverge from
# a normalized-4-field parse — the en-passant field is the only thing
# ``normalize_fen`` rewrites, so both EP directions are covered, plus the move
# kinds whose UCI encoding is special (castling, promotion) and the SAN forms that
# need the legal-move set to disambiguate.
_UCI_PARITY_CASES = [
    # EP capture is legal AND played: normalize_fen KEEPS the ep square.
    ("rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 4", "exf6"),
    # Same position, a non-EP move played while the ep square is live.
    ("rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 4", "d4"),
    # EP square present in the FEN but NO legal ep capture exists: normalize_fen
    # CLEARS it. Dropping an illegal move cannot change any legal move's SAN.
    ("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1", "e5"),
    # Castling, both sides, both colors — UCI encodes king-to-square.
    ("r3k2r/pppq1ppp/2npbn2/2b1p3/2B1P3/2NPBN2/PPPQ1PPP/R3K2R w KQkq - 0 9", "O-O"),
    ("r3k2r/pppq1ppp/2npbn2/2b1p3/2B1P3/2NPBN2/PPPQ1PPP/R3K2R w KQkq - 0 9", "O-O-O"),
    ("r3k2r/pppq1ppp/2npbn2/2b1p3/2B1P3/2NPBN2/PPPQ1PPP/R3K2R b KQkq - 0 9", "O-O"),
    # Promotion, quiet and capturing.
    ("8/4P3/8/8/8/8/k6K/8 w - - 0 1", "e8=Q"),
    ("3r4/4P3/8/8/8/8/k6K/8 w - - 0 1", "exd8=N"),
    # SAN that needs file disambiguation (two knights / two rooks reach the
    # target), and SAN that needs RANK disambiguation.
    ("4k3/8/8/8/8/8/8/1N2KN2 w - - 0 1", "Nbd2"),
    ("4k3/8/8/8/8/4K3/8/R6R w - - 0 1", "Rhf1"),
    ("4k3/8/8/8/8/R7/8/R3K3 w - - 0 1", "R1a2"),
]


class TestCachedUciParity:
    """``_CachedMove.uci`` is parsed on the RAW fen_before during replay; it
    replaced a per-build parse on the NORMALIZED 4-field FEN. Pin that the two
    agree, since the whole speedup rests on that equivalence."""

    @pytest.mark.parametrize("raw_fen,san", _UCI_PARITY_CASES)
    def test_raw_and_normalized_parses_agree(self, raw_fen, san):
        raw_uci = chess.Board(raw_fen).parse_san(san).uci()
        assert opening_evidence._uci_from_san(normalize_fen(raw_fen), san) == raw_uci

    def test_derive_session_carries_the_oracle_uci(self, db_session):
        """Integration: every uci the real replay caches equals the oracle's."""
        board = chess.Board()
        rows = []
        for ply, uci in enumerate(
            ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5c6", "d7c6"]
        ):
            move = chess.Move.from_uci(uci)
            rows.append(
                SimpleNamespace(
                    session_id="s1",
                    move_number=ply // 2 + 1,
                    color="white" if board.turn == chess.WHITE else "black",
                    move_san=board.san(move),
                    fen_before=board.fen(),
                    fen_after=(board.push(move), board.fen())[1],
                    eval_delta=10,
                    eval_cp=None,
                    best_move_eval_cp=None,
                    session_ts="2026-01-01 10:00:00",
                )
            )

        derived = opening_evidence._derive_session(rows)
        assert not derived.excluded
        assert derived.moves  # the line is inside the opening interval
        for cm in derived.moves:
            assert cm.uci == opening_evidence._uci_from_san(
                cm.norm_before, cm.move_san
            ), f"cached uci diverged at {cm.move_san} from {cm.norm_before}"

    def test_book_exit_and_extension_edges_keep_their_uci(
        self, db_session, branching_graph
    ):
        """The cached uci feeds the two former ``_uci_from_san`` call sites in
        passes 2/3 (book exit + observed continuation), so edges must still key on
        the real move."""
        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_line(db_session, sid, ["e2e4", "e7e5", "g1f3", "b8c6"])
        ov = overlay_evidence(db_session, 1, "white", branching_graph)

        assert ov.edges
        for (parent_fen, _child), edge in ov.edges.items():
            assert (
                chess.Move.from_uci(edge.uci)
                in chess.Board(parent_fen + " 0 1").legal_moves
            ), f"edge uci {edge.uci} is not legal from {parent_fen}"


# Per-column mutations that must all bust the replay cache. Each acts on a
# one-move session so no (session_id, move_number, color) uniqueness collision is
# possible, and each is a REAL change to a column the overlay consumes.
_COLUMN_MUTATIONS = [
    ("move_number", "move_number = 5"),
    ("color", "color = 'black'"),
    ("fen_before", f"fen_before = '{RAW_E4E5}'"),
    ("fen_after", f"fen_after = '{RAW_ROOT}'"),
    ("move_san", "move_san = 'd4'"),
    ("eval_delta", "eval_delta = 999"),
    ("eval_cp", "eval_cp = 42"),
    ("best_move_eval_cp", "best_move_eval_cp = 77"),
]


class TestReplayDigestColumnCoverage:
    """The replay-cache digest is computed by the DATABASE while the freshness
    digest keeps ``_sm_line``, so "same rows → same line" no longer holds by
    construction across the two. These tests are the replacement guard: every
    consumed column must invalidate BOTH."""

    def test_mutation_list_covers_every_digested_column(self):
        assert {c for c, _ in _COLUMN_MUTATIONS} == set(
            opening_evidence._SESSION_DIGEST_COLUMNS
        ), (
            "add the new _SESSION_DIGEST_COLUMNS entry to _COLUMN_MUTATIONS (and to "
            "_sm_line) — an undigested column serves a stale overlay"
        )

    @pytest.mark.parametrize("column,set_clause", _COLUMN_MUTATIONS)
    def test_every_consumed_column_busts_the_replay_cache(
        self, db_session, branching_graph, monkeypatch, column, set_clause
    ):
        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(
            db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=10
        )
        overlay_evidence(db_session, 1, "white", branching_graph)  # warm
        before_digest = raw_evidence_inputs_digest(db_session, 1, "white")

        db_session.execute(
            text(f"UPDATE session_moves SET {set_clause} WHERE session_id = :sid"),
            {"sid": sid},
        )
        db_session.commit()

        counter = _ReplayCounter(opening_evidence.reconstruct_board_sequence)
        monkeypatch.setattr(opening_evidence, "reconstruct_board_sequence", counter)
        incr = overlay_evidence(db_session, 1, "white", branching_graph)
        assert counter.count == 1, f"{column} change did not bust the replay cache"

        # The freshness digest must see it too, or a batch would stay armed.
        assert raw_evidence_inputs_digest(db_session, 1, "white") != before_digest, (
            f"{column} is digested by the replay cache but not by _sm_line"
        )

        reset_session_evidence_cache()
        _assert_overlay_equal(incr, overlay_evidence(db_session, 1, "white", branching_graph))

    def test_started_at_change_busts_the_replay_cache(
        self, db_session, branching_graph, monkeypatch
    ):
        """``session_ts`` reaches the overlay as ``NodeEvidence.last_live_at``, so
        it is digested too — from ``gs``, not ``sm``."""
        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_line(db_session, sid, ["e2e4", "e7e5"])
        ov1 = overlay_evidence(db_session, 1, "white", branching_graph)

        db_session.execute(
            text("UPDATE game_sessions SET started_at = :ts WHERE id = :sid"),
            {"ts": "2026-05-05 12:00:00", "sid": sid},
        )
        db_session.commit()

        counter = _ReplayCounter(opening_evidence.reconstruct_board_sequence)
        monkeypatch.setattr(opening_evidence, "reconstruct_board_sequence", counter)
        ov2 = overlay_evidence(db_session, 1, "white", branching_graph)
        assert counter.count == 1
        assert ov2.nodes[FEN_ROOT].last_live_at != ov1.nodes[FEN_ROOT].last_live_at

        reset_session_evidence_cache()
        _assert_overlay_equal(ov2, overlay_evidence(db_session, 1, "white", branching_graph))

    def test_sql_and_python_digest_bodies_agree(self, db_session, branching_graph):
        """The probe's SQL body and ``_session_digest_body`` must be byte-equal:
        if they drift, nothing is WRONG but every build re-replays from scratch.
        Asserted directly so that regression names itself instead of showing up as
        a mysterious replay count."""
        _insert_user(db_session)
        sid = _insert_session(db_session)
        # Both colors at the same move_number, so the explicit color rank in the
        # ORDER BY is actually exercised.
        _insert_line(db_session, sid, ["e2e4", "e7e5", "g1f3", "b8c6"])
        db_session.execute(
            text("UPDATE session_moves SET eval_cp = NULL WHERE move_number = 2"),
            {},
        )
        db_session.commit()

        probe = db_session.execute(
            text(opening_evidence._probe_sql("sqlite")),
            {"user_id": 1, "player_color": "white"},
        ).fetchall()
        assert len(probe) == 1

        rows = db_session.execute(
            text(opening_evidence._SESSION_ROWS_SQL).bindparams(
                bindparam("sids", expanding=True)
            ),
            {"user_id": 1, "player_color": "white", "sids": [sid]},
        ).fetchall()

        assert opening_evidence._session_digest_body(rows) == probe[0].body
        assert len(rows) == probe[0].row_count
        assert opening_evidence._session_digest(
            len(rows), opening_evidence._session_digest_body(rows), rows[0].session_ts
        ) == opening_evidence._session_digest(
            probe[0].row_count, probe[0].body, probe[0].session_ts
        )


class TestProbePayloadFold:
    """The probe must return a FIXED-SIZE row per session, not the raw aggregate.

    Returning the aggregate verbatim would cut round-trips and per-row python
    without cutting bytes — every FEN of every historical ply would still cross
    the wire on a warm build (~3 KB per session). Only PostgreSQL can prove the
    md5 pair agrees (see ``test_opening_evidence_digest_pg.py``); what IS provable
    here is that the fold is wired in at all, which is what a refactor would drop.
    """

    def test_postgres_probe_wraps_the_aggregate_and_changes_nothing_else(self):
        agg = opening_evidence._SESSION_DIGEST_AGG_SQL
        pg = opening_evidence._probe_sql("postgresql")
        portable = opening_evidence._probe_sql("sqlite")

        assert f"md5({agg})" in pg, "PostgreSQL probe stopped folding server-side"
        assert agg in portable and "md5(" not in portable
        # The two statements may differ ONLY by the fold: same filters, same
        # GROUP BY, same explicit color rank. Anything else is a real divergence.
        assert pg.replace(f"md5({agg})", agg) == portable

    def test_every_registered_fold_is_a_usable_pair(self):
        for dialect, (template, fn) in opening_evidence._BODY_FOLDS.items():
            assert "{body}" in template, dialect
            assert fn("some body") != "", dialect
        # md5 folds any body to 32 hex chars — this is the payload claim.
        pg_fold = opening_evidence._body_fold("postgresql")[1]
        assert len(pg_fold("x")) == 32
        assert len(pg_fold("y" * 100_000)) == 32

    def test_unknown_dialect_falls_back_to_the_identity_pair(self):
        """An unrecognised dialect must stay CORRECT (identity on both sides),
        merely un-optimised — never silently mismatched."""
        assert opening_evidence._body_fold("mysql") is opening_evidence._IDENTITY_FOLD
        assert opening_evidence._body_fold("sqlite") is opening_evidence._IDENTITY_FOLD
        assert opening_evidence._body_fold("sqlite")[1]("abc") == "abc"


class _RowFetchProxy:
    """Delegating DB proxy that intercepts the scoped row fetch of
    ``_build_move_rows`` (STEP 3) so the probe/fetch gap can be driven."""

    def __init__(self, db, on_rows=None, drop_rows=False):
        self._db = db
        self._on_rows = on_rows
        self._drop_rows = drop_rows
        self.fired = 0

    def execute(self, stmt, *args, **kwargs):
        if "sm.session_id IN" in str(stmt):
            self.fired += 1
            if self._on_rows is not None:
                self._on_rows()
            if self._drop_rows:
                return SimpleNamespace(fetchall=lambda: [])
        return self._db.execute(stmt, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._db, name)


class TestProbeFetchRace:
    """The digest probe and the scoped row fetch are separate statements, so a
    session can change in the gap. The entry must be keyed to the rows actually
    replayed, never to the probe's (possibly older) digest."""

    def test_store_key_describes_the_rows_replayed(
        self, db_session, branching_graph, monkeypatch
    ):
        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(
            db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=10
        )

        def _mutate_between():
            db_session.execute(
                text("UPDATE session_moves SET eval_delta = 555 WHERE session_id = :s"),
                {"s": sid},
            )
            db_session.commit()

        proxy = _RowFetchProxy(db_session, on_rows=_mutate_between)
        torn = overlay_evidence(proxy, 1, "white", branching_graph)
        assert proxy.fired == 1

        # The overlay reflects the POST-mutation rows (they are what got replayed),
        # and the cache entry is keyed to them — so an ordinary rebuild now hits
        # without replaying. Keyed to the probe's stale digest instead, this build
        # would re-replay, and a later build could be served the wrong content.
        counter = _ReplayCounter(opening_evidence.reconstruct_board_sequence)
        monkeypatch.setattr(opening_evidence, "reconstruct_board_sequence", counter)
        after = overlay_evidence(db_session, 1, "white", branching_graph)
        assert counter.count == 0, "stored key did not match the replayed rows"
        _assert_overlay_equal(torn, after)

        probe = db_session.execute(
            text(opening_evidence._probe_sql("sqlite")),
            {"user_id": 1, "player_color": "white"},
        ).one()
        expected_fetched_hash = opening_evidence._session_digest(
            probe.row_count,
            probe.body,
            probe.session_ts,
        )
        persisted_hash = db_session.execute(
            text(
                "SELECT content_hash FROM opening_session_replay_cache "
                "WHERE session_id = :sid"
            ),
            {"sid": sid},
        ).scalar_one()
        assert persisted_hash == expected_fetched_hash

        counter.count = 0
        reset_session_evidence_cache()
        restarted = overlay_evidence(db_session, 1, "white", branching_graph)
        assert counter.count == 0, "L2 replay key did not describe fetched rows"
        assert restarted.replay_cache_stats.l2_hits == 1
        _assert_overlay_equal(after, restarted)

    def test_probed_session_absent_from_fetch_is_skipped(
        self, db_session, branching_graph
    ):
        """A session that goes ineligible between probe and fetch contributes
        nothing rather than raising."""
        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_line(db_session, sid, ["e2e4", "e7e5"])

        proxy = _RowFetchProxy(db_session, drop_rows=True)
        ov = overlay_evidence(proxy, 1, "white", branching_graph)
        assert proxy.fired == 1
        assert ov.nodes == {}
        assert ov.edges == {}
        assert ov.excluded_sessions == 0

        # Nothing was cached for it, so the next honest build derives it normally.
        full = overlay_evidence(db_session, 1, "white", branching_graph)
        assert full.nodes[FEN_ROOT].live_attempts == 1

    def test_warm_build_fetches_no_raw_rows(self, db_session, branching_graph):
        """The point of the probe: a fully warm build issues the scoped row fetch
        zero times."""
        _insert_user(db_session)
        for _ in range(3):
            sid = _insert_session(db_session)
            _insert_line(db_session, sid, ["e2e4", "e7e5", "g1f3", "b8c6"])
        overlay_evidence(db_session, 1, "white", branching_graph)  # cold

        proxy = _RowFetchProxy(db_session)
        overlay_evidence(proxy, 1, "white", branching_graph)
        assert proxy.fired == 0


# --------------------------------------------------------------------------- #
# g-v21l: capability + submitter scoping and ongoing association freshness
# --------------------------------------------------------------------------- #
class TestOpeningEvidenceCapabilityScoping:
    """The opening fallback resolves BOTH grains under OPENING_EVIDENCE with the
    batch's ``user_id`` as viewer, and pairs them through the shared
    coherent-tuple resolver rather than an equal-strength check alone."""

    @staticmethod
    def _browser_row(db, fen, move_uci, move_san, *, played_eval, best_eval,
                     best_move_uci, best_line_uci, classification="good",
                     eval_delta=None):
        from app.analysis_profiles import (
            BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
            stamp_profile_full,
        )

        stamped = stamp_profile_full(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
        return _insert_analysis_cache(
            db, fen, move_uci, move_san,
            played_eval=played_eval, best_eval=best_eval,
            best_move_uci=best_move_uci, best_move_san=best_move_uci,
            best_line_uci=best_line_uci,
            classification=classification,
            eval_delta=max(best_eval - played_eval, 0) if eval_delta is None else eval_delta,
            source="analysis",
            analysis_profile_id=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
            evidence_contract_id="resolver-complete-v2",
            identity=stamped,
        )

    @staticmethod
    def _associate(db, row, user_id):
        from app.models import AnalysisCacheSubmission

        _insert_user(db, user_id)
        db.add(AnalysisCacheSubmission(analysis_cache_id=row.id, user_id=user_id))
        db.commit()

    def test_a_coherent_same_user_browser_pair_upgrades(self, db_session, branching_graph):
        from app.opening_quality import (
            SOURCE_ANALYSIS_CACHE,
            quality_from_win_chance_loss,
        )

        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=40)
        # One coherent combined row: the played e4 is NOT the best move (d4 is),
        # and its stored label follows from the two scores.
        row = self._browser_row(
            db_session, RAW_ROOT, "e2e4", "e4",
            played_eval=-30, best_eval=20,
            best_move_uci="d2d4", best_line_uci="d2d4 d7d5",
        )
        self._associate(db_session, row, 1)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.quality_sum == pytest.approx(quality_from_win_chance_loss(20, -30))
        assert ov.source_counts[SOURCE_ANALYSIS_CACHE] == 1

    def test_the_same_pair_does_not_upgrade_for_another_user(
        self, db_session, branching_graph
    ):
        from app.opening_quality import SOURCE_EVAL_DELTA, quality_from_eval_delta

        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=40)
        row = self._browser_row(
            db_session, RAW_ROOT, "e2e4", "e4",
            played_eval=-30, best_eval=20,
            best_move_uci="d2d4", best_line_uci="d2d4 d7d5",
        )
        self._associate(db_session, row, 999)  # someone else's submission

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.quality_sum == pytest.approx(quality_from_eval_delta(40))
        assert ov.source_counts[SOURCE_EVAL_DELTA] == 1

    def test_equal_profile_siblings_that_disagree_do_not_upgrade(
        self, db_session, branching_graph
    ):
        """The disagreement regression: an equal-profile sibling exact-move row
        whose best_move_uci / best_eval contradict the position winner leaves the
        move at its eval_delta quality. Before g-v21l the equal-strength check
        alone would have upgraded it."""
        from app.opening_quality import SOURCE_EVAL_DELTA, quality_from_eval_delta

        _insert_user(db_session)
        sid = _insert_session(db_session)
        _insert_move(db_session, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=40)
        # Position winner (lowest id at this normalized FEN): internally coherent.
        winner = self._browser_row(
            db_session, RAW_ROOT, "d2d4", "d4",
            played_eval=35, best_eval=35,
            best_move_uci="d2d4", best_line_uci="d2d4 d7d5",
            classification="best", eval_delta=0,
        )
        # The exact played-move row is ALSO internally coherent — but it asserts a
        # DIFFERENT best move and best eval than the position winner does.
        played = self._browser_row(
            db_session, RAW_ROOT, "e2e4", "e4",
            played_eval=20, best_eval=20,
            best_move_uci="e2e4", best_line_uci="e2e4 e7e5",
            classification="best", eval_delta=0,
        )
        self._associate(db_session, winner, 1)
        self._associate(db_session, played, 1)

        ov = overlay_evidence(db_session, 1, "white", branching_graph)
        node = ov.nodes[FEN_ROOT]
        assert node.quality_sum == pytest.approx(quality_from_eval_delta(40))
        assert ov.source_counts[SOURCE_EVAL_DELTA] == 1


class TestAssociationFreshness:
    """Associations gate the OPENING_EVIDENCE trust filter, so they must join the
    evidence digest — a one-time version bump cannot invalidate anything that
    changes AFTER it lands."""

    def _seed_candidate(self, db):
        _insert_user(db)
        sid = _insert_session(db)
        _insert_move(db, sid, 1, "white", "e4", RAW_ROOT, RAW_E4, eval_delta=40)
        return TestOpeningEvidenceCapabilityScoping._browser_row(
            db, RAW_ROOT, "e2e4", "e4",
            played_eval=-30, best_eval=20,
            best_move_uci="d2d4", best_line_uci="d2d4 d7d5",
        )

    def test_an_association_only_write_changes_both_digests(self, db_session):
        """Mechanism-level twin: insert the association row DIRECTLY, isolating the
        digest change from the writer."""
        from app.models import AnalysisCacheSubmission

        row = self._seed_candidate(db_session)
        snapshot = opening_evidence.raw_evidence_inputs_snapshot(db_session, 1, "white")

        _insert_user(db_session, 2)
        db_session.add(
            AnalysisCacheSubmission(analysis_cache_id=row.id, user_id=2)
        )
        db_session.commit()

        after = opening_evidence.raw_evidence_inputs_snapshot(db_session, 1, "white")
        assert after.digest != snapshot.digest
        assert after.scoped_shared_digest != snapshot.scoped_shared_digest

    def test_a_claim_through_the_writer_changes_both_digests(self, db_session):
        """The REAL owner-only mutation path: a second submitter posts an identical
        tuple, the decision is idempotent, every evidence column stays
        byte-identical, and only an association row is inserted."""
        from app.analysis_cache_policy import Reason
        from app.analysis_cache_repo import write_analysis_cache_rows
        from app.analysis_profiles import (
            BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
            stamp_profile_full,
        )

        row = self._seed_candidate(db_session)
        columns = {
            f: getattr(row, f)
            for f in ("played_eval", "best_eval", "eval_delta", "classification")
        }
        snapshot = opening_evidence.raw_evidence_inputs_snapshot(db_session, 1, "white")

        _insert_user(db_session, 2)
        db_session.commit()
        results = write_analysis_cache_rows(
            db_session,
            [{
                "fen_before": RAW_ROOT, "move_uci": "e2e4", "move_san": "e4",
                "best_move_uci": "d2d4", "best_move_san": "d2d4",
                "best_line_uci": "d2d4 d7d5",
                "played_eval": -30, "best_eval": 20, "eval_delta": 50,
                "classification": "good", "source": "analysis",
                "analysis_profile_id": BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
                "evidence_contract_id": "resolver-complete-v2",
                **stamp_profile_full(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID),
            }],
            submitter_user_id=2,
        )
        assert [r for _, r in results] == [Reason.SAME_PROFILE_IDEMPOTENT]

        db_session.expire_all()
        refreshed = db_session.get(AnalysisCache, row.id)
        assert {f: getattr(refreshed, f) for f in columns} == columns

        after = opening_evidence.raw_evidence_inputs_snapshot(db_session, 1, "white")
        assert after.digest != snapshot.digest
        assert after.scoped_shared_digest != snapshot.scoped_shared_digest

    def test_the_association_set_is_hashed_in_ac_and_acp_but_not_pa(self, db_session):
        from app.models import AnalysisCacheSubmission

        row = self._seed_candidate(db_session)
        _insert_position_analysis(
            db_session, RAW_ROOT, best_move_uci="e2e4", best_line_uci="e2e4 e7e5",
            best_eval=20,
        )
        _insert_user(db_session, 5)
        _insert_user(db_session, 3)
        db_session.add_all([
            AnalysisCacheSubmission(analysis_cache_id=row.id, user_id=5),
            AnalysisCacheSubmission(analysis_cache_id=row.id, user_id=3),
        ])
        db_session.commit()

        lines = opening_evidence._shared_evidence_lines(
            db_session, [RAW_ROOT], [normalize_fen(RAW_ROOT)]
        )
        ac = [ln for ln in lines if ln.startswith("AC|")]
        acp = [ln for ln in lines if ln.startswith("ACP|")]
        pa = [ln for ln in lines if ln.startswith("PA|")]
        # A sorted, deterministically formatted user-id list on both cache lines.
        assert all(ln.endswith("|3,5") for ln in ac), ac
        assert all(ln.endswith("|3,5") for ln in acp), acp
        # position_analysis is canonical-only storage: no association half.
        assert pa and not any(ln.endswith("|3,5") for ln in pa)

    def test_shared_evidence_lines_stay_user_independent(self, db_session):
        """Hashing the FULL set (not one viewer's membership) is what makes
        "scoped digest == full digest's shared slice" hold by construction."""
        from app.models import AnalysisCacheSubmission

        row = self._seed_candidate(db_session)
        _insert_user(db_session, 2)
        db_session.add(AnalysisCacheSubmission(analysis_cache_id=row.id, user_id=2))
        db_session.commit()

        norm = normalize_fen(RAW_ROOT)
        first = opening_evidence._shared_evidence_lines(db_session, [RAW_ROOT], [norm])
        second = opening_evidence._shared_evidence_lines(db_session, [RAW_ROOT], [norm])
        assert first == second  # no user_id argument exists to vary
        snapshot = opening_evidence.raw_evidence_inputs_snapshot(db_session, 1, "white")
        assert snapshot.scoped_shared_digest == opening_evidence.shared_scope_digest(
            db_session, snapshot.shared_raw_fens, snapshot.shared_norm_fens
        )

    def test_overlay_scope_and_digest_hash_the_same_move_row_ids(
        self, db_session, branching_graph, monkeypatch
    ):
        """The overlay viewer query and scoped digest must select one identical
        raw-FEN row set, including multiple rows at the same position."""
        first = self._seed_candidate(db_session)
        second = TestOpeningEvidenceCapabilityScoping._browser_row(
            db_session,
            RAW_ROOT,
            "d2d4",
            "d4",
            played_eval=10,
            best_eval=10,
            best_move_uci="d2d4",
            best_line_uci="d2d4 d7d5",
            classification="best",
            eval_delta=0,
        )
        db_session.commit()

        seen: list[tuple[int, ...]] = []
        real = opening_evidence.viewer_associated_ids

        def recording_viewer_ids(db, user_id, row_ids):
            seen.append(tuple(sorted(int(row_id) for row_id in row_ids)))
            return real(db, user_id, row_ids)

        monkeypatch.setattr(
            opening_evidence, "viewer_associated_ids", recording_viewer_ids
        )
        overlay = overlay_evidence(db_session, 1, "white", branching_graph)
        snapshot = opening_evidence.shared_scope_snapshot(
            db_session,
            overlay.shared_scope.raw_fens,
            overlay.shared_scope.norm_fens,
        )

        expected = tuple(sorted((first.id, second.id)))
        assert seen == [expected]
        assert overlay.shared_scope.move_row_ids == expected
        assert snapshot.move_row_ids == expected
