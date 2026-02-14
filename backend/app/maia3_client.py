"""
Maia3 remote API client.

Translates a target ELO + UCI move list into a single opponent move
by calling the maiachess.com Maia3 endpoint.
"""
import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

MAIA3_URL = "https://www.maiachess.com/api/v1/play/get_move"
MAIA3_TIMEOUT_S = 5

ELO_BINS = [
    600, 800, 1000, 1100, 1200, 1300, 1400, 1500,
    1600, 1700, 1800, 1900, 2000, 2200, 2400, 2600,
]


class Maia3Error(Exception):
    """Any failure when calling the Maia3 API."""


@dataclass
class Maia3Move:
    uci: str
    move_delay: float


def elo_to_maia_name(elo: int) -> str:
    """Map a numeric ELO to the nearest maia_kdd_<bin> model name."""
    nearest = min(ELO_BINS, key=lambda b: abs(b - elo))
    return f"maia_kdd_{nearest}"


def get_move(moves: list[str], target_elo: int) -> Maia3Move:
    """
    Call the Maia3 API and return the suggested move.

    Args:
        moves: UCI move strings from game start, e.g. ["e2e4", "e7e6"].
        target_elo: Desired opponent difficulty rating.

    Raises:
        Maia3Error: On network failure, non-200 response, or bad JSON.
    """
    maia_name = elo_to_maia_name(target_elo)
    logger.info("Maia3 request: model=%s elo=%d moves=%d", maia_name, target_elo, len(moves))

    try:
        resp = requests.post(
            MAIA3_URL,
            params={
                "maia_name": maia_name,
                "initial_clock": 0,
                "current_clock": 0,
                "maia_version": "maia3",
            },
            headers={
                "Content-Type": "application/json",
                "Origin": "https://www.maiachess.com",
            },
            json=moves,
            timeout=MAIA3_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise Maia3Error(f"Maia3 request failed: {exc}") from exc

    if resp.status_code != 200:
        raise Maia3Error(
            f"Maia3 returned HTTP {resp.status_code}: {resp.text[:200]}"
        )

    try:
        data = resp.json()
        move = Maia3Move(
            uci=data["top_move"],
            move_delay=data.get("move_delay", 0.0),
        )
        logger.info("Maia3 response: move=%s delay=%.2f (HTTP %d, %.0fms)", move.uci, move.move_delay, resp.status_code, resp.elapsed.total_seconds() * 1000)
        return move
    except (ValueError, KeyError, TypeError) as exc:
        raise Maia3Error(f"Maia3 response parse error: {exc}") from exc
