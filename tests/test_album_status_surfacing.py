"""Surface the album review status in the main flow — on the gallery hub and the galleries
list — so the studio sees at a glance which albums are in review, need changes, or are done.
"""

import io

from conftest import login_owner, onboard_studio

from hestia.albums import (
    album_status_for_gallery,
    approve_album,
    enable_album_review,
    generate_album,
    request_album_changes,
)
from hestia.galleries import add_image, create_gallery
from hestia.tenants import create_tenant


def _img(conn, storage, t_id, g_id, name, data=b"jpg"):
    return add_image(conn, storage, tenant_id=t_id, gallery_id=g_id, filename=name,
                     fileobj=io.BytesIO(data), content_type="image/jpeg")


def _seed_images(conn, storage, t_id, g_id, n=4):
    for i in range(n):
        _img(conn, storage, t_id, g_id, f"f{i}.jpg", data=bytes([i + 1]) * 20)
    conn.commit()


def test_album_status_for_gallery(conn, storage, settings):
    t = create_tenant(conn, name="Status", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="W")
    assert album_status_for_gallery(conn, t["id"], g["id"]) is None        # no album yet
    _seed_images(conn, storage, t["id"], g["id"])
    album = generate_album(conn, settings, tenant=t, gallery=g)
    assert album_status_for_gallery(conn, t["id"], g["id"]) == "draft"     # built, not shared
    tok = enable_album_review(conn, t["id"], album["id"])
    conn.commit()
    assert album_status_for_gallery(conn, t["id"], g["id"]) == "review"    # shared, awaiting
    request_album_changes(conn, tok, "brighter please")
    conn.commit()
    assert album_status_for_gallery(conn, t["id"], g["id"]) == "changes"   # client asked
    approve_album(conn, tok)
    conn.commit()
    assert album_status_for_gallery(conn, t["id"], g["id"]) == "approved"  # signed off (overrides)


def test_status_shown_on_hub_and_list(client, conn, storage, settings):
    creds = onboard_studio(client, email="hub@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    tenant = dict(conn.execute("SELECT * FROM tenants WHERE id = ?", (tid,)).fetchone())
    g = create_gallery(conn, tenant_id=tid, title="Wedding")
    _seed_images(conn, storage, tid, g["id"])
    album = generate_album(conn, settings, tenant=tenant, gallery=g)
    tok = enable_album_review(conn, tid, album["id"])
    request_album_changes(conn, tok, "brighter please")
    conn.commit()
    assert "changes requested" in client.get(f"/galleries/{g['id']}").text   # gallery hub pill
    assert "📖 changes" in client.get("/galleries").text                     # galleries list badge
    approve_album(conn, tok)
    conn.commit()
    assert "approved ✓" in client.get(f"/galleries/{g['id']}").text
    assert "📖 ✓" in client.get("/galleries").text
