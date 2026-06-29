"""Album designer — the owner overrides which frame leads each spread (their pick over the
AI's auto-chosen hero). Tenant-scoped; the chosen frame must belong to that spread.
"""

import io

from conftest import login_owner, onboard_studio

from hestia.albums import generate_album, get_album, set_spread_hero
from hestia.galleries import add_image, create_gallery
from hestia.tenants import create_tenant


def _img(conn, storage, t_id, g_id, name, data=b"jpg"):
    return add_image(conn, storage, tenant_id=t_id, gallery_id=g_id, filename=name,
                     fileobj=io.BytesIO(data), content_type="image/jpeg")


def _album(conn, storage, settings, tenant, *, n=8):
    g = create_gallery(conn, tenant_id=tenant["id"], title="Wedding")
    for i in range(n):
        _img(conn, storage, tenant["id"], g["id"], f"f{i}.jpg", data=bytes([i + 1]) * 20)
    conn.commit()
    return g, generate_album(conn, settings, tenant=tenant, gallery=g)


def test_set_spread_hero(conn, storage, settings):
    t = create_tenant(conn, name="Spread Studio", shoot_type="wedding")
    g, album = _album(conn, storage, settings, t, n=8)          # 8 photos → 2 spreads of 4
    sp1, sp2 = album["spreads"][0], album["spreads"][1]
    new_hero = sp1["photo_ids"][2]
    assert set_spread_hero(conn, t["id"], album["id"], sp1["position"], new_hero) is True
    assert get_album(conn, t["id"], album["id"])["spreads"][0]["hero_image_id"] == new_hero
    # a frame from spread 2 can't lead spread 1
    assert set_spread_hero(conn, t["id"], album["id"], sp1["position"], sp2["photo_ids"][0]) is False
    assert get_album(conn, t["id"], album["id"])["spreads"][0]["hero_image_id"] == new_hero  # unchanged
    assert set_spread_hero(conn, t["id"], album["id"], 999, new_hero) is False   # bad position
    other = create_tenant(conn, name="Other", shoot_type="portrait")
    assert set_spread_hero(conn, other["id"], album["id"], sp1["position"], new_hero) is False  # scoped


def test_owner_set_spread_hero_route(client, conn, storage, settings):
    creds = onboard_studio(client, email="spread@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    tenant = dict(conn.execute("SELECT * FROM tenants WHERE id = ?", (tid,)).fetchone())
    g, album = _album(conn, storage, settings, tenant, n=4)
    sp = album["spreads"][0]
    new_hero = sp["photo_ids"][1]
    assert new_hero != sp["hero_image_id"]
    client.post(f"/albums/{album['id']}/spreads/{sp['position']}/hero/{new_hero}")
    assert get_album(conn, tid, album["id"])["spreads"][0]["hero_image_id"] == new_hero


def test_owner_cannot_edit_foreign_album(client, conn, storage, settings):
    creds = onboard_studio(client, email="a@spread.test", name="A")
    login_owner(client, creds)
    tb = create_tenant(conn, name="B", shoot_type="portrait")
    g, album = _album(conn, storage, settings, tb, n=4)
    sp = album["spreads"][0]
    before = sp["hero_image_id"]
    client.post(f"/albums/{album['id']}/spreads/{sp['position']}/hero/{sp['photo_ids'][1]}")
    assert get_album(conn, tb["id"], album["id"])["spreads"][0]["hero_image_id"] == before  # unchanged
