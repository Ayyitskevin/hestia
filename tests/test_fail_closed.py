"""SaaS mode fails closed on default secrets.

A hosted box booted with the shipped CHANGE_ME placeholders would serve with forgeable
CSRF tokens and decryptable service tokens. Preflight flags it before boot; this is the
last-line refusal at ASGI startup (the lifespan raises, so the server never serves)."""

import dataclasses

import pytest
from fastapi.testclient import TestClient

from hestia.main import create_app


def test_saas_mode_refuses_to_boot_with_a_default_secret(settings):
    bad = dataclasses.replace(settings, saas_mode=True, session_secret="CHANGE_ME")
    app = create_app(bad)                              # construction is fine...
    with pytest.raises(RuntimeError, match="default secret"):
        with TestClient(app):                          # ...the lifespan startup refuses
            pass


def test_saas_mode_boots_with_real_secrets(settings):
    # The test settings fixture already uses non-default secrets.
    with TestClient(create_app(dataclasses.replace(settings, saas_mode=True))) as c:
        assert c.get("/healthz").status_code == 200


def test_non_saas_mode_tolerates_defaults_for_local_dev(settings):
    dev = dataclasses.replace(settings, saas_mode=False, session_secret="CHANGE_ME",
                              tenant_key_pepper="CHANGE_ME", api_token="CHANGE_ME_ADMIN")
    with TestClient(create_app(dev)) as c:             # dev convenience: no hard refusal
        assert c.get("/healthz").status_code == 200
