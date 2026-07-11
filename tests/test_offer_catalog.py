"""Per-tenant offer catalog — studio pricing on gallery offer links."""

from conftest import login_owner, onboard_studio

from hestia.features import flags_for
from hestia.galleries import create_gallery
from hestia.sales import (
    DEFAULT_CATALOG,
    build_bundles,
    create_or_update_offer,
    get_tenant_catalog,
    get_tenant_catalog_items,
    set_tenant_catalog,
)
from hestia.tenants import create_tenant

VISION = {"hero_image_ids": [1, 2], "keeper_count": 8, "keywords": ["candid"]}


def test_default_catalog_matches_legacy_prices():
    for _sku, item in DEFAULT_CATALOG.items():
        assert item["price_cents"] > 0
        assert item["enabled"] is True


def test_custom_catalog_changes_bundle_prices(conn):
    tenant = create_tenant(conn, name="Catalog Studio", shoot_type="wedding")
    set_tenant_catalog(
        conn,
        tenant["id"],
        items={
            "print_set": {
                "name": "Studio Prints",
                "blurb": "Custom blurb",
                "price_cents": 9900,
                "enabled": True,
            },
            "wall_art": DEFAULT_CATALOG["wall_art"],
            "album": DEFAULT_CATALOG["album"],
            "gift_box": {"name": "Gift", "blurb": "Gifts", "price_cents": 5000, "enabled": False},
        },
        favorite_print_cents=2000,
    )
    conn.commit()
    catalog = get_tenant_catalog_items(conn, tenant["id"])
    bundles = build_bundles(flags_for("wedding"), VISION, catalog=catalog)
    prices = {b["sku"]: b["price_cents"] for b in bundles}
    names = {b["sku"]: b["name"] for b in bundles}
    assert prices["print_set"] == 9900
    assert names["print_set"] == "Studio Prints"
    assert "gift_box" not in prices


def test_offer_uses_tenant_catalog(conn):
    tenant = create_tenant(conn, name="Offer Studio", shoot_type="wedding")
    gallery = create_gallery(conn, tenant_id=tenant["id"], title="G")
    set_tenant_catalog(
        conn,
        tenant["id"],
        items={
            **{sku: DEFAULT_CATALOG[sku] for sku in DEFAULT_CATALOG},
            "print_set": {**DEFAULT_CATALOG["print_set"], "price_cents": 15000},
        },
        favorite_print_cents=1500,
    )
    conn.commit()
    offer = create_or_update_offer(
        conn,
        tenant=dict(tenant),
        gallery=dict(gallery),
        run_id=None,
        vision_summary=VISION,
        flags=flags_for("wedding"),
    )
    print_bundle = next(b for b in offer["bundles"] if b["sku"] == "print_set")
    assert print_bundle["price_cents"] == 15000


def test_http_offer_catalog_settings(client, app):
    creds = onboard_studio(client, email="catalog@example.com")
    login_owner(client, creds)
    page = client.get("/settings/offers")
    assert page.status_code == 200
    assert "Print &amp; album offers" in page.text
    assert 'name="print_set_price"' in page.text
    saved = client.post(
        "/settings/offers",
        data={
            "favorite_print": "20",
            "print_set_name": "Fine Art Set",
            "print_set_blurb": "Ten prints",
            "print_set_price": "199",
            "print_set_enabled": "1",
            "wall_art_name": DEFAULT_CATALOG["wall_art"]["name"],
            "wall_art_blurb": DEFAULT_CATALOG["wall_art"]["blurb"],
            "wall_art_price": "220",
            "wall_art_enabled": "1",
            "album_name": DEFAULT_CATALOG["album"]["name"],
            "album_blurb": DEFAULT_CATALOG["album"]["blurb"],
            "album_price": "450",
            "album_enabled": "1",
            "gift_box_name": DEFAULT_CATALOG["gift_box"]["name"],
            "gift_box_blurb": DEFAULT_CATALOG["gift_box"]["blurb"],
            "gift_box_price": "80",
            "gift_box_enabled": "1",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert "saved=1" in saved.headers["location"]
    conn = __import__("hestia.db", fromlist=["connect"]).connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        catalog = get_tenant_catalog(conn, tid)
        assert catalog["favorite_print_cents"] == 2000
        assert catalog["items"]["print_set"]["name"] == "Fine Art Set"
        assert catalog["items"]["print_set"]["price_cents"] == 19900
    finally:
        conn.close()
