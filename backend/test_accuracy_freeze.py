"""Release A accuracy-freeze guards (g-accuracy-freeze).

These tests lock accuracy **v1** in place: they pin python-chess (runtime and
requirements file), assert the public/private import surface, and compare the
frozen ``app.accuracy_v1`` implementation against the static golden literals in
``tests/fixtures/accuracy_v1_goldens.json``.

The goldens were captured from the pre-refactor ``app.accuracy`` (blob
faed4614153c72b6a3170a9b37d5580c769f697c, commit 01f6afd) under the
production-observed python-chess 1.11.2. These tests deliberately do NOT
regenerate outputs from the moved module — they read captured literals — so a
silent behavior change in the algorithm is caught, not masked.
"""

from __future__ import annotations

import importlib.util
import json
import math
import re
from pathlib import Path

import chess

from app import accuracy as live
from app import accuracy_rows_v1, accuracy_v1
from app.accuracy import (
    ACCURACY_ALGO_VERSION,
    CHESS_VERSION_PIN,
    AccuracyMove as PublicAccuracyMove,
    accuracy_from_win_percents as public_accuracy_from_win_percents,
    compute_game_accuracy as public_compute_game_accuracy,
    expected_total_moves_from_pgn as public_expected_total_moves_from_pgn,
    win_percent_from_cp as public_win_percent_from_cp,
)

_BACKEND = Path(__file__).resolve().parent
_FIXTURE = _BACKEND / "tests" / "fixtures" / "accuracy_v1_goldens.json"
_REQUIREMENTS = _BACKEND / "requirements.txt"

# Absolute tolerance for float goldens: tight enough to freeze the algorithm
# (~1e-11 relative on 0-100 win%/accuracy values) yet immune to last-ULP libm
# differences between the capture host and CI.
_FLOAT_ABS_TOL = 1e-9


def _goldens() -> dict:
    with _FIXTURE.open() as fh:
        return json.load(fh)


def _move(d: dict) -> accuracy_v1.AccuracyMove:
    return accuracy_v1.AccuracyMove(
        color=d["color"], eval_cp=d["eval_cp"], eval_mate=d["eval_mate"]
    )


# --- Dependency provenance -------------------------------------------------


def test_runtime_chess_version_matches_pin():
    # The interpreter actually running must be the pinned python-chess. A drift
    # invalidates the frozen goldens (accuracy-v2 territory), so fail loud.
    assert chess.__version__ == CHESS_VERSION_PIN


def test_algo_version_is_one():
    assert ACCURACY_ALGO_VERSION == 1


def test_requirements_pins_chess_exactly():
    # Exactly one bare-name `chess==<version>` requirement, no range, no extras,
    # and it must equal CHESS_VERSION_PIN.
    pins: list[str] = []
    for raw in _REQUIREMENTS.read_text().splitlines():
        spec = raw.split("#", 1)[0].strip()
        if not spec:
            continue
        # Anchored `chess` distribution name (not `chessboard`, not extras like
        # `chess[foo]`), optionally followed by a version specifier.
        if re.match(r"^chess\s*([<>=!~].*)?$", spec):
            pins.append(spec.replace(" ", ""))
    assert len(pins) == 1, f"expected exactly one bare chess pin, found {pins}"
    assert pins[0] == f"chess=={CHESS_VERSION_PIN}", pins[0]


def test_goldens_recorded_under_pin():
    g = _goldens()
    assert g["chess_version"] == CHESS_VERSION_PIN
    assert g["source_blob"] == "faed4614153c72b6a3170a9b37d5580c769f697c"


# --- Import surface --------------------------------------------------------


def test_public_names_are_the_v1_objects():
    # The live surface must re-export the identical v1 callables/class, so live
    # code and history-reproducing code compute byte-identically.
    assert public_compute_game_accuracy is accuracy_v1.compute_game_accuracy
    assert public_win_percent_from_cp is accuracy_v1.win_percent_from_cp
    assert public_accuracy_from_win_percents is accuracy_v1.accuracy_from_win_percents
    assert public_expected_total_moves_from_pgn is accuracy_v1.expected_total_moves_from_pgn
    assert PublicAccuracyMove is accuracy_v1.AccuracyMove


def test_private_symbols_not_reexported_but_live_on_v1():
    # Private helpers/constants must NOT leak onto the live surface; consumers
    # (and private-symbol tests) must reach them via app.accuracy_v1.
    for name in ("_white_relative_cp", "_MATE_CP", "_clamp", "_stddev", "_INITIAL_CP"):
        assert not hasattr(live, name), f"{name} leaked onto app.accuracy"
    assert callable(accuracy_v1._white_relative_cp)
    assert accuracy_v1._MATE_CP == 10000


def test_live_surface_all_is_public_only():
    for private in ("_white_relative_cp", "_MATE_CP", "_clamp", "_stddev"):
        assert private not in live.__all__


def test_public_row_guard_names_are_the_frozen_objects():
    # The v1 INPUT contract is frozen for the same reason the algorithm is: a
    # persisted player_accuracy depends on whether this validation passed, and the
    # Release B migration imports app.accuracy_rows_v1 DIRECTLY. The live surface
    # must re-export the identical callables, or the guard the migration runs and
    # the guard the write hook runs could drift apart (g-22t8.6).
    assert live.ply_coordinates_intact is accuracy_rows_v1.ply_coordinates_intact
    assert live.ply_color is accuracy_rows_v1.ply_color


def test_row_guard_names_on_live_surface_all():
    # The live surface stays the documented import point for the guard.
    for name in ("ply_color", "ply_coordinates_intact", "game_accuracy_for_rows"):
        assert name in live.__all__, f"{name} missing from app.accuracy.__all__"


def _load_golden_generator():
    # scripts/ is not a package; load the capture tool by path. main() is guarded
    # by __name__ == "__main__", so importing it never writes the fixture.
    path = _BACKEND / "scripts" / "gen_accuracy_v1_goldens.py"
    spec = importlib.util.spec_from_file_location("gen_accuracy_v1_goldens", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_golden_generator_binds_to_frozen_v1():
    # The capture tool must resolve every algorithm symbol to app.accuracy_v1, not
    # the mutable app.accuracy re-export. Otherwise a future v2 live surface (even
    # one that keeps chess 1.11.2, so the script's version guard still passes)
    # would let a re-run overwrite the v1 fixture with v2 outputs.
    gen = _load_golden_generator()
    assert gen.win_percent_from_cp is accuracy_v1.win_percent_from_cp
    assert gen.accuracy_from_win_percents is accuracy_v1.accuracy_from_win_percents
    assert gen.compute_game_accuracy is accuracy_v1.compute_game_accuracy
    assert gen.expected_total_moves_from_pgn is accuracy_v1.expected_total_moves_from_pgn
    assert gen.AccuracyMove is accuracy_v1.AccuracyMove
    assert gen._white_relative_cp is accuracy_v1._white_relative_cp


# --- Frozen goldens (compare against captured literals, never regenerate) ---


def test_frozen_win_percent_from_cp():
    for case in _goldens()["win_percent_from_cp"]:
        got = accuracy_v1.win_percent_from_cp(case["cp"])
        assert math.isclose(got, case["expected"], rel_tol=0.0, abs_tol=_FLOAT_ABS_TOL), case


def test_frozen_accuracy_from_win_percents():
    for case in _goldens()["accuracy_from_win_percents"]:
        got = accuracy_v1.accuracy_from_win_percents(case["before"], case["after"])
        assert math.isclose(got, case["expected"], rel_tol=0.0, abs_tol=_FLOAT_ABS_TOL), case


def test_frozen_white_relative_cp():
    for case in _goldens()["white_relative_cp"]:
        got = accuracy_v1._white_relative_cp(_move(case["move"]))
        assert got == case["expected"], case  # exact int|None


def test_frozen_expected_total_moves_from_pgn():
    for case in _goldens()["expected_total_moves_from_pgn"]:
        got = accuracy_v1.expected_total_moves_from_pgn(case["pgn"])
        assert got == case["expected"], case  # exact int|None


def test_frozen_compute_game_accuracy():
    saw_mate0_win = False
    saw_none = False
    move_counts: set[int] = set()
    for case in _goldens()["compute_game_accuracy"]:
        moves = [_move(m) for m in case["moves"]]
        move_counts.add(len(moves))
        got = accuracy_v1.compute_game_accuracy(
            moves, case["player_color"], case["expected_total_moves"]
        )
        assert got == case["expected"], case  # exact int|None
        if case["name"] == "checkmate_win_mate0":
            assert got == 100  # mate-0-as-mover-win freezes at a full 100
            saw_mate0_win = True
        if case["expected"] is None:
            saw_none = True
    # The matrix must exercise the mate-0 win and legitimate None paths.
    assert saw_mate0_win
    assert saw_none
    # ...and the variable window sizing: window_size = clamp(n // 10, 2, 8). Short
    # games all use the min window (2); non-monotonic games at >=30 plies exercise
    # window_size 3+ and at >=90 plies exercise the upper clamp to 8. Without these
    # a regression in sizing/padding/clamp would leave every golden unchanged.
    assert any(n >= 30 for n in move_counts), "no case exercises window_size >= 3"
    assert any(n >= 90 for n in move_counts), "no case exercises the window_size upper clamp"
