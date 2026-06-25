"""Gallery proofing — favorites toggle, comments, scoping, and the client flow."""

import io

from hestia.db import connect
from hestia.galleries import add_image, create_gallery, publish_gallery
from hestia.proofing import (
    add_comment,
    comments_by_image,
    comments_for_gallery,
    favorite_count,
    favorite_image_ids,
    image_in_gallery,
    list_favorites,
    toggle_favorite,
)
from hestia.tenants import create_tenant


def _tenant(conn, name="Proof Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def _img(conn, storage, tenant_id, gallery_id, name="frame.jpg"):
    return add_image(conn, storage, tenant_id=tenant_id, gallery_id=gallery_id,
                     filename=name, fileobj=io.BytesIO(b"jpegbytes"), content_type="image/jpeg")


def test_toggle_favorite_idempotent(conn, storage):
    t = _tenant(conn)
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    img = _img(conn, storage, t["id"], g["id"])
    assert toggle_favorite(conn, tenant_id=t["id"], gallery_id=g["id"], image_id=img["id"]) is True
    assert favorite_image_ids(conn, g["id"]) == {img["id"]}
    # toggling again removes it
    assert toggle_favorite(conn, tenant_id=t["id"], gallery_id=g["id"], image_id=img["id"]) is False
    assert favorite_image_ids(conn, g["id"]) == set()
    # and back on
    assert toggle_favorite(conn, tenant_id=t["id"], gallery_id=g["id"], image_id=img["id"]) is True
    assert favorite_count(conn, g["id"]) == 1


def test_favorite_rejects_foreign_image(conn, storage):
    t = _tenant(conn)
    g1 = create_gallery(conn, tenant_id=t["id"], title="G1")
    g2 = create_gallery(conn, tenant_id=t["id"], title="G2")
    img2 = _img(conn, storage, t["id"], g2["id"])
    # an image from g2 cannot be favorited under g1
    assert toggle_favorite(conn, tenant_id=t["id"], gallery_id=g1["id"], image_id=img2["id"]) is None
    assert favorite_count(conn, g1["id"]) == 0
    assert image_in_gallery(conn, t["id"], g2["id"], img2["id"]) is True
    assert image_in_gallery(conn, t["id"], g1["id"], img2["id"]) is False


def test_list_favorites_for_owner(conn, storage):
    t = _tenant(conn)
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    a = _img(conn, storage, t["id"], g["id"], "a.jpg")
    _img(conn, storage, t["id"], g["id"], "b.jpg")
    toggle_favorite(conn, tenant_id=t["id"], gallery_id=g["id"], image_id=a["id"])
    favs = list_favorites(conn, t["id"], g["id"])
    assert [f["filename"] for f in favs] == ["a.jpg"]


def test_add_comment_validation(conn, storage):
    t = _tenant(conn)
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    img = _img(conn, storage, t["id"], g["id"])
    # empty body → None
    assert add_comment(conn, tenant_id=t["id"], gallery_id=g["id"], image_id=img["id"], body="  ") is None
    # foreign image → None
    g2 = create_gallery(conn, tenant_id=t["id"], title="G2")
    other = _img(conn, storage, t["id"], g2["id"])
    assert add_comment(conn, tenant_id=t["id"], gallery_id=g["id"], image_id=other["id"],
                       body="hi") is None
    # valid comment
    c = add_comment(conn, tenant_id=t["id"], gallery_id=g["id"], image_id=img["id"],
                    body="  love this one  ", author_name="Sarah")
    assert c and c["body"] == "love this one" and c["author_name"] == "Sarah"
    assert comments_by_image(conn, g["id"])[img["id"]][0]["body"] == "love this one"
    owner_view = comments_for_gallery(conn, t["id"], g["id"])
    assert owner_view[0]["filename"] == "frame.jpg"


def test_tenant_isolation(conn, storage):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    g1 = create_gallery(conn, tenant_id=t1["id"], title="G1")
    img = _img(conn, storage, t1["id"], g1["id"])
    toggle_favorite(conn, tenant_id=t1["id"], gallery_id=g1["id"], image_id=img["id"])
    # t2 sees none of t1's favorites/comments
    assert list_favorites(conn, t2["id"], g1["id"]) == []
    assert comments_for_gallery(conn, t2["id"], g1["id"]) == []


def _published_gallery_with_image(app, *, pin=None):
    """Set up a published gallery with one image directly in the app's DB."""
    conn = connect(app.state.settings.db_path)
    try:
        t = create_tenant(conn, name="Live Studio", shoot_type="wedding")
        g = create_gallery(conn, tenant_id=t["id"], title="Wedding", pin=pin)
        img = _img(conn, app.state.storage, t["id"], g["id"])
        publish_gallery(conn, t["id"], g["id"])
        conn.commit()
        return t, g, img
    finally:
        conn.close()


def test_http_favorite_and_comment_flow(client, app):
    t, g, img = _published_gallery_with_image(app)
    base = f"/g/{t['slug']}/{g['slug']}"
    page = client.get(base)
    assert page.status_code == 200 and "favorited so far" in page.text

    client.post(f"{base}/favorite/{img['id']}")
    client.post(f"{base}/comment/{img['id']}", data={"body": "Stunning!", "author_name": "Sarah"})

    conn = connect(app.state.settings.db_path)
    try:
        assert favorite_image_ids(conn, g["id"]) == {img["id"]}
        assert comments_for_gallery(conn, t["id"], g["id"])[0]["body"] == "Stunning!"
    finally:
        conn.close()
    # the rendered gallery now shows the filled heart and the note
    after = client.get(base).text
    assert "♥" in after and "Stunning!" in after


def test_http_locked_gallery_blocks_favorite(client, app):
    t, g, img = _published_gallery_with_image(app, pin="1234")
    base = f"/g/{t['slug']}/{g['slug']}"
    # no PIN cookie → favorite is ignored
    client.post(f"{base}/favorite/{img['id']}")
    conn = connect(app.state.settings.db_path)
    try:
        assert favorite_count(conn, g["id"]) == 0
    finally:
        conn.close()
