#!/usr/bin/env bash
# Dogfood Hestia end-to-end: boot the app, drive the magic moment, assert an offer.
# One app — no fleet required. Uses the mock vision backend by default.
set -euo pipefail
cd "$(dirname "$0")/.."

export HESTIA_API_TOKEN="${HESTIA_API_TOKEN:-dogfood-admin}"
export HESTIA_TENANT_KEY_PEPPER="${HESTIA_TENANT_KEY_PEPPER:-dogfood-pepper}"
export HESTIA_SESSION_SECRET="${HESTIA_SESSION_SECRET:-dogfood-secret}"
export HESTIA_VISION_BACKEND="${HESTIA_VISION_BACKEND:-mock}"
export HESTIA_DATA_DIR="$(mktemp -d)"
PORT="${HESTIA_DOGFOOD_PORT:-8590}"
export HESTIA_PUBLIC_URL="http://127.0.0.1:$PORT"

echo "→ starting hestia on :$PORT (vision=$HESTIA_VISION_BACKEND, data=$HESTIA_DATA_DIR)"
uvicorn hestia.main:app --port "$PORT" >/tmp/hestia-dogfood.log 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true; rm -rf "$HESTIA_DATA_DIR"' EXIT

for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 0.5
done

python scripts/dogfood_hestia.py "http://127.0.0.1:$PORT"
echo "→ dogfood complete"
