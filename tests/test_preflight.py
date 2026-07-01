"""Hosted deployment preflight checks."""

import dataclasses
from pathlib import Path

from hestia.preflight import PreflightCheck, run_preflight


def _hosted_settings(settings, tmp_path: Path, **overrides):
    base = dataclasses.replace(
        settings,
        saas_mode=True,
        signup_enabled=True,
        public_url="https://app.hestia.test",
        hosted_domain="hestia.test",
        api_token="a" * 32,
        tenant_key_pepper="b" * 32,
        session_secret="c" * 32,
        data_dir=tmp_path / "data",
        media_dir=tmp_path / "data" / "media",
        subscription_backend="stripe",
        payments_backend="stripe",
        stripe_secret_key="sk_live_123",
        stripe_webhook_secret="whsec_123",
        email_backend="smtp",
        smtp_host="smtp.example.com",
        smtp_from="hello@hestia.test",
        trial_days=14,
    )
    return dataclasses.replace(base, **overrides)


def _by_name(checks: list[PreflightCheck]) -> dict[str, PreflightCheck]:
    return {check.name: check for check in checks}


def test_hosted_preflight_accepts_production_shaped_config(settings, tmp_path):
    checks = run_preflight(_hosted_settings(settings, tmp_path), root=Path("."))
    by_name = _by_name(checks)

    assert not [check for check in checks if check.level == "fail"]
    assert by_name["flat price"].level == "pass"
    assert by_name["trial length"].level == "pass"
    assert by_name["runtime probe"].level == "warn"


def test_hosted_preflight_fails_on_launch_blockers(settings, tmp_path):
    bad = _hosted_settings(
        settings,
        tmp_path,
        signup_enabled=False,
        public_url="http://127.0.0.1:8500",
        hosted_domain="",
        api_token="CHANGE_ME_ADMIN",
        subscription_backend="mock",
        stripe_secret_key="",
        stripe_webhook_secret="",
        email_backend="mock",
        smtp_host="",
    )

    checks = _by_name(run_preflight(bad, root=Path(".")))

    assert checks["self-serve signup"].level == "fail"
    assert checks["public url"].level == "fail"
    assert checks["hosted domain"].level == "fail"
    assert checks["HESTIA_API_TOKEN"].level == "fail"
    assert checks["subscription backend"].level == "fail"
    assert checks["stripe secrets"].level == "fail"
    assert checks["email backend"].level == "fail"


def test_hosted_preflight_probes_health_and_readiness(settings, tmp_path):
    seen = []

    def fetcher(url: str, timeout: float):
        seen.append((url, timeout))
        if url.endswith("/healthz"):
            return 200, {"status": "ok", "db": "ok"}
        return 200, {"ready": True}

    checks = run_preflight(
        _hosted_settings(settings, tmp_path),
        root=Path("."),
        health_url="https://app.hestia.test",
        timeout=1.5,
        fetcher=fetcher,
    )
    by_name = _by_name(checks)

    assert by_name["runtime /healthz"].level == "pass"
    assert by_name["runtime /readyz"].level == "pass"
    assert seen == [
        ("https://app.hestia.test/healthz", 1.5),
        ("https://app.hestia.test/readyz", 1.5),
    ]
