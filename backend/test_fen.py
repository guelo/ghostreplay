"""Tests for FEN normalization and hashing."""

import pytest

from app.fen import active_color, fen_hash, normalize_fen


class TestNormalizeFen:
    """Tests for normalize_fen function."""

    def test_strips_move_clocks(self):
        """normalize_fen should remove halfmove clock and fullmove number."""
        # Position with legal en passant (black pawn on d4 can capture on e3)
        fen = "rnbqkbnr/ppp1pppp/8/8/3pP3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
        result = normalize_fen(fen)
        assert result == "rnbqkbnr/ppp1pppp/8/8/3pP3/8/PPPP1PPP/RNBQKBNR b KQkq e3"

    def test_same_position_different_move_numbers(self):
        """Same position with different move numbers should normalize identically."""
        fen1 = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        fen2 = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 10 50"
        assert normalize_fen(fen1) == normalize_fen(fen2)

    def test_canonicalizes_ep_square_legal_capture(self):
        """EP square kept when en passant capture is legal."""
        # After 1.e4, the e3 square is a legal EP target for Black's d-pawn if present
        # Position: White just played e4, Black has pawn on d4 that can capture e.p.
        fen = "rnbqkbnr/ppp1pppp/8/8/3pP3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
        result = normalize_fen(fen)
        assert "e3" in result

    def test_canonicalizes_ep_square_illegal_capture(self):
        """EP square stripped when no pawn can capture."""
        # After 1.e4 with no enemy pawn to capture, EP square should be stripped
        fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
        result = normalize_fen(fen)
        # python-chess only reports ep_square if capture is legal
        assert result.endswith(" -")

    def test_preserves_castling_rights(self):
        """Castling rights should be preserved."""
        fen = "r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 1"
        result = normalize_fen(fen)
        assert "KQkq" in result

    def test_partial_castling_rights(self):
        """Partial castling rights should be preserved correctly."""
        fen = "r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w Kq - 0 1"
        result = normalize_fen(fen)
        assert "Kq" in result


class TestFenHash:
    """Tests for fen_hash function."""

    def test_returns_64_char_hex_string(self):
        """fen_hash should return a 64-character hex string (SHA256)."""
        fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        result = fen_hash(fen)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_same_position_same_hash(self):
        """Same position should produce the same hash."""
        fen1 = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        fen2 = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 10 50"
        assert fen_hash(fen1) == fen_hash(fen2)

    def test_different_positions_different_hash(self):
        """Different positions should produce different hashes."""
        fen1 = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        fen2 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
        assert fen_hash(fen1) != fen_hash(fen2)

    def test_deterministic(self):
        """Same input should always produce the same hash."""
        fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        assert fen_hash(fen) == fen_hash(fen)


class TestActiveColor:
    """Tests for active_color function."""

    def test_white_to_move(self):
        """Should return 'white' when it's white's turn."""
        fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        assert active_color(fen) == "white"

    def test_black_to_move(self):
        """Should return 'black' when it's black's turn."""
        fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
        assert active_color(fen) == "black"

    def test_midgame_position(self):
        """Should work for midgame positions."""
        fen = "r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4"
        assert active_color(fen) == "white"
