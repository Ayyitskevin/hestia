"""Configuration loaded from the environment (via python-dotenv).

Hestia is one multi-tenant app — no fleet of services to point at. Config is the
control-plane secrets, the AI vision provider, and the storage backend.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Core
    port: int = 8500
    saas_mode: bool = True
    signup_enabled: bool = False
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    public_url: str = "http://127.0.0.1:8500"
    hosted_domain: str = ""
    # Number of reverse proxies the app sits behind (e.g. 1 for Caddy). The rate
    # limiter only trusts X-Forwarded-For this many hops deep; 0 = not behind a
    # trusted proxy, so XFF is ignored and the real peer IP is used (spoof-safe).
    trusted_proxies: int = 0

    # Observability. json = one structured line per log record (default).
    log_format: str = "json"  # json | plain
    log_level: str = "INFO"

    # Secrets
    api_token: str = "CHANGE_ME_ADMIN"
    tenant_key_pepper: str = "CHANGE_ME"
    session_secret: str = "CHANGE_ME"

    # AI vision provider
    vision_backend: str = "mock"  # mock | xai
    xai_api_key: str = ""
    xai_base_url: str = "https://api.x.ai/v1"
    xai_model: str = "grok-2-vision-1212"
    xai_image_model: str = "grok-imagine-image-quality"

    # Album arrangement provider (model proposes order, code validates placement)
    album_backend: str = "mock"  # mock | xai

    # Marketing content provider (shot lists, captions, campaign copy)
    content_backend: str = "mock"  # mock | xai

    # Product-photo variant renderer (marketplace-spec packshots)
    product_backend: str = "mock"  # mock | xai

    # Beta AI subsidy — founder-hosted xAI credits for the first gallery per studio.
    ai_subsidy_enabled: bool = True
    ai_subsidy_galleries_per_tenant: int = 1
    ai_subsidy_image_cap: int = 150

    # Storage (native gallery hosting). local = filesystem; s3 = S3/R2/MinIO.
    storage_backend: str = "local"  # local | s3
    media_dir: Path = field(default_factory=lambda: Path("./data/media"))
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_endpoint_url: str = ""        # set for Cloudflare R2 / MinIO; blank = AWS S3
    # Kept only to detect and reject legacy unsafe config. Client media must come
    # from a private bucket through presigned URLs.
    s3_public_base_url: str = ""

    # Payments. mock = simulate checkout (no keys, testable). stripe = live API.
    payments_backend: str = "mock"  # mock | stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    currency: str = "usd"
    flat_price_cents: int = 4000
    trial_days: int = 14

    # Studio subscriptions (billing the studios). mock = activate the plan instantly
    # (no charge, testable). stripe = Checkout Session in subscription mode + webhook.
    subscription_backend: str = "mock"  # mock | stripe

    # Email (transactional). mock = record to the outbox, send nothing (testable
    # default). smtp = deliver over SMTP and still record. See hestia/email.py.
    email_backend: str = "mock"  # mock | smtp
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    # Print fulfillment. mock = record the lab order, simulate acceptance (testable
    # default). lab = submit to a real print lab (WHCC/Bay Photo class) over HTTP.
    fulfillment_backend: str = "mock"  # mock | lab
    fulfillment_api_key: str = ""
    fulfillment_endpoint: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = Path(os.getenv("HESTIA_DATA_DIR", "./data"))
        media_dir = Path(os.getenv("HESTIA_MEDIA_DIR", str(data_dir / "media")))
        return cls(
            port=int(os.getenv("HESTIA_PORT", "8500")),
            saas_mode=_env_bool("HESTIA_SAAS_MODE", True),
            signup_enabled=_env_bool("HESTIA_SIGNUP_ENABLED", False),
            data_dir=data_dir,
            public_url=os.getenv("HESTIA_PUBLIC_URL", "http://127.0.0.1:8500"),
            hosted_domain=os.getenv("HESTIA_DOMAIN", ""),
            trusted_proxies=int(os.getenv("HESTIA_TRUSTED_PROXIES", "0")),
            log_format=os.getenv("HESTIA_LOG_FORMAT", "json"),
            log_level=os.getenv("HESTIA_LOG_LEVEL", "INFO"),
            api_token=os.getenv("HESTIA_API_TOKEN", "CHANGE_ME_ADMIN"),
            tenant_key_pepper=os.getenv("HESTIA_TENANT_KEY_PEPPER", "CHANGE_ME"),
            session_secret=os.getenv("HESTIA_SESSION_SECRET", "CHANGE_ME"),
            vision_backend=os.getenv("HESTIA_VISION_BACKEND", "mock"),
            xai_api_key=os.getenv("HESTIA_XAI_API_KEY", os.getenv("XAI_API_KEY", "")),
            xai_base_url=os.getenv("HESTIA_XAI_BASE_URL", "https://api.x.ai/v1"),
            xai_model=os.getenv("HESTIA_XAI_MODEL", "grok-2-vision-1212"),
            xai_image_model=os.getenv(
                "HESTIA_XAI_IMAGE_MODEL", "grok-imagine-image-quality"
            ),
            album_backend=os.getenv("HESTIA_ALBUM_BACKEND", "mock"),
            content_backend=os.getenv("HESTIA_CONTENT_BACKEND", "mock"),
            product_backend=os.getenv("HESTIA_PRODUCT_BACKEND", "mock"),
            ai_subsidy_enabled=_env_bool("HESTIA_AI_SUBSIDY_ENABLED", True),
            ai_subsidy_galleries_per_tenant=int(os.getenv("HESTIA_AI_SUBSIDY_GALLERIES", "1")),
            ai_subsidy_image_cap=int(os.getenv("HESTIA_AI_SUBSIDY_IMAGE_CAP", "150")),
            storage_backend=os.getenv("HESTIA_STORAGE_BACKEND", "local"),
            media_dir=media_dir,
            s3_bucket=os.getenv("HESTIA_S3_BUCKET", ""),
            s3_region=os.getenv("HESTIA_S3_REGION", "us-east-1"),
            s3_endpoint_url=os.getenv("HESTIA_S3_ENDPOINT_URL", ""),
            s3_public_base_url=os.getenv("HESTIA_S3_PUBLIC_BASE_URL", ""),
            payments_backend=os.getenv("HESTIA_PAYMENTS_BACKEND", "mock"),
            stripe_secret_key=os.getenv("HESTIA_STRIPE_SECRET_KEY", os.getenv("STRIPE_SECRET_KEY", "")),
            stripe_webhook_secret=os.getenv("HESTIA_STRIPE_WEBHOOK_SECRET", os.getenv("STRIPE_WEBHOOK_SECRET", "")),
            currency=os.getenv("HESTIA_CURRENCY", "usd"),
            flat_price_cents=4000,
            trial_days=int(os.getenv("HESTIA_TRIAL_DAYS", "14")),
            email_backend=os.getenv("HESTIA_EMAIL_BACKEND", "mock"),
            smtp_host=os.getenv("HESTIA_SMTP_HOST", ""),
            smtp_port=int(os.getenv("HESTIA_SMTP_PORT", "587")),
            smtp_user=os.getenv("HESTIA_SMTP_USER", ""),
            smtp_password=os.getenv("HESTIA_SMTP_PASSWORD", ""),
            smtp_from=os.getenv("HESTIA_SMTP_FROM", ""),
            subscription_backend=os.getenv("HESTIA_SUBSCRIPTION_BACKEND", "mock"),
            fulfillment_backend=os.getenv("HESTIA_FULFILLMENT_BACKEND", "mock"),
            fulfillment_api_key=os.getenv("HESTIA_FULFILLMENT_API_KEY", ""),
            fulfillment_endpoint=os.getenv("HESTIA_FULFILLMENT_ENDPOINT", ""),
        )

    def stripe_price_id(self, plan: str) -> str:
        # Hestia is a flat-price SaaS. Kept only for old callers; new subscription
        # checkout uses inline Stripe price_data locked to flat_price_cents.
        return "flat_40_month" if plan == "studio" else ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hestia.db"

    @property
    def insecure_secrets(self) -> list[str]:
        bad = []
        if self.api_token in ("", "CHANGE_ME_ADMIN"):
            bad.append("HESTIA_API_TOKEN")
        if self.tenant_key_pepper in ("", "CHANGE_ME"):
            bad.append("HESTIA_TENANT_KEY_PEPPER")
        if self.session_secret in ("", "CHANGE_ME"):
            bad.append("HESTIA_SESSION_SECRET")
        return bad

    @property
    def config_warnings(self) -> list[str]:
        """Misconfigurations worth shouting about at boot — a real backend selected
        without the credentials it needs would otherwise fail silently per-request."""
        warn = [f"{s} is a default — set a real value" for s in self.insecure_secrets]
        if self.payments_backend == "stripe" and not self.stripe_secret_key:
            warn.append("payments_backend=stripe but HESTIA_STRIPE_SECRET_KEY is unset")
        if self.subscription_backend == "stripe" and not self.stripe_secret_key:
            warn.append("subscription_backend=stripe but HESTIA_STRIPE_SECRET_KEY is unset")
        if {self.payments_backend, self.subscription_backend} & {"stripe"} and not self.stripe_webhook_secret:
            warn.append("a stripe backend is active but HESTIA_STRIPE_WEBHOOK_SECRET is unset (webhooks 503)")
        if self.fulfillment_backend == "lab" and not (
            self.fulfillment_api_key and self.fulfillment_endpoint
        ):
            warn.append("fulfillment_backend=lab but HESTIA_FULFILLMENT_API_KEY or "
                        "HESTIA_FULFILLMENT_ENDPOINT is unset (paid orders record as 'failed')")
        if self.storage_backend == "s3" and not self.s3_bucket:
            warn.append("storage_backend=s3 but HESTIA_S3_BUCKET is unset")
        if self.s3_public_base_url:
            warn.append(
                "HESTIA_S3_PUBLIC_BASE_URL is unsafe and unsupported; "
                "use a private bucket with presigned URLs"
            )
        if self.email_backend == "smtp" and not self.smtp_host:
            warn.append("email_backend=smtp but HESTIA_SMTP_HOST is unset")
        xai = [b for b in ("vision", "album", "content", "product")
               if getattr(self, f"{b}_backend") == "xai"]
        if xai and not self.xai_api_key:
            warn.append(f"{'/'.join(xai)} backend=xai but HESTIA_XAI_API_KEY is unset")
        return warn


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
