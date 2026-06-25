#!/usr/bin/env bash
# CI smoke: lint, tests, and a real /healthz boot check.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== ruff =="
ruff check hestia tests

echo "== pytest =="
python -m pytest -q

echo "== healthz boot =="
export HESTIA_DATA_DIR="$(mktemp -d)"
export HESTIA_VISION_BACKEND=mock
PORT=8599
uvicorn hestia.main:app --port "$PORT" >/tmp/hestia-ci.log 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true; rm -rf "$HESTIA_DATA_DIR"' EXIT
for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -sf "http://127.0.0.1:$PORT/healthz" | python -c "import sys,json; d=json.load(sys.stdin); print('healthz:', d); sys.exit(0 if d.get('db')=='ok' else 1)"
echo "== ci-smoke OK =="
