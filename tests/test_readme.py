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
        "first-party signup attribution",
        "/admin/launch",
        "docker compose up --build -d",
        "scripts/hosted-preflight.sh",
        "Hosted Launch Checklist",
        "X Launch Thread Outline",
    ]

    for phrase in required:
        assert phrase in readme

    assert "Studio Pro" not in readme
