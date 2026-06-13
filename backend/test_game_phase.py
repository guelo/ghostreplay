"""Tests for the exact Lichess phase-divider port in app.game_phase.

Fixtures isolate each middlegame trigger, probe values immediately around every
threshold, and exercise the exact middle/end control flow (end scan, no-middle
games, and collapsed middle/end markers) so the port can be trusted to match
``Divider.scala`` semantics.
"""

import chess
import pytest

from app.game_phase import (
    ContinuityError,
    Division,
    backrank_sparse,
    divide,
    is_opening_premove,
    majors_and_minors,
    mixedness,
    reconstruct_board_sequence,
)
from app.game_phase import _score  # noqa: PLC2701 - boundary unit coverage
from app.fen import normalize_fen


def board(fen: str) -> chess.Board:
    return chess.Board(fen)


def _push(fen: str, san: str) -> str:
    b = chess.Board(fen)
    b.push_san(san)
    return b.fen()


# Isolated-trigger fixtures: each board fires exactly one middlegame predicate.
START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"  # no trigger
MM10 = "1qrbk1r1/8/8/8/8/8/8/R1BQKBNR w - - 0 1"  # majors_and_minors == 10
MM11 = "1qrbk1r1/8/8/8/8/8/8/RNBQKBNR w - - 0 1"  # majors_and_minors == 11
BACKRANK = "rnbqkbnr/pppppppp/8/8/8/RNB2BNR/PPPPPPPP/3QK3 w - - 0 1"  # sparse only
MIX_HI = "rnbqkbnr/2pppp2/2PPPP2/2pppp2/2PPPP2/2pppp2/8/RNBQKBNR w - - 0 1"  # mix 156
MIX_LO = "rnbqkbnr/8/2pppp2/2PPPP2/2pppp2/2PPPP2/8/RNBQKBNR w - - 0 1"  # mix 125
MM7 = "1qr1k1r1/8/8/8/8/8/8/1QR1K1RN w - - 0 1"  # endgame boundary: not <= 6
MM6 = "1qr1k1r1/8/8/8/8/8/8/1QR1K1R1 w - - 0 1"  # endgame: majors_and_minors == 6


class TestMajorsAndMinors:
    def test_start_position(self):
        assert majors_and_minors(board(START)) == 14

    def test_boundary_values(self):
        assert majors_and_minors(board(MM10)) == 10
        assert majors_and_minors(board(MM11)) == 11
        assert majors_and_minors(board(MM6)) == 6
        assert majors_and_minors(board(MM7)) == 7

    def test_excludes_kings_and_pawns(self):
        # Only two kings and a wall of pawns -> zero majors/minors.
        assert majors_and_minors(board("4k3/pppppppp/8/8/8/8/PPPPPPPP/4K3 w - - 0 1")) == 0


class TestBackrankSparse:
    def test_full_back_ranks_not_sparse(self):
        assert backrank_sparse(board(START)) is False

    def test_white_back_rank_sparse(self):
        # White back rank holds only Q+K (2 < 4); black back rank is full.
        assert backrank_sparse(board(BACKRANK)) is True

    def test_boundary_exactly_four_not_sparse(self):
        # Exactly four white pieces on rank 1 is not sparse (strict < 4).
        assert backrank_sparse(board("rnbqkbnr/pppppppp/8/8/8/2N2N2/PPPPPPPP/R2QK2R w KQkq - 0 1")) is False


class TestMixedness:
    def test_start_position_low(self):
        assert mixedness(board(START)) == 0

    def test_real_positions_span_threshold(self):
        # MIX_LO (125) is below the > 150 cutoff; MIX_HI (156) is above.
        assert mixedness(board(MIX_LO)) == 125
        assert mixedness(board(MIX_HI)) == 156

    def test_strict_150_151_boundary(self, monkeypatch):
        # Exercise the exact `mixedness(board) > 150` comparison: 150 must not
        # trigger middlegame, 151 must. We pin the mixedness value (a 150/151
        # FEN is impractical to hand-build) and use a board that fires no other
        # trigger (START: majors 14, not backrank-sparse).
        import app.game_phase as gp

        monkeypatch.setattr(gp, "mixedness", lambda b: 150)
        assert divide([board(START)]).middle is None

        monkeypatch.setattr(gp, "mixedness", lambda b: 151)
        assert divide([board(START)]).middle == 0


class TestScoreLookup:
    """Boundary cases of the per-region score(y)(white, black) lookup."""

    @pytest.mark.parametrize(
        "y,white,black,expected",
        [
            (8, 1, 0, 1),  # (1,0): 1 + (8 - y) collapses to 1 at the back rank
            (1, 1, 0, 8),  # (1,0): rises toward the first rank
            (2, 2, 0, 0),  # (2,0): y > 2 false -> 0 at the boundary
            (3, 2, 0, 3),  # (2,0): y > 2 true -> 2 + (y - 2)
            (1, 3, 0, 0),  # (3,0): y > 1 false -> 0
            (2, 3, 0, 4),  # (3,0): y > 1 true -> 3 + (y - 1)
            (4, 1, 1, 5),  # (1,1): 5 + |4 - y| minimal at y == 4
            (1, 1, 1, 8),  # (1,1): 5 + |4 - 1|
            (6, 0, 2, 0),  # (0,2): y < 6 false -> 0
            (5, 0, 2, 3),  # (0,2): y < 6 true -> 2 + (6 - y)
            (7, 0, 3, 0),  # (0,3): y < 7 false -> 0
            (7, 0, 4, 0),  # (0,4): y < 7 false -> 0
            (2, 2, 2, 7),  # (2,2): constant 7
            (4, 3, 2, 0),  # unmatched pair -> 0 (case _)
        ],
    )
    def test_boundaries(self, y, white, black, expected):
        assert _score(y, white, black) == expected


class TestMiddlegameTriggersIndependent:
    """Each middlegame predicate fires on its own and the others stay quiet."""

    def test_majors_trigger_only(self):
        b = board(MM10)
        assert majors_and_minors(b) <= 10
        assert backrank_sparse(b) is False
        assert mixedness(b) <= 150
        assert divide([board(START), b]).middle == 1

    def test_majors_boundary_eleven_does_not_trigger(self):
        b = board(MM11)
        assert backrank_sparse(b) is False and mixedness(b) <= 150
        assert divide([board(START), b]).middle is None

    def test_backrank_trigger_only(self):
        b = board(BACKRANK)
        assert majors_and_minors(b) > 10
        assert mixedness(b) <= 150
        assert backrank_sparse(b) is True
        assert divide([board(START), b]).middle == 1

    def test_mixedness_trigger_only(self):
        b = board(MIX_HI)
        assert majors_and_minors(b) > 10
        assert backrank_sparse(b) is False
        assert mixedness(b) > 150
        assert divide([board(START), b]).middle == 1

    def test_mixedness_below_threshold_does_not_trigger(self):
        assert divide([board(START), board(MIX_LO)]).middle is None


class TestDivideControlFlow:
    def test_no_middle_for_pure_opening_sequence(self):
        div = divide([board(START), board(START)])
        assert div == Division(middle=None, end=None, plies=2)
        assert div.opening_size == 2  # whole line stays opening

    def test_middle_and_later_end(self):
        # Middle via backrank at index 1 (majors still high); endgame (<=6) at 3.
        div = divide([board(START), board(BACKRANK), board(MM7), board(MM6)])
        assert div.middle == 1
        assert div.end == 3
        assert div.plies == 4

    def test_end_scan_restarts_at_board_zero(self, monkeypatch):
        # Middle is triggered by the backrank predicate at index 2 while majors
        # stay above 6, so the end scan runs. We instrument majors_and_minors to
        # record call order and prove the end scan revisits board 0 rather than
        # resuming from the middle index. (A from-middle scan would never call
        # majors_and_minors on board 0 a second time.)
        import app.game_phase as gp

        seq = [board(START), board(START), board(BACKRANK), board(MM7), board(MM6)]
        real = gp.majors_and_minors
        calls: list[int] = []

        def spy(b):
            calls.append(id(b))
            return real(b)

        monkeypatch.setattr(gp, "majors_and_minors", spy)
        div = divide(seq)

        assert div.middle == 2
        assert div.end == 4
        b0, b2 = id(seq[0]), id(seq[2])
        # The middle search calls majors_and_minors on boards 0,1,2 then stops.
        # The end scan must then call it on board 0 again -> board 0 appears
        # after board 2 in the call log.
        assert calls.index(b2) < len(calls) - 1 - calls[::-1].index(b0)
        assert calls.count(b0) == 2  # once in middle search, once in end scan

    def test_collapsed_middle_and_end_drops_middle(self):
        # The first middlegame board is itself the first majors<=6 board, so the
        # end scan (from board 0) returns the same index. middle < end fails and
        # the middle marker is discarded, leaving only an end marker.
        div = divide([board(START), board(MM6)])
        assert div.middle is None
        assert div.end == 1
        assert div.opening_size == 2  # no middle -> full line is opening

    def test_empty_sequence(self):
        assert divide([]) == Division(middle=None, end=None, plies=0)


class TestReconstructBoardSequence:
    def _moves_from_sans(self, sans):
        """Build (fen_before, fen_after, san) triples by playing SANs from start."""
        b = chess.Board()
        moves = []
        for san in sans:
            before = b.fen()
            b.push_san(san)
            moves.append((before, b.fen(), san))
        return moves

    def test_pre_move_boards_match_upstream_count(self):
        moves = self._moves_from_sans(["e4", "e5", "Nf3", "Nc6"])
        boards = reconstruct_board_sequence(moves)
        # One pre-move board per ply (upstream chronoMoves.map(_.before.board)).
        assert len(boards) == len(moves)
        assert boards[0].fen() == chess.Board().fen()
        # The last board is the position *before* the final move, not after it.
        assert boards[-1].board_fen() == chess.Board(moves[-1][0]).board_fen()

    def test_empty_moves(self):
        assert reconstruct_board_sequence([]) == []

    def test_discontinuity_raises(self):
        moves = self._moves_from_sans(["e4", "e5"])
        # Corrupt the second move's pre-move position so it no longer matches.
        moves[1] = (
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            moves[1][1],
            moves[1][2],
        )
        with pytest.raises(ContinuityError):
            reconstruct_board_sequence(moves)

    def test_board_jump_within_a_row_raises(self):
        # The before/after chain is continuous, but the stored SAN does not
        # actually transform fen_before into fen_after (an arbitrary board swap).
        moves = self._moves_from_sans(["e4", "e5"])
        good_before, _good_after, san = moves[1]
        forged_after = chess.Board(good_before)
        forged_after.push_san("Nf6")  # legal, but not the stored "e5"
        moves[1] = (good_before, forged_after.fen(), san)
        with pytest.raises(ContinuityError):
            reconstruct_board_sequence(moves)

    def test_illegal_san_raises(self):
        start = chess.Board().fen()
        after = chess.Board()
        after.push_san("e4")
        with pytest.raises(ContinuityError):
            reconstruct_board_sequence([(start, after.fen(), "Qd5")])

    def test_missing_field_raises(self):
        with pytest.raises(ContinuityError):
            reconstruct_board_sequence([(None, "x", "e4")])

    def test_invalid_fen_raises_continuity_error(self):
        # Malformed FENs must surface as ContinuityError (the documented
        # contract), not a raw ValueError from chess.Board.
        with pytest.raises(ContinuityError):
            reconstruct_board_sequence([("not a fen", "also bad", "e4")])

    def test_equivalent_ep_canonicalization_is_continuous(self):
        # Row 0 stores its fen_after with an e3 EP marker that has no legal
        # capture; row 1 stores the project-standard "-" for the same position.
        # Canonical identity must treat these as continuous, not a jump.
        after_with_ep = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
        after_with_dash = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
        # The raw four-field FENs differ (e3 vs -) but normalize to the same key.
        assert after_with_ep.split(" ")[:4] != after_with_dash.split(" ")[:4]
        assert normalize_fen(after_with_ep) == normalize_fen(after_with_dash)

        moves = [
            (chess.Board().fen(), after_with_ep, "e4"),
            (after_with_dash, _push(after_with_dash, "e5"), "e5"),
        ]
        boards = reconstruct_board_sequence(moves)
        assert len(boards) == 2


class TestOpeningInclusion:
    def test_includes_move_entering_first_middlegame_board(self):
        div = Division(middle=3, end=5, plies=8)
        # Pre-move board 2 -> board 3 (first middlegame) is the final opening move.
        assert is_opening_premove(div, 2) is True
        # Pre-move board already at/after middle is excluded.
        assert is_opening_premove(div, 3) is False
        assert is_opening_premove(div, 4) is False

    def test_no_middle_includes_whole_line(self):
        div = Division(middle=None, end=None, plies=6)
        assert is_opening_premove(div, 0) is True
        assert is_opening_premove(div, 5) is True
