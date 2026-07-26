#!/usr/bin/env bash
# Run the isolated Chromium gallery-proofing acceptance suite.
set -euo pipefail
cd "$(dirname "$0")/.."

python -m pytest -q browser_tests --browser chromium "$@"
