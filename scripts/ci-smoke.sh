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
export HESTIA_API_TOKEN="ci-admin"
export HESTIA_TENANT_KEY_PEPPER="ci-pepper"
export HESTIA_SESSION_SECRET="ci-secret"
PORT=8599
export HESTIA_PUBLIC_URL="http://127.0.0.1:$PORT"
uvicorn hestia.main:app --port "$PORT" >/tmp/hestia-ci.log 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true; rm -rf "$HESTIA_DATA_DIR"' EXIT
for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -sf "http://127.0.0.1:$PORT/healthz" | python -c "import sys,json; d=json.load(sys.stdin); print('healthz:', d); sys.exit(0 if d.get('db')=='ok' else 1)"
echo "== privacy invariants =="
# 1) robots.txt must disallow every client-token prefix and allow everything else.
ROBOTS="$(curl -sf "http://127.0.0.1:$PORT/robots.txt")"
PRIVATE_PREFIXES="$(python -c 'from hestia.private_surfaces import PRIVATE_SURFACE_PREFIXES; print(*PRIVATE_SURFACE_PREFIXES)')"
for prefix in $PRIVATE_PREFIXES; do
  echo "$ROBOTS" | grep -q "^Disallow: $prefix\$" \
    || { echo "FAIL: robots.txt missing 'Disallow: $prefix'" >&2; exit 1; }
done
echo "$ROBOTS" | grep -q "^Allow: /\$" \
  || { echo "FAIL: robots.txt missing 'Allow: /'" >&2; exit 1; }

# 2) Every standalone client template must carry the noindex meta unless it is
#    deliberately public (marketing/commerce). Inverted check on purpose: a NEW
#    token page added without noindex fails CI the day it lands.
PUBLIC_OK='^hestia/templates/(base\.html|offer_missing\.html|mini_sessions/public\.html|studio/.*)$'
for tpl in $(grep -rl '<!DOCTYPE html' hestia/templates); do
  echo "$tpl" | grep -Eq "$PUBLIC_OK" && continue
  grep -q 'name="robots" content="noindex"' "$tpl" \
    || { echo "FAIL: private template missing noindex meta: $tpl" >&2; exit 1; }
done

# 3) The marketing landing page must STAY indexable.
if curl -sf "http://127.0.0.1:$PORT/" | grep -q 'name="robots" content="noindex"'; then
  echo "FAIL: landing page is noindexed — marketing must stay indexable" >&2; exit 1
fi
echo "privacy invariants OK"

echo "== dogfood (magic moment) =="
python scripts/dogfood_hestia.py "http://127.0.0.1:$PORT"
echo "dogfood OK"

echo "== ci-smoke OK =="
