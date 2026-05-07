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
