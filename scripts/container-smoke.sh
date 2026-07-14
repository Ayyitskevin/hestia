#!/usr/bin/env bash
# Build and boot the production container artifact with disposable state.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v docker >/dev/null 2>&1 \
  || { echo "FAIL: docker is required for the container smoke" >&2; exit 1; }

TOKEN="$$-$RANDOM"
IMAGE="hestia-container-smoke:$TOKEN"
CONTAINER="hestia-container-smoke-$TOKEN"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker image rm -f "$IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== build container =="
docker build --tag "$IMAGE" .

IMAGE_UID="$(docker run --rm --entrypoint id "$IMAGE" -u)"
IMAGE_GID="$(docker run --rm --entrypoint id "$IMAGE" -g)"
if [[ "$IMAGE_UID" == "0" ]]; then
  echo "FAIL: container runs as root" >&2
  exit 1
fi
echo "container user: uid=$IMAGE_UID gid=$IMAGE_GID"

docker run --detach --name "$CONTAINER" \
  --publish 127.0.0.1::8500 \
  --tmpfs "/data:rw,uid=$IMAGE_UID,gid=$IMAGE_GID,mode=0700" \
  --env HESTIA_DATA_DIR=/data \
  --env HESTIA_VISION_BACKEND=mock \
  --env HESTIA_API_TOKEN=container-smoke-admin \
  --env HESTIA_TENANT_KEY_PEPPER=container-smoke-pepper \
  --env HESTIA_SESSION_SECRET=container-smoke-secret \
  --env HESTIA_PUBLIC_URL=http://127.0.0.1 \
  "$IMAGE" >/dev/null

PORT_LINE="$(docker port "$CONTAINER" 8500/tcp)"
PORT="${PORT_LINE##*:}"
BASE_URL="http://127.0.0.1:$PORT"
for _ in $(seq 1 40); do
  curl -sf "$BASE_URL/healthz" >/dev/null 2>&1 && break
  sleep 0.5
done
if ! HEALTH="$(curl -sf "$BASE_URL/healthz")"; then
  docker logs "$CONTAINER" >&2
  echo "FAIL: container healthz did not become ready" >&2
  exit 1
fi
READY="$(curl -sf "$BASE_URL/readyz")"
OG_BYTES="$(curl -sf "$BASE_URL/static/og-cover.png" | wc -c)"
if [[ "$OG_BYTES" -lt 1000 ]]; then
  echo "FAIL: packaged Open Graph image is unexpectedly small ($OG_BYTES bytes)" >&2
  exit 1
fi

echo "healthz: $HEALTH"
echo "readyz: $READY"
echo "container smoke OK: non-root, og-cover.png=$OG_BYTES bytes"
