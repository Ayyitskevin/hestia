"""Sales module — bundle curation + idempotent offer minting."""

from hestia.features import flags_for
from hestia.galleries import create_gallery
from hestia.sales import build_bundles, create_or_update_offer, get_offer_by_token
from hestia.tenants import create_tenant

VISION = {"hero_image_ids": [1, 2, 3], "keeper_count": 12, "keywords": ["candid"]}


def test_album_bundle_gated_by_shoot_type():
    wedding = build_bundles(flags_for("wedding"), VISION)
    commercial = build_bundles(flags_for("commercial"), VISION)
    assert any(b["sku"] == "album" for b in wedding)
    assert not any(b["sku"] == "album" for b in commercial)
    # prints + wall art always present
    for bundles in (wedding, commercial):
        skus = {b["sku"] for b in bundles}
        assert {"print_set", "wall_art", "gift_box"} <= skus


def test_bundles_have_prices():
    for b in build_bundles(flags_for("portrait"), VISION):
        assert b["price_cents"] > 0
        assert b["price"].startswith("$")


def _seed(conn):
    tenant = create_tenant(conn, name="Sales Studio", shoot_type="wedding")
    gallery = create_gallery(conn, tenant_id=tenant["id"], title="G1")
    conn.commit()
    return tenant, gallery


def test_offer_is_idempotent_same_token(conn):
    tenant, gallery = _seed(conn)
    flags = flags_for("wedding")
    o1 = create_or_update_offer(conn, tenant=tenant, gallery=gallery, run_id=None,
                                vision_summary=VISION, flags=flags)
    o2 = create_or_update_offer(conn, tenant=tenant, gallery=gallery, run_id=None,
                                vision_summary=VISION, flags=flags)
    assert o1["token"] == o2["token"]                       # token reused
    n = conn.execute("SELECT COUNT(*) AS n FROM offers").fetchone()["n"]
    assert n == 1                                           # exactly one offer
    assert get_offer_by_token(conn, o1["token"])["gallery_id"] == gallery["id"]


def test_offer_total_matches_bundles(conn):
    tenant, gallery = _seed(conn)
    offer = create_or_update_offer(conn, tenant=tenant, gallery=gallery, run_id=None,
                                   vision_summary=VISION, flags=flags_for("wedding"))
    assert offer["total_cents"] == sum(b["price_cents"] for b in offer["bundles"])
