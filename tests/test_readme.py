"""README product-positioning guardrails."""

from pathlib import Path


def test_readme_positions_hestia_as_flat_price_hosted_microsaas():
    readme = Path("README.md").read_text(encoding="utf-8")

    required = [
        "Everything you need to run a professional photography studio",
        "fully hosted",
        "$40/month",
        "14-day free trial",
        "One flat paid plan",
        "No tiers",
        "wedding, food & beverage, or real-estate preset",
        "/pricing",
        "/beta",
        "shareable public beta landing page",
        "14-day trial proof plan",
        "/interest",
        "compatibility beta access form",
        "/invite/{token}",
        "private beta invite signup path",
        "first-party signup attribution",
        "interest-to-trial conversion tracking",
        "/admin/launch",
        "founder operating checklist",
        "beta revenue pipeline",
        "weekly launch digest",
        "founder weekly launch digest",
        "beta interest leads",
        "private invite emails",
        "cohort summary",
        "CSV export",
        "cooldown-safe trial nudges",
        "beta conversion timeline",
        "docker compose up --build -d",
        "scripts/hosted-preflight.sh",
        "Hosted Launch Checklist",
        "X Launch Thread Outline",
    ]

    for phrase in required:
        assert phrase in readme

    assert "Studio Pro" not in readme
