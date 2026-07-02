"""Guard .env.production.example against drift.

The production env template is what a founder copies on go-live day. If it ever
reverts a money/email backend to `mock`, or ships a real-looking secret, or names a
key the app doesn't read, that's a silent launch footgun. Pin the load-bearing bits.
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


def test_production_template_pins_live_backends():
    env = _parse(TEMPLATE)
    # The backends that would silently break a launch if left on mock.
    assert env["HESTIA_SAAS_MODE"] == "true"
    assert env["HESTIA_SUBSCRIPTION_BACKEND"] == "stripe"
    assert env["HESTIA_PAYMENTS_BACKEND"] == "stripe"   # else invoices settle for $0
    assert env["HESTIA_EMAIL_BACKEND"] == "smtp"         # else no verification mail
    assert env["HESTIA_PUBLIC_URL"].startswith("https://")
    assert env["HESTIA_TRIAL_DAYS"] == "14"              # preflight locks 14


def test_production_template_ships_no_real_secrets():
    env = _parse(TEMPLATE)
    # Secrets must stay placeholders — a real key must never live in a committed file.
    for key in ("HESTIA_API_TOKEN", "HESTIA_TENANT_KEY_PEPPER", "HESTIA_SESSION_SECRET",
                "HESTIA_SMTP_PASSWORD"):
        assert "CHANGE_ME" in env[key], f"{key} looks like a real secret"
    assert "CHANGE_ME" in env["HESTIA_STRIPE_SECRET_KEY"]
    assert "CHANGE_ME" in env["HESTIA_STRIPE_WEBHOOK_SECRET"]
    assert not re.match(r"sk_live_[A-Za-z0-9]{20,}", env["HESTIA_STRIPE_SECRET_KEY"])


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
