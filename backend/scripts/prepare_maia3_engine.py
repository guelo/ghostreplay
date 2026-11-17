#!/usr/bin/env python3
"""Fetch and verify the pinned Maia3 weights, then smoke-test the real engine.

Usage:
    MAIA3_ENABLED=1 python scripts/prepare_maia3_engine.py [--model NAME]
        [--no-smoke | --require-smoke]

1. Fetch. A checkpoint already in the weights dir that matches its pin
   (``app.maia3_pins``: Hugging Face revision, size, sha256) is not downloaded
   again. Otherwise it is downloaded at the pinned revision into a temp dir under
   the weights dir, verified, and renamed into place, so a failed or interrupted
   fetch never leaves a file the engine would load.
2. Smoke (only with ``MAIA3_ENABLED=1``). Starts the real engine through
   ``Maia3EngineService`` with the configured settings (spawn, handshake,
   warm-up), plays one move at 1500, reads the child's peak memory, shuts it
   down, and checks that the child is gone and that the checkpoint loaded with
   no missing or unexpected keys (upstream loads with ``strict=False`` and only
   prints them). It ends with one line, for build logs and CI::

       maia3 smoke ok model=... threads=... start_ms=... move_ms=... child_hwm_kb=...

Exit status:
    MAIA3_ENABLED=1: any failure exits 1, which fails the build.
    MAIA3_ENABLED=0 (the default): a failed fetch logs a WARNING and the script
        exits 0, and the smoke never runs. The switch goes through a build, so
        this script must not be able to block turning the engine off; the
        runtime already handles missing weights.
    --require-smoke: MAIA3_ENABLED=0 exits 1 before fetching, so a job that
        lost its MAIA3_ENABLED=1 fails instead of skipping the smoke.

The weights dir and the model come from the same MAIA3_* settings the API reads
(``--model`` overrides MAIA3_MODEL). Downloads need huggingface.co; nothing is
downloaded at runtime.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chess  # noqa: E402

from app import maia3_pins  # noqa: E402
from app.maia3_client import (  # noqa: E402
    Maia3EngineService,
    Maia3Settings,
    maia3_settings,
    position_for,
)
from app.maia3_pins import ModelPin, WeightsError, verify_weights  # noqa: E402

logger = logging.getLogger("prepare_maia3_engine")

SMOKE_ELO = 1500
# A Ruy Lopez after 3...a6: the move goes through the same history path as a
# real request (position_for roots the board 8 plies back).
SMOKE_MOVES = ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6"]
SMOKE_READY_MARGIN_S = 5.0
_CHILD_EXIT_WAIT_S = 2.0

# Upstream prints these to stderr when the checkpoint does not match the model
# (maia3/uci.py load_model); python-chess logs each stderr line at WARNING.
_KEY_WARNING_PREFIXES = ("warning: missing keys", "warning: unexpected keys")
_CHESS_ENGINE_STDERR_MSG = "%s: stderr >> %s"

Downloader = Callable[[ModelPin, Path], Path]


class SmokeFailure(Exception):
    """The real engine did not start, move, report or stop as expected."""


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def hf_download(pin: ModelPin, dest: Path) -> Path:
    """Download ``pin`` at its pinned revision into ``dest``; return the file."""
    from huggingface_hub import hf_hub_download

    return Path(
        hf_hub_download(
            repo_id=pin.repo_id,
            filename=pin.filename,
            revision=pin.revision,
            local_dir=dest,
        )
    )


def fetch_weights(
    pin: ModelPin, weights_dir: Path, download: Downloader = hf_download
) -> Path:
    """Make ``weights_dir/<pin.filename>`` the verified, pinned checkpoint.

    Raises:
        WeightsError: the downloaded bytes do not match the pin.
        Exception: whatever the download raised.
    """
    weights_dir = Path(weights_dir)
    final = weights_dir / pin.filename
    if final.exists():
        try:
            verify_weights(final, pin)
        except WeightsError as exc:
            logger.warning("existing %s failed verification (%s); downloading", pin.name, exc)
        else:
            print(f"maia3 weights verified sha256={pin.sha256} path={final} (present)")
            return final

    weights_dir.mkdir(parents=True, exist_ok=True)
    # Under the weights dir, so the final rename stays on one filesystem.
    staging = Path(tempfile.mkdtemp(prefix=".fetch-", dir=weights_dir))
    try:
        started = time.monotonic()
        downloaded = Path(download(pin, staging))
        verify_weights(downloaded, pin)
        os.replace(downloaded, final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print(
        f"maia3 weights verified sha256={pin.sha256} path={final} "
        f"download_ms={(time.monotonic() - started) * 1000:.0f}"
    )
    return final


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


class _CheckpointKeyWarnings(logging.Handler):
    """Collects upstream's missing/unexpected key warnings from engine stderr."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.msg != _CHESS_ENGINE_STDERR_MSG:
            return
        args = record.args
        if isinstance(args, tuple) and len(args) == 2:
            line = str(args[1])
            if line.startswith(_KEY_WARNING_PREFIXES):
                self.lines.append(line)


def child_memory_kb(pid: int, proc_root: Path = Path("/proc")) -> tuple[int, str]:
    """The child's memory in KiB and where it came from.

    Linux: ``VmHWM`` (peak resident set) from ``/proc/<pid>/status``. Elsewhere
    (macOS): ``ps -o rss=``, the current resident set, since there is no peak.
    """
    status = proc_root / str(pid) / "status"
    if proc_root.is_dir():
        for line in status.read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]), "vmhwm"
        raise SmokeFailure(f"no VmHWM in {status}")
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return int(out), "ps_rss"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_smoke(
    settings: Maia3Settings,
    service_factory: Callable[[Maia3Settings], Maia3EngineService] = Maia3EngineService,
    proc_root: Path = Path("/proc"),
) -> str:
    """Start the real engine, play one move, stop it; return the summary line.

    Raises:
        SmokeFailure: on any failed step.
    """
    keys = _CheckpointKeyWarnings()
    engine_logger = logging.getLogger("chess.engine")
    engine_logger.addHandler(keys)
    service = service_factory(settings)
    pid: int | None = None
    try:
        started = time.monotonic()
        if not service.wait_ready(
            settings.start_timeout_s + SMOKE_READY_MARGIN_S, stop_on_failure=True
        ):
            raise SmokeFailure(
                f"engine did not start: {service.last_spawn_error or 'timed out'}"
            )
        start_ms = (time.monotonic() - started) * 1000
        handle = service.handle
        if handle is None:
            raise SmokeFailure("engine retired right after start")
        pid = handle.pid

        board, _history = position_for(_smoke_fen(), SMOKE_MOVES)
        move_started = time.monotonic()
        move = service.play(board, SMOKE_ELO)
        move_ms = (time.monotonic() - move_started) * 1000
        if move not in board.legal_moves:
            raise SmokeFailure(f"engine played {move.uci()}, not a legal move")

        hwm_kb, mem_source = child_memory_kb(pid, proc_root)
    except SmokeFailure:
        raise
    except Exception as exc:
        raise SmokeFailure(f"{type(exc).__name__}: {exc}") from exc
    finally:
        service.shutdown()
        engine_logger.removeHandler(keys)

    deadline = time.monotonic() + _CHILD_EXIT_WAIT_S
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _pid_alive(pid):
        raise SmokeFailure(f"engine child pid={pid} survived shutdown")
    if keys.lines:
        raise SmokeFailure(f"checkpoint did not load cleanly: {keys.lines}")

    return (
        f"maia3 smoke ok model={settings.model} threads={settings.threads} "
        f"start_ms={start_ms:.0f} move_ms={move_ms:.0f} move={move.uci()} "
        f"child_hwm_kb={hwm_kb} mem_source={mem_source} pid={pid}"
    )


def _smoke_fen() -> str:
    board = chess.Board()
    for uci in SMOKE_MOVES:
        board.push_uci(uci)
    return board.fen()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch and verify the pinned Maia3 weights, then smoke-test the engine."
    )
    parser.add_argument(
        "--model",
        choices=sorted(maia3_pins.MODEL_PINS),
        default=None,
        help="checkpoint to prepare (default: MAIA3_MODEL, else maia3-23m)",
    )
    smoke = parser.add_mutually_exclusive_group()
    smoke.add_argument(
        "--no-smoke", action="store_true", help="fetch and verify only"
    )
    smoke.add_argument(
        "--require-smoke",
        action="store_true",
        help="exit 1 instead of skipping the smoke when MAIA3_ENABLED=0 (CI)",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    download: Downloader = hf_download,
    smoke: Callable[[Maia3Settings], str] = run_smoke,
) -> int:
    args = parse_args(argv)
    try:
        settings = maia3_settings(env)
        if args.model is not None:
            settings = dataclasses.replace(settings, model=args.model)
    except ValueError as exc:
        # The API would refuse to boot with these settings too.
        logger.error("invalid MAIA3_* settings: %s", exc)
        return 1
    if args.require_smoke and not settings.enabled:
        logger.error("--require-smoke, but MAIA3_ENABLED=0 would skip the smoke")
        return 1

    try:
        fetch_weights(settings.pin, settings.weights_dir, download)
    except Exception as exc:
        if not settings.enabled:
            logger.warning(
                "maia3 weights fetch failed (%s: %s); MAIA3_ENABLED=0, so the build "
                "continues without them",
                type(exc).__name__,
                exc,
            )
            return 0
        logger.error("maia3 weights fetch failed: %s: %s", type(exc).__name__, exc)
        return 1

    if not settings.enabled:
        print("maia3 smoke skipped (MAIA3_ENABLED=0)")
        return 0
    if args.no_smoke:
        print("maia3 smoke skipped (--no-smoke)")
        return 0

    try:
        line = smoke(settings)
    except SmokeFailure as exc:
        logger.error("maia3 smoke failed: %s", exc)
        return 1
    print(line)
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    sys.exit(main())
