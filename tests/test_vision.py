"""Vision module — deterministic mock provider + gallery analysis."""

import io
import json

from hestia.galleries import add_image, create_gallery
from hestia.tenants import create_tenant
from hestia.vision import MockVisionProvider, analyze_gallery


def test_mock_provider_deterministic():
    p = MockVisionProvider()
    a = p.analyze(filename="img-001.jpg", data=b"anything")
    b = p.analyze(filename="img-001.jpg", data=b"different-bytes")
    assert a.as_dict() == b.as_dict()  # keyed on filename, stable
    assert a.keywords
    assert 0.0 <= a.keeper_score <= 1.0
    assert 0.0 <= a.hero_potential <= 1.0


def _seed_gallery(conn, storage, n=6):
    tenant = create_tenant(conn, name="Vision Studio", shoot_type="wedding")
    gallery = create_gallery(conn, tenant_id=tenant["id"], title="Test Gallery")
    for i in range(n):
        add_image(conn, storage, tenant_id=tenant["id"], gallery_id=gallery["id"],
                  filename=f"frame-{i}.jpg", fileobj=io.BytesIO(bytes([i]) * 32))
    conn.commit()
    return tenant, gallery


def test_analyze_gallery_persists_and_summarizes(conn, storage, settings):
    tenant, gallery = _seed_gallery(conn, storage, n=6)
    summary = analyze_gallery(conn, storage, settings, tenant_id=tenant["id"],
                              gallery_id=gallery["id"], hero_count=3)
    assert summary["analyzed"] == 6
    assert summary["image_count"] == 6
    assert len(summary["hero_image_ids"]) == 3       # capped at hero_count
    assert summary["keywords"]                        # non-empty cloud
    rows = conn.execute("SELECT COUNT(*) AS n FROM image_analyses").fetchone()["n"]
    assert rows == 6
    # persisted keywords are valid JSON arrays
    one = conn.execute("SELECT keywords_json FROM image_analyses LIMIT 1").fetchone()
    assert isinstance(json.loads(one["keywords_json"]), list)


def test_reanalyze_is_idempotent_upsert(conn, storage, settings):
    tenant, gallery = _seed_gallery(conn, storage, n=4)
    analyze_gallery(conn, storage, settings, tenant_id=tenant["id"], gallery_id=gallery["id"])
    analyze_gallery(conn, storage, settings, tenant_id=tenant["id"], gallery_id=gallery["id"])
    rows = conn.execute("SELECT COUNT(*) AS n FROM image_analyses").fetchone()["n"]
    assert rows == 4  # upsert, not duplicate
