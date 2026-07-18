"""Guard .env.production.example against drift.

The hosted env template is a deliberately held release candidate. It pins observable
signup and client-payment holds; mock payments remain capable of local settlement, so
this is configuration intent rather than technical route disablement. It also rejects
real-looking secrets and names only settings the app reads. Pin those load-bearing
properties.
"""

import re
from pathlib import Path

TEMPLATE = Path("hestia").resolve().parent / ".env.production.example"
CONFIG = Path("hestia/config.py")


def _parse(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def test_production_template_pins_release_candidate_backends():
    env = _parse(TEMPLATE)
    # Subscription and email seams are configured for private rehearsal. Signup off,
    # test-mode Stripe, and mock invoices are observable holds, not route disablement.
    assert env["HESTIA_SAAS_MODE"] == "true"
    assert env["HESTIA_SIGNUP_ENABLED"] == "false"
    assert env["HESTIA_SUBSCRIPTION_BACKEND"] == "stripe"
    assert env["HESTIA_PAYMENTS_BACKEND"] == "mock"
    assert env["HESTIA_STRIPE_SECRET_KEY"].startswith("sk_test_")
    assert env["HESTIA_EMAIL_BACKEND"] == "smtp"
    assert env["HESTIA_PUBLIC_URL"].startswith("https://")
    assert env["HESTIA_TRIAL_DAYS"] == "14"


def test_production_template_ships_no_real_secrets():
    env = _parse(TEMPLATE)
    # Secrets must stay placeholders — a real key must never live in a committed file.
    for key in ("HESTIA_API_TOKEN", "HESTIA_TENANT_KEY_PEPPER", "HESTIA_SESSION_SECRET",
                "HESTIA_SMTP_PASSWORD"):
        assert "CHANGE_ME" in env[key], f"{key} looks like a real secret"
    assert "CHANGE_ME" in env["HESTIA_STRIPE_SECRET_KEY"]
    assert "CHANGE_ME" in env["HESTIA_STRIPE_WEBHOOK_SECRET"]
    assert not re.match(r"sk_live_[A-Za-z0-9]{20,}", env["HESTIA_STRIPE_SECRET_KEY"])
    xai_key = env["HESTIA_XAI_API_KEY"]
    assert not xai_key or "CHANGE_ME" in xai_key
    assert not re.match(r"xai-[A-Za-z0-9_-]{20,}", xai_key)


def test_production_template_keys_are_all_real_settings():
    """Every HESTIA_* key in the template must be one the app or its scripts actually
    read, so a typo can't masquerade as a configured value."""
    sources = CONFIG.read_text(encoding="utf-8")
    for script in sorted(Path("scripts").glob("*.sh")):
        sources += script.read_text(encoding="utf-8")
    known = set(re.findall(r"HESTIA_[A-Z_]+", sources))
    for key in _parse(TEMPLATE):
        if key.startswith("HESTIA_"):
            assert key in known, f"{key} is read by neither config.py nor scripts/ (typo?)"
