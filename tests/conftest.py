"""Shared pytest fixtures and helpers."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hestia.auth import SESSION_COOKIE
from hestia.config import Settings
from hestia.csrf import issue_token
from hestia.db import connect, init_db
from hestia.main import create_app
from hestia.storage import LocalStorage

ADMIN_TOKEN = "test-admin-token"

_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


class CSRFClient(TestClient):
    """A TestClient that carries the session's CSRF token on form POSTs.

    This mirrors the browser: every authenticated form ships the hidden
    ``csrf_token`` field, so tests shouldn't have to thread it through by hand.
    Injection happens only when a session cookie is present (i.e. the request is
    authenticated) and the body is form data — never for JSON/bearer API calls.
    Tests that assert *rejection* use a plain ``TestClient`` to omit the token.
    """

    def request(self, method, url, *args, **kwargs):  # noqa: D102
        if method.upper() in _UNSAFE and (session := self.cookies.get(SESSION_COOKIE)):
            if kwargs.get("json") is None and kwargs.get("content") is None:
                data = kwargs.get("data")
                if data is None:
                    data = {}
                if isinstance(data, dict) and "csrf_token" not in data:
                    secret = self.app.state.settings.session_secret
                    kwargs["data"] = {**data, "csrf_token": issue_token(session, secret)}
        return super().request(method, url, *args, **kwargs)


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
    return CSRFClient(app)


def onboard_studio(client: TestClient, *, name="Test Studio", shoot_type="wedding",
                   email="owner@example.com", password="pw12345") -> dict:
    """Admin-onboard a studio and return {email, password, slug}."""
    admin = CSRFClient(client.app)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})
    admin.post("/admin/onboarding", data={
        "name": name, "shoot_type": shoot_type,
        "owner_email": email, "owner_password": password,
    })
    return {"email": email, "password": password}


def login_owner(client: TestClient, creds: dict) -> TestClient:
    client.post("/login", data={"email": creds["email"], "password": creds["password"]})
    return client
