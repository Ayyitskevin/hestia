"""Client album review + approval — share the AI-arranged album via an unguessable link,
let the client page through the spreads and approve, surface the approval to the owner.
"""

import io

from conftest import login_owner, onboard_studio

from hestia.albums import (
    approve_album,
    enable_album_review,
    generate_album,
    get_album_by_review_token,
)
from hestia.galleries import add_image, create_gallery, list_images, set_image_hidden
from hestia.tenants import create_tenant


def _img(conn, storage, t_id, g_id, name, data=b"jpg-bytes"):
    return add_image(conn, storage, tenant_id=t_id, gallery_id=g_id, filename=name,
                     fileobj=io.BytesIO(data), content_type="image/jpeg")


def _gallery_with_album(conn, storage, settings, *, n=5, tenant_name="Album Studio"):
    t = create_tenant(conn, name=tenant_name, shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="Wedding")
    for i in range(n):
        _img(conn, storage, t["id"], g["id"], f"f{i}.jpg", data=bytes([i + 1]) * 20)
    conn.commit()
    album = generate_album(conn, settings, tenant=t, gallery=g)
    return t, g, album


# ── module logic ──────────────────────────────────────────────────────────────


def test_enable_review_idempotent(conn, storage, settings):
    t, g, album = _gallery_with_album(conn, storage, settings)
    tok = enable_album_review(conn, t["id"], album["id"])
    assert tok
    assert enable_album_review(conn, t["id"], album["id"]) == tok        # idempotent, same token
    assert get_album_by_review_token(conn, tok)["id"] == album["id"]
    assert enable_album_review(conn, t["id"], 999999) is None            # missing album
    assert get_album_by_review_token(conn, "") is None


def test_approve_is_one_way(conn, storage, settings):
    t, g, album = _gallery_with_album(conn, storage, settings)
    tok = enable_album_review(conn, t["id"], album["id"])
    assert approve_album(conn, tok) is True
    assert approve_album(conn, tok) is False                             # already approved → no-op
    row = conn.execute("SELECT approved_at FROM albums WHERE id = ?", (album["id"],)).fetchone()
    assert row["approved_at"]                                            # stamped exactly once
    assert approve_album(conn, "nope") is False


# ── owner share ───────────────────────────────────────────────────────────────


def _owner_album(client, conn, storage, settings, email):
    creds = onboard_studio(client, email=email)
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    tenant = dict(conn.execute("SELECT * FROM tenants WHERE id = ?", (tid,)).fetchone())
    g = create_gallery(conn, tenant_id=tid, title="Wedding")
    for i in range(4):
        _img(conn, storage, tid, g["id"], f"f{i}.jpg", data=bytes([i + 1]) * 20)
    conn.commit()
    album = generate_album(conn, settings, tenant=tenant, gallery=g)
    return tid, g, album


def test_owner_shares_and_sees_link(client, conn, storage, settings):
    tid, g, album = _owner_album(client, conn, storage, settings, "share@studio.test")
    assert "Share for review" in client.get(f"/albums/{album['id']}").text   # before sharing
    client.post(f"/albums/{album['id']}/share")
    tok = conn.execute("SELECT review_token FROM albums WHERE id = ?",
                       (album["id"],)).fetchone()["review_token"]
    assert tok
    page = client.get(f"/albums/{album['id']}").text
    assert "Client review" in page and f"/a/{tok}" in page              # the review link is shown


# ── public review + approval ──────────────────────────────────────────────────


def test_public_review_page_and_photo(client, conn, storage, settings):
    t, g, album = _gallery_with_album(conn, storage, settings)
    tok = enable_album_review(conn, t["id"], album["id"])
    conn.commit()
    page = client.get(f"/a/{tok}")
    assert page.status_code == 200
    assert "Approve this album" in page.text and "Spread 1" in page.text
    img_id = conn.execute("SELECT id FROM images WHERE gallery_id = ? LIMIT 1",
                          (g["id"],)).fetchone()["id"]
    r = client.get(f"/a/{tok}/photo/{img_id}/view")
    assert r.status_code == 200 and "content-disposition" not in r.headers   # inline
    assert client.get("/a/nope").status_code == 404


def test_public_photo_is_album_scoped(client, conn, storage, settings):
    t, g, album = _gallery_with_album(conn, storage, settings)
    tok = enable_album_review(conn, t["id"], album["id"])
    g2 = create_gallery(conn, tenant_id=t["id"], title="Other")
    secret = _img(conn, storage, t["id"], g2["id"], "secret.jpg", data=b"SECRET")
    conn.commit()
    # the album token must not serve another gallery's frame
    assert client.get(f"/a/{tok}/photo/{secret['id']}/view").status_code == 404


def test_public_approve_flow(client, conn, storage, settings):
    t, g, album = _gallery_with_album(conn, storage, settings)
    tok = enable_album_review(conn, t["id"], album["id"])
    conn.commit()
    assert "Approve this album" in client.get(f"/a/{tok}").text
    client.post(f"/a/{tok}/approve")
    assert conn.execute("SELECT approved_at FROM albums WHERE id = ?",
                        (album["id"],)).fetchone()["approved_at"]
    after = client.get(f"/a/{tok}").text
    assert "Approved" in after and "Approve this album" not in after    # one-way: button gone


# ── the review token must never serve a culled (hidden) frame ────────────────────


def test_culled_frame_after_generate_is_not_served(client, conn, storage, settings):
    t, g, album = _gallery_with_album(conn, storage, settings)
    tok = enable_album_review(conn, t["id"], album["id"])
    victim = list_images(conn, g["id"])[0]["id"]
    set_image_hidden(conn, t["id"], victim, True)          # cull AFTER the album was generated
    conn.commit()
    # the now-hidden frame's bytes must not be served through the review token...
    assert client.get(f"/a/{tok}/photo/{victim}/view").status_code == 404
    # ...but the review page still loads (the culled frame is just dropped from its spread)
    assert client.get(f"/a/{tok}").status_code == 200


def test_generated_album_excludes_culled_frames(conn, storage, settings):
    t = create_tenant(conn, name="Cull Album", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="Wedding")
    for i in range(4):
        _img(conn, storage, t["id"], g["id"], f"f{i}.jpg", data=bytes([i + 1]) * 20)
    conn.commit()
    victim = list_images(conn, g["id"])[0]["id"]
    set_image_hidden(conn, t["id"], victim, True)          # cull BEFORE generating
    conn.commit()
    album = generate_album(conn, settings, tenant=t, gallery=g)
    placed = {iid for sp in album["spreads"] for iid in sp["photo_ids"]}
    assert victim not in placed                            # a culled frame is never arranged
