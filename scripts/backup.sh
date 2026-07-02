#!/usr/bin/env bash
# Daily SQLite backup with 14-day rotation. Uses SQLite's online-backup API via
# python3 (safe against a live WAL database — never copy the file directly, and
# no sqlite3 CLI needed, so it runs unchanged inside the app container).
#
#   HESTIA_DATA_DIR=/srv/hestia/data bash scripts/backup.sh
#
# Hosted (docker compose): the `backup` service in docker-compose.yml runs this
# daily against the shared data volume. See docs/backup-restore.md.
set -euo pipefail

DATA_DIR="${HESTIA_DATA_DIR:-./data}"
DB="$DATA_DIR/hestia.db"
OUT_DIR="${HESTIA_BACKUP_DIR:-$DATA_DIR/backups}"
KEEP="${HESTIA_BACKUP_KEEP:-14}"

# Fail LOUDLY when the DB isn't where we expect — a silent exit-0 here would let a
# data-dir misconfiguration masquerade as "backups green" while nothing is backed up.
[ -f "$DB" ] || { echo "ERROR: no database at $DB — check HESTIA_DATA_DIR" >&2; exit 1; }
mkdir -p "$OUT_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="$OUT_DIR/hestia-$STAMP.db"
python3 - "$DB" "$TARGET" <<'PY'
import sqlite3
import sys

src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close()
src.close()
PY
gzip -f "$TARGET"

# Rotate: keep the newest $KEEP backups.
ls -1t "$OUT_DIR"/hestia-*.db.gz 2>/dev/null | tail -n "+$((KEEP + 1))" | xargs -r rm -f

echo "backed up → $TARGET.gz ($(ls -1 "$OUT_DIR" | wc -l) kept)"
