#!/usr/bin/env bash
# Restore the Hestia database from a backup produced by scripts/backup.sh.
#
#   HESTIA_DATA_DIR=/path/to/scratch bash scripts/restore.sh <backup.db[.gz]> \
#       [--allow-production] [--force-live-wal] [--manifest PATH] [--require-manifest]
#
# Safety rails, in order:
#   0. refuses production-like data dirs (incl. symlink→prod, …/hestia/data) unless
#      --allow-production or HESTIA_ALLOW_PRODUCTION_RESTORE=1
#   1. refuses while the app looks live (hestia.db-wal present) unless the loud,
#      separately named --force-live-wal override (plain --force is refused)
#   2. refuses missing / unreadable backup files before any write
#   3. when a generation manifest is present (or --require-manifest), verifies
#      DB (+ media) checksums for one generation before any live rename
#   4. preflight: enough free disk (uses gzip uncompressed size when available)
#   5. unpacks to a same-FS temp; integrity + Hestia schema gate BEFORE live touch
#   6. writes .restore-in-progress only after the gate passes (no false "interrupted")
#   7. pre-restore safety copy always on the same filesystem as hestia.db
#   8. atomic mv of temp → live DB
# Full drill: docs/backup-restore.md · automated: scripts/restore-drill.sh
set -euo pipefail

DATA_DIR="${HESTIA_DATA_DIR:-./data}"
DB="$DATA_DIR/hestia.db"
# Operator backup archive location (may be off-box). Safety copies stay with the DB.
OUT_DIR="${HESTIA_BACKUP_DIR:-$DATA_DIR/backups}"
SAFETY_DIR="$DATA_DIR/backups"
MEDIA_DIR="${HESTIA_MEDIA_DIR:-$DATA_DIR/media}"
CORRELATION_ID="${HESTIA_RECOVERY_CORRELATION_ID:-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')}"
export HESTIA_RECOVERY_CORRELATION_ID="$CORRELATION_ID"

SRC=""
FORCE_LIVE_WAL=""
ALLOW_PRODUCTION=""
MANIFEST=""
REQUIRE_MANIFEST="${HESTIA_REQUIRE_BACKUP_MANIFEST:-}"
args=("$@")
i=0
while [ "$i" -lt "${#args[@]}" ]; do
  arg="${args[$i]}"
  case "$arg" in
    --force)
      echo "ERROR: --force is no longer accepted for live-WAL override (correlation_id=$CORRELATION_ID)." >&2
      echo "       Stop the app, or pass the loud override --force-live-wal (app may still be live)." >&2
      exit 2
      ;;
    --force-live-wal) FORCE_LIVE_WAL="--force-live-wal" ;;
    --allow-production) ALLOW_PRODUCTION="--allow-production" ;;
    --require-manifest) REQUIRE_MANIFEST=1 ;;
    --manifest)
      i=$((i + 1))
      if [ "$i" -ge "${#args[@]}" ]; then
        echo "ERROR: --manifest requires a path" >&2
        exit 2
      fi
      MANIFEST="${args[$i]}"
      ;;
    --manifest=*)
      MANIFEST="${arg#--manifest=}"
      ;;
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
  i=$((i + 1))
done

if [ -z "${MANIFEST}" ] && [ -n "${HESTIA_BACKUP_MANIFEST:-}" ]; then
  MANIFEST="$HESTIA_BACKUP_MANIFEST"
fi

[ -n "$SRC" ] || { echo "usage: restore.sh <backup.db[.gz]> [--allow-production] [--force-live-wal] [--manifest PATH] [--require-manifest]" >&2; exit 2; }
[ -f "$SRC" ] || { echo "ERROR: no backup at $SRC (correlation_id=$CORRELATION_ID)" >&2; exit 1; }
[ -r "$SRC" ] || { echo "ERROR: backup not readable: $SRC (correlation_id=$CORRELATION_ID)" >&2; exit 1; }

# Production-path refusal via the shipped recovery helper (same logic as pytest).
# Resolves symlinks so a scratch-looking path that targets live data is refused.
python3 - "$DATA_DIR" "$ALLOW_PRODUCTION" "$CORRELATION_ID" <<'PY'
import sys
from hestia.recovery import RecoveryError, assert_safe_restore_target, structured_diag

data_dir, allow_flag, cid = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    target = assert_safe_restore_target(
        data_dir,
        allow_production=(allow_flag == "--allow-production"),
        correlation_id=cid,
    )
except RecoveryError as exc:
    print(f"ERROR: {exc} (correlation_id={cid})", file=sys.stderr)
    sys.exit(2)
# Log only the resolved operator path — never client tokens or media names.
structured_diag(
    "recovery.restore.begin",
    correlation_id=cid,
    data_dir=str(target),
)
print(f"restore target accepted: {target} (correlation_id={cid})")
PY

# Live-writer / WAL quiescence — plain --force is rejected above.
python3 - "$DATA_DIR" "$FORCE_LIVE_WAL" "$CORRELATION_ID" <<'PY'
import sys
from hestia.recovery import RecoveryError, assert_writer_quiescent

data_dir, force_flag, cid = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    assert_writer_quiescent(
        data_dir,
        force_live_wal=(force_flag == "--force-live-wal"),
        correlation_id=cid,
    )
except RecoveryError as exc:
    print(f"ERROR: {exc} (correlation_id={cid})", file=sys.stderr)
    sys.exit(1)
print(f"writer quiescence ok (correlation_id={cid})")
PY

# Generation manifest: auto-discover sidecar, or require when asked.
if [ -z "$MANIFEST" ] && [ -f "${SRC}.manifest.json" ]; then
  MANIFEST="${SRC}.manifest.json"
fi
if [ -n "$REQUIRE_MANIFEST" ] && [ -z "$MANIFEST" ]; then
  echo "ERROR: --require-manifest set but no manifest next to $SRC (correlation_id=$CORRELATION_ID)" >&2
  exit 1
fi
if [ -n "$MANIFEST" ]; then
  # If this generation listed media, media must be proven — never DB-only-gate and swap.
  MEDIA_REQUIRED="$(python3 - "$MANIFEST" <<'PY'
import json, sys
from pathlib import Path
m = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
media = m.get("media") or {}
files = media.get("files") or {}
n = int(media.get("file_count") or 0) or len(files)
print("1" if n > 0 else "0")
PY
)"
  MEDIA_ARGS=()
  if [ "$MEDIA_REQUIRED" = "1" ]; then
    if [ ! -d "$MEDIA_DIR" ]; then
      echo "ERROR: generation manifest lists media but media dir missing or not a directory: $MEDIA_DIR — live database untouched (correlation_id=$CORRELATION_ID)" >&2
      exit 1
    fi
    MEDIA_ARGS=(--media-dir "$MEDIA_DIR" --require-media)
  elif [ -d "$MEDIA_DIR" ]; then
    MEDIA_ARGS=(--media-dir "$MEDIA_DIR")
  fi
  if ! python3 -m hestia.recovery manifest-verify "$SRC" "$MANIFEST" \
      "${MEDIA_ARGS[@]}" \
      --correlation-id "$CORRELATION_ID"; then
    echo "ERROR: generation manifest verification refused — live database untouched (correlation_id=$CORRELATION_ID)" >&2
    exit 1
  fi
  echo "generation manifest OK (correlation_id=$CORRELATION_ID)"
fi

mkdir -p "$OUT_DIR" "$SAFETY_DIR" "$DATA_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
TMP="$DATA_DIR/.restore-$STAMP.db"    # same filesystem as $DB so the final mv is atomic
MARKER="$DATA_DIR/.restore-in-progress"

cleanup_failed() {
  rm -f "$TMP"
  # Marker is only created after the schema gate; leave it if present so operators
  # can see a half-applied attempt. Early failures never write the marker.
}
trap cleanup_failed ERR

# Disk preflight: prefer gzip's reported uncompressed size (3× compressed can under-estimate).
NEED="$(python3 - "$SRC" "$DB" <<'PY'
import struct
import sys
from pathlib import Path

src = Path(sys.argv[1])
db = Path(sys.argv[2])
raw = src.stat().st_size
need = raw * 3 + 1_048_576
if src.suffix == ".gz" or str(src).endswith(".db.gz"):
    # gzip footer holds ISIZE (uncompressed mod 2^32) — good enough for preflight.
    try:
        with open(src, "rb") as fh:
            fh.seek(-4, 2)
            isize = struct.unpack("<I", fh.read(4))[0]
        if isize > 0:
            need = max(need, isize + raw + 1_048_576)
    except OSError:
        pass
if db.is_file():
    need += db.stat().st_size
print(need)
PY
)"
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

# Integrity + Hestia schema gate BEFORE any live rename. Empty files, bare SQLite
# files that pass PRAGMA integrity_check, and unsupported schema versions all refuse here.
if ! python3 - "$TMP" "$CORRELATION_ID" <<'PY'
import sys
from hestia.recovery import RecoveryError, assert_restorable_backup

path, cid = sys.argv[1], sys.argv[2]
try:
    version = assert_restorable_backup(path, correlation_id=cid)
except RecoveryError as exc:
    print(f"integrity_check / schema gate: refused ({exc})")
    sys.exit(1)
print(f"integrity_check: ok schema={version}")
sys.exit(0)
PY
then
  rm -f "$TMP"
  echo "ERROR: backup failed integrity/schema gate — live database untouched (correlation_id=$CORRELATION_ID)" >&2
  exit 1
fi

# Gate passed — mark in-progress only now (basename of SRC only: no full path leak).
printf '%s\n' \
  "correlation_id=$CORRELATION_ID" \
  "stamp=$STAMP" \
  "src_basename=$(basename -- "$SRC")" \
  "started=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "phase=pre-swap" > "$MARKER"

if [ -f "$DB" ]; then
  # Fold any un-checkpointed WAL into the main file so the safety copy holds the
  # newest writes, then move it aside on the SAME filesystem as the live DB.
  python3 -c "import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
conn.close()" "$DB"
  # Refuse a cross-device HESTIA_BACKUP_DIR for the safety copy — use SAFETY_DIR always.
  python3 - "$DB" "$SAFETY_DIR" "$CORRELATION_ID" <<'PY'
import sys
from pathlib import Path
from hestia.recovery import same_filesystem

db, safety, cid = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
if not same_filesystem(db, safety):
    print(
        f"ERROR: safety dir {safety} is on a different filesystem than {db}; "
        f"refusing non-atomic pre-restore move (correlation_id={cid})",
        file=sys.stderr,
    )
    sys.exit(1)
print(f"safety copy filesystem ok (correlation_id={cid})")
PY
  mv "$DB" "$SAFETY_DIR/pre-restore-$STAMP.db"
  rm -f "$DB-wal" "$DB-shm"
  # Update marker so operators know the live file was moved aside.
  printf '%s\n' \
    "correlation_id=$CORRELATION_ID" \
    "stamp=$STAMP" \
    "src_basename=$(basename -- "$SRC")" \
    "started=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "phase=live-swapped" \
    "pre_restore=$SAFETY_DIR/pre-restore-$STAMP.db" > "$MARKER"
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
echo "restored $SRC -> $DB (previous database kept at $SAFETY_DIR/pre-restore-$STAMP.db) correlation_id=$CORRELATION_ID"
