"""Live session-accuracy surface.

This module is the supported import point for whole-game accuracy. Application
code imports the public names from here (``from app.accuracy import ...``); the
actual algorithm lives in the frozen :mod:`app.accuracy_v1` snapshot, which this
module re-exports.

The indirection lets a future accuracy **v2** replace what live code computes by
swapping the re-export here, while every historical session that was scored by v1
stays reproducible: migrations and the frozen-golden tests import
:mod:`app.accuracy_v1` directly and never through this re-export. See
``docs/session-accuracy-versioning.md`` for the versioning contract.

Only the supported public names are re-exported. Private helpers and constants
(``_white_relative_cp``, ``_MATE_CP``, the Lichess coefficients, …) are *not*
part of this surface; tests that need them import from :mod:`app.accuracy_v1`.
"""

from __future__ import annotations

from app.accuracy_v1 import (
    AccuracyMove,
    accuracy_from_win_percents,
    compute_game_accuracy,
    expected_total_moves_from_pgn,
    win_percent_from_cp,
)

# Algorithm version stamped onto session rows Release A writes. Bump ONLY when a
# new frozen module (accuracy_v2) becomes the live surface.
ACCURACY_ALGO_VERSION = 1

# The exact python-chess version accuracy v1 was captured and validated against
# (verified on 2026-07-11 against the deployed Railway artifact's /opt/venv). The
# runtime and requirements.txt must both enforce this pin; a drift means the
# frozen goldens are no longer guaranteed and requires an accuracy-v2 review.
CHESS_VERSION_PIN = "1.11.2"

__all__ = [
    "ACCURACY_ALGO_VERSION",
    "CHESS_VERSION_PIN",
    "AccuracyMove",
    "accuracy_from_win_percents",
    "compute_game_accuracy",
    "expected_total_moves_from_pgn",
    "win_percent_from_cp",
]
