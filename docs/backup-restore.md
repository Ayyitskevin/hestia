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

## Off-site copies (DB **and** media)

One machine is zero backups — and for a photography product the media directory (the
client galleries) is the irreplaceable half. The daily `backup` service snapshots the
**database** to `backups/`; the **media** blobs are not in those artifacts, so an
off-site copy must carry both.

`scripts/offsite-sync.sh` does exactly that — it pushes `backups/` *and* (for local
storage) the media directory to an rclone remote:

```sh
# one-time: configure the remote (S3, B2, R2, Drive, SFTP…)
rclone config

# then cron it a few minutes after the daily backup:
HESTIA_OFFSITE_REMOTE="s3:my-bucket/hestia" bash scripts/offsite-sync.sh
```

Media blobs are content-addressed and immutable, so each run copies only new files, and
it uses `copy` (never `sync`) so a client's originals are never deleted off-site. With
**S3/R2 storage** the media already lives off-box and the script syncs only the DB
backups. Preflight **fails** a local-storage launch until `HESTIA_OFFSITE_REMOTE` is set
(or `HESTIA_MEDIA_DURABILITY_ACK`, if host volume snapshots cover it) — losing every
gallery to a dead disk is not a footnote.

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
