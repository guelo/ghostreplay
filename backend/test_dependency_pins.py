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

The Maia3 engine's dependencies (``requirements-maia3.txt``: torch and maia3)
reach Railway through ``-r requirements-maia3.txt`` in that install command. They
stay out of ``requirements.txt`` so the backend suite runs without torch, and
their pins live in a second file, ``requirements-maia3.constraints.txt``, which
requirements-maia3.txt applies itself. In a venv that has them installed (dev,
the CI maia3-engine job) they are held to those pins the same way; elsewhere
those checks skip, unless ``GHOSTREPLAY_REQUIRE_MAIA3_TESTS=1`` (the CI
maia3-engine job), which makes a missing install fail instead.
"""

from __future__ import annotations

from importlib import metadata
import json
import os
from pathlib import Path
import sys
from urllib.parse import unquote

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

BACKEND = Path(__file__).resolve().parent
REQUIREMENTS = BACKEND / "requirements.txt"
CONSTRAINTS = BACKEND / "requirements.production.constraints.txt"
MAIA3_REQUIREMENTS = BACKEND / "requirements-maia3.txt"
MAIA3_CONSTRAINTS = BACKEND / "requirements-maia3.constraints.txt"

RESYNC = (
    "cd backend && .venv/bin/python -m pip install -r requirements.txt "
    "-r requirements-dev.txt -c requirements.production.constraints.txt"
)
MAIA3_RESYNC = f"{RESYNC} -r requirements-maia3.txt"

# The only pip option line any of these files may carry. Any other (-r, an index
# URL) would change what pip installs without these tests seeing it.
_ALLOWED_OPTIONS = {MAIA3_REQUIREMENTS: {f"-c {MAIA3_CONSTRAINTS.name}"}}


def _requirement_lines(path: Path) -> list[str]:
    """Requirement lines, without comments and the allowed pip option lines.

    A comment needs whitespace before its ``#``, as in pip, so a URL's
    ``#sha256=`` fragment stays part of the line.
    """
    lines = []
    for raw in path.read_text().splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            assert line in _ALLOWED_OPTIONS.get(path, ()), (
                f"{path.name}: unexpected pip option line {line!r}; "
                "these tests would not see what it installs"
            )
            continue
        lines.append(line)
    return lines


def _pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in _requirement_lines(path):
        requirement = Requirement(line)
        specifiers = list(requirement.specifier)
        assert len(specifiers) == 1 and specifiers[0].operator == "==", (
            f"{path.name}: {line!r} is not an exact '==' pin"
        )
        name = canonicalize_name(requirement.name)
        assert name not in pins, f"{path.name}: {requirement.name} is pinned twice"
        pins[name] = specifiers[0].version
    return pins


def _production_pins() -> dict[str, str]:
    return _pins(CONSTRAINTS)


def _installed_runtime_closure(
    path: Path = REQUIREMENTS, resync: str = RESYNC
) -> dict[str, str]:
    """Every distribution ``path`` pulls in here, with its installed version.

    Walks installed metadata from the direct requirements, following extras
    (``uvicorn[standard]``, ``psycopg[binary]``) and evaluating environment
    markers for this interpreter and platform, which is what pip itself did.
    """
    closure: dict[str, str] = {}
    expanded: set[tuple[str, str]] = set()
    pending = [
        requirement
        for requirement in map(Requirement, _requirement_lines(path))
        if requirement.marker is None or requirement.marker.evaluate()
    ]
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        try:
            dist = metadata.distribution(requirement.name)
        except metadata.PackageNotFoundError:
            raise AssertionError(
                f"{requirement.name} is a runtime dependency but is not installed; "
                f"build the venv with:\n  {resync}"
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


# -- the Maia3 engine (requirements-maia3.txt) -----------------------------------


def _maia3_installed() -> bool:
    try:
        metadata.distribution("maia3")
    except metadata.PackageNotFoundError:
        return False
    return True


# Required mode runs these tests without maia3, so they fail with the resync
# command instead of skipping.
requires_maia3 = pytest.mark.skipif(
    not _maia3_installed()
    and os.environ.get("GHOSTREPLAY_REQUIRE_MAIA3_TESTS") != "1",
    reason="requirements-maia3.txt is not installed in this venv",
)


def _maia3_direct_requirements() -> list[Requirement]:
    return [Requirement(line) for line in _requirement_lines(MAIA3_REQUIREMENTS)]


def _maia3_only_closure() -> dict[str, str]:
    """What requirements-maia3.txt adds beyond requirements.txt, minus torch and maia3."""
    runtime = _installed_runtime_closure()
    direct = {canonicalize_name(r.name) for r in _maia3_direct_requirements()}
    return {
        name: version
        for name, version in _installed_runtime_closure(
            MAIA3_REQUIREMENTS, MAIA3_RESYNC
        ).items()
        if name not in runtime and name not in direct
    }


def _torch_pins() -> dict[str, str]:
    """The torch version each platform line names, keyed by its marker."""
    versions = {}
    for requirement in _maia3_direct_requirements():
        if canonicalize_name(requirement.name) != "torch":
            continue
        if requirement.url:
            wheel = requirement.url.rsplit("/", 1)[-1].replace("%2B", "+")
            version = Version(wheel.split("-")[1])
            assert "#sha256=" in requirement.url, f"{wheel} has no #sha256= hash"
        else:
            (specifier,) = requirement.specifier
            assert specifier.operator == "=="
            version = Version(specifier.version)
        versions[str(requirement.marker)] = version.public
    return versions


def test_maia3_requirements_apply_their_own_constraints():
    options = [
        line.strip()
        for line in MAIA3_REQUIREMENTS.read_text().splitlines()
        if line.strip().startswith("-")
    ]
    assert options == [f"-c {MAIA3_CONSTRAINTS.name}"]


def test_any_other_pip_option_line_fails_parsing(tmp_path):
    for option in ("-r requirements-dev.txt", "--extra-index-url https://example.invalid"):
        path = tmp_path / "requirements.txt"
        path.write_text(f"fastapi\n{option}\n")
        with pytest.raises(AssertionError, match="unexpected pip option"):
            _requirement_lines(path)


def test_no_package_is_pinned_in_both_constraints_files():
    both = sorted(set(_pins(MAIA3_CONSTRAINTS)) & set(_production_pins()))
    assert not both, (
        f"pinned in both {CONSTRAINTS.name} and {MAIA3_CONSTRAINTS.name}: {both}. "
        f"Keep the production pin only."
    )


def test_the_linux_and_macos_torch_pins_name_the_same_version():
    versions = _torch_pins()
    assert len(versions) == 2, versions
    assert len(set(versions.values())) == 1, versions


def test_the_maia3_requirement_is_the_pinned_upstream_commit():
    from app.maia3_pins import UPSTREAM_COMMIT

    (maia3,) = [r for r in _maia3_direct_requirements() if r.name == "maia3"]
    assert maia3.url == (
        f"https://github.com/CSSLab/maia3/archive/{UPSTREAM_COMMIT}.zip"
    )


@requires_maia3
def test_every_maia3_dependency_has_a_pin():
    unpinned = sorted(set(_maia3_only_closure()) - set(_pins(MAIA3_CONSTRAINTS)))
    assert not unpinned, (
        f"Maia3 dependencies with no pin in {MAIA3_CONSTRAINTS.name}: {unpinned}. "
        "Pin the version this suite was run against."
    )


@requires_maia3
@pytest.mark.skipif(
    sys.platform != "linux",
    reason="the Maia3 pins are resolved for Linux; the CI maia3-engine job enforces this",
)
def test_every_maia3_pin_is_a_maia3_dependency():
    stale = sorted(set(_pins(MAIA3_CONSTRAINTS)) - set(_maia3_only_closure()))
    assert not stale, (
        f"{MAIA3_CONSTRAINTS.name} pins packages requirements-maia3.txt does not add "
        f"on Linux: {stale}"
    )


@requires_maia3
def test_installed_maia3_versions_match_their_pins():
    pins = _pins(MAIA3_CONSTRAINTS)
    drift = {
        name: f"installed {installed}, pinned {pins[name]}"
        for name, installed in sorted(_maia3_only_closure().items())
        if name in pins and Version(installed) != Version(pins[name])
    }
    (torch_pin,) = set(_torch_pins().values())
    installed_torch = Version(metadata.version("torch")).public
    if installed_torch != torch_pin:
        drift["torch"] = f"installed {installed_torch}, pinned {torch_pin}"
    assert not drift, (
        f"this venv does not run the pinned Maia3 dependencies: {drift}. "
        f"Resync it with:\n  {MAIA3_RESYNC}"
    )


@requires_maia3
def test_url_requirements_are_installed_from_their_pinned_urls():
    """A new maia3 commit is still version 0.1.0, and pip keeps an installed
    version whose URL changed, so the version checks cannot see a stale install.
    pip records the URL it installed from in direct_url.json (PEP 610)."""
    stale = {}
    for requirement in _maia3_direct_requirements():
        if not requirement.url or not (
            requirement.marker is None or requirement.marker.evaluate()
        ):
            continue
        pinned = requirement.url.split("#", 1)[0]
        try:
            recorded = metadata.distribution(requirement.name).read_text(
                "direct_url.json"
            )
        except metadata.PackageNotFoundError:
            stale[requirement.name] = f"not installed, pinned {pinned}"
            continue
        installed = json.loads(recorded)["url"] if recorded else "a package index"
        if unquote(installed) != unquote(pinned):
            stale[requirement.name] = f"installed from {installed}, pinned {pinned}"
    reinstall = "\n".join(
        f"  cd backend && .venv/bin/python -m pip install --force-reinstall "
        f"--no-deps '{requirement}'"
        for requirement in _maia3_direct_requirements()
        if requirement.name in stale
    )
    assert not stale, (
        f"this venv does not run the pinned Maia3 code: {stale}. "
        f"Reinstall with:\n{reinstall}"
    )
