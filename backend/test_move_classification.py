"""Drive the shared golden vectors through the backend classifier port.

The same fixture (tests/fixtures/classification_vectors.json) is consumed by the
TS suite (src/workers/analysisUtils.test.ts) so the two implementations cannot
drift.
"""
import json
from pathlib import Path

import pytest

from app.move_classification import (
    EngineScore,
    calculate_win_chance,
    classify_move_advanced,
    classify_root_alternative,
)

FIXTURE = Path(__file__).resolve().parent / "tests" / "fixtures" / "classification_vectors.json"
ROOT_FIXTURE = (
    Path(__file__).resolve().parent
    / "tests"
    / "fixtures"
    / "root_classification_vectors.json"
)


def _load_cases():
    with open(FIXTURE) as f:
        return json.load(f)["cases"]


def _load_root_cases():
    with open(ROOT_FIXTURE) as f:
        return json.load(f)["cases"]


@pytest.mark.parametrize("case", _load_cases())
def test_classification_golden_vectors(case):
    result = classify_move_advanced(
        EngineScore.from_dict(case["prevScore"]),
        EngineScore.from_dict(case["nextScore"]),
        case["scorePov"],
        case["mover"],
        case["isBest"],
    )
    assert result == case["expected"]


@pytest.mark.parametrize("case", _load_root_cases())
def test_root_classification_golden_vectors(case):
    result = classify_root_alternative(
        EngineScore.from_dict(case["bestScore"]),
        EngineScore.from_dict(case["playedScore"]),
        case["mover"],
        case["isBest"],
    )
    assert result == case["expected"]


def test_root_vectors_cover_every_bucket():
    """The root fixture must exercise every classification bucket and mate paths."""
    expected = {c["expected"] for c in _load_root_cases()}
    assert expected == {
        "best",
        "excellent",
        "good",
        "inaccuracy",
        "mistake",
        "blunder",
    }


def test_all_buckets_present():
    """The fixture must exercise every classification bucket and both mate paths."""
    expected = {c["expected"] for c in _load_cases()}
    assert expected == {
        "best",
        "excellent",
        "good",
        "inaccuracy",
        "mistake",
        "blunder",
    }


def test_cp_to_cp_cases_cover_every_drop_bucket():
    """Ordinary cp->cp cases must exercise each win-chance-drop bucket (incl.
    mistake), so a threshold regression cannot pass on mate cases alone."""
    cp_buckets = {
        c["expected"]
        for c in _load_cases()
        if c["prevScore"]["type"] == "cp"
        and c["nextScore"]["type"] == "cp"
        and not c["isBest"]
    }
    assert {"excellent", "good", "inaccuracy", "mistake", "blunder"} <= cp_buckets


def test_win_chance_white_relative_symmetry():
    # A symmetric cp score yields opposite win chances under opposite POV.
    s = EngineScore(type="cp", value=300)
    assert calculate_win_chance(s, "white") == pytest.approx(
        -calculate_win_chance(s, "black")
    )


def test_win_chance_mate_zero_is_loss_for_pov():
    s = EngineScore(type="mate", value=0)
    assert calculate_win_chance(s, "white") < 0
    assert calculate_win_chance(s, "black") > 0
