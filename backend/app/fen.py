"""FEN normalization and hashing utilities.

These functions ensure positions reached via different move orders
are recognized as identical by normalizing FEN strings before hashing.
"""

from __future__ import annotations

import hashlib

import chess


def normalize_fen(fen: str) -> str:
    """Strip move clocks from FEN for position hashing.

    Keeps fields 1-4 (piece placement, active color, castling rights, en passant).
    Strips fields 5-6 (halfmove clock, fullmove number).

    The en passant square is canonicalized: only kept when an actual
    en passant capture is legal (using python-chess validation).
    """
    parts = fen.split(" ")
    board = chess.Board(fen)
    # Only include EP square if a legal en passant capture exists
    if board.has_legal_en_passant():
        parts[3] = chess.square_name(board.ep_square)
    else:
        parts[3] = "-"
    return " ".join(parts[:4])


def fen_hash(fen: str) -> str:
    """Generate SHA256 hash of normalized FEN."""
    normalized = normalize_fen(fen)
    return hashlib.sha256(normalized.encode()).hexdigest()


def active_color(fen: str) -> str:
    """Return 'white' or 'black' from the FEN active color field."""
    parts = fen.split(" ")
    return "white" if parts[1] == "w" else "black"
