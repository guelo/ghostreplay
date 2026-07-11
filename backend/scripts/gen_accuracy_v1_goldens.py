"""Capture frozen golden outputs for the Release A accuracy freeze (g-accuracy-freeze).

The goldens in ``tests/fixtures/accuracy_v1_goldens.json`` were first captured,
under the production-observed python-chess version (chess==1.11.2, verified
against the deployed Railway artifact on 2026-07-11), from the pre-refactor
``app.accuracy`` implementation (blob
faed4614153c72b6a3170a9b37d5580c769f697c from commit 01f6afd). That
implementation was then moved verbatim into ``app.accuracy_v1``; a byte-for-byte
diff confirmed no change. This tool binds *every* algorithm symbol directly to
the frozen ``app.accuracy_v1`` module — never to the mutable ``app.accuracy``
re-export — so re-running it reproduces the committed fixture exactly (the
capture is idempotent) even after a future v2 becomes the live surface. A v2
freeze gets its own generator importing ``app.accuracy_v2``.

This is a capture tool, not a test, and it must NOT be used to "refresh" v1: the
frozen-fixture test compares ``app.accuracy_v1`` against the captured literals,
never against freshly regenerated output. A future semantic or chess-version
change is accuracy v2 with its own module and goldens.

Usage (from backend/):
    PYTHONPATH=. .venv/bin/python scripts/gen_accuracy_v1_goldens.py
"""

from __future__ import annotations

import json
from pathlib import Path

import chess

# Bind ALL symbols to the frozen v1 module, not the mutable app.accuracy
# re-export: this capture must reproduce v1 forever, independent of whatever the
# live surface points at.
from app.accuracy_v1 import (
    AccuracyMove,
    _white_relative_cp,
    accuracy_from_win_percents,
    compute_game_accuracy,
    expected_total_moves_from_pgn,
    win_percent_from_cp,
)

CHESS_VERSION_PIN = "1.11.2"

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "accuracy_v1_goldens.json"
)


# --- Matrix inputs ---------------------------------------------------------

# win_percent_from_cp: white-relative centipawns spanning the clamp band, the
# initial-eval value, symmetry around zero, and the mate-forced magnitudes.
WIN_PERCENT_CPS = [
    -10000, -1001, -1000, -500, -200, -100, -15, 0, 15, 100, 200, 500, 1000,
    1001, 10000,
]

# accuracy_from_win_percents: (before, after) from the mover's perspective,
# covering improvement (=>100), tiny loss, large loss, and boundary equality.
ACCURACY_PAIRS = [
    (50.0, 50.0),
    (40.0, 55.0),
    (60.0, 55.0),
    (60.0, 20.0),
    (99.0, 1.0),
    (10.0, 90.0),
    (55.5, 55.4),
    (100.0, 0.0),
    (0.0, 0.0),
    (75.25, 60.125),
]

# _white_relative_cp: mate vs cp vs missing, both colors, mate-0 (mover win),
# strictly-negative mate (mover mated), and the mate-wins-over-cp precedence.
WHITE_RELATIVE_MOVES = [
    ("white", None, 0),
    ("black", None, 0),
    ("white", None, 3),
    ("black", None, 3),
    ("white", None, -3),
    ("black", None, -3),
    ("white", 50, None),
    ("black", 50, None),
    ("white", -50, None),
    ("black", -50, None),
    ("white", None, None),
    ("black", None, None),
    ("white", 50, 2),  # mate branch takes precedence over eval_cp
    ("black", -50, -2),
]

_LONG_PGN = (
    "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5 "
    "7. Bb3 d6 8. c3 O-O 9. h3 Nb8 10. d4 Nbd7 11. Nbd2 Bb7 12. Bc2 Re8 *"
)

# expected_total_moves_from_pgn: missing, empty, short, long, non-PGN text, and
# a PGN whose illegal move populates .errors (=> not determinable => None).
EXPECTED_PGNS = [
    ("none", None),
    ("empty", ""),
    ("whitespace", "   "),
    ("short_2ply", "1. e4 e5 *"),
    ("mid_4ply", "1. e4 e5 2. Nf3 Nc6 *"),
    ("long_24ply", _LONG_PGN),
    ("non_pgn_text", "not a pgn ;;;"),
    ("illegal_move", "1. e4 e5 2. Qxd8 *"),
    ("with_headers", '[Event "Test"]\n[Result "*"]\n\n1. d4 d5 2. c4 e6 *'),
]


def _mv(color: str, cp: int | None = None, mate: int | None = None) -> AccuracyMove:
    return AccuracyMove(color=color, eval_cp=cp, eval_mate=mate)


def _white_moves(cps: list[int | None], start: str = "white") -> list[AccuracyMove]:
    """Build alternating-color moves from white-relative cps (test helper parity)."""
    colors = ["white", "black"] if start == "white" else ["black", "white"]
    out: list[AccuracyMove] = []
    for i, cp in enumerate(cps):
        color = colors[i % 2]
        if cp is None:
            out.append(_mv(color, None, None))
            continue
        sign = -1 if color == "black" else 1
        out.append(_mv(color, cp * sign, None))
    return out


def _seesaw(n: int) -> list[int]:
    """Deterministic, strongly NON-monotonic white-relative cps in the +-1000 band.

    Two overlapping integer waves of coprime periods give volatile and calmer
    stretches, so per-move accuracies vary widely and the stddev-based window
    weights differ across windows. That makes the whole-game result depend on
    ``window_size`` — the point of the >=30 / >=90 ply cases, which exercise the
    2->3->..->8 sizing ramp, the leading-window padding, and the upper clamp.
    Pure function of the index, so the capture stays reproducible.
    """
    out: list[int] = []
    for i in range(n):
        a = ((i * 137) % 600) - 300  # fast swing, -300..299
        b = ((i * 53) % 200) - 100  # slower jitter, -100..99
        out.append(a + b)
    return out


def _blocky(n: int) -> list[int]:
    """Deterministic non-monotonic white-relative cps with block structure.

    Alternating ~11-ply calm and volatile stretches at MODERATE amplitude, so
    per-window stddev stays inside the 0.5..12 weight band (unsaturated) and
    depends on how many volatile plies a window spans. That makes the result
    sensitive to the exact window size near the 8-vs-9 boundary, which is what
    freezes the ``window_size`` UPPER CLAMP to 8 (a uniformly volatile seesaw
    saturates every weight at 12, so the clamp would slip through). Pure
    integer function of the index — reproducible.
    """
    out: list[int] = []
    period = 11
    for i in range(n):
        if (i // period) % 2 == 0:
            out.append((i % 3) * 10 - 10)  # calm: -10, 0, 10
        else:
            out.append(((i * 211) % 360) - 180)  # volatile: -180..179
    return out


# compute_game_accuracy: the core matrix. Each entry is
# (name, moves, player_color, expected_total_moves).
def _compute_cases() -> list[tuple[str, list[AccuracyMove], str, int | None]]:
    checkmate_win = [
        _mv("white", 20), _mv("black", -10), _mv("white", 60), _mv("black", -40),
        _mv("white", 120), _mv("black", -90), _mv("white", 10000, 0),
    ]
    black_mates = [
        _mv("white", -20), _mv("black", 10), _mv("white", -60), _mv("black", 40),
        _mv("white", -120), _mv("black", 90), _mv("white", -200),
        _mv("black", -10000, 0),  # black delivers mate; stored mate-0 => black win
    ]
    long_game = _white_moves(
        [20, 10, 40, 30, 80, 60, 120, 100, 160, 130, 200, 170,
         240, 210, 280, 250, 320, 290, 360, 330]
    )
    return [
        ("white_steady", _white_moves([20, 10, 60, 40, 120, 90, 200, 150]), "white", 8),
        ("black_steady", _white_moves([-20, -50, -40, -120, -90, -200, -150, -260]), "black", 8),
        ("white_blunder", _white_moves([50, 40, 300, 280, -300, -310, -320, -330]), "white", 8),
        ("checkmate_win_mate0", checkmate_win, "white", 7),
        ("black_checkmate_win_mate0", black_mates, "black", 8),
        ("white_mate_short", [_mv("white", None, 3), _mv("black", -50)], "white", 2),
        ("incomplete_short", _white_moves([20, 10, 60, 40]), "white", 10),
        ("missing_expected", _white_moves([20, 10, 60, 40]), "white", None),
        ("missing_player_eval", _white_moves([20, 10, None, 40, 120, 90]), "white", 6),
        ("missing_opponent_before", _white_moves([20, None, 60, 40, 120, 90]), "white", 6),
        ("no_player_moves_black", _white_moves([20]), "black", 1),
        ("two_ply_white", _white_moves([30, -20]), "white", 2),
        ("two_ply_black", _white_moves([30, -20]), "black", 2),
        ("long_20ply_white", long_game, "white", 20),
        ("long_20ply_black", long_game, "black", 20),
        # Non-monotonic long games freeze the variable window sizing that the
        # short/monotonic cases cannot. Seesaw cases (30 -> window_size 3, 50 ->
        # 5) pin the lower ramp and leading-window padding; the 100-ply block
        # cases pin the UPPER CLAMP to 8 (moderate amplitude keeps weights
        # unsaturated, so 8-vs-9+ sizing changes the result). Because the evals
        # swing, the stddev weights and the sizing actually move the result.
        ("non_monotonic_30ply_white", _white_moves(_seesaw(30)), "white", 30),
        ("non_monotonic_30ply_black", _white_moves(_seesaw(30)), "black", 30),
        ("non_monotonic_50ply_white", _white_moves(_seesaw(50)), "white", 50),
        ("non_monotonic_100ply_white", _white_moves(_blocky(100)), "white", 100),
        ("non_monotonic_100ply_black", _white_moves(_blocky(100)), "black", 100),
    ]


def _move_to_json(m: AccuracyMove) -> dict:
    return {"color": m.color, "eval_cp": m.eval_cp, "eval_mate": m.eval_mate}


def main() -> None:
    if chess.__version__ != CHESS_VERSION_PIN:
        raise SystemExit(
            f"Refusing to capture goldens under chess {chess.__version__}; "
            f"the production-observed pin is {CHESS_VERSION_PIN}."
        )

    data: dict = {
        "description": (
            "Frozen golden outputs for the Release A accuracy freeze "
            "(g-accuracy-freeze). Captured from the pre-refactor app.accuracy "
            "(blob faed4614153c72b6a3170a9b37d5580c769f697c, commit 01f6afd) under "
            f"the production-observed python-chess {CHESS_VERSION_PIN} on "
            "2026-07-11. accuracy_v1 must reproduce these literals exactly; do "
            "NOT regenerate from the moved module. Any semantic or chess-version "
            "change is accuracy v2, not a v1 refresh."
        ),
        "chess_version": CHESS_VERSION_PIN,
        "source_blob": "faed4614153c72b6a3170a9b37d5580c769f697c",
        "win_percent_from_cp": [
            {"cp": cp, "expected": win_percent_from_cp(cp)} for cp in WIN_PERCENT_CPS
        ],
        "accuracy_from_win_percents": [
            {"before": b, "after": a, "expected": accuracy_from_win_percents(b, a)}
            for (b, a) in ACCURACY_PAIRS
        ],
        "white_relative_cp": [
            {
                "move": _move_to_json(_mv(color, cp, mate)),
                "expected": _white_relative_cp(_mv(color, cp, mate)),
            }
            for (color, cp, mate) in WHITE_RELATIVE_MOVES
        ],
        "expected_total_moves_from_pgn": [
            {"name": name, "pgn": pgn, "expected": expected_total_moves_from_pgn(pgn)}
            for (name, pgn) in EXPECTED_PGNS
        ],
        "compute_game_accuracy": [
            {
                "name": name,
                "moves": [_move_to_json(m) for m in moves],
                "player_color": color,
                "expected_total_moves": exp,
                "expected": compute_game_accuracy(moves, color, exp),
            }
            for (name, moves, color, exp) in _compute_cases()
        ],
    }

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FIXTURE_PATH.open("w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    print(f"Wrote {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
