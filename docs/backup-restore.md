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

## Read-only migration-state audit

The migration audit diagnoses the known 0065 history split and source/ledger drift. It
never applies, records, or repairs a migration.

Use only an **isolated, sidecar-free snapshot** produced by SQLite's online Backup API
or restored from a Hestia backup artifact. Do not point it at the live app path and do
not copy only `hestia.db` from a WAL database: committed state may still live in
`-wal`. SQLite documents the WAL copy boundary in its
[WAL guide](https://www.sqlite.org/wal.html#the_wal_file); Hestia's `backup.sh` already
uses the [Online Backup API](https://sqlite.org/backup.html).

For a compressed Hestia backup, create a private scratch copy and audit it from the
release candidate:

```sh
umask 077
AUDIT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hestia-audit.XXXXXX")"
trap 'rm -rf -- "$AUDIT_DIR"' EXIT HUP INT TERM
AUDIT_DB="$AUDIT_DIR/hestia.db"
gzip -dc /data/backups/hestia-<stamp>.db.gz > "$AUDIT_DB"
chmod 600 "$AUDIT_DB"
python -m hestia.migration_audit "$AUDIT_DB"
python -m hestia.migration_audit "$AUDIT_DB" --json
```

The unpredictable private directory prevents a pre-existing file or symlink from
capturing production bytes. The trap removes this task-owned scratch copy when the shell
exits; preserve a copy only under an explicit incident-artifact retention decision.

| Exit | Meaning |
|---:|---|
| 0 | Exact current source, ledger, and 0065 schema evidence observed |
| 1 | Recognized historical/pre-0065 or pending state needs a human decision |
| 2 | Partial DDL, schema/ledger drift, unknown/gapped migrations, or source checksum drift |
| 3 | Unsafe/missing input, journal sidecar, malformed manifest, unreadable/non-Hestia DB, or audit-time change |

The command refuses any `-wal`, `-shm`, or `-journal` sidecar, opens the snapshot
with SQLite `mode=ro&immutable=1`, holds one read transaction, and verifies its SHA-256,
size, mtime, inode, and sidecar state before and after inspection. Exit 0 is evidence,
not proof of the SQL bytes historically applied: today's ledger stores no checksum, so
`database_applied_sha256` is deliberately `null`. Do not wire this command into
`/readyz` or use it to authorize schema normalization until D4 in
[`HUMAN-DECISIONS.md`](HUMAN-DECISIONS.md) is approved.


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

`rclone copy` is non-deleting: it preserves destination-only objects, but a changed
object at the same path can replace prior remote bytes. D5 therefore requires
destination-side versioning, object lock, or equivalent same-path retention in addition
to remote verification and a freshness receipt. With **S3/R2 storage**, media already
lives off-box and the script copies only DB backups. Current preflight accepts a remote
or `HESTIA_MEDIA_DURABILITY_ACK`, but that configuration acknowledgment is not D5 launch
evidence. Losing every gallery to a dead disk is not a footnote.

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

## Automated scratch drill

CI exercises both scripts against disposable state on every change:

```sh
bash scripts/restore-drill.sh
```

The drill creates a migrated source database, takes an online backup, copies the
compressed artifact, restores it over different scratch state, runs SQLite integrity
checks, and proves the replaced database was kept as a usable pre-restore safety copy.
It deliberately ignores your configured `HESTIA_DATA_DIR` and cannot touch live data.

This synthetic proof catches script and schema compatibility regressions. It does not
replace the quarterly drill below, which must use a real off-site artifact and verify
that client media is present too.

## Quarterly drill checklist

- [ ] Select and download a real versioned DB artifact from the approved **remote**
      destination; record its remote object/version identity. A live-volume or local
      `/data/backups` file is not the quarterly D5 source.
- [ ] Restore that downloaded artifact on a scratch `HESTIA_DATA_DIR` (or staging) —
      never make production your first restore under pressure.
- [ ] Recover required media from the same remote protection set and verify one known
      gallery's bytes/rendering, not only its database rows.
- [ ] Confirm `healthz` is green, `integrity_check` is `ok`, and the known
      client/gallery is present.
- [ ] Review any `pre-restore-*.db` safety copies under the owner-approved retention
      policy; do not delete recovery evidence automatically.
