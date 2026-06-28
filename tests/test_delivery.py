"""Digital delivery — per-gallery download link for the high-res originals."""

import io
import zipfile

from conftest import login_owner, onboard_studio

from hestia.crm import assign_gallery_to_project, create_client, create_project
from hestia.db import connect
from hestia.delivery import (
    enable_delivery,
    get_gallery_by_delivery_token,
    regenerate_delivery_token,
    zip_gallery,
)
from hestia.email import list_emails
from hestia.galleries import add_image, create_gallery, list_images
from hestia.tenants import create_tenant


def _gallery(conn, *, title="Wedding Finals"):
    t = create_tenant(conn, name="Deliver Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title=title)
    conn.commit()
    return t, g


def _img(conn, storage, t, g, name, data):
    add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"],
              filename=name, fileobj=io.BytesIO(data))


# --- module logic -----------------------------------------------------------

def test_enable_idempotent_and_regenerate(conn):
    t, g = _gallery(conn)
    tok = enable_delivery(conn, t["id"], g["id"])
    assert tok and enable_delivery(conn, t["id"], g["id"]) == tok        # idempotent
    assert get_gallery_by_delivery_token(conn, tok)["id"] == g["id"]
    tok2 = regenerate_delivery_token(conn, t["id"], g["id"])
    assert tok2 and tok2 != tok
    assert get_gallery_by_delivery_token(conn, tok) is None              # old link revoked
    assert get_gallery_by_delivery_token(conn, tok2)["id"] == g["id"]
    assert enable_delivery(conn, t["id"], 999999) is None               # missing gallery
    assert get_gallery_by_delivery_token(conn, "") is None


def test_zip_gallery_bundles_originals(conn, storage):
    t, g = _gallery(conn)
    _img(conn, storage, t, g, "a.jpg", b"AAA-original")
    _img(conn, storage, t, g, "b.jpg", b"BBB-original")
    conn.commit()
    data = zip_gallery(storage, list_images(conn, g["id"]))
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert set(zf.namelist()) == {"a.jpg", "b.jpg"}
    assert zf.read("a.jpg") == b"AAA-original" and zf.read("b.jpg") == b"BBB-original"


def test_zip_disambiguates_duplicate_filenames(conn, storage):
    t, g = _gallery(conn)
    _img(conn, storage, t, g, "dup.jpg", b"FIRST")
    _img(conn, storage, t, g, "dup.jpg", b"SECOND")
    conn.commit()
    zf = zipfile.ZipFile(io.BytesIO(zip_gallery(storage, list_images(conn, g["id"]))))
    assert len(zf.namelist()) == 2 and len(set(zf.namelist())) == 2  # nothing clobbered


# --- public download flow ---------------------------------------------------

def test_public_download_individual_and_zip(client, conn, storage):
    t, g = _gallery(conn)
    _img(conn, storage, t, g, "a.jpg", b"AAA-original")
    _img(conn, storage, t, g, "b.jpg", b"BBB-original")
    token = enable_delivery(conn, t["id"], g["id"])
    conn.commit()
    imgs = list_images(conn, g["id"])

    page = client.get(f"/d/{token}")
    assert page.status_code == 200
    assert "Wedding Finals" in page.text and "Download all" in page.text
    assert "a.jpg" in page.text and "b.jpg" in page.text

    one = client.get(f"/d/{token}/{imgs[0]['id']}")
    assert one.status_code == 200
    assert one.headers["content-disposition"].startswith('attachment; filename="a.jpg"')
    assert one.content == b"AAA-original"   # the real original bytes, as a download

    z = client.get(f"/d/{token}/all.zip")
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    assert set(zf.namelist()) == {"a.jpg", "b.jpg"}


def test_token_cannot_reach_another_gallery(client, conn, storage):
    t, g1 = _gallery(conn, title="G1")
    g2 = create_gallery(conn, tenant_id=t["id"], title="G2")
    conn.commit()
    _img(conn, storage, t, g2, "secret.jpg", b"SECRET")
    token1 = enable_delivery(conn, t["id"], g1["id"])
    conn.commit()
    secret = list_images(conn, g2["id"])[0]
    # g1's delivery token must not serve g2's image
    assert client.get(f"/d/{token1}/{secret['id']}").status_code == 404


def test_bad_token_is_404(client):
    assert client.get("/d/nope").status_code == 404
    assert client.get("/d/nope/all.zip").status_code == 404
    assert client.get("/d/nope/1").status_code == 404


def test_owner_enables_and_sees_link(client):
    creds = onboard_studio(client, email="deliver@example.com")
    login_owner(client, creds)
    created = client.post("/galleries", data={"title": "Finals"})
    gid = created.url.path.rstrip("/").split("/")[-1]
    client.post(f"/galleries/{gid}/delivery")
    page = client.get(f"/galleries/{gid}")
    assert "Digital delivery" in page.text and "/d/" in page.text


# --- polish: inline thumbnails + auto-email on first enable ------------------

def test_delivery_view_serves_inline(client, conn, storage):
    t, g = _gallery(conn)
    _img(conn, storage, t, g, "a.jpg", b"AAA-original")
    token = enable_delivery(conn, t["id"], g["id"])
    conn.commit()
    img = list_images(conn, g["id"])[0]
    r = client.get(f"/d/{token}/{img['id']}/view")
    assert r.status_code == 200
    assert "content-disposition" not in r.headers   # inline (a thumbnail), not a download
    assert r.content == b"AAA-original"


def test_delivery_view_is_gallery_scoped(client, conn, storage):
    t, g1 = _gallery(conn, title="G1")
    g2 = create_gallery(conn, tenant_id=t["id"], title="G2")
    conn.commit()
    _img(conn, storage, t, g2, "secret.jpg", b"SECRET")
    token1 = enable_delivery(conn, t["id"], g1["id"])
    conn.commit()
    secret = list_images(conn, g2["id"])[0]
    assert client.get(f"/d/{token1}/{secret['id']}/view").status_code == 404


def test_intl_filename_downloads_without_crashing(client, conn, storage):
    # CJK/Cyrillic/emoji filenames are routine for real clients; a latin-1-only
    # Content-Disposition would 500 on them. RFC 5987 filename* fixes it.
    t, g = _gallery(conn)
    _img(conn, storage, t, g, "写真.jpg", b"JPEGDATA")
    token = enable_delivery(conn, t["id"], g["id"])
    conn.commit()
    img = list_images(conn, g["id"])[0]
    r = client.get(f"/d/{token}/{img['id']}")
    assert r.status_code == 200 and r.content == b"JPEGDATA"
    assert "filename*=UTF-8''" in r.headers["content-disposition"]


def test_inline_view_clamps_unsafe_content_type(client, conn, storage):
    # A stored "image" declared text/html must NOT be served as renderable HTML on
    # our origin (stored XSS) — the inline route clamps it to an opaque download type.
    t, g = _gallery(conn)
    add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"], filename="evil.html",
              fileobj=io.BytesIO(b"<script>alert(1)</script>"), content_type="text/html")
    token = enable_delivery(conn, t["id"], g["id"])
    conn.commit()
    img = list_images(conn, g["id"])[0]
    r = client.get(f"/d/{token}/{img['id']}/view")
    assert r.status_code == 200
    assert r.headers["content-type"].split(";")[0] == "application/octet-stream"


def test_enable_emails_client_once_on_first_enable(client, app):
    creds = onboard_studio(client, email="owner@studio.test")
    login_owner(client, creds)
    db = app.state.settings.db_path
    conn = connect(db)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        c = create_client(conn, tenant_id=tid, name="Pat", email="pat@example.com")
        p = create_project(conn, tenant_id=tid, name="Wedding", client_id=c["id"],
                           shoot_type="wedding", status="booked")
        g = create_gallery(conn, tenant_id=tid, title="Finals")
        assign_gallery_to_project(conn, tid, g["id"], p["id"])
        conn.commit()
        gid = g["id"]
    finally:
        conn.close()

    client.post(f"/galleries/{gid}/delivery")   # first enable → emails the client
    conn = connect(db)
    try:
        sent = [e for e in list_emails(conn, tid) if e["to_addr"] == "pat@example.com"]
    finally:
        conn.close()
    assert len(sent) == 1 and "/d/" in sent[0]["body"]

    client.post(f"/galleries/{gid}/delivery")   # re-enable is idempotent → no second email
    conn = connect(db)
    try:
        again = [e for e in list_emails(conn, tid) if e["to_addr"] == "pat@example.com"]
    finally:
        conn.close()
    assert len(again) == 1


def test_favorites_zip_downloads_only_hearted(client, conn, storage):
    from hestia.proofing import toggle_favorite
    t, g = _gallery(conn)
    _img(conn, storage, t, g, "a.jpg", b"AAA")
    _img(conn, storage, t, g, "b.jpg", b"BBB")
    _img(conn, storage, t, g, "c.jpg", b"CCC")
    imgs = list_images(conn, g["id"])
    toggle_favorite(conn, tenant_id=t["id"], gallery_id=g["id"], image_id=imgs[0]["id"])
    toggle_favorite(conn, tenant_id=t["id"], gallery_id=g["id"], image_id=imgs[2]["id"])
    token = enable_delivery(conn, t["id"], g["id"])
    conn.commit()

    page = client.get(f"/d/{token}").text
    assert "Download my favorites (2)" in page                  # the new CTA
    z = client.get(f"/d/{token}/favorites.zip")
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    assert set(zf.namelist()) == {"a.jpg", "c.jpg"}             # only the hearted frames


def test_favorites_zip_absent_when_none_hearted(client, conn, storage):
    t, g = _gallery(conn)
    _img(conn, storage, t, g, "a.jpg", b"AAA")
    token = enable_delivery(conn, t["id"], g["id"])
    conn.commit()
    assert "Download my favorites" not in client.get(f"/d/{token}").text
    assert client.get(f"/d/{token}/favorites.zip").status_code == 404   # nothing to zip
    assert client.get("/d/nope/favorites.zip").status_code == 404       # bad token
