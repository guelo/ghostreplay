"""The test environment must run the dependency versions production runs.

``requirements.txt`` names the runtime dependencies without versions, and
``requirements.production.constraints.txt`` pins the full runtime closure that
Railway installs (``pip install -r requirements.txt -c
requirements.production.constraints.txt``, set as the service's install
command). A venv built from ``requirements.txt`` alone, or built before the pins
last moved, tests other libraries than the ones serving traffic. g-test-deps-pinning
found exactly that: an unconstrained install resolved SQLAlchemy 2.1 (21 new
warnings-as-errors failures), and a stale venv passed three tests the production
Starlette fails.

These checks fail closed in every environment that runs the backend suite (the
pre-push gate and CI), so drift is a red gate rather than a silent difference.
Only distributions reachable from ``requirements.txt`` on this platform are held
to the pins: a pin for a platform-conditional dependency (``greenlet`` is
installed by SQLAlchemy on Linux x86_64 but not on macOS arm64) is not required
to be present, and dev-only tools from ``requirements-dev.txt`` are not pinned
here at all. That tolerance would also let a pin outlive its dependency, so on
Linux, the platform production runs on and CI runs, the pins must equal the
closure exactly.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
import sys

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

BACKEND = Path(__file__).resolve().parent
REQUIREMENTS = BACKEND / "requirements.txt"
CONSTRAINTS = BACKEND / "requirements.production.constraints.txt"

RESYNC = (
    "cd backend && .venv/bin/python -m pip install -r requirements.txt "
    "-r requirements-dev.txt -c requirements.production.constraints.txt"
)


def _requirement_lines(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _production_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in _requirement_lines(CONSTRAINTS):
        requirement = Requirement(line)
        specifiers = list(requirement.specifier)
        assert len(specifiers) == 1 and specifiers[0].operator == "==", (
            f"{CONSTRAINTS.name}: {line!r} is not an exact '==' pin"
        )
        name = canonicalize_name(requirement.name)
        assert name not in pins, f"{CONSTRAINTS.name}: {requirement.name} is pinned twice"
        pins[name] = specifiers[0].version
    return pins


def _installed_runtime_closure() -> dict[str, str]:
    """Every distribution ``requirements.txt`` pulls in here, with its installed version.

    Walks installed metadata from the direct requirements, following extras
    (``uvicorn[standard]``, ``psycopg[binary]``) and evaluating environment
    markers for this interpreter and platform, which is what pip itself did.
    """
    closure: dict[str, str] = {}
    expanded: set[tuple[str, str]] = set()
    pending = [Requirement(line) for line in _requirement_lines(REQUIREMENTS)]
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        try:
            dist = metadata.distribution(requirement.name)
        except metadata.PackageNotFoundError:
            raise AssertionError(
                f"{requirement.name} is a runtime dependency but is not installed; "
                f"build the venv with:\n  {RESYNC}"
            ) from None
        closure[name] = dist.version
        for extra in {"", *requirement.extras}:
            if (name, extra) in expanded:
                continue
            expanded.add((name, extra))
            for spec in dist.requires or ():
                child = Requirement(spec)
                if child.marker is None or child.marker.evaluate({"extra": extra}):
                    pending.append(child)
    return closure


def test_every_runtime_dependency_has_a_production_pin():
    pins = _production_pins()
    unpinned = sorted(set(_installed_runtime_closure()) - set(pins))
    assert not unpinned, (
        f"runtime dependencies with no pin in {CONSTRAINTS.name}: {unpinned}. "
        "Railway would install whatever release is current at deploy time. Pin the "
        "version this suite was run against."
    )


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="platform-conditional pins (greenlet) are absent off Linux; CI enforces this",
)
def test_every_production_pin_is_a_runtime_dependency():
    stale = sorted(set(_production_pins()) - set(_installed_runtime_closure()))
    assert not stale, (
        f"{CONSTRAINTS.name} pins packages nothing in {REQUIREMENTS.name} installs on "
        f"Linux: {stale}. Remove the pins, or restore the dependency that needed them."
    )


def test_installed_runtime_versions_match_production_pins():
    pins = _production_pins()
    drift = {
        name: f"installed {installed}, production {pins[name]}"
        for name, installed in sorted(_installed_runtime_closure().items())
        if name in pins and Version(installed) != Version(pins[name])
    }
    assert not drift, (
        f"this venv does not run the production dependency versions: {drift}. "
        f"Resync it with:\n  {RESYNC}"
    )
