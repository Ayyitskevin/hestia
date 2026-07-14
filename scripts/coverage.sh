#!/usr/bin/env bash
# Aggregate revenue-spine line-coverage gate.
#
# Measures the configured revenue-spine modules against their direct behavior
# suites and fails below ``tool.coverage.report.fail_under``. This guards broad
# coverage erosion; it does not prove that money invariants are correct or
# replace behavior tests and human review. It stays separate from ci-smoke so
# the local lint+test loop remains fast, while hosted CI runs it as a blocking,
# clearly named step on every push and PR.
#
# Run before merge when touching the configured spine.
set -euo pipefail
cd "$(dirname "$0")/.."

# The curated spine tests: the commerce-spine E2E plus the per-module suites
# that drive each configured module directly. This stays materially cheaper
# than instrumenting the full suite while keeping the measured scope explicit.
SPINE_TESTS=(
  tests/test_commerce_spine.py
  tests/test_pipeline.py
  tests/test_sales.py
  tests/test_orders.py
  tests/test_invoices.py
  tests/test_invoice_discount_lines.py
  tests/test_invoice_duplicate.py
  tests/test_invoice_filter.py
  tests/test_invoice_line_items.py
  tests/test_invoice_note.py
  tests/test_invoice_receipt.py
  tests/test_payment_plans.py
  tests/test_payment_plan_tax.py
  tests/test_payments.py
  tests/test_webhooks.py
  tests/test_subscriptions.py
  tests/test_subscription_dunning.py
  tests/test_fulfillment_lab.py
  tests/test_vision.py
  tests/test_vision_deepening.py
  tests/test_vision_prompt.py
  tests/test_xai_transport.py
  tests/test_custom_domains.py
  tests/test_ai_subsidy.py
  tests/test_ai_usage.py
)

python -m pytest -q -p no:cacheprovider "${SPINE_TESTS[@]}" \
  --cov \
  --cov-report=term-missing
