#!/usr/bin/env bash
# Restore the Hestia database from a backup produced by scripts/backup.sh.
#
#   HESTIA_DATA_DIR=/path/to/scratch bash scripts/restore.sh <backup.db[.gz]> [--force] [--allow-production]
#
# Safety rails, in order:
#   0. refuses production-like data dirs unless --allow-production or
#      HESTIA_ALLOW_PRODUCTION_RESTORE=1 (see hestia.recovery / docs/backup-restore.md)
#   1. refuses while the app looks live (hestia.db-wal present) unless --force —
#      restoring under an active writer corrupts both worlds
#   2. refuses missing / unreadable backup files (exit 1) before any write
#   3. preflight: enough free disk for the unpacked backup + safety copy
#   4. integrity-checks the unpacked backup BEFORE touching the live database
#   5. checkpoints + keeps the outgoing database as backups/pre-restore-<stamp>.db
#   6. uses a same-filesystem temp file + final mv so a crash mid-unpack leaves
#      the live DB untouched; a leftover .restore-*.db is cleaned on success
# Full drill: docs/backup-restore.md · automated: scripts/restore-drill.sh
set -euo pipefail

DATA_DIR="${HESTIA_DATA_DIR:-./data}"
DB="$DATA_DIR/hestia.db"
OUT_DIR="${HESTIA_BACKUP_DIR:-$DATA_DIR/backups}"
CORRELATION_ID="${HESTIA_RECOVERY_CORRELATION_ID:-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')}"
export HESTIA_RECOVERY_CORRELATION_ID="$CORRELATION_ID"

SRC=""
FORCE=""
ALLOW_PRODUCTION=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE="--force" ;;
    --allow-production) ALLOW_PRODUCTION="--allow-production" ;;
    -*)
      echo "ERROR: unknown flag $arg" >&2
      exit 2
      ;;
    *)
      if [ -z "$SRC" ]; then SRC="$arg"
      else
        echo "ERROR: unexpected argument $arg" >&2
        exit 2
      fi
      ;;
  esac
done

[ -n "$SRC" ] || { echo "usage: restore.sh <backup.db[.gz]> [--force] [--allow-production]" >&2; exit 2; }
[ -f "$SRC" ] || { echo "ERROR: no backup at $SRC (correlation_id=$CORRELATION_ID)" >&2; exit 1; }
[ -r "$SRC" ] || { echo "ERROR: backup not readable: $SRC (correlation_id=$CORRELATION_ID)" >&2; exit 1; }

# Production-path refusal via the shipped recovery helper (same logic as pytest).
python3 - "$DATA_DIR" "$ALLOW_PRODUCTION" "$CORRELATION_ID" <<'PY'
import sys
from hestia.recovery import RecoveryError, assert_safe_restore_target, structured_diag

data_dir, allow_flag, cid = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    assert_safe_restore_target(
        data_dir,
        allow_production=(allow_flag == "--allow-production"),
        correlation_id=cid,
    )
except RecoveryError as exc:
    print(f"ERROR: {exc} (correlation_id={cid})", file=sys.stderr)
    sys.exit(2)
structured_diag("recovery.restore.begin", correlation_id=cid, data_dir=data_dir)
print(f"restore target accepted: {data_dir} (correlation_id={cid})")
PY

# A WAL sidecar means the app is running or died mid-write. Stop it first
# (docker compose stop hestia) so the pre-restore copy is complete and coherent.
if [ -e "$DB-wal" ] && [ "$FORCE" != "--force" ]; then
  echo "ERROR: $DB-wal exists — the app looks live. Stop it first, or pass --force. (correlation_id=$CORRELATION_ID)" >&2
  exit 1
fi

mkdir -p "$OUT_DIR" "$DATA_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
TMP="$DATA_DIR/.restore-$STAMP.db"    # same filesystem as $DB so the final mv is atomic
# Mark in-progress restore so an interrupted run is detectable by operators/drills.
MARKER="$DATA_DIR/.restore-in-progress"
printf '%s\n' "correlation_id=$CORRELATION_ID" "stamp=$STAMP" "src=$SRC" "started=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$MARKER"

cleanup_failed() {
  rm -f "$TMP"
  # Leave the marker so operators can see a half-applied attempt; success path removes it.
}
trap cleanup_failed ERR

# Disk preflight: need room for unpacked backup (+ safety copy of current DB if present).
NEED=$(( $(wc -c < "$SRC") * 3 + 1048576 ))
if [ -f "$DB" ]; then
  NEED=$(( NEED + $(wc -c < "$DB") ))
fi
python3 - "$DATA_DIR" "$NEED" "$CORRELATION_ID" <<'PY'
import sys
from hestia.recovery import RecoveryError, assert_sufficient_disk

data_dir, need, cid = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    free = assert_sufficient_disk(data_dir, need, correlation_id=cid)
except RecoveryError as exc:
    print(f"ERROR: {exc} (correlation_id={cid})", file=sys.stderr)
    sys.exit(1)
print(f"disk preflight ok: free={free} need={need} (correlation_id={cid})")
PY

case "$SRC" in
  *.gz)
    if ! gunzip -c "$SRC" > "$TMP" 2>/dev/null; then
      rm -f "$TMP"
      echo "ERROR: failed to decompress backup (corrupt gzip?) — live database untouched (correlation_id=$CORRELATION_ID)" >&2
      exit 1
    fi
    ;;
  *)
    cp "$SRC" "$TMP"
    ;;
esac

# Truncated / non-SQLite payloads fail here before the live DB is moved aside.
if ! python3 - "$TMP" "$CORRELATION_ID" <<'PY'
import sqlite3
import sys

path, cid = sys.argv[1], sys.argv[2]
try:
    conn = sqlite3.connect(path)
    ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()
except sqlite3.Error as exc:
    print(f"integrity_check: error ({exc})")
    sys.exit(1)
print("integrity_check:", ok)
sys.exit(0 if ok == "ok" else 1)
PY
then
  rm -f "$TMP"
  echo "ERROR: backup failed integrity_check — live database untouched (correlation_id=$CORRELATION_ID)" >&2
  exit 1
fi

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
rm -f "$MARKER"
trap - ERR

python3 - "$DB" "$CORRELATION_ID" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
cid = sys.argv[2]
try:
    version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    tenants = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
except sqlite3.Error:
    version, tenants = "n/a", "n/a"
print(f"restored schema at migration {version}, tenants: {tenants} (correlation_id={cid})")
PY
echo "restored $SRC -> $DB (previous database kept at $OUT_DIR/pre-restore-$STAMP.db) correlation_id=$CORRELATION_ID"
