#!/usr/bin/env bash
# Start Hestia. Reads .env (via python-dotenv in hestia.config).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "No .env found — copy .env.example to .env and set your secrets." >&2
  echo "  cp .env.example .env && chmod 600 .env" >&2
fi

exec uvicorn hestia.main:app --host "${HESTIA_HOST:-127.0.0.1}" --port "${HESTIA_PORT:-8500}" "$@"
