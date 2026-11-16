"""Pinned upstream Maia3 code and weights.

Stdlib only, so the build-time weights script can import it without loading app
settings or the engine service.

Maia3 (https://github.com/CSSLab/maia3) is AGPL-3.0, and the weights are treated
the same way. The API process never imports it: the engine runs unmodified, at
``UPSTREAM_COMMIT``, as a separate process over UCI (see ``app.maia3_client``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

UPSTREAM_REPO = "https://github.com/CSSLab/maia3"
UPSTREAM_COMMIT = "1e13597c42d4858b7cfd7cfdae01e297263364b2"

_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ModelPin:
    """One checkpoint, pinned to a Hugging Face revision, size and sha256."""

    name: str
    repo_id: str
    revision: str
    filename: str
    size: int
    sha256: str


MODEL_PINS: dict[str, ModelPin] = {
    "maia3-5m": ModelPin(
        name="maia3-5m",
        repo_id="UofTCSSLab/Maia3-5M",
        revision="b6559de2398d7140b985f28fd2c19fb5e47ddabe",
        filename="maia3-5m.pt",
        size=20968049,
        sha256="ba14208b2992d85502f5fb501934abf6aaaeb355e9f3fdf90e326911f562524f",
    ),
    "maia3-23m": ModelPin(
        name="maia3-23m",
        repo_id="UofTCSSLab/Maia3-23M",
        revision="51a0145a8178046f7de23119160b136672deeb2b",
        filename="maia3-23m.pt",
        size=91799307,
        sha256="bce6cd1af5f0399ac7eed33fabb7a6a2ef6193662c2740f262bf93af7bfb3569",
    ),
}


class WeightsError(ValueError):
    """A checkpoint file is missing or does not match its pin."""


def default_weights_dir() -> Path:
    """``backend/maia3_weights`` (``/app/maia3_weights`` when backend/ is the root)."""
    return Path(__file__).resolve().parents[1] / "maia3_weights"


def verify_weights(path: Path, pin: ModelPin) -> None:
    """Raise :class:`WeightsError` unless ``path`` is exactly the pinned checkpoint.

    The size is checked first, so a truncated download fails without hashing.
    Read errors other than a missing file propagate as ``OSError``.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise WeightsError(f"{pin.name} weights missing at {path}") from exc
    if not path.is_file():
        raise WeightsError(f"{pin.name} weights path {path} is not a file")
    if size != pin.size:
        raise WeightsError(
            f"{pin.name} weights at {path} are {size} bytes, expected {pin.size}"
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != pin.sha256:
        raise WeightsError(
            f"{pin.name} weights at {path} have sha256 {actual}, expected {pin.sha256}"
        )
