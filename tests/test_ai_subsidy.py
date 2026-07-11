"""Beta AI subsidy — founder-hosted vision credits per studio."""

import io

from hestia.ai_usage import (
    gallery_has_live_vision,
    resolve_vision_provider,
    tenant_subsidy_status,
)
from hestia.galleries import add_image, create_gallery
from hestia.tenants import create_tenant
from hestia.vision import MockVisionProvider


class _LiveProvider:
    backend = "xai"

    def analyze(self, *, filename: str, data: bytes, style: str = ""):
        return MockVisionProvider().analyze(filename=filename, data=data, style=style)


def _settings(settings, **overrides):
    import dataclasses
    return dataclasses.replace(settings, **overrides)


def _gallery(conn, storage, tenant_id, n_images: int):
    g = create_gallery(conn, tenant_id=tenant_id, title="G")
    for i in range(n_images):
        add_image(conn, storage, tenant_id=tenant_id, gallery_id=g["id"],
                  filename=f"{i}.jpg", fileobj=io.BytesIO(bytes([i])),
                  content_type="image/jpeg")
    conn.commit()
    return g


def test_mock_backend_never_needs_subsidy(conn, settings):
    t = create_tenant(conn, name="Mock Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    conn.commit()
    provider, note = resolve_vision_provider(
        conn, settings, tenant_id=t["id"], gallery_id=g["id"],
    )
    assert isinstance(provider, MockVisionProvider)
    assert note is None


def test_first_gallery_gets_live_provider(conn, storage, settings):
    t = create_tenant(conn, name="Beta Studio", shoot_type="wedding")
    g = _gallery(conn, storage, t["id"], 3)
    s = _settings(settings, vision_backend="xai", xai_api_key="test-key")
    provider, note = resolve_vision_provider(
        conn, s, tenant_id=t["id"], gallery_id=g["id"], provider=_LiveProvider(),
    )
    assert provider.backend == "xai"
    assert note is None


def test_second_gallery_falls_back_to_mock(conn, storage, settings):
    t = create_tenant(conn, name="Two Gallery Studio", shoot_type="wedding")
    g1 = _gallery(conn, storage, t["id"], 2)
    g2 = _gallery(conn, storage, t["id"], 2)
    s = _settings(settings, vision_backend="xai", xai_api_key="test-key",
                  ai_subsidy_galleries_per_tenant=1)
    conn.execute(
        "INSERT INTO ai_usage_events (tenant_id, gallery_id, module, backend, units) "
        "VALUES (?, ?, 'vision', 'xai', 2)",
        (t["id"], g1["id"]),
    )
    conn.commit()
    provider, note = resolve_vision_provider(
        conn, s, tenant_id=t["id"], gallery_id=g2["id"], provider=_LiveProvider(),
    )
    assert isinstance(provider, MockVisionProvider)
    assert note and "subsidy" in note.lower()


def test_reprocess_same_gallery_stays_live(conn, storage, settings):
    t = create_tenant(conn, name="Reprocess Studio", shoot_type="wedding")
    g = _gallery(conn, storage, t["id"], 2)
    s = _settings(settings, vision_backend="xai", xai_api_key="test-key",
                  ai_subsidy_galleries_per_tenant=1)
    conn.execute(
        "INSERT INTO ai_usage_events (tenant_id, gallery_id, module, backend, units) "
        "VALUES (?, ?, 'vision', 'xai', 2)",
        (t["id"], g["id"]),
    )
    conn.commit()
    assert gallery_has_live_vision(conn, t["id"], g["id"])
    provider, note = resolve_vision_provider(
        conn, s, tenant_id=t["id"], gallery_id=g["id"], provider=_LiveProvider(),
    )
    assert provider.backend == "xai"
    assert note is None


def test_oversized_gallery_uses_mock(conn, storage, settings):
    t = create_tenant(conn, name="Big Gallery Studio", shoot_type="wedding")
    g = _gallery(conn, storage, t["id"], 5)
    s = _settings(settings, vision_backend="xai", xai_api_key="test-key",
                  ai_subsidy_image_cap=3)
    provider, note = resolve_vision_provider(
        conn, s, tenant_id=t["id"], gallery_id=g["id"], provider=_LiveProvider(),
    )
    assert isinstance(provider, MockVisionProvider)
    assert "3" in note


def test_tenant_subsidy_status_remaining(conn, settings):
    t = create_tenant(conn, name="Status Studio", shoot_type="wedding")
    conn.commit()
    s = _settings(settings, vision_backend="xai", xai_api_key="k", ai_subsidy_enabled=True)
    status = tenant_subsidy_status(conn, s, t["id"])
    assert status["active"] is True
    assert status["remaining_galleries"] == 1
    assert "next gallery" in status["message"].lower()
