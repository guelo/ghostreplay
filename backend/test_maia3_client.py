"""Tests for maia3_client module."""
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
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = str(json_data)
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
