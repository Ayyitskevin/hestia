#!/usr/bin/env bash
# Build the distributable wheel away from the source tree, install it into an
# isolated environment, and prove the installed artifact can boot with every
# runtime resource Hestia reads from disk.
set -euo pipefail
cd "$(dirname "$0")/.."

ROOT="$PWD"
TMP="$(mktemp -d)"
SRC="$TMP/src"
DIST="$TMP/dist"
SITE="$TMP/site"
PORT="${HESTIA_WHEEL_SMOKE_PORT:-8602}"
PID=""

cleanup() {
  if [[ -n "$PID" ]]; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

mkdir -p "$SRC" "$DIST"
cp pyproject.toml README.md "$SRC/"
cp -R hestia "$SRC/"

echo "== build wheel =="
if ! python -m build --wheel --no-isolation --outdir "$DIST" "$SRC" >"$TMP/build.log" 2>&1; then
  sed -n '1,240p' "$TMP/build.log" >&2
  exit 1
fi
WHEELS=("$DIST"/*.whl)
if [[ ${#WHEELS[@]} -ne 1 || ! -f "${WHEELS[0]}" ]]; then
  echo "FAIL: expected exactly one wheel in $DIST" >&2
  exit 1
fi
echo "wheel: ${WHEELS[0]}"

echo "== install wheel =="
python -m pip install --no-deps --target "$SITE" "${WHEELS[0]}"

EXPECTED_MIGRATIONS="$(find "$ROOT/hestia/migrations" -maxdepth 1 -type f -name '*.sql' | wc -l)"
export EXPECTED_MIGRATIONS SITE
cd "$TMP"
PYTHONPATH="$SITE" python -c '
import os
from pathlib import Path
import hestia

root = Path(hestia.__file__).resolve().parent
site = Path(os.environ["SITE"]).resolve()
assert root.is_relative_to(site), f"imported source tree instead of wheel: {root}"
required = (
    root / "migrations" / "0001_baseline.sql",
    root / "templates" / "base.html",
    root / "static" / "hestia.css",
    root / "static" / "og-cover.png",
)
missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
assert not missing, f"wheel missing runtime files: {missing}"
actual = len(tuple((root / "migrations").glob("*.sql")))
expected = int(os.environ["EXPECTED_MIGRATIONS"])
assert actual == expected, f"wheel has {actual}/{expected} migrations"
print(f"installed package: {root} ({actual} migrations)")
'

echo "== boot installed wheel =="
export HESTIA_DATA_DIR="$TMP/data"
export HESTIA_VISION_BACKEND=mock
export HESTIA_API_TOKEN=wheel-smoke-admin
export HESTIA_TENANT_KEY_PEPPER=wheel-smoke-pepper
export HESTIA_SESSION_SECRET=wheel-smoke-secret
export HESTIA_PUBLIC_URL="http://127.0.0.1:$PORT"
PYTHONPATH="$SITE" python -m uvicorn hestia.main:app --port "$PORT" >"$TMP/server.log" 2>&1 &
PID=$!

for _ in $(seq 1 40); do
  if curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

if ! curl -sf "http://127.0.0.1:$PORT/healthz" >"$TMP/healthz.json"; then
  echo "FAIL: installed wheel did not become healthy" >&2
  sed -n '1,160p' "$TMP/server.log" >&2
  exit 1
fi

python -c '
import json
import os
import struct
from pathlib import Path
from urllib.request import urlopen

health = json.loads(Path("healthz.json").read_text(encoding="utf-8"))
assert health.get("db") == "ok", health
port = os.environ.get("HESTIA_WHEEL_SMOKE_PORT", "8602")
with urlopen(f"http://127.0.0.1:{port}/static/og-cover.png", timeout=5) as response:
    data = response.read()
    assert response.headers.get_content_type() == "image/png"
assert data[:8] == b"\x89PNG\r\n\x1a\n"
assert struct.unpack(">II", data[16:24]) == (1200, 630)
print(f"healthz: {health}; og-cover.png: {len(data)} bytes")
'

echo "== wheel-smoke OK =="
