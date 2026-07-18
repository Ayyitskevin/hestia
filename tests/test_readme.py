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
        "wedding, portrait, food & beverage, or real-estate preset",
        "/pricing",
        "/beta",
        "shareable public beta landing page",
        "14-day trial proof plan",
        "Open Graph",
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
        "launch operations runbook",
        "Founder demo mode",
        "Mini-session launch tools",
        "Lead intelligence cockpit",
        "Client action room",
        "Gallery sales automation",
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


def test_readme_routes_local_and_hosted_setup_to_the_right_templates():
    readme = Path("README.md").read_text(encoding="utf-8")
    quickstart = readme.split("## Quickstart", 1)[1].split("## The Offer", 1)[0]
    hosted = readme.split("## Hosted Release Candidate (Held)", 1)[1].split(
        "## Trial, Billing, And Account Flow", 1
    )[0]
    local = readme.split("## Local Development", 1)[1].split(
        "## API Example", 1
    )[0]

    assert "cp .env.example .env" in quickstart
    assert "cp .env.example .env" in local
    assert "cp .env.production.example .env" in hosted
    assert "cp .env.example .env" not in hosted
