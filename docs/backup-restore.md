# Backups & restore

Hestia's state is one SQLite file (`hestia.db`) plus the media directory, both on
the `hestia-data` volume. Backups are automatic; restores are a two-command drill.

## How backups run

- The `backup` service in `docker-compose.yml` runs `scripts/backup.sh` once at
  start and then daily. It uses SQLite's online-backup API via python3 — safe
  against the live WAL database, no CLI dependency, no app downtime.
- Backups land on the shared volume at `/data/backups/hestia-<stamp>.db.gz`,
  newest `HESTIA_BACKUP_KEEP` kept (default 14).
- **Failure is loud**: a failed backup (missing DB, bad data dir) crash-loops the
  container. If `docker compose ps` shows `backup` restarting, investigate — do
  not ignore it.
- Host path to the artifacts:
  `docker volume inspect <project>_hestia-data` → `Mountpoint`, then `backups/`.

## Manual backup right now

```sh
docker compose exec hestia bash /app/scripts/backup.sh
```

Bare-metal (systemd/deploy.sh installs): `HESTIA_DATA_DIR=/srv/hestia/data bash scripts/backup.sh`.

## Off-site copies

The `.db.gz` artifacts are plain files — sync `backups/` off the box with
rsync/rclone on whatever cadence you trust the box. One machine is zero backups.

## Restore drill

1. Stop the app (leave caddy up; it will 502 briefly):
   ```sh
   docker compose stop hestia
   ```
2. Restore a chosen artifact (tab-complete the newest):
   ```sh
   docker compose run --rm --no-deps --entrypoint bash backup \
     /app/scripts/restore.sh /data/backups/hestia-<stamp>.db.gz
   ```
   `restore.sh` refuses if the app still looks live (`hestia.db-wal` present),
   integrity-checks the backup **before** touching the live DB, and keeps the
   outgoing database at `backups/pre-restore-<stamp>.db` in case you need to
   roll forward again.
3. Start and verify:
   ```sh
   docker compose start hestia
   curl -sf "https://$HESTIA_DOMAIN/healthz"
   ```
4. Log in, open a gallery, confirm the data is what you expected.

Bare-metal: same `restore.sh` with `HESTIA_DATA_DIR` pointing at the data dir.

## Quarterly drill checklist

- [ ] Pick a real backup from `/data/backups` (not one made for the test).
- [ ] Run the restore drill above on a scratch `HESTIA_DATA_DIR` (or the staging
      box) — never your first restore on the production volume under pressure.
- [ ] Confirm `healthz` is green and a known client/gallery is present.
- [ ] Confirm the off-site copy actually has yesterday's artifact.
- [ ] Delete stale `pre-restore-*.db` safety copies after a successful drill.
