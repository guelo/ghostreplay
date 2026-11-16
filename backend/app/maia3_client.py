"""Local Maia3 engine service: one long-lived UCI subprocess per API worker.

License boundary (decision 1 of g-maia3-local-engine). Maia3
(https://github.com/CSSLab/maia3) is AGPL-3.0. It runs unmodified, at the
upstream commit pinned in ``app.maia3_pins``, only as a separate process that
this module drives over UCI pipes, the way a chess GUI drives an engine. Nothing
in the API process imports ``maia3`` or ``torch``, and upstream is never
patched; a guard test checks that ``import app.main`` loads neither. Importing
maia3 in-process, or patching it, reopens that decision.

This module imports only the standard library, python-chess, ``app.fen`` and
``app.maia3_pins``, so build-time tooling can load it without app settings.

Shape of the service (``Maia3EngineService``):

* It owns an asyncio loop on a daemon thread. Every state change that follows
  a spawn, a move or an engine exit runs on that loop. Request threads only read
  state under the Condition, mark a spawn as in flight, and take ``play_lock``.
* The engine is spawned in the background and warmed up with one move before a
  request can use it. A request never spawns synchronously.
* A request's budget (``MAIA3_MOVE_TIMEOUT_S``) is separate from the per-move
  engine ceiling (``MAIA3_INFER_TIMEOUT_S``). A request that runs out abandons
  its move and leaves a healthy engine alone; only the ceiling, a crash or an
  engine error retires the engine.
* Every engine death costs exactly one backoff step, and a retry timer brings
  the engine back without waiting for a request.

``MAIA3_ENABLED`` defaults to 0. Disabled, ``get_move`` raises at once and
nothing spawns, so the endpoint answers 503 and the browser plays its local
engine.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import secrets
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Protocol

import chess
import chess.engine

from app import maia3_pins
from app.fen import fen_hash
from app.maia3_pins import ModelPin, default_weights_dir, verify_weights

logger = logging.getLogger(__name__)

# The product's bot ladder (the frontend labels and avatars mirror it). The
# engine receives the exact target Elo, clamped to this ladder's range.
ELO_BINS = [
    600, 800, 1000, 1100, 1200, 1300, 1400, 1500,
    1600, 1700, 1800, 1900, 2000, 2200, 2400, 2600,
]

# Every Maia3 preset reads a history of 8 positions.
HISTORY_PLIES = 8

WARMUP_ELO = 1500
SLOW_MOVE_MS = 1000

# Shutdown is bounded at 5 s on the loop: spawn cancel + quit + exit + move.
_SHUTDOWN_SPAWN_WAIT_S = 2.0
_SHUTDOWN_QUIT_S = 1.0
_SHUTDOWN_EXIT_WAIT_S = 1.0
_SHUTDOWN_INFLIGHT_WAIT_S = 1.0
_SHUTDOWN_RESULT_S = 6.0
_LOOP_JOIN_S = 2.0
_FAILED_START_EXIT_WAIT_S = 1.0

# The only variables the third-party child inherits. Secrets such as
# DATABASE_URL and JWT_SECRET must never reach it.
_CHILD_ENV_ALLOWLIST = (
    "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "LD_LIBRARY_PATH",
)

# Upstream's normal start-up progress on stderr. Our "engine ready" line
# replaces them; any other stderr stays at WARNING.
_STDERR_PROGRESS_PREFIXES = (
    "resolving Maia3 checkpoint",
    "loading Maia3 checkpoint",
    "Maia3 ready",
)
_CHESS_ENGINE_STDERR_MSG = "%s: stderr >> %s"


class Maia3Error(Exception):
    """The Maia3 engine could not answer (disabled, not ready, busy, crashed)."""


@dataclass(frozen=True)
class Maia3Move:
    uci: str


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


@dataclass(frozen=True)
class Maia3Settings:
    """Engine settings. Field defaults are the production defaults."""

    enabled: bool = False
    model: str = "maia3-23m"
    weights_dir: Path = field(default_factory=default_weights_dir)
    temperature: float = 1.0
    top_p: float = 1.0
    threads: int = 1
    move_timeout_s: float = 5.0
    min_play_budget_s: float = 0.5
    infer_timeout_s: float = 10.0
    start_timeout_s: float = 60.0

    def __post_init__(self) -> None:
        _require(
            self.model in maia3_pins.MODEL_PINS,
            f"MAIA3_MODEL must be one of {sorted(maia3_pins.MODEL_PINS)}, "
            f"got {self.model!r}",
        )
        for name in (
            "temperature", "top_p", "move_timeout_s", "min_play_budget_s",
            "infer_timeout_s", "start_timeout_s",
        ):
            _require(
                math.isfinite(getattr(self, name)),
                f"MAIA3_{name.upper()} must be finite",
            )
        _require(self.temperature >= 0, "MAIA3_TEMPERATURE must be >= 0")
        _require(0 < self.top_p <= 1, "MAIA3_TOP_P must be in (0, 1]")
        _require(self.threads >= 1, "MAIA3_THREADS must be >= 1")
        _require(self.min_play_budget_s > 0, "MAIA3_MIN_PLAY_BUDGET_S must be > 0")
        _require(
            self.move_timeout_s > self.min_play_budget_s,
            "MAIA3_MOVE_TIMEOUT_S must exceed MAIA3_MIN_PLAY_BUDGET_S",
        )
        _require(self.infer_timeout_s > 0, "MAIA3_INFER_TIMEOUT_S must be > 0")
        _require(self.start_timeout_s > 0, "MAIA3_START_TIMEOUT_S must be > 0")

    @property
    def pin(self) -> ModelPin:
        return maia3_pins.MODEL_PINS[self.model]

    @property
    def weights_path(self) -> Path:
        return Path(self.weights_dir) / self.pin.filename


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def maia3_settings(env: Mapping[str, str] | None = None) -> Maia3Settings:
    """Read and validate the MAIA3_* settings. An invalid value raises ValueError."""
    env = os.environ if env is None else env
    enabled = env.get("MAIA3_ENABLED", "0")
    if enabled not in {"0", "1"}:
        raise ValueError("MAIA3_ENABLED must be 0 or 1")
    weights_dir = env.get("MAIA3_WEIGHTS_DIR")
    defaults = Maia3Settings()
    return Maia3Settings(
        enabled=enabled == "1",
        model=env.get("MAIA3_MODEL", defaults.model),
        weights_dir=Path(weights_dir) if weights_dir else defaults.weights_dir,
        temperature=_env_float(env, "MAIA3_TEMPERATURE", defaults.temperature),
        top_p=_env_float(env, "MAIA3_TOP_P", defaults.top_p),
        threads=_env_int(env, "MAIA3_THREADS", defaults.threads),
        move_timeout_s=_env_float(env, "MAIA3_MOVE_TIMEOUT_S", defaults.move_timeout_s),
        min_play_budget_s=_env_float(
            env, "MAIA3_MIN_PLAY_BUDGET_S", defaults.min_play_budget_s
        ),
        infer_timeout_s=_env_float(
            env, "MAIA3_INFER_TIMEOUT_S", defaults.infer_timeout_s
        ),
        start_timeout_s=_env_float(
            env, "MAIA3_START_TIMEOUT_S", defaults.start_timeout_s
        ),
    )


# ---------------------------------------------------------------------------
# Position presentation and the engine command
# ---------------------------------------------------------------------------


def clamp_elo(elo: int) -> int:
    """The exact target Elo, clamped to the ladder's range (no bin snapping)."""
    return min(max(int(elo), ELO_BINS[0]), ELO_BINS[-1])


def position_for(fen: str, moves: list[str]) -> tuple[chess.Board, int]:
    """The board to send the engine, and how many history plies it carries.

    When ``moves`` replays from the standard start to ``fen``, the board is
    rooted ``HISTORY_PLIES`` plies back with the last moves pushed, so the engine
    gets its full history while each request sends a bounded command. Otherwise
    the board is the FEN alone (the engine pads its history) and a WARNING says
    why. The engine silently ignores a position command with an illegal move, so
    only replayed, legal history is ever sent.

    Raises:
        ValueError: on a malformed FEN, or a position with no move to play.
    """
    target = chess.Board(fen)
    outcome = target.outcome()
    if outcome is not None:
        raise ValueError(
            f"Position is over ({outcome.termination.name.lower()}) for FEN {fen}"
        )

    replayed = chess.Board()
    reason: str | None = None
    for ply, uci in enumerate(moves, start=1):
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            reason = f"unparsable move {uci!r} at ply {ply}"
            break
        if not replayed.is_legal(move):
            reason = f"illegal move {uci} at ply {ply}"
            break
        replayed.push(move)

    if reason is None and fen_hash(replayed.fen()) != fen_hash(fen):
        reason = "moves reach a different position"

    if reason is None:
        board = replayed.copy(stack=min(HISTORY_PLIES, len(replayed.move_stack)))
        return board, len(board.move_stack)

    logger.warning(
        "maia3 history mismatch (%s); plies=%d; sending the FEN without history",
        reason,
        len(moves),
    )
    return chess.Board(fen), 0


def engine_command(
    settings: Maia3Settings, weights_path: Path, seed: int
) -> tuple[list[str], dict[str, str]]:
    """The child's argv and its allowlisted environment."""
    argv = [
        sys.executable, "-m", "maia3.uci",
        "--model", settings.model,
        "--checkpoint-path", str(weights_path),
        "--device", "cpu",
        "--no-use-amp",
        "--use-uci-history",
        "--multipv", "1",
        "--temperature", str(float(settings.temperature)),
        "--top-p", str(float(settings.top_p)),
        "--seed", str(seed),
    ]
    env = {name: os.environ[name] for name in _CHILD_ENV_ALLOWLIST if name in os.environ}
    threads = str(settings.threads)
    env["OMP_NUM_THREADS"] = threads
    env["MKL_NUM_THREADS"] = threads
    env["HF_HUB_OFFLINE"] = "1"
    return argv, env


# ---------------------------------------------------------------------------
# Engine handles
# ---------------------------------------------------------------------------


class EngineHandle(Protocol):
    """One running engine process, used only on the service loop.

    ``kill()`` is the exception: it is thread-safe and idempotent. ``exited`` is
    an asyncio future on the service loop that resolves to the exit code
    (negative for a signal; -9 is SIGKILL, which the OOM killer sends).
    """

    pid: int
    exited: asyncio.Future[int]

    async def ping(self) -> None: ...

    async def play(self, board: chess.Board, elo: int) -> chess.Move | None: ...

    async def quit(self) -> None: ...

    def kill(self) -> None: ...


Opener = Callable[[list[str], dict[str, str]], Awaitable[EngineHandle]]


class _UciHandle:
    """A UCI child process driven through python-chess's asyncio protocol."""

    def __init__(
        self,
        transport: asyncio.SubprocessTransport,
        protocol: chess.engine.UciProtocol,
    ) -> None:
        self._transport = transport
        self._protocol = protocol
        self._loop = protocol.loop
        self.pid = transport.get_pid()
        self.exited = protocol.returncode

    async def ping(self) -> None:
        await self._protocol.ping()

    async def play(self, board: chess.Board, elo: int) -> chess.Move | None:
        result = await self._protocol.play(
            board, chess.engine.Limit(nodes=1), options={"Elo": elo}
        )
        return result.move

    async def quit(self) -> None:
        await self._protocol.quit()

    def kill(self) -> None:
        # transport.close() kills a running child and is a no-op afterwards.
        # transport.kill() would raise ProcessLookupError once the child has been
        # reaped, logging an asyncio "Exception in callback" on clean shutdowns.
        try:
            self._loop.call_soon_threadsafe(self._transport.close)
        except RuntimeError:
            pass  # loop already closed: nothing left to close on it


async def _wait_for_exit(exited: asyncio.Future[int], timeout: float) -> None:
    """Wait up to ``timeout`` for a child's exit to be processed.

    A loop closed right after a kill never processes the exit, and the
    subprocess object then warns that the child is still running. A cancellation
    that lands during the wait is deferred to the end of it, not lost.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    cancelled = False
    while not exited.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait({exited}, timeout=remaining)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


async def open_uci(argv: list[str], env: dict[str, str]) -> _UciHandle:
    """Spawn and initialize a UCI engine.

    This is ``chess.engine.popen_uci`` made from its two calls, so that a
    failed or cancelled handshake also waits for the killed child's exit before
    re-raising. ``popen_uci`` re-raises at once, and a loop closed right after
    can then leave a "subprocess is still running" warning.
    """
    transport, protocol = await chess.engine.UciProtocol.popen(
        argv, setpgrp=True, env=env
    )
    try:
        await protocol.initialize()
    except BaseException:
        transport.close()
        await _wait_for_exit(protocol.returncode, _FAILED_START_EXIT_WAIT_S)
        raise
    return _UciHandle(transport, protocol)


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


@dataclass
class _MoveRequest:
    """Per-request record shared with the move coroutine (feeds the log only)."""

    abandoned: bool = False


class _SpawnAborted(Exception):
    """The service closed while a spawn was finishing."""


class Maia3EngineService:
    """One warm Maia3 engine, its restart policy and its request admission."""

    def __init__(
        self,
        settings: Maia3Settings,
        opener: Opener = open_uci,
        proc_root: Path = Path("/proc"),
        backoff_base_s: float = 5.0,
        backoff_cap_s: float = 300.0,
    ) -> None:
        self.settings = settings
        self._opener = opener
        self._proc_root = Path(proc_root)
        self._backoff_base_s = backoff_base_s
        self._backoff_cap_s = backoff_cap_s

        self._cond = threading.Condition()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.loop_thread: threading.Thread | None = None
        self.handle: EngineHandle | None = None
        self.spawn_in_flight = False
        self._spawn_task: asyncio.Task[None] | None = None
        # "The engine has a move in flight." A request thread acquires it and the
        # move coroutine releases it on the loop, which a plain Lock allows.
        self.play_lock = threading.Lock()
        self._inflight_task: asyncio.Task[object] | None = None
        self.backoff_s = backoff_base_s
        self.next_spawn_at = 0.0
        self._retry_timer: asyncio.TimerHandle | None = None
        self.closed = False
        self.spawn_failures = 0
        self.last_spawn_error: str | None = None
        self._oom_warned = False

    # -- loop ---------------------------------------------------------------

    def _ensure_loop_locked(self) -> asyncio.AbstractEventLoop:
        # Created lazily, so a service that never starts has no loop to close.
        if self.loop is None:
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=self._run_loop, args=(loop,), name="maia3-engine", daemon=True
            )
            self.loop = loop
            self.loop_thread = thread
            thread.start()
        return self.loop

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    # -- spawning -----------------------------------------------------------

    def ensure_started(self) -> None:
        """Start a spawn unless one is running, a handle exists or we are in backoff.

        Any thread. ``spawn_in_flight`` is set before this returns, so a caller
        that then checks the Condition sees a handle or a spawn in flight.
        """
        with self._cond:
            if self.closed or self.handle is not None or self.spawn_in_flight:
                return
            if time.monotonic() < self.next_spawn_at:
                return
            loop = self._ensure_loop_locked()
            self.spawn_in_flight = True
            # Inside the Condition: shutdown sets `closed` under it before it
            # closes the loop, so the loop is still open here.
            loop.call_soon_threadsafe(self._start_spawn)

    def _start_spawn(self) -> None:
        with self._cond:
            if self.closed:
                self.spawn_in_flight = False
                self._cond.notify_all()
                return
            assert self.loop is not None
            self._spawn_task = self.loop.create_task(self._spawn())

    def _on_retry_timer(self) -> None:
        # The loop may run a timer a clock tick early, so the timer itself ends
        # the backoff window rather than racing ensure_started's check.
        with self._cond:
            self._retry_timer = None
            self.next_spawn_at = min(self.next_spawn_at, time.monotonic())
        self.ensure_started()

    def _bump_backoff(self) -> None:
        """Loop only: open a backoff window and schedule the retry at its end."""
        with self._cond:
            delay = self.backoff_s
            self.next_spawn_at = time.monotonic() + delay
            if self._retry_timer is not None:
                self._retry_timer.cancel()
                self._retry_timer = None
            if not self.closed:
                assert self.loop is not None
                self._retry_timer = self.loop.call_later(delay, self._on_retry_timer)
            self.backoff_s = min(self.backoff_s * 2, self._backoff_cap_s)

    def _retire(self, handle: EngineHandle) -> None:
        """Loop only: kill ``handle``; if it is current, clear it and back off once."""
        handle.kill()
        with self._cond:
            if self.handle is not handle:
                return
            self.handle = None
            self._bump_backoff()
            self._cond.notify_all()

    async def _spawn(self) -> None:
        started = time.monotonic()
        seed = secrets.randbits(31)
        try:
            await asyncio.wait_for(
                self._start(seed, started), self.settings.start_timeout_s
            )
        except asyncio.CancelledError:
            logger.debug("maia3 engine spawn cancelled")
            raise
        except Exception as exc:
            reason = (
                f"start timed out after {self.settings.start_timeout_s}s"
                if isinstance(exc, TimeoutError)
                else f"{type(exc).__name__}: {exc}"
            )
            with self._cond:
                closed = self.closed
                if not closed:
                    self.last_spawn_error = reason
                    self.spawn_failures += 1
                    self._bump_backoff()
                    retry_in = max(0.0, self.next_spawn_at - time.monotonic())
            if closed:
                logger.debug("maia3 engine spawn ended by shutdown: %s", reason)
            else:
                logger.error(
                    "maia3 engine spawn failed (%s); retrying in %.1fs",
                    reason,
                    retry_in,
                )
        finally:
            with self._cond:
                self.spawn_in_flight = False
                self._spawn_task = None
                self._cond.notify_all()

    async def _start(self, seed: int, started: float) -> None:
        settings = self.settings
        pin = settings.pin
        path = settings.weights_path
        try:
            await asyncio.to_thread(verify_weights, path, pin)
        except Exception as exc:
            logger.error(
                "Maia3 weights missing or corrupt; the build step did not run: %s", exc
            )
            raise
        argv, env = engine_command(settings, path, seed)
        # On a cancel or timeout in here, open_uci kills the child and waits
        # for its exit before re-raising.
        handle = await self._opener(argv, env)
        try:
            await handle.ping()  # forces the model load
            warmup = chess.Board()
            warmup.push_uci("e2e4")
            move = await handle.play(warmup, WARMUP_ELO)
            if not move:
                raise Maia3Error("warm-up returned no move")
            self._set_oom_score_adj(handle.pid)
            with self._cond:
                if self.closed:
                    raise _SpawnAborted("service closed during spawn")
                self.handle = handle
                # An asyncio future runs this on the loop even if the child has
                # already died.
                handle.exited.add_done_callback(
                    lambda fut, h=handle: self._on_engine_exit(h, fut)
                )
                self._cond.notify_all()
        except BaseException:
            handle.kill()
            with self._cond:
                closed = self.closed
            if closed:
                await _wait_for_exit(handle.exited, _SHUTDOWN_EXIT_WAIT_S)
            raise
        logger.info(
            "maia3 engine ready pid=%s model=%s revision=%s upstream=%s seed=%s "
            "threads=%s start_ms=%.0f",
            handle.pid,
            settings.model,
            pin.revision,
            maia3_pins.UPSTREAM_COMMIT,
            seed,
            settings.threads,
            (time.monotonic() - started) * 1000,
        )

    def _set_oom_score_adj(self, pid: int) -> None:
        """Best effort: make the engine, not the API, the OOM killer's victim.

        Effective where the kernel picks a single victim (cgroup v1, or v2 with
        memory.oom.group = 0). Skipped where there is no /proc (macOS).
        """
        if not self._proc_root.is_dir():
            return
        try:
            (self._proc_root / str(pid) / "oom_score_adj").write_text("1000")
        except OSError as exc:
            if not self._oom_warned:
                self._oom_warned = True
                logger.warning(
                    "maia3 could not set oom_score_adj for pid %s: %s", pid, exc
                )

    def _on_engine_exit(self, handle: EngineHandle, fut: asyncio.Future[int]) -> None:
        """Loop: retire an engine when it dies, idle or not."""
        with self._cond:
            current = self.handle is handle and not self.closed
        code = None
        if not fut.cancelled() and fut.exception() is None:
            code = fut.result()
        if not current:
            logger.debug("maia3 engine exited code=%s pid=%s", code, handle.pid)
            return
        hint = " (SIGKILL, likely the OOM killer)" if code == -9 else ""
        logger.warning("maia3 engine exited code=%s pid=%s%s", code, handle.pid, hint)
        self._retire(handle)

    # -- readiness ----------------------------------------------------------

    def wait_ready(self, timeout: float, stop_on_failure: bool = False) -> bool:
        """Wait for a published engine (build smoke, probe, release test only).

        Waits through backoffs. With ``stop_on_failure`` it returns False as soon
        as one more spawn has failed; ``last_spawn_error`` holds the reason.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            failures_at_call = self.spawn_failures
        self.ensure_started()
        with self._cond:
            while True:
                if self.handle is not None:
                    return True
                if self.closed:
                    return False
                if stop_on_failure and self.spawn_failures > failures_at_call:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)

    # -- moves --------------------------------------------------------------

    def play(self, board: chess.Board, elo: int) -> chess.Move:
        """One engine move within ``MAIA3_MOVE_TIMEOUT_S``. Request threads.

        Raises:
            Maia3Error: whenever the engine cannot answer in time.
        """
        settings = self.settings
        deadline = time.monotonic() + settings.move_timeout_s
        with self._cond:
            if self.closed:
                raise Maia3Error("engine shut down")
        self.ensure_started()

        with self._cond:
            while self.handle is None:
                if self.closed:
                    raise Maia3Error("engine shut down")
                if not self.spawn_in_flight:
                    # In backoff, or the spawn we were waiting on failed.
                    raise Maia3Error("engine unavailable (restarting)")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Maia3Error("engine not ready")
                self._cond.wait(remaining)

        remaining = deadline - time.monotonic()
        if not self.play_lock.acquire(timeout=max(0.0, remaining)):
            raise Maia3Error("engine busy")

        req = _MoveRequest()
        try:
            # The move ahead may have killed the engine while this request was
            # queued; its coroutine retires the handle before releasing the lock.
            with self._cond:
                if self.closed:
                    raise Maia3Error("engine shut down")
                handle = self.handle
                if handle is None:
                    raise Maia3Error("engine unavailable (restarting)")
                loop = self.loop
            if deadline - time.monotonic() < settings.min_play_budget_s:
                raise Maia3Error("engine busy")
            coro = self._play_on_loop(handle, board, elo, req)
            try:
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
            except RuntimeError as exc:
                coro.close()
                raise Maia3Error("engine shut down") from exc
        except BaseException:
            self.play_lock.release()
            raise
        # From here the move coroutine owns play_lock and releases it.

        try:
            try:
                move = fut.result(timeout=max(0.0, deadline - time.monotonic()))
            except TimeoutError:
                if fut.done():
                    # The coroutine finished at the deadline, or hit its own
                    # inference ceiling: its outcome decides.
                    move = fut.result()
                else:
                    # Leave the engine alone: the move finishes in the
                    # background, is discarded, and releases the lock.
                    req.abandoned = True
                    logger.info(
                        "maia3 move abandoned after %.0f ms elo=%d plies=%d",
                        settings.move_timeout_s * 1000,
                        elo,
                        board.ply(),
                    )
                    raise Maia3Error("engine slow; move abandoned") from None
        except Maia3Error:
            raise
        except TimeoutError as exc:
            raise Maia3Error("engine hung past the inference ceiling") from exc
        except Exception as exc:
            # Includes concurrent.futures.CancelledError. The coroutine has
            # already retired the handle.
            raise Maia3Error(f"engine failed: {type(exc).__name__}: {exc}") from exc
        if not move:
            raise Maia3Error("engine returned no move")
        return move

    async def _play_on_loop(
        self,
        handle: EngineHandle,
        board: chess.Board,
        elo: int,
        req: _MoveRequest,
    ) -> chess.Move | None:
        """Loop only. Never cancelled: shutdown ends a move by killing the engine.

        The bookkeeping happens here, not in a callback on the request's future,
        so it always runs on the loop and before the request sees the result.
        """
        self._inflight_task = asyncio.current_task()
        started = time.monotonic()
        try:
            move = await asyncio.wait_for(
                handle.play(board, elo), self.settings.infer_timeout_s
            )
        except BaseException as exc:
            # Respawns on the retry timer after the backoff, not immediately.
            self._retire(handle)
            with self._cond:
                closed = self.closed
            reason = (
                f"exceeded the {self.settings.infer_timeout_s}s inference ceiling"
                if isinstance(exc, TimeoutError)
                else f"{type(exc).__name__}: {exc}"
            )
            (logger.debug if closed else logger.warning)(
                "maia3 move failed (%s); engine pid=%s retired", reason, handle.pid
            )
            raise
        else:
            elapsed_ms = (time.monotonic() - started) * 1000
            if move:
                with self._cond:
                    self.backoff_s = self._backoff_base_s
                logger.info(
                    "maia3 move elo=%d plies=%d history_used=%d ms=%.0f abandoned=%s",
                    elo,
                    board.ply(),
                    len(board.move_stack),
                    elapsed_ms,
                    req.abandoned,
                )
                if elapsed_ms >= SLOW_MOVE_MS:
                    logger.info("maia3 move slow elo=%d ms=%.0f", elo, elapsed_ms)
            else:
                logger.warning(
                    "maia3 engine returned no move elo=%d plies=%d", elo, board.ply()
                )
            return move
        finally:
            self._inflight_task = None
            # Always last, so a queued request sees the retirement.
            self.play_lock.release()

    # -- shutdown -----------------------------------------------------------

    def shutdown(self) -> None:
        """Stop the engine and close the loop. Any thread but the loop's; idempotent."""
        with self._cond:
            if self.closed:
                # A second call must not submit to the closed loop.
                return
            self.closed = True
            self._cond.notify_all()
            loop = self.loop
            thread = self.loop_thread
        if loop is None or thread is None:
            return

        try:
            asyncio.run_coroutine_threadsafe(self._shutdown_on_loop(), loop).result(
                timeout=_SHUTDOWN_RESULT_S
            )
        except Exception:
            logger.warning("maia3 engine shutdown did not finish cleanly", exc_info=True)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=_LOOP_JOIN_S)
        if thread.is_alive():
            # A daemon thread never blocks interpreter exit, and an orphaned
            # child exits on stdin EOF.
            logger.warning("maia3 engine loop did not stop; leaving it")
            return
        # A stopped loop that is never closed warns "unclosed event loop" when
        # collected; closing it also makes a late submit raise instead of hang.
        loop.close()

    async def _shutdown_on_loop(self) -> None:
        if self._retry_timer is not None:
            self._retry_timer.cancel()
            self._retry_timer = None

        # A queued _start_spawn ran before this (FIFO), saw `closed` and created
        # no task; otherwise cancelling kills the child at whatever stage it is.
        task = self._spawn_task
        if task is not None:
            task.cancel()
            await asyncio.wait({task}, timeout=_SHUTDOWN_SPAWN_WAIT_S)

        with self._cond:
            handle, self.handle = self.handle, None
        if handle is not None:
            try:
                await asyncio.wait_for(handle.quit(), _SHUTDOWN_QUIT_S)
            except Exception as exc:
                logger.debug("maia3 engine quit failed: %s", exc)
            handle.kill()
            await _wait_for_exit(handle.exited, _SHUTDOWN_EXIT_WAIT_S)

        # Killing the engine fails the move inside its coroutine, whose finally
        # releases play_lock; wait so no task is pending when the loop stops.
        inflight = self._inflight_task
        if inflight is not None:
            await asyncio.wait({inflight}, timeout=_SHUTDOWN_INFLIGHT_WAIT_S)


# ---------------------------------------------------------------------------
# stderr filter
# ---------------------------------------------------------------------------


class _EngineStderrFilter(logging.Filter):
    """Drop upstream's start-up progress lines from python-chess's stderr log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg != _CHESS_ENGINE_STDERR_MSG:
            return True
        args = record.args
        if not isinstance(args, tuple) or len(args) != 2:
            return True
        return not str(args[1]).startswith(_STDERR_PROGRESS_PREFIXES)


def _install_stderr_filter() -> None:
    engine_logger = logging.getLogger("chess.engine")
    if not any(isinstance(f, _EngineStderrFilter) for f in engine_logger.filters):
        engine_logger.addFilter(_EngineStderrFilter())


_install_stderr_filter()


# ---------------------------------------------------------------------------
# Module API
# ---------------------------------------------------------------------------

_service: Maia3EngineService | None = None
_service_lock = threading.Lock()
_boot_weights_error: str | None = None


def _get_service() -> Maia3EngineService:
    global _service
    with _service_lock:
        if _service is None:
            _service = Maia3EngineService(maia3_settings())
        return _service


def check_maia3_weights_at_boot(settings: Maia3Settings, on_platform: bool) -> None:
    """Verify the configured weights once at boot and record the result.

    Overwrites the recorded value on every call. A failure never aborts boot:
    it logs an ERROR, the engine is not started, and ``get_move`` fails fast.
    """
    global _boot_weights_error
    if not settings.enabled:
        _boot_weights_error = None
        return
    try:
        verify_weights(settings.weights_path, settings.pin)
    except Exception as exc:
        _boot_weights_error = f"{type(exc).__name__}: {exc}"
        logger.error(
            "Maia3 weights missing or corrupt at boot (%s); model=%s "
            "deploy_platform=%s; the engine stays off and opponent moves answer 503",
            _boot_weights_error,
            settings.model,
            on_platform,
        )
        return
    _boot_weights_error = None


def boot_weights_error() -> str | None:
    """Why the boot weights check failed, or None."""
    return _boot_weights_error


def start_engine() -> None:
    """Begin the background spawn. Never blocks.

    A service closed by ``shutdown_engine`` is replaced, so a later lifespan in
    the same process (tests enter it more than once) gets a working engine.
    """
    global _service
    settings = maia3_settings()
    if not settings.enabled:
        logger.info("maia3 engine disabled")
        return
    logger.info("maia3 engine starting model=%s", settings.model)
    with _service_lock:
        if _service is None or _service.closed:
            _service = Maia3EngineService(settings)
        service = _service
    service.ensure_started()


def shutdown_engine() -> None:
    """Shut down the engine, if one was ever built.

    The closed service stays in place until the next ``start_engine``, so a
    request still running during teardown fails with "engine shut down" instead
    of building a new engine.
    """
    with _service_lock:
        service = _service
    if service is not None:
        service.shutdown()


def get_move(fen: str, moves: list[str], target_elo: int) -> Maia3Move:
    """Ask the local Maia3 engine for a move in ``fen``.

    Raises:
        Maia3Error: the engine is disabled, missing its weights, or cannot
            answer within the request budget.
        ValueError: a malformed FEN, or a position with no move to play.
    """
    settings = maia3_settings()
    if not settings.enabled:
        raise Maia3Error("Maia3 engine disabled")
    if _boot_weights_error is not None:
        raise Maia3Error("Maia3 weights missing")
    board, _history_used = position_for(fen, moves)
    move = _get_service().play(board, clamp_elo(target_elo))
    return Maia3Move(move.uci())
