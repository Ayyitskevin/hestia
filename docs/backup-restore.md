# Backups & restore

Hestia's state is one SQLite file (`hestia.db`) plus the media directory, both on
the `hestia-data` volume. Backups are automatic; restores are a two-command drill
with production-path refusal, integrity checks, and post-restore verification.

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

## Production-path refusal (do not skip)

`scripts/restore.sh` and `python -m hestia.recovery check-target` refuse to write into
paths that look like a live production data directory:

- `./data` / `data` relative to the current working directory
- `/data`, `/srv/hestia/data`, `/var/lib/hestia/data`
- whatever `HESTIA_PRODUCTION_DATA_DIR` names, if set

A deliberate live restore requires **both** a conscious operator action and one of:

```sh
# flag form (preferred — visible in shell history)
HESTIA_DATA_DIR=/srv/hestia/data bash scripts/restore.sh /path/to/hestia-….db.gz --allow-production

# or env form (for automation that already gates on a change ticket)
HESTIA_ALLOW_PRODUCTION_RESTORE=1 HESTIA_DATA_DIR=/srv/hestia/data \
  bash scripts/restore.sh /path/to/hestia-….db.gz
```

Scratch and staging restores never need the override. The automated drill
(`scripts/restore-drill.sh`) proves the refusal path on every CI run.

## Restore procedure (live or staging)

1. Stop the app (leave caddy up; it will 502 briefly):
   ```sh
   docker compose stop hestia
   ```
2. Confirm the artifact exists and is the one you intend (checksum / remote version id).
3. Restore:
   ```sh
   docker compose run --rm --no-deps --entrypoint bash backup \
     /app/scripts/restore.sh /data/backups/hestia-<stamp>.db.gz --allow-production
   ```
   Safety rails, in order:
   - production-path refusal (see above)
   - refuses while the app looks live (`hestia.db-wal` present) unless `--force`
   - missing / unreadable backup → exit non-zero, **live DB untouched**
   - corrupt gzip → exit non-zero, **live DB untouched**
   - empty backup / bare SQLite that passes `PRAGMA integrity_check` but is not Hestia
     (no `schema_migrations`) → refused **before** any live rename
   - unsupported / unknown schema version → refused **before** any live rename
     (`assert_restorable_backup` / `python -m hestia.recovery gate-backup`)
   - disk preflight: refuses when free space cannot hold the unpacked backup + safety copy
   - writes via same-filesystem temp (`.restore-<stamp>.db`) + atomic `mv`
   - keeps the outgoing DB at `backups/pre-restore-<stamp>.db`
   - leaves `.restore-in-progress` if the process dies mid-restore (clear only after you
     understand the half-applied state)
   - every run prints a `correlation_id=` you can grep in logs
4. Restore **media** for the same recovery point (rclone pull, or object-store already
   holds it when `HESTIA_STORAGE_BACKEND=s3`).
5. Verify before opening the door:
   ```sh
   python -m hestia.recovery verify /data/hestia.db \
     --media-dir /data/media \
     --backup /data/backups/hestia-<stamp>.db.gz \
     --require-media \
     --json-out /tmp/hestia-verify.json
   ```
   Success looks like: `integrity_check=ok`, `ok=true`, empty `missing_blobs`, a
   `representative_gallery` with `first_blob_present=true`, and printed
   `rto_ms` / `rpo_s` fields.
6. Start and smoke:
   ```sh
   docker compose start hestia
   curl -sf "https://$HESTIA_DOMAIN/healthz"
   curl -sf "https://$HESTIA_DOMAIN/readyz"
   ```
7. Log in, open one known gallery, confirm bytes and client access.

Bare-metal: same `restore.sh` with `HESTIA_DATA_DIR` pointing at the data dir (and the
production override only when that path is live).

## Automated scratch drill

CI exercises backup → media copy → restore → verify against disposable state on every
change:

```sh
bash scripts/restore-drill.sh
# optional: keep the verification JSON
HESTIA_DRILL_REPORT=/tmp/drill-report.json bash scripts/restore-drill.sh
```

The drill:

1. Seeds a migrated source DB **with a published gallery and real JPEG media**.
2. Takes an online backup and copies the compressed artifact.
3. Proves `HESTIA_DATA_DIR=./data` is **refused** without an override.
4. Restores over different scratch state and keeps a pre-restore safety copy.
5. Runs `python -m hestia.recovery verify` (SQLite integrity, schema support, tenant
   ownership, DB↔media consistency, representative gallery blob presence).
6. Prints `restore drill OK` with `rto_ms`, `rpo_s`, and `correlation_id`.

It deliberately ignores your configured `HESTIA_DATA_DIR` and cannot touch live data.
This synthetic proof catches script and schema compatibility regressions. It does not
replace the quarterly drill below, which must use a real off-site artifact.

### Failure modes the tooling must fail loud on

| Scenario | Expected behavior |
|----------|-------------------|
| Missing backup file | exit ≠ 0; target DB unchanged |
| Empty `.db` / empty `.db.gz` | exit ≠ 0; target DB unchanged (never install 0-byte) |
| Corrupt gzip | exit ≠ 0; target DB unchanged |
| Gzip of non-SQLite garbage | integrity/schema gate fails; target DB unchanged |
| Non-Hestia SQLite (integrity ok, no ledger) | schema gate refuses; target DB unchanged |
| Unsupported schema version (future ledger) | restore + verify refuse; target DB unchanged |
| Partial media (DB row, missing blob) | `verify` → `ok=false`, `missing_blobs` listed |
| Size mismatch (truncated blob) | `verify` → `ok=false`, `size_mismatches` listed |
| Checksum mismatch vs source inventory | `verify --expected-checksums` → `checksum_mismatches` |
| Interrupted restore (`.restore-in-progress`) | operator inspects; re-run restore after quiescing WAL |
| Insufficient disk (preflight) | exit ≠ 0 before any live rename |
| Accidental `./data` / `/data` target | refused without `--allow-production` |

Pytest covers these under `tests/test_disaster_recovery.py` and side-effect
idempotency under `tests/test_dr_idempotency.py`.

## Post-restore verification (operators)

```sh
# Full report (JSON to stdout + optional file)
python -m hestia.recovery verify "$HESTIA_DATA_DIR/hestia.db" \
  --media-dir "$HESTIA_DATA_DIR/media" \
  --backup /path/to/hestia-<stamp>.db.gz \
  --require-media \
  --json-out ./verify-report.json

# Consistency only
python -m hestia.recovery consistency "$HESTIA_DATA_DIR/hestia.db" "$HESTIA_DATA_DIR/media"

# Path safety only
python -m hestia.recovery check-target "$HESTIA_DATA_DIR"
```

### Interpreting diagnostics

Structured lines use logger `hestia.recovery` with fields:

- `action` — e.g. `recovery.restore.begin`, `recovery.verify.complete`,
  `recovery.restore.refused_production`, `recovery.disk.insufficient`
- `correlation_id` / `request_id` — 12-hex id tying shell + Python steps (also printed
  by `restore.sh` / the drill)
- counts: `tenant_count`, `gallery_count`, `image_count`, `elapsed_ms`, `rpo_seconds`
- **never** client tokens, passwords, secrets, or email bodies

### RPO / RTO fields

| Field | Meaning |
|-------|---------|
| `rpo_seconds` | Age of the backup artifact at verification time (wall clock − backup mtime). Lower is fresher. |
| `elapsed_ms` / drill `rto_ms` | Time spent in verification (or the full scratch drill). This is a **lower bound** on operator RTO; a real incident also includes decision time, media pull, and DNS/TLS. |

Honest default targets for a single-node SQLite deploy (tune with evidence):

- **RPO**: ≤ 24 h when daily backup + off-site sync are green (bounded by backup cadence).
- **RTO**: tens of minutes for DB restore + verify on scratch; add media transfer time for
  large local galleries.

## Rollback after a failed deploy

App/image rollback (code only — data already migrated forward may not reverse):

```sh
# 1. Note the running image / compose digest before changing anything
docker compose images
# 2. Redeploy the previous known-good commit/image
git -C /path/to/deploy fetch && git -C /path/to/deploy checkout <good-sha>
docker compose up -d --build
# 3. Confirm
bash scripts/hosted-preflight.sh --url "https://$HESTIA_DOMAIN"
curl -sf "https://$HESTIA_DOMAIN/readyz"
```

Data rollback (when a bad migration or bad write corrupted state):

```sh
docker compose stop hestia
# Prefer the pre-restore safety copy written just before the last restore, or a
# known-good off-site artifact:
bash scripts/restore.sh /data/backups/pre-restore-<stamp>.db --allow-production
# or: bash scripts/restore.sh /data/backups/hestia-<good>.db.gz --allow-production
python -m hestia.recovery verify /data/hestia.db --media-dir /data/media --require-media
docker compose start hestia
```

Forward-only migrations never rewrite history: if the bad release applied a new
`NNNN_*.sql`, rolling the image back without a data restore leaves the ledger ahead of
the code. Prefer **restore-to-known-good backup** over hand-editing `schema_migrations`.

## Escalation

| Severity | Trigger | Action |
|----------|---------|--------|
| Page | `backup` container crash-looping; off-site sync stale > 48 h; `/readyz` red | Page on-call; restore from last good off-site artifact onto **staging** first |
| High | `verify` reports `missing_blobs` after restore; media/DB mismatch | Stop serving galleries if needed; pull media from off-site; do not invent blobs |
| High | migration audit exit 2–3 on a candidate release | Hold deploy; see D4 in `HUMAN-DECISIONS.md` |
| Medium | climbing failed jobs / dead-letter | Requeue only after root-cause fix (`/admin/system`); handlers are at-least-once |
| Info | restore drill or CI red | Fix before merge — do not ship a broken recovery path |

## Quarterly drill checklist

- [ ] Select and download a real versioned DB artifact from the approved **remote**
      destination; record its remote object/version identity. A live-volume or local
      `/data/backups` file is not the quarterly D5 source.
- [ ] Restore that downloaded artifact on a scratch `HESTIA_DATA_DIR` (or staging) —
      never make production your first restore under pressure.
- [ ] Recover required media from the same remote protection set and verify one known
      gallery's bytes/rendering, not only its database rows
      (`python -m hestia.recovery verify … --require-media`).
- [ ] Confirm `healthz` / `readyz` green, `integrity_check` is `ok`, and the known
      client/gallery is present.
- [ ] Run `python -m hestia.migration_audit` against the restored scratch DB.
- [ ] Record observed `rpo_seconds` and wall-clock RTO in the incident/ops log.
- [ ] Review any `pre-restore-*.db` safety copies under the owner-approved retention
      policy; do not delete recovery evidence automatically.
