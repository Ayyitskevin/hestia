"""Offer curation — the proofing→sales bridge (favorites become a package)."""

import io

from hestia.db import connect
from hestia.galleries import add_image, create_gallery, publish_gallery
from hestia.proofing import toggle_favorite
from hestia.sales import create_or_update_offer, favorites_package
from hestia.tenants import create_tenant, tenant_flags


def _img(conn, storage, tenant_id, gallery_id, name):
    return add_image(conn, storage, tenant_id=tenant_id, gallery_id=gallery_id,
                     filename=name, fileobj=io.BytesIO(b"jpg"), content_type="image/jpeg")


def test_favorites_package_pricing():
    assert favorites_package(0) is None
    one = favorites_package(1)
    assert one["count"] == 1 and one["price"] == "$15" and "1 archival print" in one["name"]
    three = favorites_package(3)
    assert three["price_cents"] == 4500 and three["price"] == "$45" and "prints" in three["name"]


def _setup(app, *, favorite=0):
    conn = connect(app.state.settings.db_path)
    try:
        t = create_tenant(conn, name="Curate Studio", shoot_type="wedding")
        g = create_gallery(conn, tenant_id=t["id"], title="Wedding")
        imgs = [_img(conn, app.state.storage, t["id"], g["id"], f"{i}.jpg") for i in range(4)]
        publish_gallery(conn, t["id"], g["id"])
        vision = {"hero_image_ids": [imgs[0]["id"]], "keeper_count": 4}
        offer = create_or_update_offer(conn, tenant=dict(t), gallery=dict(g), run_id=None,
                                       vision_summary=vision, flags=tenant_flags(t))
        for i in range(favorite):
            toggle_favorite(conn, tenant_id=t["id"], gallery_id=g["id"], image_id=imgs[i]["id"])
        conn.commit()
        return t, g, offer
    finally:
        conn.close()


def test_offer_hides_favorites_when_none(client, app):
    t, g, offer = _setup(app, favorite=0)
    page = client.get(f"/s/{t['slug']}/{offer['token']}")
    assert page.status_code == 200
    assert "The photos you loved" not in page.text


def test_offer_shows_favorites_package(client, app):
    t, g, offer = _setup(app, favorite=2)
    page = client.get(f"/s/{t['slug']}/{offer['token']}")
    assert page.status_code == 200
    assert "The photos you loved" in page.text
    assert "Your Favorites — 2 archival prints" in page.text
    assert "$30" in page.text  # 2 × $15
