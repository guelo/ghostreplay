"""Tests for maia3_client and opponent_move_controller modules."""
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.maia3_client import (
    ELO_BINS,
    Maia3Error,
    Maia3Move,
    elo_to_maia_name,
    get_move,
)
from app.opponent_move_controller import ControllerMove, choose_move


# --- elo_to_maia_name ---


@pytest.mark.parametrize(
    "elo, expected",
    [
        (600, "maia_kdd_600"),
        (800, "maia_kdd_800"),
        (1050, "maia_kdd_1000"),
        (1100, "maia_kdd_1100"),
        (1149, "maia_kdd_1100"),
        (1150, "maia_kdd_1100"),  # tie goes to lower (min picks first)
        (1151, "maia_kdd_1200"),
        (1550, "maia_kdd_1500"),  # tie: 1500 appears before 1600
        (2600, "maia_kdd_2600"),
        (3000, "maia_kdd_2600"),
        (100, "maia_kdd_600"),
    ],
)
def test_elo_to_maia_name(elo: int, expected: str):
    assert elo_to_maia_name(elo) == expected


def test_all_bins_roundtrip():
    """Each bin value maps exactly to itself."""
    for b in ELO_BINS:
        assert elo_to_maia_name(b) == f"maia_kdd_{b}"


# --- get_move ---


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    from datetime import timedelta

    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = str(json_data)
    resp.elapsed = timedelta(milliseconds=100)
    return resp


@patch("app.maia3_client.requests.post")
def test_get_move_success(mock_post: MagicMock):
    mock_post.return_value = _mock_response(
        {"top_move": "e7e5", "move_delay": 0.42}
    )
    result = get_move(["e2e4"], 1200)

    assert result == Maia3Move(uci="e7e5", move_delay=0.42)
    mock_post.assert_called_once()

    # Verify query params include mapped maia_name
    call_kwargs = mock_post.call_args
    assert call_kwargs.kwargs["params"]["maia_name"] == "maia_kdd_1200"
    assert call_kwargs.kwargs["json"] == ["e2e4"]


@patch("app.maia3_client.requests.post")
def test_get_move_empty_moves(mock_post: MagicMock):
    """Starting position (no moves yet) should work."""
    mock_post.return_value = _mock_response(
        {"top_move": "e2e4", "move_delay": 0.0}
    )
    result = get_move([], 1500)
    assert result.uci == "e2e4"
    assert mock_post.call_args.kwargs["json"] == []


@patch("app.maia3_client.requests.post")
def test_get_move_network_error(mock_post: MagicMock):
    mock_post.side_effect = requests.ConnectionError("connection refused")
    with pytest.raises(Maia3Error, match="request failed"):
        get_move(["e2e4"], 1200)


@patch("app.maia3_client.requests.post")
def test_get_move_timeout(mock_post: MagicMock):
    mock_post.side_effect = requests.Timeout("read timed out")
    with pytest.raises(Maia3Error, match="request failed"):
        get_move(["e2e4"], 1200)


@patch("app.maia3_client.requests.post")
def test_get_move_non_200(mock_post: MagicMock):
    resp = _mock_response({}, status_code=500)
    resp.text = "Internal Server Error"
    mock_post.return_value = resp
    with pytest.raises(Maia3Error, match="HTTP 500"):
        get_move(["e2e4"], 1200)


@patch("app.maia3_client.requests.post")
def test_get_move_bad_json(mock_post: MagicMock):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.json.side_effect = ValueError("bad json")
    mock_post.return_value = resp
    with pytest.raises(Maia3Error, match="parse error"):
        get_move(["e2e4"], 1200)


@patch("app.maia3_client.requests.post")
def test_get_move_missing_top_move(mock_post: MagicMock):
    mock_post.return_value = _mock_response({"move_delay": 0.0})
    with pytest.raises(Maia3Error, match="parse error"):
        get_move(["e2e4"], 1200)


@patch("app.maia3_client.requests.post")
def test_get_move_delay_defaults(mock_post: MagicMock):
    """move_delay is optional in the response."""
    mock_post.return_value = _mock_response({"top_move": "d7d5"})
    result = get_move(["e2e4"], 800)
    assert result.move_delay == 0.0


# --- choose_move() controller ---

# Position after 1.e4 (black to move)
FEN_AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"


@patch("app.opponent_move_controller.maia3_get_move")
def test_choose_move_returns_controller_move(mock_get: MagicMock):
    mock_get.return_value = Maia3Move(uci="e7e5", move_delay=0.5)

    result = choose_move(FEN_AFTER_E4, target_elo=1200, moves=["e2e4"])

    assert isinstance(result, ControllerMove)
    assert result.uci == "e7e5"
    assert result.san == "e5"
    assert result.method == "maia3_api"


@patch("app.opponent_move_controller.maia3_get_move")
def test_choose_move_passes_moves_and_elo(mock_get: MagicMock):
    mock_get.return_value = Maia3Move(uci="e7e5", move_delay=0.0)

    choose_move(FEN_AFTER_E4, target_elo=800, moves=["e2e4"])

    mock_get.assert_called_once_with(moves=["e2e4"], target_elo=800)


@patch("app.opponent_move_controller.maia3_get_move")
def test_choose_move_none_moves_defaults_empty(mock_get: MagicMock):
    mock_get.return_value = Maia3Move(uci="e7e5", move_delay=0.0)

    choose_move(FEN_AFTER_E4, target_elo=800, moves=None)

    mock_get.assert_called_once_with(moves=[], target_elo=800)


@patch("app.opponent_move_controller.maia3_get_move")
def test_choose_move_knight_uci_to_san(mock_get: MagicMock):
    mock_get.return_value = Maia3Move(uci="g8f6", move_delay=0.0)

    result = choose_move(FEN_AFTER_E4, target_elo=1200, moves=["e2e4"])

    assert result.uci == "g8f6"
    assert result.san == "Nf6"


@patch("app.opponent_move_controller.maia3_get_move")
def test_choose_move_promotion(mock_get: MagicMock):
    fen = "7k/4P3/8/8/8/8/8/K7 w - - 0 1"
    mock_get.return_value = Maia3Move(uci="e7e8q", move_delay=0.0)

    result = choose_move(fen, target_elo=1200, moves=[])

    assert result.uci == "e7e8q"
    assert result.san == "e8=Q+"  # gives check


@patch("app.opponent_move_controller.maia3_get_move")
def test_choose_move_illegal_move_raises(mock_get: MagicMock):
    mock_get.return_value = Maia3Move(uci="a1a8", move_delay=0.0)

    with pytest.raises(ValueError, match="illegal move"):
        choose_move(FEN_AFTER_E4, target_elo=1200, moves=["e2e4"])


@patch("app.opponent_move_controller.maia3_get_move")
def test_choose_move_maia3_error_propagates(mock_get: MagicMock):
    mock_get.side_effect = Maia3Error("API down")

    with pytest.raises(Maia3Error):
        choose_move(FEN_AFTER_E4, target_elo=1200, moves=["e2e4"])
