"""Gallery proofing — favorites toggle, comments, scoping, and the client flow."""

import io

from conftest import onboard_studio

from hestia.automations import TRIGGERS
from hestia.db import connect
from hestia.email import list_emails
from hestia.galleries import (
    add_image,
    create_gallery,
    get_gallery,
    publish_gallery,
    submit_selections,
)
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


# ── Submit selections (close the proofing → album/offer handoff) ──────────────


def test_submit_selections_trigger_registered():
    # the automation engine must know the event or emit_event silently drops it
    assert "gallery.selections_submitted" in TRIGGERS


def test_submit_selections_idempotent(conn):
    t = _tenant(conn)
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    assert submit_selections(conn, tenant_id=t["id"], gallery_id=g["id"]) is True
    stamped = get_gallery(conn, t["id"], g["id"])["selections_submitted_at"]
    assert stamped  # first submit stamps it
    # re-submits are no-ops: return False and never re-stamp
    assert submit_selections(conn, tenant_id=t["id"], gallery_id=g["id"]) is False
    assert submit_selections(conn, tenant_id=t["id"], gallery_id=g["id"]) is False
    assert get_gallery(conn, t["id"], g["id"])["selections_submitted_at"] == stamped


def test_submit_selections_tenant_isolation(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    g1 = create_gallery(conn, tenant_id=t1["id"], title="G1")
    # a different tenant cannot finalize t1's gallery
    assert submit_selections(conn, tenant_id=t2["id"], gallery_id=g1["id"]) is False
    assert get_gallery(conn, t1["id"], g1["id"])["selections_submitted_at"] is None
    # the real owner can
    assert submit_selections(conn, tenant_id=t1["id"], gallery_id=g1["id"]) is True


def test_http_submit_notifies_owner_once(client, app):
    # onboarding creates an owner user, so owner_digest_recipient resolves to an inbox
    creds = onboard_studio(client, name="Pixel Studio", email="pix@example.com")
    conn = connect(app.state.settings.db_path)
    try:
        row = conn.execute(
            "SELECT t.id, t.slug FROM tenants t JOIN users u ON u.tenant_id = t.id "
            "WHERE u.email = ?", (creds["email"],)
        ).fetchone()
        tid, slug = row["id"], row["slug"]
        g = create_gallery(conn, tenant_id=tid, title="Wedding")
        img = _img(conn, app.state.storage, tid, g["id"])
        publish_gallery(conn, tid, g["id"])
        conn.commit()
    finally:
        conn.close()

    base = f"/g/{slug}/{g['slug']}"
    client.post(f"{base}/favorite/{img['id']}")
    # before submit: the finalize button is shown, no confirmation banner yet
    assert "I'm done" in client.get(base).text

    client.post(f"{base}/submit")
    conn = connect(app.state.settings.db_path)
    try:
        sent = [e for e in list_emails(conn, tid, to_addr=creds["email"])
                if "favorites" in e["subject"].lower()]
        assert len(sent) == 1                       # owner notified exactly once
        first_ts = get_gallery(conn, tid, g["id"])["selections_submitted_at"]
        assert first_ts
    finally:
        conn.close()

    # after submit: the gallery shows the confirmation banner, not the button
    after = client.get(base).text
    assert "You've sent" in after

    # a second submit must not re-notify or re-stamp
    client.post(f"{base}/submit")
    conn = connect(app.state.settings.db_path)
    try:
        sent = [e for e in list_emails(conn, tid, to_addr=creds["email"])
                if "favorites" in e["subject"].lower()]
        assert len(sent) == 1                       # still exactly one
        assert get_gallery(conn, tid, g["id"])["selections_submitted_at"] == first_ts
    finally:
        conn.close()


def test_http_locked_gallery_blocks_submit(client, app):
    t, g, _img_ = _published_gallery_with_image(app, pin="1234")
    base = f"/g/{t['slug']}/{g['slug']}"
    # no PIN cookie → submit is ignored, gallery stays un-finalized
    client.post(f"{base}/submit")
    conn = connect(app.state.settings.db_path)
    try:
        assert get_gallery(conn, t["id"], g["id"])["selections_submitted_at"] is None
    finally:
        conn.close()
