"""Behaviour contracts for the local Maia3 engine service (``app.maia3_client``).

No torch, no weights and no subprocess here (``test_maia3_open_uci.py`` has the
real children). The service runs its real daemon loop against a fake opener
whose handles behave like a process: ``kill()`` resolves ``exited`` with -9, a
clean ``quit()`` with 0, and a move in flight fails like python-chess's when the
engine dies. Fakes block on gates the test opens, never on sleeps, and every
budget and backoff is in milliseconds, so nothing waits on a production default.
Every service is shut down in fixture teardown: a stopped, unclosed loop fails
the gate's ``python -W error`` run when it is collected.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import anyio
import chess
import chess.engine
import pytest
from fastapi.testclient import TestClient

import app.api.health as health_api
import app.main as main
import app.maia3_client as mc
from app import maia3_pins
from app.maia3_client import (
    ELO_BINS,
    Maia3EngineService,
    Maia3Error,
    Maia3Move,
    Maia3Settings,
    boot_weights_error,
    check_maia3_weights_at_boot,
    clamp_elo,
    engine_command,
    format_temperature_curve,
    get_move,
    maia3_settings,
    parse_temperature_curve,
    position_for,
    start_engine,
    temperature_at,
)

BACKEND_DIR = Path(__file__).resolve().parent
AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
FAKE_MODEL = "maia3-fake"
FAKE_WEIGHTS = b"not a real maia3 checkpoint"
FAKE_PIN = maia3_pins.ModelPin(
    name=FAKE_MODEL,
    repo_id="test/maia3-fake",
    revision="0" * 40,
    filename="maia3-fake.pt",
    size=len(FAKE_WEIGHTS),
    sha256=hashlib.sha256(FAKE_WEIGHTS).hexdigest(),
)


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


def _resolve(fut: asyncio.Future) -> None:
    if not fut.done():
        fut.set_result(None)


class Gate:
    """A latch any thread can open; fakes await it on the service loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open = False
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []

    def open(self) -> None:
        with self._lock:
            self._open = True
            waiters, self._waiters = self._waiters, []
        for loop, fut in waiters:
            try:
                loop.call_soon_threadsafe(_resolve, fut)
            except RuntimeError:
                pass  # that service's loop is already closed

    def future(self) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        with self._lock:
            if self._open:
                fut.set_result(None)
            else:
                self._waiters.append((loop, fut))
        return fut

    async def wait(self) -> None:
        await self.future()


def first_legal(board: chess.Board) -> chess.Move:
    return min(board.legal_moves, key=lambda move: move.uci())


class FakeHandle:
    def __init__(self, engine: FakeEngine, pid: int) -> None:
        self.engine = engine
        self.pid = pid
        self.loop = asyncio.get_running_loop()
        self.exited: asyncio.Future[int] = self.loop.create_future()
        self.kills = 0
        self.warmed = False
        self.plays: list[tuple[int, list[str], str]] = []  # request moves only
        self.temperatures: list[float] = []  # request moves only
        self.warmup_temperature: float | None = None

    async def ping(self) -> None:
        self.engine.events.append("ping")
        if self.engine.on_ping is not None:
            await self.engine.on_ping(self)

    async def play(
        self, board: chess.Board, elo: int, temperature: float
    ) -> chess.Move | None:
        if self.exited.done():
            raise chess.engine.EngineTerminatedError("engine process dead")
        stack = [move.uci() for move in board.move_stack]
        if not self.warmed:
            self.warmed = True
            self.warmup_temperature = temperature
            self.engine.events.append(("warmup", elo, stack))
            self.engine.warmup_started.set()
            behavior = self.engine.on_warmup
        else:
            self.plays.append((elo, stack, board.fen()))
            self.temperatures.append(temperature)
            self.engine.events.append(("move", elo))
            self.engine.move_started.set()
            behavior = self.engine.on_move
        if behavior is None:
            return first_legal(board)
        return await behavior(self, board, elo)

    async def quit(self) -> None:
        if self.engine.on_quit is not None:
            await self.engine.on_quit(self)
        self._exit(0)

    def kill(self) -> None:
        self.kills += 1
        self.exit_threadsafe(-9)

    def _exit(self, code: int) -> None:
        if not self.exited.done():
            self.exited.set_result(code)

    def exit_threadsafe(self, code: int) -> None:
        try:
            self.loop.call_soon_threadsafe(self._exit, code)
        except RuntimeError:
            pass

    async def until(self, gate: Gate) -> None:
        """Wait for ``gate``, or fail like python-chess if the engine dies first."""
        opened = gate.future()
        try:
            await asyncio.wait({opened, self.exited}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not opened.done():
                opened.cancel()
        if opened.cancelled():
            raise chess.engine.EngineTerminatedError("engine process dead")


class FakeEngine:
    """The opener plus scripted behaviour shared by every handle it opens."""

    def __init__(self) -> None:
        self.handles: list[FakeHandle] = []
        self.commands: list[tuple[list[str], dict[str, str]]] = []
        self.open_times: list[float] = []
        self.events: list[object] = []
        self.on_open = None  # async () -> None
        self.on_ping = None  # async (handle) -> None
        self.on_warmup = None  # async (handle, board, elo) -> move
        self.on_move = None  # async (handle, board, elo) -> move
        self.on_quit = None  # async (handle) -> None
        self.warmup_started = threading.Event()
        self.move_started = threading.Event()
        self.open_cancelled = False

    @property
    def opens(self) -> int:
        return len(self.open_times)

    async def opener(self, argv: list[str], env: dict[str, str]) -> FakeHandle:
        self.commands.append((argv, env))
        self.open_times.append(time.monotonic())
        self.events.append("open")
        if self.on_open is not None:
            try:
                await self.on_open()
            except asyncio.CancelledError:
                self.open_cancelled = True
                raise
        handle = FakeHandle(self, pid=40000 + len(self.handles))
        self.handles.append(handle)
        return handle


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


def in_thread(fn, *args) -> concurrent.futures.Future:
    fut: concurrent.futures.Future = concurrent.futures.Future()

    def run() -> None:
        try:
            fut.set_result(fn(*args))
        except BaseException as exc:
            fut.set_exception(exc)

    threading.Thread(target=run, daemon=True).start()
    return fut


def on_loop(svc: Maia3EngineService, fn, *args):
    async def call():
        return fn(*args)

    return asyncio.run_coroutine_threadsafe(call(), svc.loop).result(timeout=5)


def after_e4() -> chess.Board:
    board = chess.Board()
    board.push_uci("e2e4")
    return board


def raise_terminated():
    """A move that fails at once, without awaiting anything."""

    async def behavior(handle, board, elo):
        raise chess.engine.EngineTerminatedError("engine process dead (exit code: -9)")

    return behavior


def block_on(gate: Gate):
    """A move that answers once ``gate`` opens, or fails if the engine dies."""

    async def behavior(handle, board, elo):
        await handle.until(gate)
        return first_legal(board)

    return behavior


@pytest.fixture
def weights_dir(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "weights"
    directory.mkdir()
    (directory / FAKE_PIN.filename).write_bytes(FAKE_WEIGHTS)
    monkeypatch.setitem(maia3_pins.MODEL_PINS, FAKE_MODEL, FAKE_PIN)
    return directory


def fast_settings(weights_dir: Path, **overrides) -> Maia3Settings:
    values = dict(
        enabled=True,
        model=FAKE_MODEL,
        weights_dir=weights_dir,
        move_timeout_s=2.0,
        min_play_budget_s=0.01,
        infer_timeout_s=2.0,
        start_timeout_s=2.0,
    )
    values.update(overrides)
    return Maia3Settings(**values)


@pytest.fixture
def make_service(weights_dir, tmp_path):
    services: list[Maia3EngineService] = []

    def make(
        engine: FakeEngine,
        *,
        proc_root: Path | None = None,
        backoff_base_s: float = 10.0,
        backoff_cap_s: float = 60.0,
        **overrides,
    ) -> Maia3EngineService:
        svc = Maia3EngineService(
            fast_settings(weights_dir, **overrides),
            opener=engine.opener,
            proc_root=proc_root if proc_root is not None else tmp_path / "no-proc",
            backoff_base_s=backoff_base_s,
            backoff_cap_s=backoff_cap_s,
        )
        services.append(svc)
        return svc

    yield make
    for svc in services:
        svc.shutdown()


def ready(svc: Maia3EngineService) -> FakeHandle:
    assert svc.wait_ready(3.0)
    return svc.handle


# ---------------------------------------------------------------------------
# Position and Elo
# ---------------------------------------------------------------------------


# A 30-ply Ruy Lopez (Breyer).
RUY_LOPEZ_SAN = (
    "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7 Re1 b5 Bb3 d6 c3 O-O h3 Nb8 d4 Nbd7 "
    "Nbd2 Bb7 Bc2 Re8 Nf1 Bf8 Ng3 g6 a4 c5"
).split()


def _game(plies: int) -> list[str]:
    board = chess.Board()
    moves = []
    for san in RUY_LOPEZ_SAN[:plies]:
        move = board.parse_san(san)
        moves.append(move.uci())
        board.push(move)
    assert len(moves) == plies and not board.is_game_over()
    return moves


def _board_after(moves: list[str]) -> chess.Board:
    board = chess.Board()
    for uci in moves:
        board.push_uci(uci)
    return board


def test_history_that_replays_to_the_fen_sends_the_last_eight_plies():
    moves = _game(30)
    fen = _board_after(moves).fen()

    board, history_used = position_for(fen, moves)

    assert board.fen() == fen
    assert history_used == 8
    assert [move.uci() for move in board.move_stack] == moves[-8:]
    assert board.root().fen() == _board_after(moves[:-8]).fen()


def test_a_short_history_is_sent_whole_from_the_start():
    board, history_used = position_for(AFTER_E4_FEN, ["e2e4"])

    assert board.fen() == AFTER_E4_FEN
    assert history_used == 1
    assert board.root().fen() == chess.STARTING_FEN


def test_empty_moves_at_the_start_position():
    board, history_used = position_for(chess.STARTING_FEN, [])

    assert board.fen() == chess.STARTING_FEN
    assert board.move_stack == []
    assert history_used == 0


def test_moves_that_reach_another_position_send_the_fen_alone(caplog):
    with caplog.at_level(logging.WARNING, logger="app.maia3_client"):
        board, history_used = position_for(AFTER_E4_FEN, ["d2d4"])

    assert board.fen() == AFTER_E4_FEN
    assert board.move_stack == []
    assert history_used == 0
    assert any("history mismatch" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "moves", [["e2e4", "bogus"], ["e2e5"], ["0000"], ["e2e4", "e2e4"]]
)
def test_unparsable_or_illegal_history_sends_the_fen_alone(moves):
    board, history_used = position_for(AFTER_E4_FEN, moves)

    assert board.fen() == AFTER_E4_FEN
    assert board.move_stack == []
    assert history_used == 0


@pytest.mark.parametrize(
    "fen",
    [
        # White is checkmated (fool's mate).
        "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3",
        # Black is stalemated.
        "7k/5Q2/6K1/8/8/8/8/8 b - - 0 1",
        # Bare kings.
        "8/8/8/4k3/8/8/8/4K3 w - - 0 1",
    ],
)
def test_finished_positions_raise_before_the_engine_is_asked(fen, monkeypatch):
    monkeypatch.setenv("MAIA3_ENABLED", "1")
    monkeypatch.setattr(mc, "_get_service", Mock(side_effect=AssertionError("engine")))

    with pytest.raises(ValueError):
        get_move(fen, [], 1500)

    mc._get_service.assert_not_called()


def test_a_malformed_fen_raises_value_error():
    with pytest.raises(ValueError):
        position_for("not a fen", [])


@pytest.mark.parametrize(
    "elo, expected", [(600, 600), (1550, 1550), (100, 600), (3000, 2600)]
)
def test_clamp_elo_keeps_the_exact_value_inside_the_ladder(elo, expected):
    assert clamp_elo(elo) == expected


def test_clamp_range_is_the_ladder():
    assert (ELO_BINS[0], ELO_BINS[-1]) == (600, 2600)


@pytest.mark.parametrize("elo, expected", [(3000, 2600), (100, 600), (1550, 1550)])
def test_get_move_sends_the_clamped_elo_and_the_replayed_history(
    elo, expected, make_service, monkeypatch
):
    engine = FakeEngine()
    svc = make_service(engine)
    monkeypatch.setenv("MAIA3_ENABLED", "1")
    monkeypatch.setattr(mc, "_get_service", lambda: svc)

    result = get_move(AFTER_E4_FEN, ["e2e4"], elo)

    played_elo, stack, fen = engine.handles[0].plays[-1]
    assert played_elo == expected
    assert stack == ["e2e4"]
    assert fen == AFTER_E4_FEN
    assert result == Maia3Move(first_legal(chess.Board(AFTER_E4_FEN)).uci())


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------


def test_first_play_spawns_once_and_serves_concurrent_waiters(make_service):
    engine = FakeEngine()
    gate = Gate()
    engine.on_open = gate.wait
    svc = make_service(engine)

    # A request right after construction waits for the spawn instead of
    # failing "restarting": spawn_in_flight is set before ensure_started returns.
    svc.ensure_started()
    assert svc.spawn_in_flight
    first = in_thread(svc.play, after_e4(), 1500)
    second = in_thread(svc.play, after_e4(), 1500)
    assert wait_until(lambda: engine.opens == 1)
    gate.open()

    assert first.result(timeout=5)
    assert second.result(timeout=5)
    assert engine.opens == 1
    assert len(engine.handles[0].plays) == 2


def test_sequential_plays_reuse_one_handle(make_service):
    engine = FakeEngine()
    svc = make_service(engine)

    for _ in range(3):
        svc.play(after_e4(), 1500)

    assert engine.opens == 1
    assert len(engine.handles[0].plays) == 3


def test_spawn_order_and_no_request_before_the_warm_up_finishes(make_service):
    engine = FakeEngine()
    gate = Gate()

    async def warmup(handle, board, elo):
        await gate.wait()
        return first_legal(board)

    engine.on_warmup = warmup
    svc = make_service(engine)
    request = in_thread(svc.play, after_e4(), 1200)
    assert engine.warmup_started.wait(3)

    assert svc.handle is None
    assert engine.handles[0].plays == []
    assert not request.done()
    gate.open()

    assert request.result(timeout=5)
    assert engine.events == ["open", "ping", ("warmup", 1500, ["e2e4"]), ("move", 1200)]


def _failing_ping(gate):
    async def ping(handle):
        await gate.wait()
        raise chess.engine.EngineError("ping failed")

    return {"on_ping": ping}


def _failing_warmup(gate):
    async def warmup(handle, board, elo):
        await gate.wait()
        raise chess.engine.EngineError("warm-up failed")

    return {"on_warmup": warmup}


def _null_warmup(gate):
    async def warmup(handle, board, elo):
        await gate.wait()
        return chess.Move.null()

    return {"on_warmup": warmup}


@pytest.mark.parametrize("script", [_failing_ping, _failing_warmup, _null_warmup])
def test_a_failed_spawn_kills_the_handle_and_fails_waiters_at_once(script, make_service):
    engine = FakeEngine()
    gate = Gate()
    for name, behavior in script(gate).items():
        setattr(engine, name, behavior)
    svc = make_service(engine, move_timeout_s=5.0)
    request = in_thread(svc.play, after_e4(), 1500)
    assert wait_until(lambda: engine.handles)

    gate.open()
    opened_at = time.monotonic()
    with pytest.raises(Maia3Error, match="restarting"):
        request.result(timeout=5)

    assert time.monotonic() - opened_at < 1.0
    assert engine.handles[0].kills >= 1
    assert svc.handle is None
    assert svc.spawn_failures == 1
    assert svc.next_spawn_at > time.monotonic()


def test_ping_and_warm_up_share_one_start_deadline(make_service):
    engine = FakeEngine()
    start_timeout = 0.2

    async def slow_ping(handle):
        await asyncio.sleep(0.6 * start_timeout)

    async def slow_warmup(handle, board, elo):
        await asyncio.sleep(0.6 * start_timeout)
        return first_legal(board)

    engine.on_ping = slow_ping
    engine.on_warmup = slow_warmup
    svc = make_service(engine, start_timeout_s=start_timeout)

    assert not svc.wait_ready(3.0, stop_on_failure=True)
    assert "timed out" in svc.last_spawn_error
    assert engine.handles[0].kills >= 1
    assert svc.handle is None


# ---------------------------------------------------------------------------
# Request budget vs inference ceiling
# ---------------------------------------------------------------------------


def test_a_late_request_abandons_its_move_and_keeps_the_engine(make_service):
    engine = FakeEngine()
    gate = Gate()

    async def slow_move(handle, board, elo):
        await handle.until(gate)
        return first_legal(board)

    engine.on_move = slow_move
    svc = make_service(engine, move_timeout_s=0.2)
    handle = ready(svc)

    with pytest.raises(Maia3Error, match="abandoned"):
        svc.play(after_e4(), 1500)

    assert handle.kills == 0
    assert svc.play_lock.locked()
    engine.on_move = None
    gate.open()
    assert wait_until(lambda: not svc.play_lock.locked())

    assert svc.play(after_e4(), 1500)
    assert svc.handle is handle
    assert engine.opens == 1


def test_below_the_play_floor_after_the_lock_raises_busy_without_the_engine(make_service):
    engine = FakeEngine()
    svc = make_service(engine, move_timeout_s=1.0, min_play_budget_s=0.9)
    handle = ready(svc)

    svc.play_lock.acquire()  # a move in flight
    request = in_thread(svc.play, after_e4(), 1500)
    time.sleep(0.3)
    svc.play_lock.release()

    with pytest.raises(Maia3Error, match="busy"):
        request.result(timeout=5)
    assert handle.plays == []
    assert not svc.play_lock.locked()


def _hang(gate):
    async def move(handle, board, elo):
        await gate.wait()  # never opened: only the ceiling ends it

    return move


def _terminate(_gate):
    async def move(handle, board, elo):
        raise chess.engine.EngineTerminatedError("engine process dead (exit code: 1)")

    return move


@pytest.mark.parametrize("failure", [_hang, _terminate])
def test_an_engine_failure_retires_it_and_the_timer_respawns_after_backoff(
    failure, make_service
):
    engine = FakeEngine()
    svc = make_service(engine, infer_timeout_s=0.05, backoff_base_s=0.3)
    old = ready(svc)
    engine.on_move = failure(Gate())

    before = time.monotonic()
    with pytest.raises(Maia3Error):
        svc.play(after_e4(), 1500)

    assert old.kills >= 1
    assert svc.handle is None
    assert engine.opens == 1
    # No further requests: the retry timer brings a new engine up on its own,
    # after the backoff and not before.
    assert wait_until(lambda: svc.handle is not None, timeout=5)
    assert svc.handle is not old
    assert engine.open_times[1] >= before + 0.3


def test_an_instant_failure_does_its_bookkeeping_on_the_loop(make_service, monkeypatch):
    threads: dict[str, set[int]] = {"_retire": set(), "_bump_backoff": set()}
    for name in threads:
        original = getattr(Maia3EngineService, name)

        def recording(self, *args, _original=original, _name=name):
            threads[_name].add(threading.get_ident())
            return _original(self, *args)

        monkeypatch.setattr(Maia3EngineService, name, recording)

    engine = FakeEngine()
    base = 0.2
    svc = make_service(engine, backoff_base_s=base)
    ready(svc)
    engine.on_move = raise_terminated()

    before = time.monotonic()
    with pytest.raises(Maia3Error):
        svc.play(after_e4(), 1500)
    after = time.monotonic()

    # Already retired and backed off when play raised: no polling needed.
    assert svc.handle is None
    assert before + base <= svc.next_spawn_at <= after + base
    loop_ident = svc.loop_thread.ident
    assert threads["_retire"] == {loop_ident}
    assert threads["_bump_backoff"] == {loop_ident}
    engine.on_move = None
    assert wait_until(lambda: engine.opens == 2 and svc.handle is not None)


def test_an_idle_engine_death_is_retired_when_it_dies(make_service, caplog):
    engine = FakeEngine()
    svc = make_service(engine, backoff_base_s=0.02)
    old = ready(svc)

    with caplog.at_level(logging.WARNING, logger="app.maia3_client"):
        old.exit_threadsafe(-9)
        assert wait_until(lambda: svc.handle is not None and svc.handle is not old)

    messages = [r.getMessage() for r in caplog.records]
    assert any("code=-9" in m and "OOM" in m for m in messages)
    assert engine.opens == 2


def test_death_during_a_move_bumps_the_backoff_once_exit_first(make_service):
    engine = FakeEngine()
    gate = Gate()
    engine.on_move = block_on(gate)
    svc = make_service(engine, backoff_base_s=10.0)
    old = ready(svc)
    request = in_thread(svc.play, after_e4(), 1500)
    assert engine.move_started.wait(3)

    old.exit_threadsafe(-9)  # the watcher and the move's failure both see it

    with pytest.raises(Maia3Error):
        request.result(timeout=5)
    assert svc.handle is None
    assert svc.backoff_s == 20.0
    assert not svc.play_lock.locked()


def test_death_during_a_move_bumps_the_backoff_once_move_first(make_service):
    engine = FakeEngine()
    engine.on_move = raise_terminated()
    svc = make_service(engine, backoff_base_s=10.0)
    old = ready(svc)

    with pytest.raises(Maia3Error):
        svc.play(after_e4(), 1500)
    old.exit_threadsafe(-9)
    on_loop(svc, lambda: None)  # let the exit callback run

    assert svc.backoff_s == 20.0
    assert svc.handle is None


@pytest.mark.parametrize("null", [chess.Move.null(), None])
def test_a_null_move_raises_and_keeps_the_engine(null, make_service):
    engine = FakeEngine()

    async def no_move(handle, board, elo):
        return null

    engine.on_move = no_move
    svc = make_service(engine)
    handle = ready(svc)

    with pytest.raises(Maia3Error, match="no move"):
        svc.play(after_e4(), 1500)

    assert svc.handle is handle
    assert handle.kills == 0


def test_a_second_request_behind_a_held_move_raises_busy_within_its_budget(make_service):
    engine = FakeEngine()
    gate = Gate()
    engine.on_move = block_on(gate)
    svc = make_service(engine, move_timeout_s=0.2)
    ready(svc)
    first = in_thread(svc.play, after_e4(), 1500)
    assert engine.move_started.wait(3)

    started = time.monotonic()
    with pytest.raises(Maia3Error, match="busy"):
        svc.play(after_e4(), 1500)
    assert time.monotonic() - started < 1.0

    # The first request is abandoned at its own deadline, unless its thread
    # wakes after the move finished, when the finished move decides. Either way
    # it ends, and the lock is released.
    gate.open()
    concurrent.futures.wait([first], timeout=5)
    assert first.done()
    assert wait_until(lambda: not svc.play_lock.locked())


def test_queued_requests_after_a_crash_never_touch_the_dead_engine(make_service):
    engine = FakeEngine()
    gate = Gate()
    calls = {"n": 0}

    async def crash_once(handle, board, elo):
        calls["n"] += 1
        await gate.wait()
        raise chess.engine.EngineTerminatedError("engine process dead (exit code: -9)")

    engine.on_move = crash_once
    svc = make_service(engine, backoff_base_s=10.0)
    old = ready(svc)
    leader = in_thread(svc.play, after_e4(), 1500)
    assert engine.move_started.wait(3)
    queued = [in_thread(svc.play, after_e4(), 1500) for _ in range(3)]
    time.sleep(0.1)  # let them reach play_lock

    crashed_at = time.monotonic()
    gate.open()
    with pytest.raises(Maia3Error):
        leader.result(timeout=5)
    for request in queued:
        with pytest.raises(Maia3Error, match="restarting"):
            request.result(timeout=5)

    assert calls["n"] == 1
    assert len(old.plays) == 1
    assert svc.backoff_s == 20.0  # bumped once, not once per queued request
    assert svc.next_spawn_at < crashed_at + 10.0 + 1.0
    next_spawn_at = svc.next_spawn_at
    on_loop(svc, svc._retire, old)  # a retired handle: no-op
    assert (svc.backoff_s, svc.next_spawn_at) == (20.0, next_spawn_at)


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


def test_a_spawn_failure_fails_fast_during_the_backoff(make_service):
    engine = FakeEngine()

    async def broken():
        raise OSError("no such engine")

    engine.on_open = broken
    svc = make_service(engine, backoff_base_s=10.0)
    assert not svc.wait_ready(3.0, stop_on_failure=True)

    started = time.monotonic()
    with pytest.raises(Maia3Error, match="restarting"):
        svc.play(after_e4(), 1500)
    assert time.monotonic() - started < 0.5
    assert engine.opens == 1


def test_the_retry_timer_respawns_with_a_doubling_capped_backoff(make_service):
    engine = FakeEngine()

    async def broken():
        raise OSError("no such engine")

    engine.on_open = broken
    svc = make_service(engine, backoff_base_s=0.05, backoff_cap_s=0.2)
    svc.ensure_started()

    assert wait_until(lambda: engine.opens >= 5, timeout=5)
    gaps = [b - a for a, b in zip(engine.open_times, engine.open_times[1:])]
    for gap, expected in zip(gaps, [0.05, 0.1, 0.2, 0.2]):
        assert gap >= expected - 0.01
        assert gap < expected + 0.5
    assert svc.backoff_s == 0.2


def test_the_retry_timer_ends_the_backoff_even_when_it_fires_early(make_service):
    """asyncio may run a timer a clock tick before its deadline. The timer firing
    is the end of the backoff, so it must spawn rather than re-check the clock
    and find the window still open, which would leave no retry scheduled."""
    engine = FakeEngine()

    async def broken():
        raise OSError("no such engine")

    engine.on_open = broken
    svc = make_service(engine, backoff_base_s=10.0)
    assert not svc.wait_ready(3.0, stop_on_failure=True)
    assert time.monotonic() < svc.next_spawn_at

    def fire_the_timer_now():
        svc._retry_timer.cancel()
        svc._on_retry_timer()

    on_loop(svc, fire_the_timer_now)

    assert wait_until(lambda: engine.opens == 2)


def test_an_engine_that_dies_on_every_move_is_backed_off_too(make_service):
    engine = FakeEngine()
    engine.on_move = raise_terminated()
    base = 0.05
    svc = make_service(engine, backoff_base_s=base)
    ready(svc)
    assert svc.backoff_s == base

    with pytest.raises(Maia3Error):
        svc.play(after_e4(), 1500)
    assert wait_until(lambda: svc.handle is not None)
    # A successful warm-up does not reset the backoff.
    assert svc.backoff_s == 2 * base

    with pytest.raises(Maia3Error):
        svc.play(after_e4(), 1500)
    assert svc.backoff_s == 4 * base
    # During the backoff the request path never spawns.
    for _ in range(20):
        with pytest.raises(Maia3Error):
            svc.play(after_e4(), 1500)
    assert engine.opens == 2

    engine.on_move = None
    assert wait_until(lambda: svc.handle is not None)
    assert svc.play(after_e4(), 1500)
    assert svc.backoff_s == base  # a successful request move resets it
    assert engine.opens == 3


def _remove(path: Path) -> None:
    path.unlink()


def _corrupt(path: Path) -> None:
    path.write_bytes(b"x" * len(FAKE_WEIGHTS))  # right size, wrong sha256


@pytest.mark.parametrize("damage", [_remove, _corrupt])
def test_bad_weights_never_reach_the_opener(damage, make_service, weights_dir, caplog):
    damage(weights_dir / FAKE_PIN.filename)
    engine = FakeEngine()
    svc = make_service(engine)

    with caplog.at_level(logging.ERROR, logger="app.maia3_client"):
        assert not svc.wait_ready(3.0, stop_on_failure=True)

    assert engine.opens == 0
    assert any("weights missing or corrupt" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def test_shutdown_closes_the_engine_and_later_plays_fail_at_once(make_service, monkeypatch):
    calls = {"n": 0}
    original = Maia3EngineService._shutdown_on_loop

    def counting(self):
        calls["n"] += 1
        return original(self)

    monkeypatch.setattr(Maia3EngineService, "_shutdown_on_loop", counting)
    engine = FakeEngine()
    svc = make_service(engine, move_timeout_s=5.0)
    handle = ready(svc)

    svc.shutdown()

    assert svc.handle is None
    assert svc.loop.is_closed()
    assert handle.exited.done()
    started = time.monotonic()
    with pytest.raises(Maia3Error, match="shut down"):
        svc.play(after_e4(), 1500)
    assert time.monotonic() - started < 0.5
    assert handle.plays == []
    assert not svc.play_lock.locked()
    assert not svc.wait_ready(1.0)

    svc.shutdown()  # returns at once and submits nothing to the closed loop
    assert calls["n"] == 1
    assert engine.opens == 1


def test_shutdown_wakes_waiters_on_a_spawn(make_service):
    engine = FakeEngine()
    engine.on_open = Gate().wait  # never opened
    svc = make_service(engine, move_timeout_s=5.0)
    request = in_thread(svc.play, after_e4(), 1500)
    waiter = in_thread(svc.wait_ready, 5.0)
    assert wait_until(lambda: engine.opens == 1)

    started = time.monotonic()
    svc.shutdown()

    with pytest.raises(Maia3Error, match="shut down"):
        request.result(timeout=5)
    assert waiter.result(timeout=5) is False
    assert time.monotonic() - started < 2.0


def test_shutdown_cancels_a_pending_retry_timer(make_service):
    engine = FakeEngine()

    async def broken():
        raise OSError("no such engine")

    engine.on_open = broken
    svc = make_service(engine, backoff_base_s=0.1)
    assert not svc.wait_ready(3.0, stop_on_failure=True)

    svc.shutdown()
    time.sleep(0.3)  # well past the backoff

    assert engine.opens == 1


def test_a_request_after_shutdown_engine_builds_no_new_engine(make_service, monkeypatch):
    engine = FakeEngine()
    svc = make_service(engine)
    ready(svc)
    monkeypatch.setattr(mc, "_service", svc)
    monkeypatch.setenv("MAIA3_ENABLED", "1")

    mc.shutdown_engine()

    with pytest.raises(Maia3Error, match="shut down"):
        get_move(AFTER_E4_FEN, ["e2e4"], 1500)
    assert mc._get_service() is svc
    assert engine.opens == 1


def test_start_engine_after_shutdown_engine_replaces_the_closed_service(
    make_service, monkeypatch
):
    """A second lifespan in one process gets a working engine, not a no-op."""
    engine = FakeEngine()
    first = make_service(engine)
    ready(first)
    monkeypatch.setattr(mc, "_service", first)
    monkeypatch.setattr(mc, "Maia3EngineService", lambda settings: make_service(engine))
    monkeypatch.setenv("MAIA3_ENABLED", "1")

    mc.shutdown_engine()
    start_engine()

    second = mc._get_service()
    assert second is not first
    assert second.wait_ready(3.0)
    expected = first_legal(chess.Board(AFTER_E4_FEN)).uci()
    assert get_move(AFTER_E4_FEN, ["e2e4"], 1500) == Maia3Move(expected)
    assert engine.opens == 2


def test_an_exit_wait_defers_repeated_cancels_to_the_end_of_its_timeout():
    """A cancel must not cut the wait for a killed child short: the loop may be
    closed right after, and an unprocessed exit warns at collection."""

    async def scenario() -> tuple[float, bool]:
        loop = asyncio.get_running_loop()
        never_exits: asyncio.Future[int] = loop.create_future()
        started = loop.time()
        task = asyncio.ensure_future(mc._wait_for_exit(never_exits, 0.3))
        for _ in range(2):
            await asyncio.sleep(0.05)
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return loop.time() - started, task.cancelled()

    elapsed, cancelled = asyncio.run(scenario())

    assert elapsed >= 0.3
    assert cancelled


def test_an_exit_wait_ends_at_the_exit_and_still_raises_the_cancel():
    async def scenario() -> tuple[float, bool]:
        loop = asyncio.get_running_loop()
        exited: asyncio.Future[int] = loop.create_future()
        started = loop.time()
        task = asyncio.ensure_future(mc._wait_for_exit(exited, 5.0))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        exited.set_result(-9)
        with pytest.raises(asyncio.CancelledError):
            await task
        return loop.time() - started, task.cancelled()

    elapsed, cancelled = asyncio.run(scenario())

    assert elapsed < 1.0
    assert cancelled


def test_a_service_that_never_started_has_no_loop(make_service):
    svc = make_service(FakeEngine())

    svc.shutdown()

    assert svc.loop is None


def test_shutdown_is_bounded_when_quit_hangs(make_service):
    engine = FakeEngine()

    async def hang(handle):
        await Gate().wait()

    engine.on_quit = hang
    svc = make_service(engine)
    handle = ready(svc)

    started = time.monotonic()
    svc.shutdown()

    assert time.monotonic() - started < 2.0
    assert handle.kills >= 1
    assert handle.exited.result() == -9
    assert svc.loop.is_closed()


def test_shutdown_while_the_opener_is_blocked_cancels_it(make_service):
    engine = FakeEngine()
    engine.on_open = Gate().wait
    svc = make_service(engine)
    svc.ensure_started()
    assert wait_until(lambda: engine.opens == 1)

    svc.shutdown()

    assert engine.open_cancelled
    assert engine.handles == []
    assert svc.handle is None


def test_shutdown_during_the_warm_up_kills_the_unpublished_handle(make_service):
    engine = FakeEngine()
    gate = Gate()

    async def warmup(handle, board, elo):
        await handle.until(gate)
        return first_legal(board)

    engine.on_warmup = warmup
    svc = make_service(engine)
    svc.ensure_started()
    assert engine.warmup_started.wait(3)

    svc.shutdown()

    assert engine.handles[0].kills >= 1
    assert engine.handles[0].exited.done()
    assert svc.handle is None


def test_shutdown_with_a_move_in_flight(make_service, caplog):
    engine = FakeEngine()
    gate = Gate()
    engine.on_move = block_on(gate)
    svc = make_service(engine, move_timeout_s=5.0)
    ready(svc)
    request = in_thread(svc.play, after_e4(), 1500)
    assert engine.move_started.wait(3)

    with caplog.at_level(logging.DEBUG):
        svc.shutdown()
        with pytest.raises(Maia3Error):
            request.result(timeout=5)

    assert not svc.play_lock.locked()
    assert svc._retry_timer is None
    assert svc.loop.is_closed()
    assert not [r for r in caplog.records if r.name == "asyncio" and r.levelno >= logging.ERROR]


def test_wait_ready_returns_true_once_published(make_service):
    svc = make_service(FakeEngine())

    assert svc.wait_ready(3.0)
    assert svc.handle is not None


def test_wait_ready_times_out_while_the_opener_is_blocked(make_service):
    engine = FakeEngine()
    engine.on_open = Gate().wait
    svc = make_service(engine)

    started = time.monotonic()
    assert not svc.wait_ready(0.1)
    assert time.monotonic() - started < 1.0


def test_wait_ready_stop_on_failure_returns_after_one_failed_spawn(make_service):
    engine = FakeEngine()

    async def broken():
        raise OSError("boom")

    engine.on_open = broken
    svc = make_service(engine, backoff_base_s=10.0)

    started = time.monotonic()
    assert not svc.wait_ready(5.0, stop_on_failure=True)
    assert time.monotonic() - started < 1.0
    assert "boom" in svc.last_spawn_error


def test_wait_ready_waits_through_the_backoff(make_service):
    engine = FakeEngine()
    failures = {"left": 1}

    async def flaky():
        if failures["left"]:
            failures["left"] -= 1
            raise OSError("first spawn fails")

    engine.on_open = flaky
    svc = make_service(engine, backoff_base_s=0.05)

    assert svc.wait_ready(3.0)
    assert engine.opens == 2


def test_wait_ready_returns_false_once_closed(make_service):
    engine = FakeEngine()
    engine.on_open = Gate().wait
    svc = make_service(engine)
    waiter = in_thread(svc.wait_ready, 5.0)
    assert wait_until(lambda: engine.opens == 1)

    svc.shutdown()

    assert waiter.result(timeout=5) is False


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_oom_score_adj_is_raised_for_the_engine(make_service, tmp_path):
    proc = tmp_path / "proc"
    (proc / "40000").mkdir(parents=True)
    svc = make_service(FakeEngine(), proc_root=proc)

    ready(svc)

    assert (proc / "40000" / "oom_score_adj").read_text() == "1000"


def test_an_oom_score_adj_write_failure_does_not_fail_the_spawn(
    make_service, tmp_path, caplog
):
    proc = tmp_path / "proc"
    proc.mkdir()  # no <pid> directory, so the write fails
    svc = make_service(FakeEngine(), proc_root=proc)

    with caplog.at_level(logging.WARNING, logger="app.maia3_client"):
        assert svc.wait_ready(3.0)

    warnings = [r for r in caplog.records if "oom_score_adj" in r.getMessage()]
    assert len(warnings) == 1


def test_stderr_filter_drops_only_upstream_progress_lines(caplog):
    lines = [
        "resolving Maia3 checkpoint maia3-23m",
        "loading Maia3 checkpoint /app/maia3_weights/maia3-23m.pt",
        "Maia3 ready",
        "warning: missing keys ['head.bias']",
        "Traceback (most recent call last):",
    ]

    async def feed():
        protocol = chess.engine.UciProtocol()
        for line in lines:
            protocol.error_line_received(line)

    with caplog.at_level(logging.WARNING, logger="chess.engine"):
        asyncio.run(feed())

    messages = [r.getMessage() for r in caplog.records if r.name == "chess.engine"]
    assert all(r.levelno == logging.WARNING for r in caplog.records if r.name == "chess.engine")
    assert len(messages) == 2
    assert any("warning: missing keys" in m for m in messages)
    assert any("Traceback" in m for m in messages)


def test_engine_command_is_the_contract_with_the_child(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/nix/store/lib")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("JWT_SECRET", "secret")
    monkeypatch.setenv("FOO", "bar")
    settings = Maia3Settings(temperature=0.0, top_p=0.9, threads=2)
    weights = tmp_path / "maia3-23m.pt"

    argv, env = engine_command(settings, weights, seed=1234)

    assert argv[:3] == [sys.executable, "-m", "maia3.uci"]
    joined = " ".join(argv)
    for flag in (
        "--use-uci-history",
        "--multipv 1",
        "--device cpu",
        "--no-use-amp",
        f"--checkpoint-path {weights}",
        "--model maia3-23m",
        "--top-p 0.9",
        "--seed 1234",
    ):
        assert flag in joined
    # The temperature travels with each move, never on the command line.
    assert "--temperature" not in argv
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["OMP_NUM_THREADS"] == env["MKL_NUM_THREADS"] == "2"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["LD_LIBRARY_PATH"] == "/nix/store/lib"
    for secret in ("DATABASE_URL", "JWT_SECRET", "FOO"):
        assert secret not in env


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_defaults():
    settings = maia3_settings({})

    assert settings == Maia3Settings(
        enabled=False,
        model="maia3-23m",
        weights_dir=maia3_pins.default_weights_dir(),
        temperature=None,
        temperature_curve=None,
        top_p=1.0,
        threads=1,
        move_timeout_s=5.0,
        min_play_budget_s=0.5,
        infer_timeout_s=10.0,
        start_timeout_s=60.0,
    )
    assert settings.weights_path == BACKEND_DIR / "maia3_weights" / "maia3-23m.pt"


@pytest.mark.parametrize("raw, enabled", [("0", False), ("1", True)])
def test_maia3_enabled_parsing(raw, enabled):
    assert maia3_settings({"MAIA3_ENABLED": raw}).enabled is enabled


@pytest.mark.parametrize(
    "env",
    [
        {"MAIA3_ENABLED": "true"},
        {"MAIA3_ENABLED": "2"},
        {"MAIA3_ENABLED": ""},
        {"MAIA3_MODEL": "maia3-79m"},
        {"MAIA3_TEMPERATURE": "-0.1"},
        {"MAIA3_TEMPERATURE": "warm"},
        {"MAIA3_TEMPERATURE": "nan"},
        {"MAIA3_TEMPERATURE": "2.5"},
        {"MAIA3_TEMPERATURE": ""},
        {"MAIA3_TEMPERATURE_CURVE": ""},
        {"MAIA3_TEMPERATURE_CURVE": "600"},
        {"MAIA3_TEMPERATURE_CURVE": "600:"},
        {"MAIA3_TEMPERATURE_CURVE": "600:1,"},
        {"MAIA3_TEMPERATURE_CURVE": "600:1:2"},
        {"MAIA3_TEMPERATURE_CURVE": "abc:1"},
        {"MAIA3_TEMPERATURE_CURVE": "600.5:1"},
        {"MAIA3_TEMPERATURE_CURVE": "600:warm"},
        {"MAIA3_TEMPERATURE_CURVE": "600:nan"},
        {"MAIA3_TEMPERATURE_CURVE": "600:-0.1"},
        {"MAIA3_TEMPERATURE_CURVE": "600:2.5"},
        {"MAIA3_TEMPERATURE_CURVE": "1500:1,600:0.8"},
        {"MAIA3_TEMPERATURE_CURVE": "600:1,600:0.8"},
        {"MAIA3_TOP_P": "0"},
        {"MAIA3_TOP_P": "1.5"},
        {"MAIA3_THREADS": "0"},
        {"MAIA3_THREADS": "1.5"},
        {"MAIA3_MOVE_TIMEOUT_S": "0.5"},
        {"MAIA3_MOVE_TIMEOUT_S": "inf"},
        {"MAIA3_MIN_PLAY_BUDGET_S": "0"},
        {"MAIA3_INFER_TIMEOUT_S": "0"},
        {"MAIA3_START_TIMEOUT_S": "-1"},
    ],
)
def test_invalid_settings_raise_value_error(env):
    with pytest.raises(ValueError):
        maia3_settings(env)


# ---------------------------------------------------------------------------
# Temperature per Elo
# ---------------------------------------------------------------------------

THREE_ANCHORS = ((600, 0.75), (1100, 0.7), (1500, 1.0))
# Anchors beyond the ladder on both sides, so a clamped Elo and an unclamped
# one read different temperatures.
WIDE_CURVE = ((100, 0.0), (600, 0.2), (2600, 1.0), (3000, 2.0))


@pytest.mark.parametrize(
    "elo, expected",
    [
        (300, 0.75),  # below the first anchor
        (600, 0.75),
        (850, 0.725),  # halfway between two anchors
        (1100, 0.7),
        (1300, 0.85),
        (1500, 1.0),
        (2600, 1.0),  # above the last anchor
    ],
)
def test_a_curve_is_linear_between_anchors_and_flat_outside(elo, expected):
    assert temperature_at(THREE_ANCHORS, elo) == expected


@pytest.mark.parametrize("elo", [600, 1500, 2600])
def test_a_one_anchor_curve_is_the_same_temperature_everywhere(elo):
    assert temperature_at(((1500, 0.4),), elo) == 0.4


def test_the_temperature_is_rounded_to_four_decimals():
    assert temperature_at(((600, 0.0), (900, 1.0)), 700) == 0.3333


def test_a_curve_string_parses_with_spaces_and_formats_back():
    curve = parse_temperature_curve(" 600:0.75 , 1100 : 0.7,1500:1 ")

    assert curve == THREE_ANCHORS
    assert parse_temperature_curve(format_temperature_curve(curve)) == curve
    assert maia3_settings({"MAIA3_TEMPERATURE_CURVE": "600:0.75"}).curve == ((600, 0.75),)


@pytest.mark.parametrize(
    "overrides",
    [
        {"temperature_curve": ()},
        {"temperature_curve": ((600, 1.0), (600, 0.8))},
        {"temperature_curve": ((1500, 1.0), (600, 0.8))},
        {"temperature_curve": ((600, float("nan")),)},
        {"temperature_curve": ((600.0, 1.0),)},
        {"temperature_curve": [(600, 1.0)]},
        {"temperature": float("nan")},
        {"temperature": 2.5},
        {"temperature": -0.1},
    ],
)
def test_settings_refuse_an_unusable_temperature(overrides):
    with pytest.raises(ValueError):
        Maia3Settings(**overrides)


def test_a_flat_temperature_wins_over_the_curve():
    settings = maia3_settings(
        {"MAIA3_TEMPERATURE": "0.4", "MAIA3_TEMPERATURE_CURVE": "600:0.2,2600:1.0"}
    )

    assert [settings.temperature_for(elo) for elo in (600, 2600)] == [0.4, 0.4]
    label = settings.temperature_label()
    assert label.startswith("flat 0.4 (MAIA3_TEMPERATURE;") and "ignored" in label
    flat_only = maia3_settings({"MAIA3_TEMPERATURE": "0"})
    assert flat_only.temperature_label() == "flat 0.0 (MAIA3_TEMPERATURE)"


def test_a_default_curve_belongs_to_one_model(monkeypatch):
    monkeypatch.setitem(mc.DEFAULT_TEMPERATURE_CURVES, "maia3-23m", ((600, 0.2), (2600, 1.0)))
    measured = Maia3Settings(model="maia3-23m")
    unmeasured = Maia3Settings(model="maia3-5m")

    assert [measured.temperature_for(elo) for elo in (600, 1600, 2600)] == [0.2, 0.6, 1.0]
    assert [unmeasured.temperature_for(elo) for elo in (600, 1500, 2600)] == [1.0, 1.0, 1.0]
    assert "default for maia3-23m" in measured.temperature_label()
    assert "maia3-5m" in unmeasured.temperature_label()
    # An explicit curve applies to whichever model is configured.
    for model in ("maia3-23m", "maia3-5m"):
        explicit = Maia3Settings(model=model, temperature_curve=((600, 0.5),))
        assert explicit.temperature_for(2000) == 0.5
        assert "(MAIA3_TEMPERATURE_CURVE)" in explicit.temperature_label()


def test_only_the_measured_model_may_have_a_default_curve():
    assert set(mc.DEFAULT_TEMPERATURE_CURVES) <= {"maia3-23m"}
    for model, curve in mc.DEFAULT_TEMPERATURE_CURVES.items():
        assert model in maia3_pins.MODEL_PINS
        # A default passes the same validation as an explicit curve.
        Maia3Settings(model=model, temperature_curve=curve)
    unmeasured = Maia3Settings(model="maia3-5m")
    assert {unmeasured.temperature_for(elo) for elo in ELO_BINS} == {1.0}


# The curve validated on 2026-11-23
# (docs/analysis/maia3-temperature-curve-2026-11-23.md), and what it gives each
# bot level.
VALIDATED_23M_CURVE = "600:0.72,1300:1.0,1500:1.0,2000:0.94,2300:0.7,2600:0.49"
VALIDATED_23M_LEVELS = {
    600: 0.72, 800: 0.8, 1000: 0.88, 1100: 0.92, 1200: 0.96, 1300: 1.0,
    1400: 1.0, 1500: 1.0, 1600: 0.988, 1700: 0.976, 1800: 0.964, 1900: 0.952,
    2000: 0.94, 2200: 0.78, 2400: 0.63, 2600: 0.49,
}


def test_the_validated_curve_is_the_default_for_maia3_23m():
    settings = maia3_settings({})

    assert settings.model == "maia3-23m"
    assert format_temperature_curve(settings.curve) == VALIDATED_23M_CURVE
    assert {elo: settings.temperature_for(elo) for elo in ELO_BINS} == VALIDATED_23M_LEVELS
    # 2300 is an anchor but not a bot level.
    assert settings.temperature_for(2300) == 0.7
    assert settings.temperature_label() == (
        f"curve {VALIDATED_23M_CURVE} (default for maia3-23m)"
    )


@pytest.mark.parametrize("elo", [600, 1500, 2600])
def test_maia3_5m_still_plays_at_temperature_1_by_default(elo):
    settings = maia3_settings({"MAIA3_MODEL": "maia3-5m"})

    assert settings.temperature_for(elo) == 1.0
    assert settings.temperature_label() == "flat 1.0 (no measured curve for maia3-5m)"


def test_maia3_temperature_1_turns_the_default_curve_off():
    """The documented rollback: one service variable, no deploy of code."""
    settings = maia3_settings({"MAIA3_TEMPERATURE": "1"})

    assert {settings.temperature_for(elo) for elo in ELO_BINS} == {1.0}
    assert settings.temperature_label() == "flat 1.0 (MAIA3_TEMPERATURE)"


def test_each_request_is_played_at_its_elos_temperature(make_service):
    engine = FakeEngine()
    svc = make_service(engine, temperature_curve=((600, 0.2), (2600, 1.0)))
    handle = ready(svc)

    for elo in (600, 1600, 2600):
        svc.play(after_e4(), elo)

    assert handle.temperatures == [0.2, 0.6, 1.0]
    assert handle.warmup_temperature == 0.56  # the curve at the warm-up's 1500


def test_a_flat_temperature_reaches_the_warm_up_and_every_request(make_service):
    engine = FakeEngine()
    svc = make_service(engine, temperature=0.0, temperature_curve=WIDE_CURVE)
    handle = ready(svc)

    for elo in (600, 2600):
        svc.play(after_e4(), elo)

    assert handle.temperatures == [0.0, 0.0]
    assert handle.warmup_temperature == 0.0


def test_the_probes_latency_wrapper_passes_the_temperature_through(
    weights_dir, tmp_path, monkeypatch
):
    """``probe_maia3.py --latency`` wraps every handle the service opens.

    A wrapper whose ``play`` lacks the temperature stops the warm-up, and the
    engine never starts. This fails when the service and the probe's wrapper
    are not changed in the same commit.
    """
    from scripts import probe_maia3 as probe

    engine = FakeEngine()
    monkeypatch.setattr(probe, "open_uci", engine.opener)
    timing = probe.SpawnTiming()
    svc = Maia3EngineService(
        fast_settings(weights_dir, temperature=0.4),
        opener=timing.opener,
        proc_root=tmp_path / "no-proc",
    )
    try:
        assert svc.wait_ready(3.0, stop_on_failure=True), svc.last_spawn_error
        svc.play(after_e4(), 1500)
    finally:
        svc.shutdown()

    assert engine.handles[0].warmup_temperature == 0.4
    assert engine.handles[0].temperatures == [0.4]
    assert {"spawn", "uciok", "readyok", "warmup"} <= set(timing.marks)


@pytest.mark.parametrize("elo, expected", [(100, 0.2), (3000, 1.0), (1600, 0.6)])
def test_get_move_clamps_the_elo_before_it_reads_the_curve(
    elo, expected, make_service, monkeypatch
):
    engine = FakeEngine()
    svc = make_service(engine, temperature_curve=WIDE_CURVE)
    monkeypatch.setenv("MAIA3_ENABLED", "1")
    monkeypatch.setattr(mc, "_get_service", lambda: svc)

    get_move(AFTER_E4_FEN, ["e2e4"], elo)

    assert engine.handles[0].temperatures == [expected]


# ---------------------------------------------------------------------------
# Disabled engine and the boot weights check
# ---------------------------------------------------------------------------


def test_disabled_get_move_raises_without_the_service(monkeypatch):
    assert maia3_settings().enabled is False  # conftest forces MAIA3_ENABLED=0
    monkeypatch.setattr(mc, "_get_service", Mock(side_effect=AssertionError("service")))

    with pytest.raises(Maia3Error, match="disabled"):
        get_move(AFTER_E4_FEN, ["e2e4"], 1500)

    mc._get_service.assert_not_called()


def test_disabled_start_engine_is_a_no_op(monkeypatch, caplog):
    monkeypatch.setattr(mc, "_get_service", Mock(side_effect=AssertionError("service")))

    with caplog.at_level(logging.INFO, logger="app.maia3_client"):
        start_engine()

    mc._get_service.assert_not_called()
    assert any(r.getMessage() == "maia3 engine disabled" for r in caplog.records)


@pytest.mark.parametrize("on_platform", [True, False])
@pytest.mark.parametrize("damage", [_remove, _corrupt])
def test_boot_check_records_bad_weights_without_raising(
    damage, on_platform, weights_dir, caplog
):
    damage(weights_dir / FAKE_PIN.filename)
    settings = fast_settings(weights_dir)

    with caplog.at_level(logging.ERROR, logger="app.maia3_client"):
        assert check_maia3_weights_at_boot(settings, on_platform) is None

    assert boot_weights_error() is not None
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_a_recorded_boot_error_fails_get_move_without_the_service(
    weights_dir, monkeypatch
):
    (weights_dir / FAKE_PIN.filename).unlink()
    check_maia3_weights_at_boot(fast_settings(weights_dir), on_platform=False)
    monkeypatch.setenv("MAIA3_ENABLED", "1")
    monkeypatch.setattr(mc, "_get_service", Mock(side_effect=AssertionError("service")))

    with pytest.raises(Maia3Error, match="weights missing"):
        get_move(AFTER_E4_FEN, ["e2e4"], 1500)

    mc._get_service.assert_not_called()


def test_a_disabled_boot_check_never_verifies(monkeypatch, weights_dir):
    monkeypatch.setattr(mc, "verify_weights", Mock(side_effect=AssertionError("verify")))

    check_maia3_weights_at_boot(fast_settings(weights_dir, enabled=False), True)

    assert boot_weights_error() is None
    mc.verify_weights.assert_not_called()


def test_a_passing_boot_check_overwrites_an_earlier_failure(weights_dir):
    path = weights_dir / FAKE_PIN.filename
    path.unlink()
    check_maia3_weights_at_boot(fast_settings(weights_dir), False)
    assert boot_weights_error() is not None

    path.write_bytes(FAKE_WEIGHTS)
    check_maia3_weights_at_boot(fast_settings(weights_dir), False)

    assert boot_weights_error() is None


def test_isolation_a_test_leaves_a_recorded_boot_error(weights_dir):
    (weights_dir / FAKE_PIN.filename).unlink()
    check_maia3_weights_at_boot(fast_settings(weights_dir), False)

    assert boot_weights_error() is not None


def test_isolation_the_next_test_starts_without_it():
    assert boot_weights_error() is None


def _patch_lifespan_dependencies(monkeypatch) -> dict[str, Mock]:
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    # Replace the module's engine rather than patching methods on the shared
    # SQLAlchemy Engine instance.
    db_engine = Mock()
    db_engine.connect = Mock(return_value=connection)
    monkeypatch.setattr(main, "engine", db_engine)
    mocks = {
        name: Mock()
        for name in (
            "get_scheduler", "get_delta_lane", "get_evidence_scheduler",
            "get_baseline_scheduler", "start_prewarm", "start_cleanup_job",
            "start_engine", "shutdown_engine",
        )
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(main, name, mock)
    return mocks


def _run_lifespan() -> None:
    async def exercise():
        async with main.lifespan(main.app):
            pass

    anyio.run(exercise)


def test_lifespan_with_missing_weights_boots_without_the_engine(
    monkeypatch, tmp_path
):
    mocks = _patch_lifespan_dependencies(monkeypatch)
    monkeypatch.setenv("MAIA3_ENABLED", "1")
    monkeypatch.setenv("MAIA3_WEIGHTS_DIR", str(tmp_path / "empty"))

    _run_lifespan()

    mocks["start_prewarm"].assert_called_once_with()
    mocks["start_engine"].assert_not_called()
    mocks["shutdown_engine"].assert_called_once_with()
    assert boot_weights_error() is not None


def test_lifespan_on_a_deploy_platform_with_missing_weights_fails_health(
    monkeypatch, tmp_path
):
    mocks = _patch_lifespan_dependencies(monkeypatch)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("MAIA3_ENABLED", "1")
    monkeypatch.setenv("MAIA3_WEIGHTS_DIR", str(tmp_path / "empty"))

    _run_lifespan()

    mocks["start_engine"].assert_not_called()
    assert TestClient(main.app).get("/health").status_code == 503


@pytest.mark.parametrize(
    ("weights_error", "on_platform", "expected_status", "expected_body"),
    [
        (
            "FileNotFoundError: missing",
            True,
            503,
            {"status": "error", "reason": "maia3 weights missing"},
        ),
        ("FileNotFoundError: missing", False, 200, {"status": "ok"}),
        (None, True, 200, {"status": "ok"}),
        (None, False, 200, {"status": "ok"}),
    ],
)
def test_health_fails_only_on_a_deploy_platform_with_bad_boot_weights(
    monkeypatch, weights_error, on_platform, expected_status, expected_body
):
    monkeypatch.setattr(health_api, "is_deploy_platform", lambda: on_platform)
    monkeypatch.setattr(mc, "_boot_weights_error", weights_error)

    response = TestClient(main.app).get("/health")

    assert response.status_code == expected_status
    assert response.json() == expected_body


def test_lifespan_with_verified_weights_starts_the_engine(monkeypatch, weights_dir):
    mocks = _patch_lifespan_dependencies(monkeypatch)
    monkeypatch.setenv("MAIA3_ENABLED", "1")
    monkeypatch.setenv("MAIA3_MODEL", FAKE_MODEL)
    monkeypatch.setenv("MAIA3_WEIGHTS_DIR", str(weights_dir))

    _run_lifespan()

    mocks["start_engine"].assert_called_once_with()
    mocks["shutdown_engine"].assert_called_once_with()
    assert boot_weights_error() is None


def test_lifespan_rejects_an_invalid_maia3_setting(monkeypatch):
    mocks = _patch_lifespan_dependencies(monkeypatch)
    monkeypatch.setenv("MAIA3_ENABLED", "yes")

    with pytest.raises(ValueError, match="MAIA3_ENABLED"):
        _run_lifespan()

    mocks["get_scheduler"].assert_not_called()
    mocks["start_engine"].assert_not_called()


# ---------------------------------------------------------------------------
# License boundary
# ---------------------------------------------------------------------------


def test_importing_the_api_loads_neither_maia3_nor_torch():
    """Decision 1: the engine runs out of process; the API never imports it."""
    script = (
        "import sys, app.main\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in ('maia3', 'torch'))\n"
        "print(loaded)\n"
        "sys.exit(1 if loaded else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
