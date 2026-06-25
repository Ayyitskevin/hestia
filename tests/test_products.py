"""Product photography — presets, variant generation, idempotency, isolation."""

import dataclasses
import io

import pytest
from conftest import login_owner, onboard_studio

from hestia.galleries import add_image, create_gallery
from hestia.products import (
    PRESETS,
    MockRenderer,
    XaiRenderer,
    build_renderer,
    generate_product_set,
    get_set_for_gallery,
)
from hestia.tenants import create_tenant


def test_presets_shape():
    keys = {p["key"] for p in PRESETS}
    assert {"catalog_square", "transparent_cutout", "hero_wide"} <= keys
    for p in PRESETS:
        assert p["width"] and p["height"] and p["format"]


def test_build_renderer_selection(settings):
    assert isinstance(build_renderer(settings), MockRenderer)
    assert isinstance(build_renderer(dataclasses.replace(settings, product_backend="xai")), XaiRenderer)


def test_mock_renderer_plans_only():
    r = MockRenderer().render(image={"storage_key": "t/1/2.jpg"}, preset=PRESETS[0])
    assert r["status"] == "planned"
    assert r["output_ref"] == "t/1/2.jpg"  # references the source, fabricates nothing


def _gallery(conn, storage, n=3):
    t = create_tenant(conn, name="Shop", shoot_type="commercial")
    g = create_gallery(conn, tenant_id=t["id"], title="Products")
    for i in range(n):
        add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"],
                  filename=f"p{i}.jpg", fileobj=io.BytesIO(bytes([i]) * 16))
    conn.commit()
    return t, g


def test_generate_full_matrix(conn, storage, settings):
    t, g = _gallery(conn, storage, n=3)
    pset = generate_product_set(conn, settings, tenant=t, gallery=g)
    assert pset["variant_count"] == 3 * len(PRESETS)
    v = pset["variants"][0]
    assert {"image_id", "preset", "width", "height", "format", "status"} <= set(v)


def test_generate_subset_of_presets(conn, storage, settings):
    t, g = _gallery(conn, storage, n=2)
    pset = generate_product_set(conn, settings, tenant=t, gallery=g,
                                preset_keys=["catalog_square", "transparent_cutout"])
    assert pset["variant_count"] == 2 * 2


def test_idempotent_per_gallery(conn, storage, settings):
    t, g = _gallery(conn, storage, n=2)
    a = generate_product_set(conn, settings, tenant=t, gallery=g)
    b = generate_product_set(conn, settings, tenant=t, gallery=g)
    assert a["id"] == b["id"]
    assert conn.execute("SELECT COUNT(*) AS n FROM product_sets").fetchone()["n"] == 1


def test_empty_gallery_raises(conn, storage, settings):
    t = create_tenant(conn, name="Empty", shoot_type="commercial")
    g = create_gallery(conn, tenant_id=t["id"], title="Nothing")
    conn.commit()
    with pytest.raises(ValueError):
        generate_product_set(conn, settings, tenant=t, gallery=g)


def test_tenant_isolation(conn, storage, settings):
    t1, g1 = _gallery(conn, storage, n=2)
    generate_product_set(conn, settings, tenant=t1, gallery=g1)
    t2 = create_tenant(conn, name="Other", shoot_type="commercial")
    conn.commit()
    assert get_set_for_gallery(conn, t2["id"], g1["id"]) is None


def test_http_generate_and_view(client):
    creds = onboard_studio(client, shoot_type="commercial", email="shop@example.com")
    login_owner(client, creds)
    gid = client.post("/galleries", data={"title": "Catalog"}).url.path.rstrip("/").split("/")[-1]
    client.post(f"/galleries/{gid}/images",
                files=[("files", (f"p{i}.jpg", bytes([i]) * 32, "image/jpeg")) for i in range(2)])
    r = client.post(f"/galleries/{gid}/products")
    assert "/products/" in str(r.url)
    page = client.get(str(r.url).replace("http://testserver", ""))
    assert page.status_code == 200 and "Catalog square" in page.text


def test_http_view_shows_rendered_thumbnails(client, conn, storage):
    # A mock set is "planned only"; once a variant is rendered (xai), the view
    # should surface the rendered pixels as a viewable thumbnail.
    creds = onboard_studio(client, shoot_type="commercial", email="shop2@example.com")
    login_owner(client, creds)
    gid = client.post("/galleries", data={"title": "Catalog"}).url.path.rstrip("/").split("/")[-1]
    client.post(f"/galleries/{gid}/images",
                files=[("files", ("p0.jpg", b"x" * 32, "image/jpeg"))])
    set_id = int(str(client.post(f"/galleries/{gid}/products").url).rstrip("/").split("/")[-1])

    # Simulate an xai render landing: flip the first variant to 'rendered'.
    import json
    row = conn.execute("SELECT variants_json FROM product_sets WHERE id = ?", (set_id,)).fetchone()
    variants = json.loads(row["variants_json"])
    variants[0] = {**variants[0], "status": "rendered", "output_ref": "rendered/hero.png"}
    conn.execute("UPDATE product_sets SET variants_json = ? WHERE id = ?",
                 (json.dumps(variants), set_id))
    conn.commit()

    page = client.get(f"/products/{set_id}")
    assert page.status_code == 200
    assert "1 rendered" in page.text
    assert storage.public_path("rendered/hero.png") in page.text  # /media/rendered/hero.png


def test_http_view_planned_has_no_thumbnail(client):
    # A pure mock set must not fabricate rendered thumbnails — planned only.
    creds = onboard_studio(client, shoot_type="commercial", email="shop3@example.com")
    login_owner(client, creds)
    gid = client.post("/galleries", data={"title": "Plan"}).url.path.rstrip("/").split("/")[-1]
    client.post(f"/galleries/{gid}/images",
                files=[("files", ("p0.jpg", b"x" * 32, "image/jpeg"))])
    set_id = int(str(client.post(f"/galleries/{gid}/products").url).rstrip("/").split("/")[-1])
    page = client.get(f"/products/{set_id}")
    assert page.status_code == 200
    assert "rendered" not in page.text  # no rendered-count badge
    assert "thumb-grid" not in page.text  # no fabricated thumbnails


_IMG = {"storage_key": "t/1/2.jpg", "filename": "p.jpg"}


def test_xai_renderer_falls_back_without_key(settings):
    r = XaiRenderer(settings).render(image=_IMG, preset=PRESETS[0], storage=None)
    assert r["status"] == "planned" and "no xai key" in r["note"]


def test_xai_renderer_falls_back_without_storage(settings):
    s = dataclasses.replace(settings, xai_api_key="test-key")
    # has a key, but no storage to read source / write output → still degrades safely
    r = XaiRenderer(s).render(image=_IMG, preset=PRESETS[0], storage=None)
    assert r["status"] == "planned"


def test_variants_include_output_ref(conn, storage, settings):
    t, g = _gallery(conn, storage, n=1)
    pset = generate_product_set(conn, settings, tenant=t, gallery=g, storage=storage)
    assert all("output_ref" in v for v in pset["variants"])
    assert pset["variants"][0]["output_ref"]  # mock → the source key
