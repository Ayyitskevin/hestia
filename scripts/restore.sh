#!/usr/bin/env bash
# Restore the Hestia database from a backup produced by scripts/backup.sh.
#
#   HESTIA_DATA_DIR=/srv/hestia/data bash scripts/restore.sh <backup.db[.gz]> [--force]
#
# Safety rails, in order:
#   1. refuses while the app looks live (hestia.db-wal present) unless --force —
#      restoring under an active writer corrupts both worlds
#   2. integrity-checks the unpacked backup BEFORE touching the live database
#   3. checkpoints + keeps the outgoing database as backups/pre-restore-<stamp>.db
# Full drill: docs/backup-restore.md.
set -euo pipefail

DATA_DIR="${HESTIA_DATA_DIR:-./data}"
DB="$DATA_DIR/hestia.db"
OUT_DIR="${HESTIA_BACKUP_DIR:-$DATA_DIR/backups}"

SRC="${1:-}"
FORCE="${2:-}"
[ -n "$SRC" ] || { echo "usage: restore.sh <backup.db[.gz]> [--force]" >&2; exit 2; }
[ -f "$SRC" ] || { echo "ERROR: no backup at $SRC" >&2; exit 1; }

# A WAL sidecar means the app is running or died mid-write. Stop it first
# (docker compose stop hestia) so the pre-restore copy is complete and coherent.
if [ -e "$DB-wal" ] && [ "$FORCE" != "--force" ]; then
  echo "ERROR: $DB-wal exists — the app looks live. Stop it first, or pass --force." >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
TMP="$DATA_DIR/.restore-$STAMP.db"    # same filesystem as $DB so the final mv is atomic

case "$SRC" in
  *.gz) gunzip -c "$SRC" > "$TMP" ;;
  *)    cp "$SRC" "$TMP" ;;
esac

python3 - "$TMP" <<'PY' || { rm -f "$TMP"; echo "ERROR: backup failed integrity_check — live database untouched" >&2; exit 1; }
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
print("integrity_check:", ok)
sys.exit(0 if ok == "ok" else 1)
PY

if [ -f "$DB" ]; then
  # Fold any un-checkpointed WAL into the main file so the safety copy holds the
  # newest writes, then move it aside. Stale sidecars must not shadow the restore.
  python3 -c "import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
conn.close()" "$DB"
  mv "$DB" "$OUT_DIR/pre-restore-$STAMP.db"
  rm -f "$DB-wal" "$DB-shm"
fi
mv "$TMP" "$DB"

python3 - "$DB" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
try:
    version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    tenants = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
except sqlite3.Error:
    version, tenants = "n/a", "n/a"
print(f"restored schema at migration {version}, tenants: {tenants}")
PY
echo "restored $SRC -> $DB (previous database kept at $OUT_DIR/pre-restore-$STAMP.db)"
