"""Gallery engagement — view/download tracking and the owner's Engagement card."""

import io

from conftest import login_owner, onboard_studio

from hestia.db import connect
from hestia.delivery import enable_delivery
from hestia.galleries import (
    add_image,
    create_gallery,
    get_gallery,
    list_images,
    record_gallery_download,
    record_gallery_view,
)
from hestia.tenants import create_tenant


def _gallery(conn, *, title="Finals"):
    t = create_tenant(conn, name="Eng Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title=title)
    conn.commit()
    return t, g


def test_counters_default_zero_and_increment(conn):
    t, g = _gallery(conn)
    assert get_gallery(conn, t["id"], g["id"])["view_count"] == 0       # default for existing rows
    record_gallery_view(conn, g["id"])
    record_gallery_view(conn, g["id"])
    record_gallery_download(conn, g["id"])
    conn.commit()
    got = get_gallery(conn, t["id"], g["id"])
    assert got["view_count"] == 2 and got["download_count"] == 1 and got["last_viewed_at"]


def test_delivery_page_and_downloads_are_counted(client, conn, storage, app):
    t, g = _gallery(conn)
    add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"],
              filename="a.jpg", fileobj=io.BytesIO(b"AAA-original"))
    token = enable_delivery(conn, t["id"], g["id"])
    conn.commit()

    assert client.get(f"/d/{token}").status_code == 200                 # opening the page → 1 view
    img_id = list_images(conn, g["id"])[0]["id"]
    assert client.get(f"/d/{token}/{img_id}").status_code == 200        # file download → +1
    assert client.get(f"/d/{token}/all.zip").status_code == 200         # zip download → +1
    client.get(f"/d/{token}/{img_id}/view")                             # inline thumb → NOT counted

    fresh = connect(app.state.settings.db_path)
    try:
        row = fresh.execute("SELECT view_count, download_count, last_viewed_at "
                            "FROM galleries WHERE id = ?", (g["id"],)).fetchone()
    finally:
        fresh.close()
    assert row["view_count"] == 1                                       # the inline thumb didn't add a view
    assert row["download_count"] == 2                                   # file + zip
    assert row["last_viewed_at"]


def test_gallery_detail_shows_engagement(client, app):
    creds = onboard_studio(client, email="eng@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        g = create_gallery(conn, tenant_id=tid, title="Finals")
        conn.execute("UPDATE galleries SET view_count = 3, download_count = 1 WHERE id = ?", (g["id"],))
        conn.commit()
        gid = g["id"]
    finally:
        conn.close()
    page = client.get(f"/galleries/{gid}")
    assert page.status_code == 200
    assert "Engagement" in page.text and "views" in page.text and "downloads" in page.text


def test_galleries_list_shows_engagement_column(client, app):
    creds = onboard_studio(client, email="list@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        g = create_gallery(conn, tenant_id=tid, title="Opened")
        conn.execute("UPDATE galleries SET view_count = 4, download_count = 2 WHERE id = ?", (g["id"],))
        create_gallery(conn, tenant_id=tid, title="Untouched")
        conn.commit()
    finally:
        conn.close()
    page = client.get("/galleries")
    assert page.status_code == 200 and "Engagement" in page.text
    assert "👁 4 · ⬇ 2" in page.text                                  # the opened gallery's counts
