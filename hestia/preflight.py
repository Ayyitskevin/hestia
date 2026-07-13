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
import time
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
FetchText = Callable[[str, float], tuple[int, str]]

LOCAL_HOSTS = {"", "127.0.0.1", "::1", "0.0.0.0", "localhost", "testserver"}

# Client-token prefixes that must be robots-disallowed on the LIVE domain — the
# same list ci-smoke enforces in code; this proves the deployed box serves it.
ROBOTS_REQUIRED = ("/portal/", "/d/", "/pay/", "/a/", "/sign/", "/g/", "/t/", "/q/",
                   "/invite/", "/media/")


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


def fetch_text(url: str, timeout: float) -> tuple[int, str]:
    try:
        with urlopen(url, timeout=timeout) as resp:  # noqa: S310 - operator-provided URL
            return resp.status, resp.read().decode("utf-8", "replace")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _runtime_probe(
    base_url: str,
    *,
    timeout: float,
    fetcher: FetchJson,
    text_fetcher: FetchText,
) -> list[PreflightCheck]:
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
    url = f"{base}/robots.txt"
    try:
        status, text = text_fetcher(url, timeout)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        checks.append(_check("fail", "runtime /robots.txt", f"{url} is not reachable: {exc}"))
    else:
        missing = [p for p in ROBOTS_REQUIRED if f"Disallow: {p}" not in text]
        if status == 200 and not missing:
            checks.append(
                _check("pass", "runtime /robots.txt", "client-token paths are disallowed on the live domain")
            )
        elif status != 200:
            checks.append(_check("fail", "runtime /robots.txt", f"unexpected status {status}"))
        else:
            checks.append(
                _check("fail", "runtime /robots.txt", f"missing Disallow entries: {', '.join(missing)}")
            )
    return checks


def _backup_freshness(settings: Settings, env: dict[str, str], *, max_age_hours: float = 26.0) -> PreflightCheck:
    """Backups must actually be happening. Warn before first boot (no artifacts is
    legitimate then); FAIL when the newest artifact is stale — a dead backup loop
    on a live box is a launch blocker, not a footnote."""
    backups_dir = Path(env.get("HESTIA_BACKUP_DIR") or settings.data_dir / "backups")
    artifacts = sorted(backups_dir.glob("hestia-*.db.gz"), key=lambda p: p.stat().st_mtime)
    if not artifacts:
        return _check(
            "warn",
            "backup freshness",
            f"no backups in {backups_dir} yet — the compose backup service creates one at start",
        )
    newest = artifacts[-1]
    age_hours = (time.time() - newest.stat().st_mtime) / 3600
    if age_hours > max_age_hours:
        return _check(
            "fail",
            "backup freshness",
            f"newest backup {newest.name} is {age_hours:.1f}h old (limit {max_age_hours:.0f}h) — "
            "is the backup service running?",
        )
    return _check("pass", "backup freshness", f"newest backup {newest.name} is {age_hours:.1f}h old")


def _media_durability(env: dict[str, str]) -> PreflightCheck:
    """Local media sits on the same volume as the DB, so a volume loss takes every
    client gallery with it — and unlike the DB, there's nothing to restore from. For a
    photography product that's the worst possible loss, so require an off-site copy:
    HESTIA_OFFSITE_REMOTE (scripts/offsite-sync.sh pushes DB backups + media there)
    closes it. An operator who handles durability at the infrastructure layer (e.g.
    daily volume snapshots Hestia can't see) can acknowledge with
    HESTIA_MEDIA_DURABILITY_ACK. S3/R2 storage never reaches this check — the object
    store is already durable and off-box."""
    remote = (env.get("HESTIA_OFFSITE_REMOTE") or "").strip()
    ack = (env.get("HESTIA_MEDIA_DURABILITY_ACK") or "").strip()
    if remote:
        return _check("pass", "media durability",
                      f"media + DB backups sync off-site to {remote} via scripts/offsite-sync.sh")
    if ack:
        return _check("pass", "media durability",
                      f"off-site media durability acknowledged as handled externally ({ack})")
    return _check(
        "fail", "media durability",
        "local media has no off-site copy — a volume loss would lose every client "
        "gallery, unrecoverably. Set HESTIA_OFFSITE_REMOTE and cron scripts/offsite-sync.sh, "
        "switch to S3 storage, or set HESTIA_MEDIA_DURABILITY_ACK if host volume snapshots cover it.",
    )


def run_preflight(
    settings: Settings | None = None,
    *,
    root: Path | str = ".",
    health_url: str = "",
    timeout: float = 3.0,
    env: dict[str, str] | None = None,
    fetcher: FetchJson = fetch_json,
    text_fetcher: FetchText = fetch_text,
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

    # Live AI vision is the product's wedge. mock is runnable but the magic moment
    # (real culls) is fake — warn so the operator knows demos run on simulated AI.
    # xai selected without a key silently falls back to mock per-request — fail.
    if settings.vision_backend == "xai":
        checks.append(
            _check(
                "pass" if settings.xai_api_key else "fail",
                "live ai vision",
                (
                    "xAI vision is configured for real culls"
                    if settings.xai_api_key
                    else "HESTIA_VISION_BACKEND=xai but HESTIA_XAI_API_KEY is unset; vision silently falls back to mock"
                ),
            )
        )
    else:
        checks.append(
            _check(
                "warn",
                "live ai vision",
                "vision_backend=mock; the magic moment runs on simulated culls — set HESTIA_VISION_BACKEND=xai for real demos",
            )
        )

    # The beta subsidy promises founder-hosted xAI credits for the first gallery
    # per studio. With it enabled but no live vision, that promise is hollow.
    if settings.ai_subsidy_enabled:
        subsidy_live = settings.vision_backend == "xai" and bool(settings.xai_api_key)
        checks.append(
            _check(
                "pass" if subsidy_live else "warn",
                "ai subsidy coherence",
                (
                    f"subsidy is live: {settings.ai_subsidy_galleries_per_tenant} gallery · "
                    f"{settings.ai_subsidy_image_cap} image cap on xAI"
                    if subsidy_live
                    else "ai_subsidy_enabled but vision is not live; subsidized galleries cull on mock — set HESTIA_VISION_BACKEND=xai + key"
                ),
            )
        )

    # FAIL, not warn: with mock invoice payments a client who clicks "pay" flips the
    # invoice to paid (and fulfills any backing order) with $0 actually charged. On a
    # live box that is silent revenue loss — gate it as hard as the subscription backend.
    if settings.payments_backend == "stripe":
        checks.append(_check("pass", "invoice payments", "Stripe invoice checkout is active"))
    else:
        checks.append(
            _check("fail", "invoice payments",
                   "set HESTIA_PAYMENTS_BACKEND=stripe; mock payments mark invoices paid without charging")
        )

    # Print fulfillment. lab selected without credentials records paid print orders
    # as 'failed' — silent revenue leakage on a live box, so fail like mock payments.
    # mock is runnable but physical prints never ship automatically; warn for beta.
    if settings.fulfillment_backend == "lab":
        lab_ready = bool(settings.fulfillment_api_key and settings.fulfillment_endpoint)
        checks.append(
            _check(
                "pass" if lab_ready else "fail",
                "print fulfillment",
                (
                    "lab fulfillment is configured to submit real print orders"
                    if lab_ready
                    else "HESTIA_FULFILLMENT_BACKEND=lab but HESTIA_FULFILLMENT_API_KEY or "
                         "HESTIA_FULFILLMENT_ENDPOINT is unset; paid orders record as 'failed'"
                ),
            )
        )
    else:
        checks.append(
            _check(
                "warn",
                "print fulfillment",
                "fulfillment_backend=mock; paid print orders are recorded but never shipped — "
                "set HESTIA_FULFILLMENT_BACKEND=lab to close the physical print loop",
            )
        )

    data_ok, data_detail = _can_write_dir(settings.data_dir)
    checks.append(_check("pass" if data_ok else "fail", "data volume", data_detail))
    checks.append(_backup_freshness(settings, env))
    if settings.storage_backend == "local":
        media_ok, media_detail = _can_write_dir(settings.media_dir)
        checks.append(_check("pass" if media_ok else "fail", "media volume", media_detail))
        checks.append(_media_durability(env))
    elif settings.storage_backend == "s3":
        checks.append(
            _check(
                "pass" if settings.s3_bucket else "fail",
                "s3 bucket",
                f"S3 bucket is {settings.s3_bucket}" if settings.s3_bucket else "set HESTIA_S3_BUCKET",
            )
        )
        checks.append(
            _check(
                "fail" if settings.s3_public_base_url else "pass",
                "private s3 media",
                (
                    "unset HESTIA_S3_PUBLIC_BASE_URL; public object URLs bypass "
                    "gallery visibility and capability checks"
                    if settings.s3_public_base_url
                    else "S3 media uses private, short-lived presigned URLs"
                ),
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
        checks.extend(
            _runtime_probe(probe_url, timeout=timeout, fetcher=fetcher, text_fetcher=text_fetcher)
        )
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
