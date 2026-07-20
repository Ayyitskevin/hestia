#!/usr/bin/env bash
# Exercise backup → media copy → restore → verify entirely in scratch data.
# Safe for CI and local verification; never reads or writes a production HESTIA_DATA_DIR.
#
# Success prints structured recovery metrics (RTO/RPO fields) and exits 0.
# Optional: HESTIA_DRILL_REPORT=/path/to/report.json to keep the verification JSON.
set -euo pipefail
cd "$(dirname "$0")/.."

# Clear any ambient production-restore override so the drill proves the default refuse path.
unset HESTIA_ALLOW_PRODUCTION_RESTORE || true

ROOT="$(mktemp -d "${TMPDIR:-/tmp}/hestia-restore-drill.XXXXXX")"
SOURCE="$ROOT/source"
TARGET="$ROOT/target"
ARTIFACTS="$ROOT/artifacts"
SAFETY="$ROOT/safety"
MEDIA_SRC="$SOURCE/media"
MEDIA_DST="$TARGET/media"
CORRELATION_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
export HESTIA_RECOVERY_CORRELATION_ID="$CORRELATION_ID"
REPORT="${HESTIA_DRILL_REPORT:-$ROOT/verify-report.json}"
mkdir -p "$SOURCE" "$TARGET" "$ARTIFACTS" "$SAFETY" "$MEDIA_SRC" "$MEDIA_DST"
# Keep ROOT on failure when HESTIA_DRILL_KEEP=1 so operators can inspect; else always clean.
cleanup() {
  if [ "${HESTIA_DRILL_KEEP:-0}" = "1" ]; then
    echo "drill scratch kept at $ROOT (HESTIA_DRILL_KEEP=1)" >&2
  else
    rm -rf "$ROOT"
  fi
}
trap cleanup EXIT

DRILL_START="$(python3 -c 'import time; print(time.monotonic())')"

echo "→ seed source DB + media (correlation_id=$CORRELATION_ID)"
python3 - "$SOURCE/hestia.db" "$MEDIA_SRC" <<'PY'
import io
import sys
from pathlib import Path

from PIL import Image

from hestia.db import connect, init_db
from hestia.galleries import add_image, create_gallery, publish_gallery
from hestia.storage import LocalStorage
from hestia.tenants import create_tenant

db, media = sys.argv[1], Path(sys.argv[2])
media.mkdir(parents=True, exist_ok=True)
init_db(db)
conn = connect(db)
tenant = create_tenant(conn, name="Restore Drill Source", shoot_type="wedding")
g = create_gallery(conn, tenant_id=tenant["id"], title="Drill Gallery", client_name="Drill Client")
storage = LocalStorage(media)
buf = io.BytesIO()
Image.new("RGB", (32, 24), color=(180, 90, 40)).save(buf, format="JPEG")
buf.seek(0)
img = add_image(
    conn, storage,
    tenant_id=tenant["id"], gallery_id=g["id"],
    filename="drill.jpg", fileobj=buf, content_type="image/jpeg",
)
assert img is not None, "failed to seed drill image"
assert publish_gallery(conn, tenant["id"], g["id"]) is True
conn.commit()
conn.close()
print(f"seeded tenant={tenant['id']} gallery={g['id']} image={img['id']} key={img['storage_key']}")
PY

echo "→ backup source (+ generation manifest)"
HESTIA_DATA_DIR="$SOURCE" HESTIA_BACKUP_DIR="$ARTIFACTS" HESTIA_MEDIA_DIR="$MEDIA_SRC" \
  HESTIA_BACKUP_KEEP=2 bash scripts/backup.sh

shopt -s nullglob
backups=("$ARTIFACTS"/hestia-*.db.gz)
if [[ "${#backups[@]}" -ne 1 ]]; then
  echo "FAIL: expected one backup artifact, found ${#backups[@]}" >&2
  exit 1
fi
COPIED="$ROOT/copied-backup.db.gz"
COPIED_MANIFEST="$ROOT/copied-backup.db.gz.manifest.json"
cp "${backups[0]}" "$COPIED"
if [ ! -f "${backups[0]}.manifest.json" ]; then
  echo "FAIL: backup did not write generation manifest" >&2
  exit 1
fi
cp "${backups[0]}.manifest.json" "$COPIED_MANIFEST"
echo "generation manifest present: $(python3 -c "import json;print(json.load(open('$COPIED_MANIFEST'))['generation_id'])")"

# Off-site media half of the story: copy media into the restore target as a
# stand-in for rclone restore of the media tree (no real remote required).
echo "→ sync media into target (local stand-in for off-site media restore)"
cp -a "$MEDIA_SRC/." "$MEDIA_DST/"
# Capture source media checksums so post-restore verify proves content identity, not only presence.
CHECKSUMS="$ROOT/media-checksums.json"
python3 - "$MEDIA_SRC" "$CHECKSUMS" <<'PY'
import json
import sys
from hestia.recovery import media_checksum_map

src, out = sys.argv[1], sys.argv[2]
open(out, "w", encoding="utf-8").write(json.dumps(media_checksum_map(src), indent=2, sort_keys=True) + "\n")
print(f"media checksum inventory → {out} ({len(json.loads(open(out).read()))} files)")
PY

echo "→ seed disposable target DB that restore will replace"
python3 - "$TARGET/hestia.db" <<'PY'
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

echo "→ prove production-path refusal (default ./data without override)"
if HESTIA_DATA_DIR="./data" bash scripts/restore.sh "$COPIED" 2>"$ROOT/refuse.err"; then
  echo "FAIL: restore into ./data should have been refused" >&2
  cat "$ROOT/refuse.err" >&2
  exit 1
fi
grep -q "production\|refusing restore" "$ROOT/refuse.err" || {
  echo "FAIL: expected production refusal message" >&2
  cat "$ROOT/refuse.err" >&2
  exit 1
}
echo "production refusal OK"

echo "→ restore into scratch target (generation-gated)"
# HESTIA_BACKUP_DIR may point off-box for archives; pre-restore safety always lands
# under $HESTIA_DATA_DIR/backups (same filesystem as hestia.db).
HESTIA_DATA_DIR="$TARGET" HESTIA_BACKUP_DIR="$SAFETY" HESTIA_MEDIA_DIR="$MEDIA_DST" \
  bash scripts/restore.sh "$COPIED" --manifest "$COPIED_MANIFEST" --require-manifest

safety_copies=("$TARGET"/backups/pre-restore-*.db)
if [[ "${#safety_copies[@]}" -ne 1 ]]; then
  echo "FAIL: expected one pre-restore safety copy under target/backups, found ${#safety_copies[@]}" >&2
  exit 1
fi

echo "→ post-restore verification (integrity + media checksums + ownership + synthetic timings)"
python3 -m hestia.recovery verify "$TARGET/hestia.db" \
  --media-dir "$MEDIA_DST" \
  --backup "$COPIED" \
  --require-media \
  --expected-checksums "$CHECKSUMS" \
  --measurement-kind synthetic_scratch_drill \
  --json-out "$REPORT" \
  --correlation-id "$CORRELATION_ID"

python3 - "$TARGET/hestia.db" "${safety_copies[0]}" "$REPORT" "$DRILL_START" "$CORRELATION_ID" <<'PY'
import json
import sqlite3
import sys
import time

restored = sqlite3.connect(sys.argv[1])
safety = sqlite3.connect(sys.argv[2])
report = json.loads(open(sys.argv[3], encoding="utf-8").read())
drill_start = float(sys.argv[4])
cid = sys.argv[5]

assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
assert safety.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

restored_names = {row[0] for row in restored.execute("SELECT name FROM tenants")}
safety_names = {row[0] for row in safety.execute("SELECT name FROM tenants")}
assert restored_names == {"Restore Drill Source"}, restored_names
assert safety_names == {"Restore Drill Replaced"}, safety_names

images = restored.execute("SELECT COUNT(*) FROM images").fetchone()[0]
assert images >= 1, images
gals = restored.execute(
    "SELECT COUNT(*) FROM galleries WHERE status='published'"
).fetchone()[0]
assert gals >= 1, gals

assert report["ok"] is True, report.get("failures")
assert report["integrity_check"] == "ok"
assert report["image_count"] >= 1
assert report["consistency"]["ok"] is True
assert report["consistency"]["missing_blobs"] == []
assert report["representative_gallery"] is not None
assert report["representative_gallery"]["first_blob_present"] is True
# Privacy: report must not carry client-facing secrets.
rep = report["representative_gallery"]
for banned in ("access_token", "email", "client_name", "token", "password"):
    assert banned not in rep, banned
assert report["correlation_id"] == cid
assert report["rpo_seconds"] is not None
assert report["measurement_kind"] == "synthetic_scratch_drill"
assert "not real-incident" in report["timing_disclaimer"].lower() or "not" in report["timing_disclaimer"].lower()
synthetic_elapsed_ms = int((time.monotonic() - drill_start) * 1000)
print(
    f"restore drill OK: integrity=ok, migration={report['schema_version']}, "
    f"tenants={report['tenant_count']}, galleries={report['gallery_count']}, "
    f"images={report['image_count']}, media_ok=1, safety_copy=ok, "
    f"generation_manifest=ok, "
    f"synthetic_elapsed_ms={synthetic_elapsed_ms}, verify_elapsed_ms={report['elapsed_ms']}, "
    f"artifact_age_s={report['rpo_seconds']}, measurement_kind={report['measurement_kind']}, "
    f"correlation_id={cid} "
    f"(SYNTHETIC timings — not production RTO/RPO)"
)
PY

# If the operator asked to keep a report outside the scratch tree, copy it before EXIT cleanup.
if [ -n "${HESTIA_DRILL_REPORT:-}" ] && [ -f "$REPORT" ]; then
  echo "verification report → $REPORT"
fi
