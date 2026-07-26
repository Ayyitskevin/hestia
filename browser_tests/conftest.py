"""Isolated live Hestia server for Chromium acceptance tests."""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

# Importing hestia.main constructs its module-level ASGI app. Establish a disposable,
# mock-only environment first so collection cannot inherit paths, credentials, or
# providers from a developer's .env. python-dotenv does not override pre-set values.
_BOOTSTRAP_TEMP = tempfile.TemporaryDirectory(prefix="hestia-browser-bootstrap-")
_BOOTSTRAP_ROOT = Path(_BOOTSTRAP_TEMP.name)
_ORIGINAL_ENV = os.environ.copy()
os.environ.update(
    {
        "HESTIA_PORT": "0",
        "HESTIA_SAAS_MODE": "1",
        "HESTIA_SIGNUP_ENABLED": "0",
        "HESTIA_DATA_DIR": str(_BOOTSTRAP_ROOT),
        "HESTIA_MEDIA_DIR": str(_BOOTSTRAP_ROOT / "media"),
        "HESTIA_PUBLIC_URL": "http://127.0.0.1:9",
        "HESTIA_DOMAIN": "",
        "HESTIA_TRUSTED_PROXIES": "0",
        "HESTIA_LOG_FORMAT": "plain",
        "HESTIA_LOG_LEVEL": "WARNING",
        "HESTIA_API_TOKEN": "browser-bootstrap-admin-token",
        "HESTIA_TENANT_KEY_PEPPER": "browser-bootstrap-tenant-pepper",
        "HESTIA_SESSION_SECRET": "browser-bootstrap-session-secret",
        "HESTIA_VISION_BACKEND": "mock",
        "HESTIA_ALBUM_BACKEND": "mock",
        "HESTIA_CONTENT_BACKEND": "mock",
        "HESTIA_PRODUCT_BACKEND": "mock",
        "HESTIA_AI_SUBSIDY_ENABLED": "0",
        "HESTIA_AI_SUBSIDY_GALLERIES": "1",
        "HESTIA_AI_SUBSIDY_IMAGE_CAP": "150",
        "HESTIA_TRIAL_DAYS": "14",
        "HESTIA_XAI_API_KEY": "",
        "XAI_API_KEY": "",
        "HESTIA_XAI_BASE_URL": "http://127.0.0.1:9/v1",
        "HESTIA_STORAGE_BACKEND": "local",
        "HESTIA_S3_BUCKET": "",
        "HESTIA_S3_ENDPOINT_URL": "",
        "HESTIA_S3_PUBLIC_BASE_URL": "",
        "HESTIA_PAYMENTS_BACKEND": "mock",
        "HESTIA_SUBSCRIPTION_BACKEND": "mock",
        "HESTIA_STRIPE_SECRET_KEY": "",
        "STRIPE_SECRET_KEY": "",
        "HESTIA_STRIPE_WEBHOOK_SECRET": "",
        "STRIPE_WEBHOOK_SECRET": "",
        "HESTIA_EMAIL_BACKEND": "mock",
        "HESTIA_SMTP_HOST": "",
        "HESTIA_SMTP_PORT": "587",
        "HESTIA_SMTP_USER": "",
        "HESTIA_SMTP_PASSWORD": "",
        "HESTIA_SMTP_FROM": "",
        "HESTIA_FULFILLMENT_BACKEND": "mock",
        "HESTIA_FULFILLMENT_API_KEY": "",
        "HESTIA_FULFILLMENT_ENDPOINT": "",
    }
)

try:
    from hestia.config import Settings  # noqa: E402
    from hestia.main import create_app  # noqa: E402
finally:
    os.environ.clear()
    os.environ.update(_ORIGINAL_ENV)
    del _ORIGINAL_ENV

_HOST = "127.0.0.1"


@pytest.fixture(scope="module")
def live_hestia(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Serve a mock-only app on an OS-assigned loopback port with disposable state."""
    data_dir = tmp_path_factory.mktemp("gallery-browser")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((_HOST, 0))
    port = listener.getsockname()[1]
    base_url = f"http://{_HOST}:{port}"

    settings = Settings(
        port=port,
        saas_mode=True,
        signup_enabled=False,
        data_dir=data_dir,
        media_dir=data_dir / "media",
        public_url=base_url,
        hosted_domain="",
        trusted_proxies=0,
        api_token="browser-admin-token",
        tenant_key_pepper="browser-tenant-pepper",
        session_secret="browser-session-secret",
        vision_backend="mock",
        xai_api_key="",
        xai_base_url="http://127.0.0.1:9/v1",
        album_backend="mock",
        content_backend="mock",
        product_backend="mock",
        ai_subsidy_enabled=False,
        storage_backend="local",
        s3_bucket="",
        s3_endpoint_url="",
        s3_public_base_url="",
        payments_backend="mock",
        stripe_secret_key="",
        stripe_webhook_secret="",
        subscription_backend="mock",
        email_backend="mock",
        smtp_host="",
        smtp_user="",
        smtp_password="",
        smtp_from="",
        fulfillment_backend="mock",
        fulfillment_api_key="",
        fulfillment_endpoint="",
        log_format="plain",
        log_level="WARNING",
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            host=_HOST,
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="hestia-browser-server",
        daemon=True,
    )
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        raise RuntimeError("isolated Hestia browser server did not start")

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("isolated Hestia browser server did not stop")
