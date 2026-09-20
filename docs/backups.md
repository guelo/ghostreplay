The db server is running on railway.
Daily Backups are being dumped from a cron job on valtron /usr/local/bin/backup-ghostreplay-postgres with configuration in
/etc/ghostreplay-postgres-backup.env

To inspect/restore a dump:

gunzip -c /srv/backups/ghostreplay-postgres/ghostreplay-YYYYMMDDTHHMMSSZ.dump.gz > /tmp/ghostreplay-restore.dump

pg_restore --list /tmp/ghostreplay-restore.dump | head

For a real restore into a target database:

pg_restore \
--clean \
--if-exists \
--no-owner \
--no-acl \
-d "$TARGET_DATABASE_URL" \
/tmp/ghostreplay-restore.dump

Volume headroom for that same database has its own read-only check: see
`backend/scripts/MONITOR_PG_VOLUME.md` for the daily volume/top-file check that
belongs on this host next to the dump job. It is not installed yet — the cron
entry and its alert are tracked by `g-volume-alert-cron`.
