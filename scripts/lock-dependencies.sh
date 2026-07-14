#!/usr/bin/env bash
# Compile hash-locked Python 3.12 dependency sets while keeping pip as the
# installer used by contributors, CI, and Docker.
set -euo pipefail
cd "$(dirname "$0")/.."

EXPECTED_UV_VERSION="0.11.28"
MODE="${1:-}"
case "$MODE" in
  ""|--check|--upgrade) ;;
  *) echo "usage: $0 [--check|--upgrade]" >&2; exit 2 ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  echo "FAIL: uv $EXPECTED_UV_VERSION is required to compile dependency locks" >&2
  exit 1
fi
ACTUAL_UV_VERSION="$(uv --version | awk '{print $2}')"
if [[ "$ACTUAL_UV_VERSION" != "$EXPECTED_UV_VERSION" ]]; then
  echo "FAIL: uv $EXPECTED_UV_VERSION required; found $ACTUAL_UV_VERSION" >&2
  exit 1
fi

UPGRADE=()
if [[ "$MODE" == "--upgrade" ]]; then
  UPGRADE=(--upgrade)
fi

CHECK_DIR=""
if [[ "$MODE" == "--check" ]]; then
  for lock in requirements/runtime.lock requirements/dev.lock; do
    if [[ ! -f "$lock" ]]; then
      echo "FAIL: missing committed dependency lock: $lock" >&2
      exit 1
    fi
  done
  CHECK_DIR="$(mktemp -d)"
  cp requirements/runtime.lock requirements/dev.lock "$CHECK_DIR/"
fi

cleanup() {
  if [[ -n "$CHECK_DIR" ]]; then
    rm -rf "$CHECK_DIR"
  fi
}
trap cleanup EXIT

COMMON=(
  pyproject.toml
  requirements/build.in
  --python-version 3.12
  --universal
  --generate-hashes
  --quiet
  --custom-compile-command "bash scripts/lock-dependencies.sh --upgrade"
)

echo "== compile runtime lock =="
uv pip compile "${COMMON[@]}" --extra s3 "${UPGRADE[@]}" \
  --output-file requirements/runtime.lock

echo "== compile development lock =="
uv pip compile "${COMMON[@]}" --all-extras "${UPGRADE[@]}" \
  --output-file requirements/dev.lock

if [[ "$MODE" == "--check" ]]; then
  for lock in runtime.lock dev.lock; do
    if ! cmp -s "$CHECK_DIR/$lock" "requirements/$lock"; then
      echo "FAIL: requirements/$lock is stale; run bash scripts/lock-dependencies.sh --upgrade" >&2
      exit 1
    fi
  done
  echo "dependency locks match pyproject.toml"
fi
