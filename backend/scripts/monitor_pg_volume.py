"""One finite, read-only check of the production Postgres volume (g-stop-pgss-bloat).

Two independent questions, one run:

1. *Is the volume filling?* Railway reports the volume's current and provisioned
   size, so the fill percent catches growth from any source, including ones this
   script does not name.
2. *Is a known bloat path growing again?* The volume total moves slowly and would
   hide a fast local regression for weeks. The watched directories are the paths
   that have actually bloated or plausibly can, each with its own explicit bound.

``/pgdata/pg_stat_tmp`` is the reason this exists. ``pg_stat_statements`` held a
151 MB ``pgss_query_texts.stat`` on a 1 GB volume while its extension objects were
absent, so nothing could even read the statistics it was paying for. The collector
is disabled by an ``shared_preload_libraries = ''`` override appended to
``postgresql.conf``, and the Railway postgres-ssl wrapper re-adds its own managed
line on boot — so the override, not the wrapper, is what holds. If an image change
ever wins that argument, the query-text file starts growing again within minutes
of the restart, and this bound is what says so. See MONITOR_PG_VOLUME.md.

Directory sizes are the sum of the files directly in the directory: Railway
reports 4096 for a subdirectory rather than its recursive size, and a recursive
walk of ``base/`` would be thousands of calls naming relation files no operator
can act on. Every watched path is a flat directory where that sum is the answer.

Read-only by construction: it lists, and never downloads, renames, or deletes.

Exit status: 0 healthy, 1 alerting (a threshold was crossed), 2 refused (the CLI
is missing, unauthenticated, unlinked, a watched path is gone, or the answer was
unparsable or in an unexpected shape). The two non-zero statuses are kept apart
on purpose: 1 means the check ran and the volume needs attention, 2 means no
check happened and the monitoring itself is broken -- a silent 2 in cron is the
failure mode worth noticing.

What this script must never do is report healthy without having seen the data.
Every field it reads is validated rather than defaulted, and a watched path that
does not exist is a refusal, not an empty directory: a monitor that says healthy
because it looked in the wrong place is worse than no monitor at all, since it
also silences the missing-ping alarm that would otherwise catch its absence.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import math
import subprocess
import sys
import traceback

MIB = 1024 * 1024
VOLUME = "postgres-volume"
TIMEOUT_SECONDS = 60.0

# 70% of the 5 GB volume is ~3.5 GB. At the growth observed between the
# 2026-08-21 rollout and 2026-09-20 (943 MB -> 1041 MB, ~100 MB/month) that is
# well over a year of warning before the volume is full.
MAX_USED_PERCENT = 70.0

# Bounds are MiB. pg_stat_tmp holds one 49 KB frozen query-text file, so 1 MiB is
# generous for the healthy state and far below the 151 MB it reached when the
# collector ran. pg_wal sits at 128 MiB; 1536 MiB is above anything max_wal_size
# produces normally and below anything that threatens a 5 GB volume, so it fires
# on WAL that is stuck (a dead replication slot, failed archiving), not on load.
WATCHES = {"/pgdata/pg_stat_tmp": 1.0, "/pgdata/pg_wal": 1536.0}

# The entry kinds the CLI returned on 2026-09-20. An unknown kind refuses rather
# than being skipped: a renamed or recased type would silently total zero bytes.
ENTRY_TYPES = frozenset({"file", "directory"})

ALERTING = 1
REFUSED = 2


class CheckRefused(Exception):
    """The check could not be performed. Never used to report an unhealthy volume."""


def _human(size_bytes: int) -> str:
    """Size in the unit an operator can compare at a glance.

    A 49 KB query-text file and a 10 KB bound both read as "0.0 MiB", which is
    exactly the comparison this alert exists to make legible.
    """
    if size_bytes < MIB:
        return f"{size_bytes / 1024:.1f} KiB"
    if size_bytes < 1024 * MIB:
        return f"{size_bytes / MIB:.1f} MiB"
    return f"{size_bytes / (1024 * MIB):.2f} GiB"


@dataclass(frozen=True)
class DirectoryReport:
    path: str
    total_bytes: int
    limit_bytes: int
    file_count: int
    largest_name: str | None
    largest_bytes: int
    # Size alone cannot tell a live collector from a frozen leftover: after the
    # disable the 49,375-byte query-text file stayed byte-identical across
    # restarts. An unchanged mtime is the sharper signal, so carry it into the
    # alert body where triage can use it without a second trip to the CLI.
    largest_modified: str | None = None

    @property
    def alerting(self) -> bool:
        return self.total_bytes > self.limit_bytes


@dataclass
class VolumeReport:
    volume: str
    mount_path: str
    used_mb: float
    size_mb: float
    max_used_percent: float
    directories: list[DirectoryReport] = field(default_factory=list)

    @property
    def used_percent(self) -> float:
        return 0.0 if self.size_mb <= 0 else self.used_mb / self.size_mb * 100.0

    @property
    def alerts(self) -> list[str]:
        found = []
        if self.used_percent > self.max_used_percent:
            found.append(
                f"{self.volume} is {self.used_percent:.1f}% full "
                f"({self.used_mb:.0f} of {self.size_mb:.0f} MB), over {self.max_used_percent:.0f}%"
            )
        for directory in self.directories:
            if directory.alerting:
                largest = ""
                if directory.largest_name:
                    when = directory.largest_modified
                    when = f", modified {when}" if when else ""
                    largest = (
                        f", largest {directory.largest_name} "
                        f"({_human(directory.largest_bytes)}{when})"
                    )
                found.append(
                    f"{directory.path} holds {_human(directory.total_bytes)}, "
                    f"over its {_human(directory.limit_bytes)} bound{largest}"
                )
        return found

    @property
    def healthy(self) -> bool:
        return not self.alerts

    @property
    def summary(self) -> str:
        if self.healthy:
            return (
                f"healthy: {self.volume} {self.used_percent:.1f}% full "
                f"({self.used_mb:.0f}/{self.size_mb:.0f} MB), watched paths within bounds"
            )
        return "ALERTING: " + "; ".join(self.alerts)


def _run_railway(argv: list[str]) -> str:
    """Run the Railway CLI and return stdout, or raise CheckRefused."""
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError as missing:
        raise CheckRefused(f"Railway CLI not found: {missing}") from missing
    except subprocess.TimeoutExpired as slow:
        raise CheckRefused(f"Railway CLI timed out after {TIMEOUT_SECONDS:.0f}s") from slow
    if done.returncode != 0:
        raise CheckRefused(
            f"`{' '.join(argv)}` exited {done.returncode}: {done.stderr.strip() or '(no stderr)'}"
        )
    return done.stdout


def _parse(stdout: str, argv: list[str]) -> dict:
    """Parse the CLI's JSON leniently.

    stdout is pure JSON on the CLI version verified here, but the CLI also emits
    prompt echoes and deprecation notices, and a cron host may run a version that
    puts one of them on stdout. Anchoring on the first object survives that
    without hiding a genuinely broken answer.
    """
    value = None
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        start = stdout.find("{")
        if start >= 0:
            try:
                value, _ = json.JSONDecoder().raw_decode(stdout[start:])
            except json.JSONDecodeError:
                value = None
    # A list or a bare string parses fine and then fails deep inside the reader
    # with an AttributeError; refuse here, where the message still says what ran.
    if isinstance(value, dict):
        return value
    raise CheckRefused(f"`{' '.join(argv)}` returned unparsable output: {stdout.strip()[:200]!r}")


def _unexpected(argv: list[str], detail: str) -> CheckRefused:
    """Refuse on CLI drift instead of reading a missing field as zero.

    Every field this script reads was taken from a live answer on 2026-09-20. If
    one is renamed or retyped, `.get(...) or 0` would total zero bytes and report
    a healthy volume it never looked at -- the one outcome worth refusing over.
    """
    return CheckRefused(f"`{' '.join(argv)}` answered in an unexpected shape: {detail}")


def _number(value: object, name: str, argv: list[str]) -> float:
    """A real, finite number, or a refusal naming the field that was not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _unexpected(argv, f"{name} is {value!r}, expected a number")
    return float(value)


def _cli(runner, args: list[str]) -> tuple[dict, list[str]]:
    argv = ["railway", *args, "--json"]
    return _parse(runner(argv), argv), argv


def volume_usage(runner, volume: str) -> dict:
    """The named volume's entry, with its two sizes checked to be usable numbers."""
    payload, argv = _cli(runner, ["volume", "list"])
    volumes = payload.get("volumes")
    if not isinstance(volumes, list):
        raise _unexpected(argv, f"no list under 'volumes' (keys: {sorted(payload)})")
    for candidate in volumes:
        if not isinstance(candidate, dict):
            raise _unexpected(argv, f"volume entry {candidate!r} is not an object")
        if candidate.get("name") == volume or candidate.get("id") == volume:
            _number(candidate.get("currentSizeMB"), "currentSizeMB", argv)
            size_mb = _number(candidate.get("sizeMB"), "sizeMB", argv)
            if size_mb <= 0:
                raise _unexpected(argv, f"sizeMB is {size_mb}, expected a provisioned size")
            return candidate
    names = ", ".join(sorted(str(c.get("name")) for c in volumes)) or "(none)"
    raise CheckRefused(f"Volume {volume!r} not found in the linked project. Found: {names}")


def directory_usage(runner, volume: str, path: str, limit_mib: float) -> DirectoryReport:
    """Sum the files directly in one watched directory.

    An *empty* directory answers normally with an empty file list, so a listing
    that fails means the path is not there -- a PGDATA layout change from an image
    or major-version bump, say. That is a broken check, not a healthy state:
    `initdb` creates pg_stat_tmp and pg_wal cannot be missing from a running
    cluster, so absence would silence the sentinel permanently. Let it refuse.
    """
    limit_bytes = int(limit_mib * MIB)
    payload, argv = _cli(runner, ["volume", "files", "--volume", volume, "list", path])
    entries = payload.get("files")
    if not isinstance(entries, list):
        raise _unexpected(argv, f"no list under 'files' (keys: {sorted(payload)})")
    files: list[tuple[int, str, str | None]] = []
    for entry in entries:
        kind = entry.get("type") if isinstance(entry, dict) else None
        if kind not in ENTRY_TYPES:
            raise _unexpected(
                argv, f"entry {entry!r} has type {kind!r}, not one of {sorted(ENTRY_TYPES)}"
            )
        if kind != "file":
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise _unexpected(argv, f"file entry {entry!r} has no name")
        size = int(_number(entry.get("size"), f"size of {name!r}", argv))
        modified = entry.get("modifiedAt")
        files.append((size, name, modified if isinstance(modified, str) else None))
    largest = max(files, default=None)
    return DirectoryReport(
        path=path,
        total_bytes=sum(size for size, _, _ in files),
        limit_bytes=limit_bytes,
        file_count=len(files),
        largest_name=None if largest is None else largest[1],
        largest_bytes=0 if largest is None else largest[0],
        largest_modified=None if largest is None else largest[2],
    )


def check(
    runner=None,
    *,
    volume: str = VOLUME,
    max_used_percent: float = MAX_USED_PERCENT,
    watches: dict[str, float] | None = None,
) -> VolumeReport:
    runner = _run_railway if runner is None else runner
    found = volume_usage(runner, volume)
    report = VolumeReport(
        volume=str(found.get("name") or volume),
        mount_path=str(found.get("mountPath") or ""),
        used_mb=float(found["currentSizeMB"]),
        size_mb=float(found["sizeMB"]),
        max_used_percent=max_used_percent,
    )
    for path, limit_mib in (WATCHES if watches is None else watches).items():
        report.directories.append(directory_usage(runner, volume, path, limit_mib))
    return report


def report_json(report: VolumeReport) -> str:
    payload = {
        "volume": report.volume,
        "mount_path": report.mount_path,
        "used_mb": round(report.used_mb, 3),
        "size_mb": round(report.size_mb, 3),
        "used_percent": round(report.used_percent, 2),
        "max_used_percent": report.max_used_percent,
        "directories": [
            {
                "path": directory.path,
                "bytes": directory.total_bytes,
                "limit_bytes": directory.limit_bytes,
                "file_count": directory.file_count,
                "largest_name": directory.largest_name,
                "largest_bytes": directory.largest_bytes,
                "largest_modified": directory.largest_modified,
                "alerting": directory.alerting,
            }
            for directory in report.directories
        ],
        "alerts": report.alerts,
        "healthy": report.healthy,
        "summary": report.summary,
    }
    return json.dumps(payload, sort_keys=True)


def _bound(text: str, unit: str) -> float:
    """A positive, finite threshold.

    `nan` compares false against everything, so a threshold of nan silently
    disables the very check it was passed to configure; a negative one is
    nonsense in either direction. Both are refused at the boundary.
    """
    try:
        number = float(text)
    except ValueError as bad:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of {unit}") from bad
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive number of {unit}, got {text!r}")
    return number


def _percent(value: str) -> float:
    number = _bound(value, "percent")
    if number > 100:
        raise argparse.ArgumentTypeError(f"expected a percent of 100 or less, got {value!r}")
    return number


def _watch(value: str) -> tuple[str, float]:
    path, _, limit = value.partition("=")
    if not path.startswith("/") or not limit:
        raise argparse.ArgumentTypeError(f"expected /absolute/path=MiB, got {value!r}")
    return path, _bound(limit, "MiB")


def _refuse(detail: str) -> int:
    print(json.dumps({"refused": detail}, sort_keys=True), file=sys.stderr)
    return REFUSED


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--volume", default=VOLUME, help="Volume name or id")
    parser.add_argument("--max-used-percent", type=_percent, default=MAX_USED_PERCENT)
    parser.add_argument(
        "--watch",
        type=_watch,
        action="append",
        metavar="PATH=MiB",
        help="Replace the default watched directories. Repeatable.",
    )
    args = parser.parse_args()
    try:
        report = check(
            volume=args.volume,
            max_used_percent=args.max_used_percent,
            watches=dict(args.watch) if args.watch else None,
        )
    except CheckRefused as refusal:
        return _refuse(str(refusal))
    except Exception as unexpected:
        # An exception would otherwise exit 1, the status that means "the check
        # ran and the volume needs attention". A crashed check ran nothing.
        traceback.print_exc()
        return _refuse(f"{type(unexpected).__name__}: {unexpected}")
    print(report_json(report))
    return 0 if report.healthy else ALERTING


if __name__ == "__main__":
    raise SystemExit(main())
