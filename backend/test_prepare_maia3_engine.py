"""scripts/prepare_maia3_engine.py: fetch, verify, smoke and exit status.

No torch, no weights and no network: the download and the engine service are
replaced. The real engine is covered by test_maia3_engine_release.py and the CI
maia3-engine job.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
import sys
from pathlib import Path

import chess
import chess.engine
import pytest

from app import maia3_pins
from app.maia3_client import Maia3Settings
from app.maia3_pins import ModelPin, WeightsError
from scripts import prepare_maia3_engine as prep

GOOD = b"pinned maia3 checkpoint bytes"
MODEL = "maia3-23m"


def _pin(data: bytes = GOOD, model: str = MODEL) -> ModelPin:
    real = maia3_pins.MODEL_PINS[model]
    return ModelPin(
        name=real.name,
        repo_id=real.repo_id,
        revision=real.revision,
        filename=real.filename,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


@pytest.fixture
def pin(monkeypatch) -> ModelPin:
    """The configured model's pin, pointing at GOOD instead of the real bytes."""
    fake = _pin()
    monkeypatch.setitem(maia3_pins.MODEL_PINS, MODEL, fake)
    return fake


class Downloads:
    """A fake Hugging Face download that writes fixed bytes and records calls."""

    def __init__(self, data: bytes = GOOD, error: Exception | None = None) -> None:
        self.data = data
        self.error = error
        self.dests: list[Path] = []

    def __call__(self, pin: ModelPin, dest: Path) -> Path:
        self.dests.append(dest)
        if self.error is not None:
            raise self.error
        out = dest / pin.filename
        out.write_bytes(self.data)
        return out


def _env(tmp_path: Path, enabled: str) -> dict[str, str]:
    return {"MAIA3_ENABLED": enabled, "MAIA3_WEIGHTS_DIR": str(tmp_path / "w")}


def _no_smoke(_settings: Maia3Settings) -> str:
    raise AssertionError("the smoke must not run")


# -- fetch --------------------------------------------------------------------


def test_a_verified_existing_file_is_not_downloaded_again(pin, tmp_path):
    weights = tmp_path / "w"
    weights.mkdir()
    (weights / pin.filename).write_bytes(GOOD)
    downloads = Downloads(error=AssertionError("must not download"))

    path = prep.fetch_weights(pin, weights, downloads)

    assert path == weights / pin.filename
    assert downloads.dests == []


def test_correct_bytes_are_renamed_into_place_from_a_staging_dir(pin, tmp_path):
    weights = tmp_path / "w"
    downloads = Downloads()

    path = prep.fetch_weights(pin, weights, downloads)

    assert path.read_bytes() == GOOD
    # Staged under the weights dir (same filesystem, so the rename is atomic)
    # and removed afterwards.
    (staging,) = downloads.dests
    assert staging.parent == weights
    assert not staging.exists()
    assert sorted(p.name for p in weights.iterdir()) == [pin.filename]


def test_a_corrupt_existing_file_is_replaced(pin, tmp_path):
    weights = tmp_path / "w"
    weights.mkdir()
    (weights / pin.filename).write_bytes(b"x" * len(GOOD))

    prep.fetch_weights(pin, weights, Downloads())

    assert (weights / pin.filename).read_bytes() == GOOD


def test_wrong_downloaded_bytes_raise_and_leave_no_file(pin, tmp_path):
    weights = tmp_path / "w"

    with pytest.raises(WeightsError, match="sha256"):
        prep.fetch_weights(pin, weights, Downloads(data=b"y" * len(GOOD)))

    assert list(weights.iterdir()) == []


# -- exit status ----------------------------------------------------------------


def test_enabled_wrong_downloaded_bytes_exit_1_without_a_final_file(pin, tmp_path):
    code = prep.main(
        [], _env(tmp_path, "1"), download=Downloads(data=b"z"), smoke=_no_smoke
    )

    assert code == 1
    assert list((tmp_path / "w").iterdir()) == []


def test_enabled_fetch_error_exits_1(pin, tmp_path):
    code = prep.main(
        [], _env(tmp_path, "1"), download=Downloads(error=OSError("offline")),
        smoke=_no_smoke,
    )

    assert code == 1


def test_disabled_fetch_failure_warns_exits_0_and_never_smokes(pin, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="prepare_maia3_engine"):
        code = prep.main(
            [], _env(tmp_path, "0"), download=Downloads(error=OSError("offline")),
            smoke=_no_smoke,
        )

    assert code == 0
    (record,) = [r for r in caplog.records if r.name == "prepare_maia3_engine"]
    assert record.levelno == logging.WARNING
    assert "offline" in record.getMessage()


def test_disabled_successful_fetch_skips_the_smoke(pin, tmp_path, capsys):
    code = prep.main([], _env(tmp_path, "0"), download=Downloads(), smoke=_no_smoke)

    assert code == 0
    assert (tmp_path / "w" / pin.filename).read_bytes() == GOOD
    assert "smoke skipped (MAIA3_ENABLED=0)" in capsys.readouterr().out


def test_a_failing_smoke_exits_1(pin, tmp_path):
    def failing(_settings):
        raise prep.SmokeFailure("engine did not start: boom")

    code = prep.main([], _env(tmp_path, "1"), download=Downloads(), smoke=failing)

    assert code == 1


def test_enabled_success_runs_the_smoke_with_the_configured_settings(pin, tmp_path, capsys):
    seen = []

    def ok(settings):
        seen.append(settings)
        return "maia3 smoke ok model=maia3-23m"

    code = prep.main([], _env(tmp_path, "1"), download=Downloads(), smoke=ok)

    assert code == 0
    (settings,) = seen
    assert settings.enabled and settings.model == MODEL
    assert settings.weights_dir == tmp_path / "w"
    assert "maia3 smoke ok" in capsys.readouterr().out


def test_model_flag_overrides_the_configured_model(monkeypatch, tmp_path):
    other = "maia3-5m"
    assert other != MODEL and Maia3Settings().model == MODEL
    fake = _pin(model=other)
    monkeypatch.setitem(maia3_pins.MODEL_PINS, other, fake)
    seen = []

    def ok(settings):
        seen.append(settings.model)
        return "maia3 smoke ok"

    code = prep.main(
        ["--model", other], _env(tmp_path, "1"), download=Downloads(), smoke=ok
    )

    assert code == 0
    assert seen == [other]
    assert (tmp_path / "w" / fake.filename).read_bytes() == GOOD


def test_no_smoke_fetches_and_skips_the_smoke(pin, tmp_path, capsys):
    code = prep.main(
        ["--no-smoke"], _env(tmp_path, "1"), download=Downloads(), smoke=_no_smoke
    )

    assert code == 0
    assert (tmp_path / "w" / pin.filename).read_bytes() == GOOD
    assert "smoke skipped (--no-smoke)" in capsys.readouterr().out


def test_require_smoke_when_disabled_exits_1_before_fetching(pin, tmp_path):
    downloads = Downloads()

    code = prep.main(
        ["--require-smoke"], _env(tmp_path, "0"), download=downloads, smoke=_no_smoke
    )

    assert code == 1
    assert downloads.dests == []


def test_require_smoke_when_enabled_runs_the_smoke(pin, tmp_path, capsys):
    code = prep.main(
        ["--require-smoke"], _env(tmp_path, "1"), download=Downloads(),
        smoke=lambda _settings: "maia3 smoke ok",
    )

    assert code == 0
    assert "maia3 smoke ok" in capsys.readouterr().out


def test_invalid_settings_exit_1(tmp_path):
    env = {**_env(tmp_path, "1"), "MAIA3_TOP_P": "2"}

    assert prep.main([], env, download=Downloads(), smoke=_no_smoke) == 1


# -- smoke ------------------------------------------------------------------------


class _Handle:
    def __init__(self, pid: int) -> None:
        self.pid = pid


class FakeService:
    """Stands in for Maia3EngineService around a real, harmless child process.

    The child is a sleeping Python process, so the smoke's memory reading and
    its "child is gone" check run against a real pid.
    """

    def __init__(
        self,
        settings: Maia3Settings,
        *,
        ready: bool = True,
        stderr_lines: tuple[str, ...] = (),
        kill_on_shutdown: bool = True,
    ) -> None:
        self.settings = settings
        self.ready = ready
        self.stderr_lines = stderr_lines
        self.kill_on_shutdown = kill_on_shutdown
        self.last_spawn_error = None if ready else "ModuleNotFoundError: maia3"
        self.child: subprocess.Popen | None = None
        self.handle: _Handle | None = None
        self.shutdowns = 0

    def wait_ready(self, timeout: float, stop_on_failure: bool = False) -> bool:
        assert stop_on_failure
        if not self.ready:
            return False
        self.child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        self.handle = _Handle(self.child.pid)
        # Through python-chess's real stderr path, as the engine's lines arrive.
        if self.stderr_lines:
            async def feed():
                protocol = chess.engine.UciProtocol()
                for line in self.stderr_lines:
                    protocol.error_line_received(line)

            asyncio.run(feed())
        return True

    def play(self, board: chess.Board, elo: int) -> chess.Move:
        return next(iter(board.legal_moves))

    def shutdown(self) -> None:
        self.shutdowns += 1
        if self.child is not None and self.kill_on_shutdown:
            self.child.kill()
            self.child.wait()


@pytest.fixture
def services():
    made: list[FakeService] = []
    yield made
    for svc in made:
        if svc.child is not None and svc.child.poll() is None:
            svc.child.kill()
            svc.child.wait()


def _factory(services, **kwargs):
    def make(settings):
        svc = FakeService(settings, **kwargs)
        services.append(svc)
        return svc

    return make


SETTINGS = Maia3Settings(enabled=True, threads=2)


def test_the_smoke_line_reports_the_child_memory(services):
    line = prep.run_smoke(SETTINGS, _factory(services))

    assert line.startswith("maia3 smoke ok model=maia3-23m threads=2 ")
    fields = dict(part.split("=", 1) for part in line.split()[3:])
    assert int(fields["child_hwm_kb"]) > 0
    expected = "vmhwm" if sys.platform == "linux" else "ps_rss"
    assert fields["mem_source"] == expected
    assert int(fields["pid"]) == services[0].child.pid
    assert services[0].shutdowns == 1


def test_a_missing_keys_stderr_line_fails_the_smoke(services):
    factory = _factory(
        services,
        stderr_lines=(
            "loading Maia3 checkpoint /w/maia3-23m.pt",
            "warning: missing keys: ['head.bias']",
            "Maia3 ready",
        ),
    )

    with pytest.raises(prep.SmokeFailure, match="missing keys"):
        prep.run_smoke(SETTINGS, factory)
    assert services[0].shutdowns == 1


def test_an_unexpected_keys_stderr_line_fails_the_smoke(services):
    factory = _factory(services, stderr_lines=("warning: unexpected keys: ['x']",))

    with pytest.raises(prep.SmokeFailure, match="unexpected keys"):
        prep.run_smoke(SETTINGS, factory)


def test_an_engine_that_never_starts_fails_the_smoke_with_the_spawn_error(services):
    with pytest.raises(prep.SmokeFailure, match="ModuleNotFoundError"):
        prep.run_smoke(SETTINGS, _factory(services, ready=False))
    assert services[0].shutdowns == 1


def test_a_child_that_survives_shutdown_fails_the_smoke(services, monkeypatch):
    monkeypatch.setattr(prep, "_CHILD_EXIT_WAIT_S", 0.2)
    with pytest.raises(prep.SmokeFailure, match="survived shutdown"):
        prep.run_smoke(SETTINGS, _factory(services, kill_on_shutdown=False))


def test_child_memory_reads_vmhwm_from_proc_status(tmp_path):
    status = tmp_path / "proc" / "42" / "status"
    status.parent.mkdir(parents=True)
    status.write_text("Name:\tpython\nVmPeak:\t 900 kB\nVmHWM:\t  512345 kB\nVmRSS:\t 1 kB\n")

    assert prep.child_memory_kb(42, tmp_path / "proc") == (512345, "vmhwm")
