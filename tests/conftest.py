"""Shared pytest fixtures and helpers."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hestia.config import Settings
from hestia.db import connect, init_db
from hestia.main import create_app
from hestia.storage import LocalStorage

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return dataclasses.replace(
        Settings.from_env(),
        data_dir=tmp_path,
        media_dir=tmp_path / "media",
        api_token=ADMIN_TOKEN,
        tenant_key_pepper="test-pepper",
        session_secret="test-secret",
        vision_backend="mock",
        storage_backend="local",
        public_url="http://testserver",
    )


@pytest.fixture
def db_path(settings: Settings) -> Path:
    init_db(settings.db_path)
    return settings.db_path


@pytest.fixture
def conn(db_path: Path):
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def storage(settings: Settings) -> LocalStorage:
    return LocalStorage(settings.media_dir)


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def onboard_studio(client: TestClient, *, name="Test Studio", shoot_type="wedding",
                   email="owner@example.com", password="pw12345") -> dict:
    """Admin-onboard a studio and return {email, password, slug}."""
    admin = TestClient(client.app)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})
    admin.post("/admin/onboarding", data={
        "name": name, "shoot_type": shoot_type,
        "owner_email": email, "owner_password": password,
    })
    return {"email": email, "password": password}


def login_owner(client: TestClient, creds: dict) -> TestClient:
    client.post("/login", data={"email": creds["email"], "password": creds["password"]})
    return client
