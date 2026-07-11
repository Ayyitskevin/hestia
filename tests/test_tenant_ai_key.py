"""Tenant-owned xAI keys — bring-your-own live vision after the beta subsidy."""

import dataclasses
import io

from hestia.ai_usage import resolve_vision_provider, tenant_subsidy_status
from hestia.galleries import add_image, create_gallery
from hestia.tenants import (
    clear_tenant_ai_key,
    create_tenant,
    get_tenant_ai_key,
    set_tenant_ai_key,
    tenant_has_own_ai_key,
)
from hestia.vision import MockVisionProvider, XaiVisionProvider
from tests.conftest import login_owner, onboard_studio


def _settings(settings, **overrides):
    return dataclasses.replace(settings, **overrides)


def _gallery(conn, storage, tenant_id, n_images: int):
    g = create_gallery(conn, tenant_id=tenant_id, title="G")
    for i in range(n_images):
        add_image(conn, storage, tenant_id=tenant_id, gallery_id=g["id"],
                  filename=f"{i}.jpg", fileobj=io.BytesIO(bytes([i])),
                  content_type="image/jpeg")
    conn.commit()
    return g


class _LiveProvider:
    backend = "xai"

    def analyze(self, *, filename: str, data: bytes, style: str = ""):
        return MockVisionProvider().analyze(filename=filename, data=data, style=style)


def test_key_is_encrypted_at_rest(conn, settings):
    t = create_tenant(conn, name="Key Studio", shoot_type="wedding")
    set_tenant_ai_key(conn, t["id"], "xai-secret-123", session_secret=settings.session_secret)
    conn.commit()
    row = conn.execute("SELECT key_enc, has_key FROM tenant_ai_keys WHERE tenant_id = ?",
                       (t["id"],)).fetchone()
    assert row["has_key"] == 1
    assert "xai-secret-123" not in row["key_enc"]
    assert get_tenant_ai_key(conn, t["id"], session_secret=settings.session_secret) == "xai-secret-123"
    assert tenant_has_own_ai_key(conn, t["id"]) is True


def test_clear_key(conn, settings):
    t = create_tenant(conn, name="Clear Studio", shoot_type="wedding")
    set_tenant_ai_key(conn, t["id"], "k", session_secret=settings.session_secret)
    conn.commit()
    clear_tenant_ai_key(conn, t["id"])
    conn.commit()
    assert tenant_has_own_ai_key(conn, t["id"]) is False
    assert get_tenant_ai_key(conn, t["id"], session_secret=settings.session_secret) == ""


def test_empty_key_clears(conn, settings):
    t = create_tenant(conn, name="Blank Studio", shoot_type="wedding")
    set_tenant_ai_key(conn, t["id"], "k", session_secret=settings.session_secret)
    set_tenant_ai_key(conn, t["id"], "", session_secret=settings.session_secret)
    conn.commit()
    assert tenant_has_own_ai_key(conn, t["id"]) is False


def test_own_key_bypasses_subsidy_gallery_cap(conn, storage, settings):
    t = create_tenant(conn, name="Capped Studio", shoot_type="wedding")
    g1 = _gallery(conn, storage, t["id"], 2)
    g2 = _gallery(conn, storage, t["id"], 2)
    s = _settings(settings, vision_backend="xai", xai_api_key="founder-key",
                  ai_subsidy_galleries_per_tenant=1)
    # subsidy already spent on g1
    conn.execute(
        "INSERT INTO ai_usage_events (tenant_id, gallery_id, module, backend, units) "
        "VALUES (?, ?, 'vision', 'xai', 2)", (t["id"], g1["id"]),
    )
    set_tenant_ai_key(conn, t["id"], "tenant-own-key", session_secret=s.session_secret)
    conn.commit()
    provider, note = resolve_vision_provider(
        conn, s, tenant_id=t["id"], gallery_id=g2["id"], provider=_LiveProvider(),
    )
    assert isinstance(provider, XaiVisionProvider)
    assert provider.settings.xai_api_key == "tenant-own-key"
    assert note and "own xai key" in note.lower()


def test_own_key_bypasses_image_cap(conn, storage, settings):
    t = create_tenant(conn, name="Big Own Key Studio", shoot_type="wedding")
    g = _gallery(conn, storage, t["id"], 5)
    s = _settings(settings, vision_backend="xai", xai_api_key="founder-key",
                  ai_subsidy_image_cap=3)
    set_tenant_ai_key(conn, t["id"], "tenant-own-key", session_secret=s.session_secret)
    conn.commit()
    provider, note = resolve_vision_provider(
        conn, s, tenant_id=t["id"], gallery_id=g["id"], provider=_LiveProvider(),
    )
    assert isinstance(provider, XaiVisionProvider)
    assert provider.settings.xai_api_key == "tenant-own-key"


def test_own_key_takes_precedence_over_unused_subsidy(conn, storage, settings):
    """A studio with an own key uses it even when founder subsidy is still available —
    conserving founder credits."""
    t = create_tenant(conn, name="Precedence Studio", shoot_type="wedding")
    g = _gallery(conn, storage, t["id"], 2)
    s = _settings(settings, vision_backend="xai", xai_api_key="founder-key",
                  ai_subsidy_galleries_per_tenant=1)
    set_tenant_ai_key(conn, t["id"], "tenant-own-key", session_secret=s.session_secret)
    conn.commit()
    provider, note = resolve_vision_provider(
        conn, s, tenant_id=t["id"], gallery_id=g["id"], provider=_LiveProvider(),
    )
    assert isinstance(provider, XaiVisionProvider)
    assert provider.settings.xai_api_key == "tenant-own-key"


def test_own_key_works_without_founder_key(conn, storage, settings):
    """A deployment can run live vision purely on tenant keys — no founder key set."""
    t = create_tenant(conn, name="Self-Funded Studio", shoot_type="wedding")
    g = _gallery(conn, storage, t["id"], 2)
    s = _settings(settings, vision_backend="xai", xai_api_key="",
                  ai_subsidy_galleries_per_tenant=1)
    set_tenant_ai_key(conn, t["id"], "tenant-own-key", session_secret=s.session_secret)
    conn.commit()
    provider, note = resolve_vision_provider(
        conn, s, tenant_id=t["id"], gallery_id=g["id"], provider=_LiveProvider(),
    )
    assert isinstance(provider, XaiVisionProvider)
    assert provider.settings.xai_api_key == "tenant-own-key"


def test_subsidy_status_reports_own_key(conn, settings):
    t = create_tenant(conn, name="Status Own Studio", shoot_type="wedding")
    s = _settings(settings, vision_backend="xai", xai_api_key="founder-key",
                  ai_subsidy_enabled=True)
    set_tenant_ai_key(conn, t["id"], "tenant-own-key", session_secret=s.session_secret)
    conn.commit()
    status = tenant_subsidy_status(conn, s, t["id"])
    assert status["own_key"] is True
    assert status["live_backend"] is True
    assert "own xai key" in status["message"].lower()


def test_owner_can_set_and_clear_own_ai_key(settings):
    import dataclasses

    from hestia.main import create_app
    from tests.conftest import CSRFClient, login_owner, onboard_studio

    app = create_app(dataclasses.replace(
        settings, vision_backend="xai", xai_api_key="founder-key"))
    client = CSRFClient(app)
    creds = onboard_studio(client)
    login_owner(client, creds)
    # set
    r = client.post("/settings/ai-key", data={"xai_api_key": "xai-from-owner"})
    assert r.status_code in (200, 303)
    # site settings page reflects it
    page = client.get("/settings/site").text
    assert "Your own xAI key is set" in page
    # clear
    r = client.post("/settings/ai-key/clear")
    assert r.status_code in (200, 303)
    page = client.get("/settings/site").text
    assert "Your own xAI key is set" not in page


def test_bring_your_own_card_only_when_vision_live(client):
    """When the deployment runs mock vision, the own-key card is not offered."""
    creds = onboard_studio(client)
    login_owner(client, creds)
    page = client.get("/settings/site").text
    assert "your own xAI key" not in page
