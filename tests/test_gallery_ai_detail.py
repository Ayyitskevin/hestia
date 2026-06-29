"""Owner gallery — per-frame AI detail (what the AI saw), linked to the Library.

The vision pass tags each frame with keywords, a shot type and a keeper score. This surfaces
them under each thumbnail on the owner's gallery view, with the tags linking back to the
catalog-wide Library search.
"""

import io
import json

from conftest import login_owner, onboard_studio

from hestia.galleries import add_image, create_gallery
from hestia.tenants import create_tenant
from hestia.vision import gallery_analysis_map


def _img(conn, storage, t_id, g_id, name, data=b"x" * 16):
    return add_image(conn, storage, tenant_id=t_id, gallery_id=g_id,
                     filename=name, fileobj=io.BytesIO(data))


def _analyze(conn, t_id, g_id, image_id, keywords, *, shot="candid", keeper=0.8):
    conn.execute(
        "INSERT INTO image_analyses (image_id, gallery_id, tenant_id, keywords_json, "
        "keeper_score, hero_potential, shot_type, alt_text) VALUES (?, ?, ?, ?, ?, 0.5, ?, '')",
        (image_id, g_id, t_id, json.dumps(keywords), keeper, shot),
    )


def test_gallery_analysis_map(conn, storage):
    t = create_tenant(conn, name="AI Detail", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    a = _img(conn, storage, t["id"], g["id"], "a.jpg")
    b = _img(conn, storage, t["id"], g["id"], "b.jpg")
    _analyze(conn, t["id"], g["id"], a["id"], ["candid", "golden-hour"], shot="portrait", keeper=0.9)
    _analyze(conn, t["id"], g["id"], b["id"], ["detail"], shot="detail", keeper=0.5)
    conn.commit()
    m = gallery_analysis_map(conn, g["id"])
    assert m[a["id"]]["shot_type"] == "portrait"
    assert m[a["id"]]["keywords"] == ["candid", "golden-hour"]
    assert m[a["id"]]["keeper"] is True                       # 0.9 ≥ keeper threshold
    assert m[b["id"]]["keeper"] is False                      # 0.5 below it
    assert _img(conn, storage, t["id"], g["id"], "c.jpg")["id"] not in m   # unanalyzed → absent


def test_owner_gallery_shows_ai_detail_with_library_links(client, conn, storage):
    creds = onboard_studio(client, email="detail@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    g = create_gallery(conn, tenant_id=tid, title="Wedding")
    a = _img(conn, storage, tid, g["id"], "a.jpg")
    _analyze(conn, tid, g["id"], a["id"], ["golden-hour"], shot="portrait", keeper=0.9)
    conn.commit()
    page = client.get(f"/galleries/{g['id']}").text
    assert 'href="/library?shot=portrait"' in page            # shot type links to the Library
    assert 'href="/library?q=golden-hour"' in page            # keyword links to the Library
    assert "★" in page                                         # strong-keeper marker
