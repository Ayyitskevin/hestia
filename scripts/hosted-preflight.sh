#!/usr/bin/env bash
# Hosted SaaS preflight: validate env, secrets, billing, domain, volumes, and optional runtime health.
set -euo pipefail
cd "$(dirname "$0")/.."

python -m hestia.preflight "$@"
