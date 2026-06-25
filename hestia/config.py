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

    # Secrets
    api_token: str = "CHANGE_ME_ADMIN"
    tenant_key_pepper: str = "CHANGE_ME"
    session_secret: str = "CHANGE_ME"

    # AI vision provider
    vision_backend: str = "mock"  # mock | xai
    xai_api_key: str = ""
    xai_base_url: str = "https://api.x.ai/v1"
    xai_model: str = "grok-2-vision-1212"

    # Album arrangement provider (model proposes order, code validates placement)
    album_backend: str = "mock"  # mock | xai

    # Marketing content provider (shot lists, captions, campaign copy)
    content_backend: str = "mock"  # mock | xai

    # Product-photo variant renderer (marketplace-spec packshots)
    product_backend: str = "mock"  # mock | xai

    # Storage (native gallery hosting). local = filesystem; s3 = S3/R2/MinIO.
    storage_backend: str = "local"  # local | s3
    media_dir: Path = field(default_factory=lambda: Path("./data/media"))
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_endpoint_url: str = ""        # set for Cloudflare R2 / MinIO; blank = AWS S3
    s3_public_base_url: str = ""     # public/CDN base; blank = presigned GET urls

    # Payments. mock = simulate checkout (no keys, testable). stripe = live API.
    payments_backend: str = "mock"  # mock | stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    currency: str = "usd"

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
            api_token=os.getenv("HESTIA_API_TOKEN", "CHANGE_ME_ADMIN"),
            tenant_key_pepper=os.getenv("HESTIA_TENANT_KEY_PEPPER", "CHANGE_ME"),
            session_secret=os.getenv("HESTIA_SESSION_SECRET", "CHANGE_ME"),
            vision_backend=os.getenv("HESTIA_VISION_BACKEND", "mock"),
            xai_api_key=os.getenv("HESTIA_XAI_API_KEY", os.getenv("XAI_API_KEY", "")),
            xai_base_url=os.getenv("HESTIA_XAI_BASE_URL", "https://api.x.ai/v1"),
            xai_model=os.getenv("HESTIA_XAI_MODEL", "grok-2-vision-1212"),
            album_backend=os.getenv("HESTIA_ALBUM_BACKEND", "mock"),
            content_backend=os.getenv("HESTIA_CONTENT_BACKEND", "mock"),
            product_backend=os.getenv("HESTIA_PRODUCT_BACKEND", "mock"),
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
        )

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
