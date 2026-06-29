"""Album approval → notify the owner (fires an automation) and lock the layout, so an
approved album can't change out from under the client.
"""

import io

from conftest import login_owner, onboard_studio

from hestia.albums import (
    approve_album,
    enable_album_review,
    generate_album,
    get_album,
    set_spread_hero,
)
from hestia.automations import TRIGGERS, create_automation
from hestia.galleries import add_image, create_gallery
from hestia.tenants import create_tenant


def _img(conn, storage, t_id, g_id, name, data=b"jpg"):
    return add_image(conn, storage, tenant_id=t_id, gallery_id=g_id, filename=name,
                     fileobj=io.BytesIO(data), content_type="image/jpeg")


def _album(conn, storage, settings, tenant, *, n=4):
    g = create_gallery(conn, tenant_id=tenant["id"], title="Wedding")
    for i in range(n):
        _img(conn, storage, tenant["id"], g["id"], f"f{i}.jpg", data=bytes([i + 1]) * 20)
    conn.commit()
    return g, generate_album(conn, settings, tenant=tenant, gallery=g)


def _automation_jobs(conn, tenant_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE tenant_id = ? AND kind = 'automation.run'",
        (tenant_id,),
    ).fetchone()["n"]


def test_album_approved_is_a_valid_trigger():
    assert "album.approved" in TRIGGERS                    # selectable in the automations UI


def test_approval_fires_automation_once(conn, storage, settings):
    t = create_tenant(conn, name="Notify Studio", shoot_type="wedding")
    g, album = _album(conn, storage, settings, t)
    create_automation(conn, tenant_id=t["id"], name="Tell me", trigger="album.approved",
                      subject="Album approved", body="The client approved {title}.")
    tok = enable_album_review(conn, t["id"], album["id"])
    conn.commit()
    assert approve_album(conn, tok) is True
    assert _automation_jobs(conn, t["id"]) == 1            # approval enqueued the automation
    assert approve_album(conn, tok) is False               # second approval is a no-op...
    assert _automation_jobs(conn, t["id"]) == 1            # ...and fires nothing more


def test_approval_locks_layout(conn, storage, settings):
    t = create_tenant(conn, name="Lock Studio", shoot_type="wedding")
    g, album = _album(conn, storage, settings, t, n=4)
    sp = album["spreads"][0]
    custom = sp["photo_ids"][2]                            # an owner override the AI wouldn't pick
    assert set_spread_hero(conn, t["id"], album["id"], sp["position"], custom) is True
    tok = enable_album_review(conn, t["id"], album["id"])
    assert approve_album(conn, tok) is True
    conn.commit()
    # regenerating is now a no-op — the approved layout (custom hero) survives unchanged
    regen = generate_album(conn, settings, tenant=t, gallery=g)
    assert regen["id"] == album["id"]
    assert regen["spreads"][0]["hero_image_id"] == custom
    # and editing the spread hero is locked too
    assert set_spread_hero(conn, t["id"], album["id"], sp["position"], sp["photo_ids"][0]) is False
    assert get_album(conn, t["id"], album["id"])["spreads"][0]["hero_image_id"] == custom


def test_approved_album_page_hides_edit_controls(client, conn, storage, settings):
    creds = onboard_studio(client, email="lock@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    tenant = dict(conn.execute("SELECT * FROM tenants WHERE id = ?", (tid,)).fetchone())
    g, album = _album(conn, storage, settings, tenant, n=4)
    tok = enable_album_review(conn, tid, album["id"])
    approve_album(conn, tok)
    conn.commit()
    page = client.get(f"/albums/{album['id']}").text
    assert "locked" in page
    assert "Regenerate" not in page and "Make hero" not in page
