#!/usr/bin/env bash
# Daily SQLite backup with 14-day rotation. Uses SQLite's online-backup API via
# python3 (safe against a live WAL database — never copy the file directly, and
# no sqlite3 CLI needed, so it runs unchanged inside the app container).
#
# Also writes a privacy-safe generation manifest next to the artifact that binds
# the DB checksum to the media inventory for this generation (see
# docs/backup-restore.md · hestia.recovery).
#
#   HESTIA_DATA_DIR=/srv/hestia/data bash scripts/backup.sh
#
# Hosted (docker compose): the `backup` service in docker-compose.yml runs this
# daily against the shared data volume. See docs/backup-restore.md.
set -euo pipefail

DATA_DIR="${HESTIA_DATA_DIR:-./data}"
DB="$DATA_DIR/hestia.db"
OUT_DIR="${HESTIA_BACKUP_DIR:-$DATA_DIR/backups}"
MEDIA_DIR="${HESTIA_MEDIA_DIR:-$DATA_DIR/media}"
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

# Keep an unpacked copy long enough to stamp schema into the generation manifest.
UNPACKED_FOR_MANIFEST="$OUT_DIR/.manifest-src-$STAMP.db"
cp "$TARGET" "$UNPACKED_FOR_MANIFEST"
gzip -f "$TARGET"
ARTIFACT="$TARGET.gz"

MEDIA_ARGS=()
if [ -d "$MEDIA_DIR" ]; then
  MEDIA_ARGS=(--media-dir "$MEDIA_DIR")
fi
python3 -m hestia.recovery manifest-build "$ARTIFACT" \
  --unpacked-db "$UNPACKED_FOR_MANIFEST" \
  "${MEDIA_ARGS[@]}" \
  --out "${ARTIFACT}.manifest.json"
rm -f "$UNPACKED_FOR_MANIFEST"

# Rotate: keep the newest $KEEP backups (and their sidecars).
while IFS= read -r old; do
  [ -n "$old" ] || continue
  rm -f "$old" "${old}.manifest.json"
done < <(ls -1t "$OUT_DIR"/hestia-*.db.gz 2>/dev/null | tail -n "+$((KEEP + 1))" || true)

echo "backed up → $ARTIFACT (+ ${ARTIFACT}.manifest.json) ($(ls -1 "$OUT_DIR"/hestia-*.db.gz 2>/dev/null | wc -l) kept)"
