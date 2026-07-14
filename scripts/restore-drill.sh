#!/usr/bin/env bash
# Exercise backup.sh -> copied artifact -> restore.sh entirely in scratch data.
# This is safe for CI and local verification; it never reads HESTIA_DATA_DIR.
set -euo pipefail
cd "$(dirname "$0")/.."

ROOT="$(mktemp -d)"
SOURCE="$ROOT/source"
TARGET="$ROOT/target"
ARTIFACTS="$ROOT/artifacts"
SAFETY="$ROOT/safety"
mkdir -p "$SOURCE" "$TARGET" "$ARTIFACTS" "$SAFETY"
trap 'rm -rf "$ROOT"' EXIT

python - "$SOURCE/hestia.db" <<'PY'
import sys

from hestia.db import connect, init_db
from hestia.tenants import create_tenant

db = sys.argv[1]
init_db(db)
conn = connect(db)
create_tenant(conn, name="Restore Drill Source", shoot_type="wedding")
conn.commit()
conn.close()
PY

HESTIA_DATA_DIR="$SOURCE" HESTIA_BACKUP_DIR="$ARTIFACTS" HESTIA_BACKUP_KEEP=2 \
  bash scripts/backup.sh

shopt -s nullglob
backups=("$ARTIFACTS"/hestia-*.db.gz)
if [[ "${#backups[@]}" -ne 1 ]]; then
  echo "FAIL: expected one backup artifact, found ${#backups[@]}" >&2
  exit 1
fi
COPIED="$ROOT/copied-backup.db.gz"
cp "${backups[0]}" "$COPIED"

python - "$TARGET/hestia.db" <<'PY'
import sys

from hestia.db import connect, init_db
from hestia.tenants import create_tenant

db = sys.argv[1]
init_db(db)
conn = connect(db)
create_tenant(conn, name="Restore Drill Replaced", shoot_type="commercial")
conn.commit()
conn.close()
PY

HESTIA_DATA_DIR="$TARGET" HESTIA_BACKUP_DIR="$SAFETY" \
  bash scripts/restore.sh "$COPIED"

safety_copies=("$SAFETY"/pre-restore-*.db)
if [[ "${#safety_copies[@]}" -ne 1 ]]; then
  echo "FAIL: expected one pre-restore safety copy, found ${#safety_copies[@]}" >&2
  exit 1
fi

python - "$TARGET/hestia.db" "${safety_copies[0]}" <<'PY'
import sqlite3
import sys

restored = sqlite3.connect(sys.argv[1])
safety = sqlite3.connect(sys.argv[2])

assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
assert safety.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

restored_names = {row[0] for row in restored.execute("SELECT name FROM tenants")}
safety_names = {row[0] for row in safety.execute("SELECT name FROM tenants")}
assert restored_names == {"Restore Drill Source"}, restored_names
assert safety_names == {"Restore Drill Replaced"}, safety_names

version = restored.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
assert version is not None
print(f"restore drill OK: integrity=ok, migration={version}, safety_copy=ok")
PY
