#!/usr/bin/env bash
# Spine coverage gate.
#
# Measures line coverage of the revenue-spine modules against the tests that
# exercise them, and fails below the threshold set in pyproject
# (``--cov-fail-under``). This is deliberately a separate, opt-in check — it is
# NOT part of ci-smoke — so a fast lint+test loop stays fast and a coverage dip
# surfaces as a signal, not a launch blocker.
#
# Run before merge when touching the spine: pipeline / sales / orders /
# invoices / payments / fulfillment / vision / ai_usage / domains.
set -euo pipefail
cd "$(dirname "$0")/.."

# The curated spine tests: the commerce-spine E2E plus the per-module suites
# that drive each spine module directly. These run fast (~15s) and hit the
# spine hard, which is what a regression gate should do.
SPINE_TESTS=(
  tests/test_commerce_spine.py
  tests/test_pipeline.py
  tests/test_sales.py
  tests/test_orders.py
  tests/test_invoices.py
  tests/test_payments.py
  tests/test_vision.py
  tests/test_custom_domains.py
  tests/test_ai_subsidy.py
  tests/test_ai_usage.py
)

python -m pytest -q -p no:cacheprovider "${SPINE_TESTS[@]}" \
  --cov=hestia.pipeline \
  --cov=hestia.sales \
  --cov=hestia.orders \
  --cov=hestia.invoices \
  --cov=hestia.payments \
  --cov=hestia.fulfillment \
  --cov=hestia.vision \
  --cov=hestia.ai_usage \
  --cov=hestia.domains \
  --cov-report=term-missing \
  --cov-fail-under=70
