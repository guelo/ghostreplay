"""Release seal: the Maia3 engine service against the real upstream engine.

Real subprocesses running ``python -m maia3.uci`` (torch, the pinned weights).
The unit tests in test_maia3_client.py and test_maia3_open_uci.py cover the same
state machine against fake engines; this checks it against the real one.

When to run it: after changing the torch or maia3 pins, the torch version, the
weights pins (app/maia3_pins.py) or the engine wrapper (app/maia3_client.py).
The CI maia3-engine job runs it on every backend push (Linux). Locally:

    cd backend
    MAIA3_ENABLED=1 .venv/bin/python scripts/prepare_maia3_engine.py
    .venv/bin/python -W error -m pytest -c pytest.ini -m release_seal test_maia3_engine_release.py

It needs requirements-maia3.txt installed and the weights fetched, and skips with
a reason otherwise; with ``GHOSTREPLAY_REQUIRE_MAIA3_TESTS=1`` (the CI job) it
fails instead. MAIA3_MODEL and MAIA3_WEIGHTS_DIR select the checkpoint, as for
the API.

No assertion depends on how fast the runner is. A test that needs a slow move,
or a spawn that has not finished, stops the child with SIGSTOP instead of racing
it, and resumes or kills it in a finally, so a failing test never leaves a
stopped process. The short limits a test sets are a second apart (request budget
and its floor, request budget and inference ceiling), around steps that take
microseconds, and what runs after the stop runs under generous limits. Each test
links the weights into its own tmp dir, which appears in the child's argv, so
``pgrep -f`` finds only that test's children.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import logging
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import NoReturn

import chess
import pytest

from app import maia3_pins
from app.maia3_client import (
    Maia3EngineService,
    Maia3Error,
    Maia3Settings,
    maia3_settings,
    position_for,
)

BACKEND = Path(__file__).resolve().parent
READY_S = 30.0
# Opening-book positions at plies 8-20 (sequences of UCI moves from the start).
# g-maia3-strength-probe repoints the 600/2600 check at its probe fixture.
DISAGREEMENT_LINES = [
    # Ruy Lopez, Closed (ply 10)
    "e2e4 e7e5 g1f3 b8c6 f1b5 a7a6 b5a4 g8f6 e1g1 f8e7",
    # Sicilian Najdorf (ply 10)
    "e2e4 c7c5 g1f3 d7d6 d2d4 c5d4 f3d4 g8f6 b1c3 a7a6",
    # Queen's Gambit Declined (ply 8)
    "d2d4 d7d5 c2c4 e7e6 b1c3 g8f6 c1g5 f8e7",
    # King's Indian, Classical (ply 12)
    "d2d4 g8f6 c2c4 g7g6 b1c3 f8g7 e2e4 d7d6 g1f3 e8g8 f1e2 e7e5",
    # Italian Game, Giuoco Piano (ply 12)
    "e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 c2c3 g8f6 d2d3 d7d6 e1g1 e8g8",
    # French, Winawer (ply 8)
    "e2e4 e7e6 d2d4 d7d5 b1c3 f8b4 e4e5 c7c5",
    # Caro-Kann, Classical (ply 10)
    "e2e4 c7c6 d2d4 d7d5 b1c3 d5e4 c3e4 c8f5 e4g3 f5g6",
    # English, Symmetrical (ply 8)
    "c2c4 c7c5 b1c3 b8c6 g2g3 g7g6 f1g2 f8g7",
    # Slav (ply 10)
    "d2d4 d7d5 c2c4 c7c6 g1f3 g8f6 b1c3 d5c4 a2a4 c8f5",
    # Scotch (ply 14)
    "e2e4 e7e5 g1f3 b8c6 d2d4 e5d4 f3d4 g8f6 d4c6 b7c6 e4e5 d8e7 d1e2 f6d5",
    # Nimzo-Indian, Rubinstein (ply 16)
    "d2d4 g8f6 c2c4 e7e6 b1c3 f8b4 e2e3 e8g8 f1d3 d7d5 g1f3 c7c5 e1g1 b8c6 a2a3 b4c3",
    # Grünfeld, Exchange (ply 20)
    "d2d4 g8f6 c2c4 g7g6 b1c3 d7d5 c4d5 f6d5 e2e4 d5c3 b2c3 f8g7 f1c4 c7c5 "
    "g1e2 b8c6 c1e3 e8g8 e1g1 c5d4",
]

pytestmark = [
    pytest.mark.release_seal,
    pytest.mark.skipif(os.name != "posix", reason="SIGSTOP/SIGCONT need POSIX"),
]

REQUIRED = os.environ.get("GHOSTREPLAY_REQUIRE_MAIA3_TESTS") == "1"


def _unavailable(reason: str) -> NoReturn:
    if REQUIRED:
        pytest.fail(f"GHOSTREPLAY_REQUIRE_MAIA3_TESTS=1, but {reason}", pytrace=False)
    pytest.skip(reason)


@pytest.fixture(autouse=True)
def _maia3_installed() -> None:
    # find_spec, not importorskip: the test process never imports maia3 or
    # torch; only the engine child does.
    if importlib.util.find_spec("maia3") is None:
        _unavailable("maia3 is not installed (pip install -r requirements-maia3.txt)")


def _source_settings() -> Maia3Settings:
    # conftest forces MAIA3_ENABLED=0 for the API; the model and weights dir
    # still come from the environment.
    return maia3_settings()


@pytest.fixture
def weights_dir(tmp_path: Path) -> Path:
    """A unique dir holding a symlink to the fetched, verified checkpoint."""
    source = _source_settings()
    path = source.weights_path
    if not path.exists():
        _unavailable(
            f"{source.model} weights not fetched at {path} "
            "(MAIA3_ENABLED=1 python scripts/prepare_maia3_engine.py)"
        )
    maia3_pins.verify_weights(path, source.pin)
    linked = tmp_path / "weights"
    linked.mkdir()
    (linked / source.pin.filename).symlink_to(path.resolve())
    return linked


def _children(marker: Path) -> list[int]:
    out = subprocess.run(
        ["pgrep", "-f", str(marker)], capture_output=True, text=True
    ).stdout
    return [int(pid) for pid in out.split()]


def _wait_until(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(0.05)


@pytest.fixture
def make_service(weights_dir):
    made: list[Maia3EngineService] = []

    def make(backoff_base_s: float = 0.1, **overrides) -> Maia3EngineService:
        source = _source_settings()
        settings = Maia3Settings(
            enabled=True,
            model=source.model,
            weights_dir=weights_dir,
            threads=source.threads,
            **overrides,
        )
        svc = Maia3EngineService(settings, backoff_base_s=backoff_base_s)
        made.append(svc)
        return svc

    yield make
    for svc in made:
        svc.shutdown()
    _wait_until(lambda: not _children(weights_dir), 10.0, "engine children to exit")


def _board(line: str = "") -> chess.Board:
    moves = line.split()
    board = chess.Board()
    for uci in moves:
        board.push_uci(uci)
    presented, _history = position_for(board.fen(), moves)
    return presented


def _pid(svc: Maia3EngineService) -> int:
    handle = svc.handle
    assert handle is not None
    return handle.pid


def _with_generous_limits(svc: Maia3EngineService) -> None:
    """The request budget and the inference ceiling, for a move after a stop.

    The settings are frozen; the service reads them per request.
    """
    svc.settings = dataclasses.replace(
        svc.settings, move_timeout_s=READY_S, infer_timeout_s=READY_S
    )


@contextlib.contextmanager
def _stopped(pid: int):
    os.kill(pid, signal.SIGSTOP)
    try:
        yield
    finally:
        # In the ceiling test the child is already killed and reaped by now.
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGCONT)


def test_a_legal_move_from_the_start_at_600_and_2600(make_service):
    svc = make_service()
    assert svc.wait_ready(READY_S, stop_on_failure=True), svc.last_spawn_error

    board = _board()
    for elo in (600, 2600):
        move = svc.play(board, elo)
        assert move in board.legal_moves


def test_600_and_2600_disagree_at_temperature_0(make_service):
    svc = make_service(temperature=0.0)
    assert svc.wait_ready(READY_S, stop_on_failure=True), svc.last_spawn_error

    disagreements = []
    for line in DISAGREEMENT_LINES:
        board = _board(line)
        low, high = svc.play(board, 600), svc.play(board, 2600)
        assert low in board.legal_moves and high in board.legal_moves
        # T=0 is the argmax, so the answer is a function of the position and Elo.
        assert svc.play(board, 600) == low
        if low != high:
            disagreements.append((line, low.uci(), high.uci()))

    assert disagreements, "600 and 2600 played the same move in every position"


def test_an_abandoned_move_finishes_and_the_same_engine_serves_the_next(make_service):
    # The floor is not under test; 0.01 s leaves the budget check a second.
    svc = make_service(move_timeout_s=1.0, min_play_budget_s=0.01)
    assert svc.wait_ready(READY_S, stop_on_failure=True), svc.last_spawn_error
    pid = _pid(svc)
    board = _board()

    with _stopped(pid):
        with pytest.raises(Maia3Error, match="abandoned"):
            svc.play(board, 1500)

    # After SIGCONT the stalled move completes and releases the engine.
    _wait_until(lambda: not svc.play_lock.locked(), READY_S, "the stalled move")
    _with_generous_limits(svc)
    move = svc.play(board, 1500)

    assert move in board.legal_moves
    assert _pid(svc) == pid


def test_the_inference_ceiling_kills_a_stopped_engine_and_a_new_one_serves(make_service):
    # The request gives up a second before the ceiling, so it sees "abandoned"
    # and the ceiling then fires with no request waiting.
    svc = make_service(move_timeout_s=1.0, min_play_budget_s=0.01, infer_timeout_s=2.0)
    assert svc.wait_ready(READY_S, stop_on_failure=True), svc.last_spawn_error
    pid = _pid(svc)
    board = _board()

    with _stopped(pid):
        with pytest.raises(Maia3Error, match="abandoned"):
            svc.play(board, 1500)
        # The ceiling (2 s) kills the stopped child; SIGKILL works on it.
        _wait_until(
            lambda: svc.handle is None or svc.handle.pid != pid, READY_S,
            "the ceiling to retire the engine",
        )

    assert svc.wait_ready(READY_S), svc.last_spawn_error
    new_pid = _pid(svc)
    assert new_pid != pid
    _with_generous_limits(svc)
    assert svc.play(board, 1500) in board.legal_moves
    assert _pid(svc) == new_pid


def test_an_idle_engine_killed_by_sigkill_is_replaced_without_a_request(
    make_service, caplog
):
    svc = make_service()
    assert svc.wait_ready(READY_S, stop_on_failure=True), svc.last_spawn_error
    pid = _pid(svc)

    with caplog.at_level(logging.WARNING, logger="app.maia3_client"):
        os.kill(pid, signal.SIGKILL)
        _wait_until(
            lambda: svc.handle is None or svc.handle.pid != pid, READY_S,
            "the exit watcher to retire the engine",
        )
        assert svc.wait_ready(READY_S), svc.last_spawn_error

    assert _pid(svc) != pid
    exits = [r for r in caplog.records if "maia3 engine exited" in r.getMessage()]
    assert exits and "code=-9" in exits[0].getMessage()
    assert exits[0].levelno == logging.WARNING


def test_no_child_survives_a_clean_shutdown_and_asyncio_logs_no_error(
    make_service, weights_dir, caplog
):
    svc = make_service()
    assert svc.wait_ready(READY_S, stop_on_failure=True), svc.last_spawn_error
    board = _board()
    assert svc.play(board, 1500) in board.legal_moves
    assert _children(weights_dir) == [_pid(svc)]

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        svc.shutdown()

    assert _children(weights_dir) == []
    assert [r for r in caplog.records if r.name == "asyncio"] == []


@pytest.mark.skipif(sys.platform != "linux", reason="oom_score_adj is Linux only")
def test_the_engine_child_is_the_oom_killers_first_choice(make_service):
    svc = make_service()
    assert svc.wait_ready(READY_S, stop_on_failure=True), svc.last_spawn_error

    adj = Path(f"/proc/{_pid(svc)}/oom_score_adj").read_text().strip()

    assert adj == "1000"


_SHUTDOWN_DURING_SPAWN = textwrap.dedent(
    """
    import gc, os, signal, subprocess, sys, threading, time
    from pathlib import Path

    sys.path.insert(0, sys.argv[1])
    import chess.engine
    from app.maia3_client import Maia3EngineService, Maia3Settings

    # Record the child's pid. open_uci, the opener under test, still makes
    # every call.
    child = []
    spawned = threading.Event()
    popen = chess.engine.UciProtocol.popen.__func__

    async def popen_and_record(cls, *args, **kwargs):
        transport, protocol = await popen(cls, *args, **kwargs)
        child.append(transport.get_pid())
        spawned.set()
        return transport, protocol

    chess.engine.UciProtocol.popen = classmethod(popen_and_record)

    def rss_mb(pid):
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True
        ).stdout.strip()
        return int(out) // 1024 if out else 0

    settings = Maia3Settings(enabled=True, model=sys.argv[3], weights_dir=Path(sys.argv[2]))
    service = Maia3EngineService(settings)
    service.ensure_started()
    assert spawned.wait(30), "the engine child was never spawned"
    (pid,) = child
    # Past 100 MB torch is loading (the engine ends near 400 MB); the handshake
    # still needs the rest of torch, the weights and the warm-up. Stopping the
    # child there keeps the spawn in flight however fast the runner is, and
    # shutdown then kills a large process, which takes long enough to exit
    # that a transport dropped before the exit warns "still running".
    deadline = time.monotonic() + 30
    while rss_mb(pid) < 100:
        assert time.monotonic() < deadline, "the engine child never reached 100 MB"
        time.sleep(0.01)
    os.kill(pid, signal.SIGSTOP)
    print(f"in_flight={service.spawn_in_flight} ready={service.handle is not None}", flush=True)
    service.shutdown()
    del service
    # Collect the transports now: a "still running" warning is only written
    # when the subprocess object is collected.
    gc.collect()
    time.sleep(0.3)
    gc.collect()
    print("exiting", flush=True)
    """
)


def test_shutdown_during_spawn_exits_promptly_and_leaves_no_child(weights_dir):
    try:
        proc = subprocess.run(
            [
                sys.executable, "-W", "always::ResourceWarning", "-c",
                _SHUTDOWN_DURING_SPAWN, str(BACKEND), str(weights_dir),
                _source_settings().model,
            ],
            capture_output=True,
            text=True,
            timeout=READY_S,
        )

        assert proc.returncode == 0, proc.stderr
        # The child was stopped while torch loaded, so the spawn was in flight.
        assert "in_flight=True ready=False" in proc.stdout, proc.stdout
        assert "exiting" in proc.stdout
        assert "still running" not in proc.stderr, proc.stderr
        assert _children(weights_dir) == []
    finally:
        # A script that failed after its SIGSTOP leaves a stopped child.
        for pid in _children(weights_dir):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
