"""The advertised local path boots safely without weakening hosted defaults."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from dotenv import dotenv_values
from fastapi.testclient import TestClient

from hestia.config import Settings
from hestia.main import create_app

LOCAL_ENV = Path(".env.example")
MOCK_BACKENDS = (
    "HESTIA_VISION_BACKEND",
    "HESTIA_ALBUM_BACKEND",
    "HESTIA_CONTENT_BACKEND",
    "HESTIA_PRODUCT_BACKEND",
    "HESTIA_PAYMENTS_BACKEND",
    "HESTIA_SUBSCRIPTION_BACKEND",
    "HESTIA_EMAIL_BACKEND",
    "HESTIA_FULFILLMENT_BACKEND",
)


def _local_values() -> dict[str, str]:
    return {
        key: value or ""
        for key, value in dotenv_values(LOCAL_ENV).items()
        if key is not None
    }


def test_local_env_example_is_non_saas_mock_first():
    env = _local_values()

    assert env["HESTIA_SAAS_MODE"] == "false"
    assert env["HESTIA_SIGNUP_ENABLED"] == "false"
    assert env["HESTIA_STORAGE_BACKEND"] == "local"
    for key in MOCK_BACKENDS:
        assert env[key] == "mock"


def test_local_env_example_boots_with_placeholder_secrets(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("HESTIA_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in _local_values().items():
        if key.startswith("HESTIA_"):
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("HESTIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HESTIA_MEDIA_DIR", str(tmp_path / "media"))

    settings = Settings.from_env()
    assert settings.saas_mode is False
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200


def test_start_script_defaults_to_loopback_and_preserves_override(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uvicorn = fake_bin / "uvicorn"
    fake_uvicorn.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$CAPTURE_FILE\"\n",
        encoding="utf-8",
    )
    fake_uvicorn.chmod(0o755)

    base_env = os.environ.copy()
    base_env["PATH"] = f"{fake_bin}:{base_env['PATH']}"
    base_env.pop("HESTIA_HOST", None)
    base_env.pop("HESTIA_PORT", None)

    default_capture = tmp_path / "default-argv"
    default_env = {**base_env, "CAPTURE_FILE": str(default_capture)}
    subprocess.run(
        ["bash", "scripts/start-hestia.sh", "--log-level", "warning"],
        check=True,
        env=default_env,
        capture_output=True,
        text=True,
    )
    assert default_capture.read_text(encoding="utf-8").splitlines() == [
        "hestia.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8500",
        "--log-level",
        "warning",
    ]

    override_capture = tmp_path / "override-argv"
    override_env = {
        **base_env,
        "CAPTURE_FILE": str(override_capture),
        "HESTIA_HOST": "0.0.0.0",
    }
    subprocess.run(
        ["bash", "scripts/start-hestia.sh"],
        check=True,
        env=override_env,
        capture_output=True,
        text=True,
    )
    assert override_capture.read_text(encoding="utf-8").splitlines()[2] == "0.0.0.0"
