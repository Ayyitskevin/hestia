"""Hosted deployment preflight checks.

This is an operator-facing gate for the $40/month hosted SaaS mode. It reads the
same Settings object the app boots with, then fails loudly on configuration that
would block a real studio from signing up, verifying email, starting a trial, or
using the hosted domain.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from .config import Settings
from .domains import normalize_custom_domain, validate_custom_domain

Level = Literal["pass", "warn", "fail"]
FetchJson = Callable[[str, float], tuple[int, dict]]

LOCAL_HOSTS = {"", "127.0.0.1", "::1", "0.0.0.0", "localhost", "testserver"}


@dataclass(frozen=True)
class PreflightCheck:
    level: Level
    name: str
    detail: str


def _check(level: Level, name: str, detail: str) -> PreflightCheck:
    return PreflightCheck(level=level, name=name, detail=detail)


def _secret_ok(value: str) -> bool:
    return bool(value) and not value.startswith("CHANGE_ME") and len(value) >= 24


def _url_host(value: str) -> str:
    return (urlparse(value).hostname or "").lower()


def _is_https_public_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() not in LOCAL_HOSTS


def _can_write_dir(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(prefix=".hestia-preflight-", dir=path, delete=True) as fh:
            fh.write(b"ok")
            fh.flush()
        return True, f"{path} is writable"
    except Exception as exc:  # noqa: BLE001
        return False, f"{path} is not writable: {exc}"


def _file_mode_warning(path: Path) -> str:
    if not path.exists():
        return ""
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return f"{path} is readable by group/other; prefer chmod 600"
    return ""


def fetch_json(url: str, timeout: float) -> tuple[int, dict]:
    try:
        with urlopen(url, timeout=timeout) as resp:  # noqa: S310 - operator-provided URL
            body = resp.read()
            return resp.status, json.loads(body.decode("utf-8"))
    except HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return exc.code, {}


def _runtime_probe(base_url: str, *, timeout: float, fetcher: FetchJson) -> list[PreflightCheck]:
    base = base_url.rstrip("/")
    checks: list[PreflightCheck] = []
    for path in ("/healthz", "/readyz"):
        url = f"{base}{path}"
        try:
            status, body = fetcher(url, timeout)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            checks.append(_check("fail", f"runtime {path}", f"{url} is not reachable: {exc}"))
            continue
        if path == "/healthz":
            if status == 200 and body.get("status") == "ok" and body.get("db") == "ok":
                checks.append(_check("pass", "runtime /healthz", "service, database, and storage are ok"))
            else:
                checks.append(_check("fail", "runtime /healthz", f"unexpected response {status}: {body}"))
        else:
            if status == 200 and body.get("ready") is True:
                checks.append(_check("pass", "runtime /readyz", "app is ready to serve traffic"))
            else:
                checks.append(_check("fail", "runtime /readyz", f"unexpected response {status}: {body}"))
    return checks


def run_preflight(
    settings: Settings | None = None,
    *,
    root: Path | str = ".",
    health_url: str = "",
    timeout: float = 3.0,
    env: dict[str, str] | None = None,
    fetcher: FetchJson = fetch_json,
) -> list[PreflightCheck]:
    settings = settings or Settings.from_env()
    root = Path(root)
    env = env if env is not None else dict(os.environ)
    checks: list[PreflightCheck] = []

    checks.append(
        _check(
            "pass" if settings.saas_mode else "fail",
            "hosted mode",
            "HESTIA_SAAS_MODE=true" if settings.saas_mode else "set HESTIA_SAAS_MODE=true",
        )
    )
    checks.append(
        _check(
            "pass" if settings.signup_enabled else "fail",
            "self-serve signup",
            "signup is enabled" if settings.signup_enabled else "set HESTIA_SIGNUP_ENABLED=true",
        )
    )

    checks.append(
        _check(
            "pass" if _is_https_public_url(settings.public_url) else "fail",
            "public url",
            (
                f"{settings.public_url} is a public HTTPS URL"
                if _is_https_public_url(settings.public_url)
                else "HESTIA_PUBLIC_URL must be an https:// URL with a non-local host"
            ),
        )
    )
    hosted_domain = normalize_custom_domain(settings.hosted_domain)
    checks.append(
        _check(
            "pass" if validate_custom_domain(hosted_domain) else "fail",
            "hosted domain",
            (
                f"wildcard tenant domain is {hosted_domain}"
                if validate_custom_domain(hosted_domain)
                else "set HESTIA_DOMAIN to the hosted wildcard domain"
            ),
        )
    )
    public_host = _url_host(settings.public_url)
    if hosted_domain and public_host:
        matches = public_host == hosted_domain or public_host.endswith(f".{hosted_domain}")
        checks.append(
            _check(
                "pass" if matches else "warn",
                "domain alignment",
                (
                    f"{settings.public_url} is under {hosted_domain}"
                    if matches
                    else f"HESTIA_PUBLIC_URL host {public_host} is not under HESTIA_DOMAIN {hosted_domain}"
                ),
            )
        )

    secret_map = {
        "HESTIA_API_TOKEN": settings.api_token,
        "HESTIA_TENANT_KEY_PEPPER": settings.tenant_key_pepper,
        "HESTIA_SESSION_SECRET": settings.session_secret,
    }
    for name, value in secret_map.items():
        checks.append(
            _check(
                "pass" if _secret_ok(value) else "fail",
                name,
                f"{name} is set to a non-default value" if _secret_ok(value) else f"{name} must be changed",
            )
        )
    env_mode = _file_mode_warning(root / ".env")
    if env_mode:
        checks.append(_check("warn", ".env permissions", env_mode))

    checks.append(
        _check(
            "pass" if settings.flat_price_cents == 4000 else "fail",
            "flat price",
            (
                "Hestia Studio is locked to $40/month"
                if settings.flat_price_cents == 4000
                else "flat_price_cents must stay locked to 4000"
            ),
        )
    )
    checks.append(
        _check(
            "pass" if settings.trial_days == 14 else "fail",
            "trial length",
            "trial is 14 days" if settings.trial_days == 14 else "HESTIA_TRIAL_DAYS must be 14",
        )
    )

    stripe_ready = bool(settings.stripe_secret_key and settings.stripe_webhook_secret)
    checks.append(
        _check(
            "pass" if settings.subscription_backend == "stripe" else "fail",
            "subscription backend",
            (
                "Stripe subscriptions are active"
                if settings.subscription_backend == "stripe"
                else "set HESTIA_SUBSCRIPTION_BACKEND=stripe for hosted billing"
            ),
        )
    )
    checks.append(
        _check(
            "pass" if stripe_ready else "fail",
            "stripe secrets",
            (
                "Stripe secret key and webhook secret are set"
                if stripe_ready
                else "set HESTIA_STRIPE_SECRET_KEY and HESTIA_STRIPE_WEBHOOK_SECRET"
            ),
        )
    )
    if settings.stripe_secret_key.startswith("sk_test_"):
        checks.append(_check("warn", "stripe mode", "Stripe key is test mode, not live mode"))
    elif settings.stripe_secret_key.startswith("sk_live_"):
        checks.append(_check("pass", "stripe mode", "Stripe key appears to be live mode"))
    elif settings.stripe_secret_key:
        checks.append(_check("warn", "stripe mode", "Stripe key prefix is not recognized"))

    checks.append(
        _check(
            "pass" if settings.email_backend == "smtp" else "fail",
            "email backend",
            (
                "SMTP email is active for verification and notifications"
                if settings.email_backend == "smtp"
                else "set HESTIA_EMAIL_BACKEND=smtp so signup verification emails send"
            ),
        )
    )
    smtp_ready = bool(settings.smtp_host and (settings.smtp_from or settings.smtp_user))
    checks.append(
        _check(
            "pass" if smtp_ready else "fail",
            "smtp config",
            (
                "SMTP host and sender are configured"
                if smtp_ready
                else "set HESTIA_SMTP_HOST plus HESTIA_SMTP_FROM or HESTIA_SMTP_USER"
            ),
        )
    )

    if settings.payments_backend == "stripe":
        checks.append(_check("pass", "invoice payments", "Stripe invoice checkout is active"))
    else:
        checks.append(
            _check("warn", "invoice payments", "HESTIA_PAYMENTS_BACKEND is not stripe; invoices stay simulated")
        )

    data_ok, data_detail = _can_write_dir(settings.data_dir)
    checks.append(_check("pass" if data_ok else "fail", "data volume", data_detail))
    if settings.storage_backend == "local":
        media_ok, media_detail = _can_write_dir(settings.media_dir)
        checks.append(_check("pass" if media_ok else "fail", "media volume", media_detail))
    elif settings.storage_backend == "s3":
        checks.append(
            _check(
                "pass" if settings.s3_bucket else "fail",
                "s3 bucket",
                f"S3 bucket is {settings.s3_bucket}" if settings.s3_bucket else "set HESTIA_S3_BUCKET",
            )
        )
        if not (env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY")):
            checks.append(
                _check("warn", "s3 credentials", "no AWS env credentials found; relying on host IAM/role")
            )
    else:
        checks.append(_check("fail", "storage backend", f"unknown storage backend {settings.storage_backend!r}"))

    caddyfile = root / "Caddyfile"
    compose = root / "docker-compose.yml"
    caddy_text = caddyfile.read_text(encoding="utf-8") if caddyfile.exists() else ""
    checks.append(
        _check(
            "pass" if compose.exists() else "fail",
            "docker compose",
            "docker-compose.yml is present" if compose.exists() else "docker-compose.yml is missing",
        )
    )
    checks.append(
        _check(
            "pass" if "{$HESTIA_DOMAIN}" in caddy_text and "*.{$HESTIA_DOMAIN}" in caddy_text else "fail",
            "caddy wildcard",
            (
                "Caddyfile covers apex and wildcard hosted domains"
                if "{$HESTIA_DOMAIN}" in caddy_text and "*.{$HESTIA_DOMAIN}" in caddy_text
                else "Caddyfile must cover {$HESTIA_DOMAIN} and *.{$HESTIA_DOMAIN}"
            ),
        )
    )

    probe_url = health_url or env.get("HESTIA_PREFLIGHT_URL", "")
    if probe_url:
        checks.extend(_runtime_probe(probe_url, timeout=timeout, fetcher=fetcher))
    else:
        checks.append(
            _check("warn", "runtime probe", "skipped; set HESTIA_PREFLIGHT_URL or pass --url after boot")
        )
    return checks


def print_report(checks: list[PreflightCheck]) -> None:
    print("== Hestia hosted preflight ==")
    for check in checks:
        print(f"{check.level.upper():4} {check.name}: {check.detail}")
    counts = {level: sum(1 for c in checks if c.level == level) for level in ("pass", "warn", "fail")}
    print(f"== summary: {counts['pass']} pass, {counts['warn']} warn, {counts['fail']} fail ==")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Hestia hosted SaaS deployment readiness.")
    parser.add_argument("--url", default="", help="Optional base URL to probe /healthz and /readyz.")
    parser.add_argument("--root", default=".", help="Repo/deployment root containing Caddyfile and compose file.")
    parser.add_argument("--timeout", default=3.0, type=float, help="Runtime probe timeout in seconds.")
    args = parser.parse_args(argv)

    checks = run_preflight(root=args.root, health_url=args.url, timeout=args.timeout)
    print_report(checks)
    return 1 if any(check.level == "fail" for check in checks) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
