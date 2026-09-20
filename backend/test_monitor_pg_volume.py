"""Tests for the production volume check (g-stop-pgss-bloat).

Every Railway call is INJECTED, so this file makes no network calls and reads no
production state: the script is structured around a ``runner`` for exactly that
reason. The fixture payloads below are the shapes the CLI actually returned on
2026-09-20 -- a 5000 MB volume 20.8% full, a 49,375-byte frozen
``pgss_query_texts.stat``, eight 16 MiB WAL segments. Fixtures cannot notice that
the live CLI has changed; what they pin is the script's response to drift, which
is to refuse. The behaviour worth protecting is that no mangled answer -- a
renamed field, a recased type, a missing path -- can come back healthy.

What is deliberately NOT here: the live run against the production volume. That
is the runbook's verification step, recorded in the bead as evidence.
"""

from __future__ import annotations

import json

import pytest

import scripts.monitor_pg_volume as monitor

VOLUME_LIST = {
    "environment": "production",
    "project": "ghostreplay",
    "volumes": [
        {
            "currentSizeMB": 1041.145856,
            "id": "00000000-0000-0000-0000-000000000000",
            "mountPath": "/var/lib/postgresql/data",
            "name": "postgres-volume",
            "sizeMB": 5000,
            "status": "Ready",
        }
    ],
}

STAT_TMP = {
    "files": [
        {
            "modifiedAt": "2026-08-21T08:57:56Z",
            "name": "pgss_query_texts.stat",
            "path": "/pgdata/pg_stat_tmp/pgss_query_texts.stat",
            "size": 49375,
            "type": "file",
        }
    ],
    "remotePath": "/pgdata/pg_stat_tmp",
}

PG_WAL = {
    "files": [
        {"name": f"0000000100000016000000{n:02X}", "size": 16 * 1024 * 1024, "type": "file"}
        for n in range(0x88, 0x90)
    ]
    + [{"name": "archive_status", "size": 4096, "type": "directory"}],
    "remotePath": "/pgdata/pg_wal",
}

BIG = {"name": "pgss_query_texts.stat", "size": 200 * 1024 * 1024, "type": "file"}

MISSING = (
    "Failed to list remote directory /var/lib/postgresql/data/pgdata/log\n"
    "Caused by:\n    Failure: securejoin.OpenInRoot: no such file or directory"
)


def fake_runner(*, volumes=VOLUME_LIST, listings=None, errors=None):
    """Answer the two CLI shapes the script issues, by path."""
    listings = {"/pgdata/pg_stat_tmp": STAT_TMP, "/pgdata/pg_wal": PG_WAL} | (listings or {})
    errors = errors or {}

    def run(argv):
        if argv[1] == "volume" and argv[2] == "list":
            return json.dumps(volumes)
        path = argv[-2]
        if path in errors:
            raise monitor.CheckRefused(errors[path])
        return json.dumps(listings[path])

    return run


def test_healthy_production_shape_reports_every_watched_path():
    report = monitor.check(fake_runner())

    assert report.healthy
    assert report.used_percent == pytest.approx(20.82, abs=0.01)
    stat_tmp, pg_wal = report.directories
    assert (stat_tmp.path, stat_tmp.total_bytes) == ("/pgdata/pg_stat_tmp", 49375)
    assert stat_tmp.largest_name == "pgss_query_texts.stat"
    assert stat_tmp.largest_modified == "2026-08-21T08:57:56Z"
    assert pg_wal.total_bytes == 8 * 16 * 1024 * 1024  # the directory entry is excluded
    assert pg_wal.file_count == 8
    assert pg_wal.largest_modified is None  # optional: absent from this listing


def test_regrown_query_text_file_alerts_and_names_the_file():
    """The whole point of the bound: pg_stat_statements collecting again."""
    regrown = {"files": [dict(STAT_TMP["files"][0], size=151 * 1024 * 1024)]}
    report = monitor.check(fake_runner(listings={"/pgdata/pg_stat_tmp": regrown}))

    assert not report.healthy
    assert "pgss_query_texts.stat" in report.summary
    assert "151.0 MiB" in report.summary and "1.0 MiB bound" in report.summary
    # mtime is the signal that tells a live collector from a frozen leftover.
    assert "modified 2026-08-21T08:57:56Z" in report.summary


def test_sub_mib_sizes_stay_distinguishable_in_the_alert():
    report = monitor.check(fake_runner(), watches={"/pgdata/pg_stat_tmp": 0.01})

    assert "48.2 KiB" in report.summary and "10.2 KiB bound" in report.summary


def test_full_volume_alerts_even_when_every_watched_path_is_clean():
    full = {"volumes": [dict(VOLUME_LIST["volumes"][0], currentSizeMB=4200.0)]}
    report = monitor.check(fake_runner(volumes=full))

    assert not report.healthy
    assert report.alerts == [
        "postgres-volume is 84.0% full (4200 of 5000 MB), over 70%",
    ]
    assert all(not directory.alerting for directory in report.directories)


def test_empty_watched_directory_is_healthy():
    """The good outcome for pg_stat_tmp: it lists fine and holds nothing."""
    report = monitor.check(fake_runner(listings={"/pgdata/pg_stat_tmp": {"files": []}}))

    assert report.healthy
    assert (report.directories[0].file_count, report.directories[0].largest_name) == (0, None)


def test_absent_watched_directory_refuses_rather_than_passing_as_empty():
    """An empty directory lists fine, so a failed listing means the path is gone.

    A PGDATA layout change would otherwise silence the sentinel permanently:
    every watched path reporting "no such file" would read as healthy.
    """
    with pytest.raises(monitor.CheckRefused, match="no such file"):
        monitor.check(fake_runner(errors={"/pgdata/log": MISSING}), watches={"/pgdata/log": 1.0})


@pytest.mark.parametrize(
    "listing",
    [
        pytest.param({"entries": [BIG]}, id="files-key-renamed"),
        pytest.param(
            {"files": [{"name": "q.stat", "bytes": 200 * 1024, "type": "file"}]}, id="size-renamed"
        ),
        pytest.param({"files": [dict(BIG, type="FILE")]}, id="type-recased"),
        pytest.param({"files": [dict(BIG, size="209715200")]}, id="size-as-string"),
        pytest.param({"files": [{"size": 200 * 1024, "type": "file"}]}, id="name-missing"),
    ],
)
def test_listing_drift_refuses_instead_of_totalling_zero_bytes(listing):
    """Each of these once read as an empty directory: 0 bytes, healthy, exit 0."""
    with pytest.raises(monitor.CheckRefused, match="unexpected shape"):
        monitor.check(fake_runner(listings={"/pgdata/pg_stat_tmp": listing}))


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"currentSizeMB": None}, id="used-null"),
        pytest.param({"currentSizeMB": "1041"}, id="used-as-string"),
        pytest.param({"sizeMB": 0}, id="size-zero"),
    ],
)
def test_volume_drift_refuses_instead_of_reporting_zero_percent(override):
    """`0.0% full (0/5000 MB)` is not a healthy volume, it is an unread one."""
    payload = {"volumes": [dict(VOLUME_LIST["volumes"][0], **override)]}
    with pytest.raises(monitor.CheckRefused, match="unexpected shape"):
        monitor.check(fake_runner(volumes=payload))


def test_volume_missing_its_provisioned_size_refuses():
    entry = {k: v for k, v in VOLUME_LIST["volumes"][0].items() if k != "sizeMB"}
    with pytest.raises(monitor.CheckRefused, match="sizeMB"):
        monitor.check(fake_runner(volumes={"volumes": [entry]}))


def test_json_that_is_not_an_object_refuses():
    """A list parses cleanly and then fails deep inside the reader."""
    with pytest.raises(monitor.CheckRefused, match="unparsable"):
        monitor._parse('[{"name": "postgres-volume"}]', ["railway", "volume", "list"])


def test_listing_failure_that_is_not_absence_refuses():
    denied = "Unauthorized: run `railway login`"
    with pytest.raises(monitor.CheckRefused, match="Unauthorized"):
        monitor.check(fake_runner(errors={"/pgdata/pg_wal": denied}))


def test_unknown_volume_refuses_and_names_what_exists():
    with pytest.raises(monitor.CheckRefused, match="postgres-volume"):
        monitor.check(fake_runner(), volume="nope-xyz")


def test_cli_noise_around_the_json_is_tolerated():
    noisy = "> Select a volume postgres-volume\n" + json.dumps(VOLUME_LIST) + "\nwarning: deprecated"
    assert monitor._parse(noisy, ["railway"]) == VOLUME_LIST


def test_unparsable_output_refuses_rather_than_reporting_healthy():
    with pytest.raises(monitor.CheckRefused, match="unparsable"):
        monitor._parse("Failure: openat2 denied", ["railway", "volume", "list"])


def test_exit_status_separates_alerting_from_refused(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["monitor_pg_volume.py"])
    monkeypatch.setattr(monitor, "_run_railway", fake_runner())
    assert monitor.main() == 0
    assert json.loads(capsys.readouterr().out)["healthy"] is True

    monkeypatch.setattr("sys.argv", ["monitor_pg_volume.py", "--max-used-percent", "5"])
    assert monitor.main() == monitor.ALERTING

    monkeypatch.setattr("sys.argv", ["monitor_pg_volume.py", "--volume", "gone"])
    assert monitor.main() == monitor.REFUSED
    assert "refused" in capsys.readouterr().err


def test_unexpected_failure_exits_refused_not_alerting(monkeypatch, capsys):
    """Exit 1 means the volume needs attention; a crashed check examined nothing."""

    def boom(argv):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("sys.argv", ["monitor_pg_volume.py"])
    monkeypatch.setattr(monitor, "_run_railway", boom)

    assert monitor.main() == monitor.REFUSED
    assert "kaboom" in capsys.readouterr().err


@pytest.mark.parametrize(
    "bad",
    [
        "pg_wal=10",
        "/pgdata/pg_wal",
        "/pgdata/pg_wal=big",
        "/pgdata/pg_wal=nan",  # compares false against every size: no bound at all
        "/pgdata/pg_wal=-5",
        "/pgdata/pg_wal=0",
    ],
)
def test_watch_argument_rejects_unusable_bounds(bad):
    with pytest.raises(Exception, match="expected|not a number"):
        monitor._watch(bad)


@pytest.mark.parametrize("bad", ["nan", "inf", "-5", "0", "101", "most"])
def test_fill_threshold_rejects_values_that_would_disable_the_check(bad):
    """`--max-used-percent nan` once reported a 99.98%-full volume as healthy."""
    with pytest.raises(Exception, match="expected|not a number"):
        monitor._percent(bad)
