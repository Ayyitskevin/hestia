#!/usr/bin/env bash
# Nightly SQLite backup with 14-day rotation. Uses sqlite3's online .backup
# (safe against a live WAL database — never copy the file directly).
#
#   HESTIA_DATA_DIR=/srv/hestia/data bash scripts/backup.sh
#
# Hosted (docker compose): run on the host against the mounted volume, e.g.
#   HESTIA_DATA_DIR=/var/lib/docker/volumes/hestia_data/_data bash scripts/backup.sh
# and wire it to cron or a systemd timer.
set -euo pipefail

DATA_DIR="${HESTIA_DATA_DIR:-./data}"
DB="$DATA_DIR/hestia.db"
OUT_DIR="$DATA_DIR/backups"
KEEP="${HESTIA_BACKUP_KEEP:-14}"

# Fail LOUDLY when the DB isn't where we expect — a silent exit-0 here would let a
# data-dir misconfiguration masquerade as "backups green" while nothing is backed up.
[ -f "$DB" ] || { echo "ERROR: no database at $DB — check HESTIA_DATA_DIR" >&2; exit 1; }
mkdir -p "$OUT_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="$OUT_DIR/hestia-$STAMP.db"
sqlite3 "$DB" ".backup '$TARGET'"
gzip -f "$TARGET"

# Rotate: keep the newest $KEEP backups.
ls -1t "$OUT_DIR"/hestia-*.db.gz 2>/dev/null | tail -n "+$((KEEP + 1))" | xargs -r rm -f

echo "backed up → $TARGET.gz ($(ls -1 "$OUT_DIR" | wc -l) kept)"
