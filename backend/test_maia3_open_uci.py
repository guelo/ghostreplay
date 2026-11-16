"""The real UCI plumbing (``open_uci`` and ``_UciHandle``) against real children.

No torch and no weights: the children are small Python programs. The service
tests in ``test_maia3_client.py`` use fakes, and the real-engine test is
release-seal, so these are the pre-push gate's only check of the code that
talks to a process.

The first test's child is a plain Python sleep, so it never answers ``uci``. The
handshake is cancelled (or timed out) on a daemon-thread loop that is then
stopped and closed at once, as the service's shutdown does. ``open_uci`` must
kill the child AND wait for its exit before re-raising; otherwise the closed
loop never processes the exit and the subprocess object warns "subprocess N is
still running" when it is collected. Through ``chess.engine.popen_uci``, which
re-raises without waiting, this failed 13 of 15 runs in review, so a regression
fails here most of the time.

The second test runs the service against a stub engine that speaks UCI and logs
what it receives: spawn, a move, a SIGKILL, the respawn, another move and
shutdown.

Warnings are recorded explicitly, so neither test depends on the gate's
``-W error``.
"""

from __future__ import annotations

import asyncio
import collections
import gc
import hashlib
import os
import signal
import sys
import threading
import time
import warnings
from pathlib import Path

import chess
import chess.engine
import pytest

from app import maia3_pins
from app.maia3_client import (
    Maia3EngineService,
    Maia3Settings,
    engine_command,
    open_uci,
    position_for,
)

CHILD = [sys.executable, "-c", "import time; time.sleep(60)"]

# A UCI engine in a dozen lines: it answers the handshake, declares the Elo
# option the service sets, plays the first legal move in UCI order, and logs
# "<pid> <line>" for every line it receives.
STUB_ENGINE = """\
import os
import sys

import chess

log = open(sys.argv[1], "a", buffering=1)
board = chess.Board()


def say(*lines):
    sys.stdout.write("".join(line + "\\n" for line in lines))
    sys.stdout.flush()


for raw in sys.stdin:
    line = raw.strip()
    log.write(f"{os.getpid()} {line}\\n")
    tokens = line.split()
    if line == "uci":
        say(
            "id name StubMaia3",
            "option name Elo type spin default 1500 min 600 max 2600",
            "uciok",
        )
    elif line == "isready":
        say("readyok")
    elif tokens[:1] == ["position"]:
        moves_at = tokens.index("moves") if "moves" in tokens else len(tokens)
        if tokens[1] == "startpos":
            board = chess.Board()
        else:
            board = chess.Board(" ".join(tokens[2:moves_at]))
        for uci in tokens[moves_at + 1 :]:
            board.push_uci(uci)
    elif tokens[:1] == ["go"]:
        say("bestmove " + min(move.uci() for move in board.legal_moves))
    elif line == "quit":
        break
"""


@pytest.fixture
def child_pids(monkeypatch) -> list[int]:
    """Record the pid of every engine child UciProtocol.popen starts."""
    pids: list[int] = []
    original = chess.engine.UciProtocol.popen.__func__

    async def recording_popen(cls, *args, **kwargs):
        transport, protocol = await original(cls, *args, **kwargs)
        pids.append(transport.get_pid())
        return transport, protocol

    monkeypatch.setattr(chess.engine.UciProtocol, "popen", classmethod(recording_popen))
    return pids


async def _cancelled_handshake() -> type[BaseException]:
    _argv, env = engine_command(Maia3Settings(), "unused.pt", seed=1)
    task = asyncio.ensure_future(open_uci(CHILD, env))
    await asyncio.sleep(0.5)
    task.cancel()
    try:
        await task
    except BaseException as exc:
        return type(exc)
    raise AssertionError("open_uci answered a child that never speaks UCI")


async def _timed_out_handshake() -> type[BaseException]:
    _argv, env = engine_command(Maia3Settings(), "unused.pt", seed=1)
    try:
        await asyncio.wait_for(open_uci(CHILD, env), 0.5)
    except BaseException as exc:
        return type(exc)
    raise AssertionError("open_uci answered a child that never speaks UCI")


def _run_then_close_loop(scenario) -> type[BaseException]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        return asyncio.run_coroutine_threadsafe(scenario(), loop).result(timeout=10)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def _pid_is_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.parametrize(
    "scenario, expected",
    [
        (_cancelled_handshake, asyncio.CancelledError),
        (_timed_out_handshake, TimeoutError),
    ],
)
def test_an_abandoned_handshake_reaps_the_child_before_the_loop_closes(
    scenario, expected, child_pids
):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        outcome = _run_then_close_loop(scenario)
        gc.collect()

    assert outcome is expected
    assert len(child_pids) == 1
    assert _pid_is_gone(child_pids[0])
    resource_warnings = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert resource_warnings == [], [str(w.message) for w in resource_warnings]


def _wait_until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _lines_by_pid(log: Path) -> dict[int, list[str]]:
    received: dict[int, list[str]] = collections.defaultdict(list)
    for entry in log.read_text().splitlines():
        pid, _, line = entry.partition(" ")
        received[int(pid)].append(line)
    return received


def _first_legal(board: chess.Board) -> chess.Move:
    return min(board.legal_moves, key=lambda move: move.uci())


@pytest.fixture
def stub_settings(tmp_path, monkeypatch) -> Maia3Settings:
    weights = b"stub weights"
    pin = maia3_pins.ModelPin(
        name="maia3-stub",
        repo_id="test/maia3-stub",
        revision="0" * 40,
        filename="maia3-stub.pt",
        size=len(weights),
        sha256=hashlib.sha256(weights).hexdigest(),
    )
    monkeypatch.setitem(maia3_pins.MODEL_PINS, pin.name, pin)
    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()
    (weights_dir / pin.filename).write_bytes(weights)
    return Maia3Settings(
        enabled=True,
        model=pin.name,
        weights_dir=weights_dir,
        move_timeout_s=5.0,
        min_play_budget_s=0.01,
        infer_timeout_s=5.0,
        start_timeout_s=10.0,
    )


def test_the_service_drives_a_real_uci_child_through_a_kill_and_a_respawn(
    tmp_path, stub_settings, child_pids
):
    script = tmp_path / "stub_uci.py"
    script.write_text(STUB_ENGINE)
    log = tmp_path / "uci.log"
    log.touch()

    async def opener(argv: list[str], env: dict[str, str]):
        return await open_uci([sys.executable, str(script), str(log)], env)

    svc = Maia3EngineService(
        stub_settings,
        opener=opener,
        proc_root=tmp_path / "no-proc",
        backoff_base_s=0.05,
        backoff_cap_s=0.05,
    )
    history = ["e2e4", "e7e5", "g1f3"]
    target = chess.Board()
    for uci in history:
        target.push_uci(uci)
    board, history_used = position_for(target.fen(), history)
    assert history_used == 3
    expected = _first_legal(board)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            assert svc.wait_ready(10.0)
            first = svc.handle.pid
            assert svc.play(board, 1234) == expected

            os.kill(first, signal.SIGKILL)
            assert _wait_until(
                lambda: svc.handle is not None and svc.handle.pid != first, 10.0
            )
            second = svc.handle.pid
            assert svc.play(board, 1777) == expected
        finally:
            svc.shutdown()
        gc.collect()

    assert child_pids == [first, second]
    assert all(_pid_is_gone(pid) for pid in child_pids)
    assert svc.loop.is_closed()
    resource_warnings = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert resource_warnings == [], [str(w.message) for w in resource_warnings]

    received = _lines_by_pid(log)
    for pid, elo in ((first, 1234), (second, 1777)):
        lines = received[pid]
        assert lines[0] == "uci"
        # The warm-up position, then the request's position with its history.
        assert "position startpos moves e2e4" in lines
        assert f"setoption name Elo value {elo}" in lines
        assert "position startpos moves e2e4 e7e5 g1f3" in lines
        assert lines.count("go nodes 1") == 2
    assert "quit" not in received[first]  # SIGKILLed
    assert received[second][-1] == "quit"  # shut down cleanly
