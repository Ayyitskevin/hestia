"""AI usage ledger — live provider call tracking."""

import io

from hestia.ai_usage import (
    gallery_usage_summary,
    operator_usage_summary,
    record_usage,
    tenant_usage_summary,
)
from hestia.galleries import add_image, create_gallery
from hestia.tenants import create_tenant
from hestia.vision import analyze_gallery


def test_mock_vision_does_not_record_usage(conn, storage, settings):
    t = create_tenant(conn, name="Usage Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    add_image(
        conn,
        storage,
        tenant_id=t["id"],
        gallery_id=g["id"],
        filename="a.jpg",
        fileobj=io.BytesIO(b"x"),
        content_type="image/jpeg",
    )
    conn.commit()
    analyze_gallery(conn, storage, settings, tenant_id=t["id"], gallery_id=g["id"])
    assert conn.execute("SELECT COUNT(*) AS n FROM ai_usage_events").fetchone()["n"] == 0


def test_record_usage_ignores_mock(conn):
    t = create_tenant(conn, name="Ledger Studio", shoot_type="wedding")
    conn.commit()
    record_usage(conn, tenant_id=t["id"], module="vision", backend="mock", units=5)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS n FROM ai_usage_events").fetchone()["n"] == 0


def test_record_usage_persists_live_calls(conn):
    t = create_tenant(conn, name="Live Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    conn.commit()
    record_usage(
        conn, tenant_id=t["id"], module="vision", backend="xai", units=12, gallery_id=g["id"]
    )
    conn.commit()
    summary = tenant_usage_summary(conn, t["id"])
    assert summary["total_units"] == 12
    assert summary["by_module"][0]["module"] == "vision"
    gallery = gallery_usage_summary(conn, t["id"], g["id"])
    assert gallery["total_units"] == 12


def test_operator_usage_aggregates_tenants(conn):
    t1 = create_tenant(conn, name="A", shoot_type="wedding")
    t2 = create_tenant(conn, name="B", shoot_type="portrait")
    conn.commit()
    record_usage(conn, tenant_id=t1["id"], module="vision", backend="xai", units=3)
    record_usage(conn, tenant_id=t2["id"], module="album", backend="xai", units=1)
    conn.commit()
    op = operator_usage_summary(conn)
    assert op["total_units"] == 4
    assert len(op["top_tenants"]) == 2


class _LiveProvider:
    backend = "xai"

    def analyze(self, *, filename: str, data: bytes, style: str = ""):
        from hestia.vision import MockVisionProvider

        return MockVisionProvider().analyze(filename=filename, data=data, style=style)


def test_analyze_gallery_records_live_provider(conn, storage, settings):
    t = create_tenant(conn, name="XAI Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    for i in range(3):
        add_image(
            conn,
            storage,
            tenant_id=t["id"],
            gallery_id=g["id"],
            filename=f"{i}.jpg",
            fileobj=io.BytesIO(bytes([i])),
            content_type="image/jpeg",
        )
    conn.commit()
    analyze_gallery(
        conn, storage, settings, tenant_id=t["id"], gallery_id=g["id"], provider=_LiveProvider()
    )
    assert gallery_usage_summary(conn, t["id"], g["id"])["total_units"] == 3
