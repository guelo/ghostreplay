# Production Postgres volume and top-file monitoring

Authority: `g-stop-pgss-bloat`. Tooling: `scripts/monitor_pg_volume.py`.

## Why this exists

Production ran out of room quietly. The Railway Postgres volume was 1 GB and sat
at about 84% full, and 151 MB of that was a single file:
`pg_stat_tmp/pgss_query_texts.stat`, retained by `pg_stat_statements` with
`max=5000, save=on`. The extension objects were absent, so nothing could read the
statistics that file was paying for — the cost was pure waste.

The rollout on 2026-08-21 resized the volume to 5 GB, reset the collector's
statistics and query text, and disabled the collector with a
`shared_preload_libraries = ''` assignment appended to `postgresql.conf`. The
Railway postgres-ssl wrapper still writes its own `pg_stat_statements` line into
that file on boot; the later override is what wins, and that is the fragile part.
If an image change ever reorders or drops the override, the collector starts
again and the query-text file starts growing within minutes of the restart.

So there are two things to watch, and they fail on different timescales:

- **Volume fill** moves slowly and catches growth from any source, including
  sources this runbook does not name.
- **A watched directory** catches a fast local regression that a slow-moving
  total would hide for weeks. `/pgdata/pg_stat_tmp` is the regression sentinel
  for the collector; `/pgdata/pg_wal` is the other path that can plausibly eat a
  volume without anyone writing a row.

## Running the check

Requires the Railway CLI, logged in, in a directory linked to the `ghostreplay`
project (`railway link`). It needs no database credentials, no venv, and no
`DATABASE_URL`: it reads the volume through Railway, not through Postgres.

```bash
cd /path/to/ghostreplay            # any directory linked to the project
python3 backend/scripts/monitor_pg_volume.py
```

It lists, and never downloads, renames, or deletes. One run is finite — one
Railway call per watched directory plus one for the volume, three in all — and
keeps no state between invocations, so it can be killed or run twice without
consequence.

**Exit status**

| Code | Meaning | Response |
| --- | --- | --- |
| 0 | Healthy. Fill is under the limit and every watched path is within its bound. | None. |
| 1 | Alerting. A threshold was crossed. The `summary` field names which. | Triage below. |
| 2 | Refused. The check did not run: CLI missing, logged out, unlinked, volume renamed, a watched path gone, an unusable threshold, or an answer in an unexpected shape. | Fix the monitoring. A silent 2 in cron is the failure mode worth noticing. |

The check refuses rather than defaulting, because the one result it must never
produce is *healthy without having looked*. That outcome is worse than no
monitor: the ping still succeeds, so the missing-ping alarm that would otherwise
catch a broken check stays quiet too. An unusable argument exits 2 as well —
argparse's own error status happens to agree with this contract.

The report is one JSON object on stdout (a refusal is one JSON object on stderr).
`summary` is a single human-readable line written for a cron mail subject;
`alerts` is the machine-readable list behind it. Healthy production looks like:

```json
{"healthy": true, "summary": "healthy: postgres-volume 20.8% full (1041/5000 MB), watched paths within bounds", ...}
```

Flags: `--volume NAME`, `--max-used-percent N`, and `--watch /path=MiB`
(repeatable; replaces the defaults rather than adding to them).

## The thresholds, and why those numbers

| What | Bound | Reasoning |
| --- | --- | --- |
| Volume fill | 70% of 5000 MB | Growth between the 2026-08-21 rollout and 2026-09-20 was 943 MB → 1041 MB, about 100 MB/month. 70% is ~3.5 GB, which at that rate is over a year of warning before the volume is full. |
| `/pgdata/pg_stat_tmp` | 1 MiB | The frozen `pgss_query_texts.stat` is 49,375 bytes. 1 MiB is generous for the healthy state and two orders of magnitude below the 151 MB it reached while collecting. When the collector was live this file went 717 B → 49 KB in minutes, so regrowth shows up immediately in `modifiedAt`; how long it takes to cross 1 MiB was never measured. |
| `/pgdata/pg_wal` | 1536 MiB | Steady state is 128 MiB (eight 16 MiB segments). 1.5 GiB is above anything `max_wal_size` produces under load and below anything that threatens a 5 GB volume, so it fires on WAL that is *stuck* — a dead replication slot, failed archiving — not on write volume. |

Directory sizes are the sum of the files directly in the directory. Railway
reports 4096 for a subdirectory rather than its recursive size, and walking
`base/` would be thousands of calls naming relation files no operator can act on.
Every watched path is a flat directory where that sum is the answer. Database
growth inside `base/` is what the volume fill percent is for.

An **empty** watched directory is healthy: it lists normally with no files, and
`pg_stat_tmp` holding nothing at all is the best outcome here. An **absent** one
is exit 2 — `initdb` creates `pg_stat_tmp` and a running cluster cannot be missing
`pg_wal`, so a path that is gone means the PGDATA layout moved under the check (an
image or major-version bump), and both sentinels would otherwise report healthy
forever.

The same rule covers the CLI's own JSON. Every field the check reads is validated
instead of defaulted, so a renamed key, a recased `type`, or a non-numeric size is
exit 2 rather than a total of zero bytes. Each watched directory's largest file
carries its `modifiedAt` into the report as `largest_modified`, and the alert
names it: size alone cannot tell a live collector from the frozen leftover this
bead produced.

## Recurring schedule and alerting

Two layers that fail independently: the cron check sees the files but depends on
one host, and Railway's own monitor sees only the volume total but needs no host
of ours. Only the first is available on this workspace's plan (see layer 2), so
it carries both jobs — which is why it reports to a cron monitor rather than
mailing, so that the host going quiet is itself an alert.

### 1. Cron on the backup host, pinging a cron monitor

`valtron` already runs the nightly dump (see `docs/backups.md`), so it is the
host that already has an opinion about this database. Daily is the right cadence
for the volume, which moves ~3 MB/day. For the file bound it is the cadence that
was chosen rather than one that was measured: regrowth begins within minutes of a
restart, but nothing here establishes how long it takes to cross 1 MiB. The
report carries each watched directory's largest `modifiedAt`, which moves as soon
as the collector writes — the sharper signal, and the natural thing to alert on
if a day ever proves too slow (`g-volume-alert-cron`).

valtron has **no MTA**, so cron's own `MAILTO` mail goes nowhere. Rather than
stand up an SMTP relay for one job, the run reports to a cron monitor
(Healthchecks.io or equivalent), which owns the alerting:

```cron
# Daily production Postgres volume + top-file check (g-stop-pgss-bloat).
# The ping URL's last path segment is the exit status: 0 succeeds the check,
# 1 (alerting) and 2 (refused) fail it immediately. The report is the body.
17 7 * * * cd /path/to/linked-checkout && out=$(python3 backend/scripts/monitor_pg_volume.py 2>&1); curl -fsS -m 10 --retry 3 --data-raw "$out" "https://hc-ping.com/<uuid>/$?" >/dev/null
```

`$?` immediately after the assignment is the script's own status: a command
substitution passes its exit code through the assignment. Verified under
`/bin/sh` against the live volume — healthy pinged `/0` with the JSON report as
the body, `--max-used-percent 5` pinged `/1`, and an unknown volume pinged `/2`
with the refusal. Sending the status
rather than only pinging on success means a bad run alerts at once instead of
waiting out the dead-man grace period, and the JSON report arrives as the body
of the alert.

This shape covers the failure a plain mail alert cannot: a check that **stopped
running** — dead host, deleted crontab, logged-out CLI — looks exactly like a
healthy one to email, and trips the monitor's missing-ping alarm here. That
matters because layer 2 is unavailable on this plan and nothing else is
watching. Point the backup job at a second check while you are there.

The script is stdlib-only and needs no venv — verified on Python 3.9.6. It
always prints its report, so `out` carries the JSON on every outcome. The
working directory must be linked to the Railway project; a project token in
`RAILWAY_TOKEN` is the documented alternative to a link, but it has **not** been
tested for this script — verify a `0` exit before relying on it.

If an MTA is ever configured on that host, the mail-only form is
`MAILTO=<address>` plus `... || echo "$out"`, which stays quiet on a healthy day
because cron mails only when a job writes output. It still cannot notice silence.

**Status: not installed.** Tracked by `g-volume-alert-cron`. The host is outside
this repo and needs an operator with shell access.

### 2. Railway disk-usage monitor — **Pro plan only**

The production dashboard already has a **Disk usage** item
(`VOLUME_METRICS_ITEM`, `DISK_USAGE_GB`) with an empty `monitors` list. Railway's
monitors alert on CPU, RAM, disk usage and network egress crossing a threshold,
and deliver by email, in-app notification and the project webhook. They are a
**Pro plan** feature ([docs](https://docs.railway.com/observability)).

This workspace is on **Hobby**, verified 2026-09-20:
`query { me { workspaces { plan } } }` returns `HOBBY`, and its plan limits
(`observability.logRetentionDays: 7`, `volumes.maxBackupsCount: 0`) agree — the
same entitlement that made the on-demand backup during the 2026-08-21 rollout
fail with "You do not have access to this resource". So the widget's three-dot
menu has no **Add monitor** entry today. Until the plan changes, layer 1 is the
only alert, which is why its bounds cover the volume total as well as the files.

On Pro, the setup is:

1. Railway → `ghostreplay` → production → Observability.
2. Three-dot menu on the **Disk usage** widget → **Add monitor**.
3. Threshold **3.5 GB** (70% of the 5 GB volume), matching `--max-used-percent`.

Note what this layer would and would not cover: the volume total only. A
`pgss_query_texts.stat` growing back to 151 MB moves the total by 3% and would
never cross a 3.5 GB threshold — that regression is layer 1's job, on any plan.

Creating the monitor cannot be scripted. Checked against the live GraphQL schema
on 2026-09-20: `observabilityDashboardCreate`/`Update` accept only
`ObservabilityDashboardItemConfigInput` (`measurements`, `resourceIds`,
`httpMetric`, `logsFilter`, `projectUsageProperties`) — there is no monitor or
threshold input anywhere in the mutation surface, and
`ObservabilityDashboardMonitor` is read-only. `notificationRuleCreate` exists but
takes deployment-style `eventTypes`, not metric thresholds. Re-check before
concluding it is still impossible; do not improvise through unsupported calls.

**Status: unavailable on this plan.**

To read the current state at any time:

```bash
railway api --var environmentId=be83d48a-d5a4-460e-95fd-fd810b4d16de \
  'query($environmentId: String!) { observabilityDashboards(environmentId: $environmentId, first: 20)
     { edges { node { items { dashboardItem { name type monitors { id } } } } } } }'
```

## Triage

**`/pgdata/pg_stat_tmp` over bound** — `pg_stat_statements` is collecting again.
This is the regression this bead closed. Confirm from the database:

```sql
SHOW shared_preload_libraries;                        -- expect empty
SELECT * FROM pg_extension WHERE extname = 'pg_stat_statements';  -- expect no rows
SELECT sourcefile, sourceline, error FROM pg_file_settings
 WHERE name = 'shared_preload_libraries';
```

If the override is gone from `postgresql.conf`, re-append
`shared_preload_libraries = ''` as the **last** assignment of that setting, check
`pg_file_settings` reports no error, then restart in a reviewed window and
re-verify. The pre-change config copy is kept on the volume at
`/pgdata/postgresql.conf.pre-pgss-disable-20260821`
(SHA-256 `16b9600362ac031606bdae900def23d1d7d8c2a105e7477d736070ab2781a9ff`).

**Never** use `ALTER SYSTEM SET shared_preload_libraries = ''`. It serializes into
`postgresql.auto.conf` as a quoted empty library *name*, and Postgres then fails
to start with `FATAL: could not access file ""`. That happened here on
2026-08-21; recovery was editing `postgresql.auto.conf` on the volume.

A residual stale query-text file is not itself evidence the collector is live —
after the disable, the 49,375-byte file stayed byte-identical with an unchanged
mtime across reads, restart, and redeploy. Compare `modifiedAt`, not just size:
the report and the alert both carry it as `largest_modified`.
Do not delete files under PGDATA by hand.

**`/pgdata/pg_wal` over bound** — WAL is not being recycled. Check
`pg_replication_slots` for an inactive slot holding WAL, and archiving status.
Do not delete WAL segments by hand.

**Volume fill over 70%** — find out what grew before resizing. `base/` growth is
real data: check the largest relations (`pg_total_relation_size`), the retention
jobs (`scripts/retain_opponent_decisions.py`, `RETAIN_SRS_OPPORTUNITIES.md`),
and whether a migration left a bloated table. Resize before any `VACUUM FULL` or
`REINDEX`: both need free space to run.

**Exit 2** — the monitoring is broken, not the volume. Usually the CLI is logged
out, or the directory is no longer linked. The refusal message carries the CLI's
own stderr. Two refusals mean the check's assumptions have expired rather than
its credentials: `no such file or directory` on a watched path (the PGDATA layout
moved — find the new path and update `WATCHES`) and `answered in an unexpected
shape` (the Railway CLI's JSON changed — the message names the field, and the
fixtures in `backend/test_monitor_pg_volume.py` are what to update alongside).

## Verified state

Recorded 2026-09-20, one month after the rollout, from live runs:

- Volume `postgres-volume`: 1029–1041 MB of 5000 MB, ~20.7% full. The swing
  across runs is WAL recycling, not growth.
- `/pgdata/pg_stat_tmp`: one file, `pgss_query_texts.stat`, **49,375 bytes**,
  mtime `2026-08-21T08:57:56Z` — byte-identical and untouched since the disable,
  a month and many deploys later. The collector is not running.
- `/pgdata/pg_wal`: seven to eight 16 MiB segments, 112–128 MiB.
- Exit 0. Exit 1 was exercised live via `--max-used-percent`; exit 2 live via an
  unknown volume, an absent watched path, and a `nan` threshold. The CLI-drift
  refusals are covered by injected fixtures rather than live runs — the live CLI
  cannot be made to answer in a shape it does not produce.
