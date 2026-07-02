"""Media access control — client image URLs are unguessable capability tokens, and
the legacy enumerable storage-key path (<tenant>/<gallery>/<image>.<ext>) is
owner-only. Regression guard for the RC finding: a leaked tenant id + sequential
ids let anyone pull a PIN-protected or delivery-expired gallery's originals."""

import io

from conftest import CSRFClient, login_owner, onboard_studio

from hestia.galleries import add_image, create_gallery, publish_gallery, set_image_hidden
from hestia.tenants import create_tenant


def _img(conn, storage, tenant_id, *, published=True, hidden=False, title="G"):
    g = create_gallery(conn, tenant_id=tenant_id, title=title)
    img = add_image(conn, storage, tenant_id=tenant_id, gallery_id=g["id"],
                    filename="a.jpg", fileobj=io.BytesIO(b"JPEGBYTES" * 4),
                    content_type="image/jpeg")
    if published:
        publish_gallery(conn, tenant_id, g["id"])
    if hidden:
        set_image_hidden(conn, tenant_id, img["id"], True)
    conn.commit()
    return g, img


def test_legacy_key_path_is_owner_only(client, conn, storage):
    """THE exploit, closed: even for a published gallery, the enumerable storage-key
    path is refused to a non-owner — so a harvested tenant id + walked ids get nothing."""
    t = create_tenant(conn, name="Private Studio", shoot_type="wedding")
    _g, img = _img(conn, storage, t["id"], published=True)
    r = client.get(f"/media/{img['storage_key']}")     # <tenant>/<gid>/<iid>.jpg
    assert r.status_code == 403


def test_capability_token_serves_published_image(client, conn, storage):
    t = create_tenant(conn, name="Open Studio", shoot_type="wedding")
    _g, img = _img(conn, storage, t["id"], published=True)
    assert img["access_token"] and "/" not in img["access_token"]   # unguessable, no slash
    r = client.get(f"/media/{img['access_token']}")
    assert r.status_code == 200 and r.content.startswith(b"JPEG")


def test_capability_token_refuses_hidden_and_unpublished_to_non_owner(client, conn, storage):
    t = create_tenant(conn, name="Guard Studio", shoot_type="wedding")
    _g1, hidden_img = _img(conn, storage, t["id"], published=True, hidden=True, title="Culled")
    _g2, draft_img = _img(conn, storage, t["id"], published=False, title="Draft")
    assert client.get(f"/media/{hidden_img['access_token']}").status_code == 403  # culled frame
    assert client.get(f"/media/{draft_img['access_token']}").status_code == 403   # unpublished


def test_owner_sees_own_images_via_both_paths(app, conn, storage):
    owner = CSRFClient(app)
    login_owner(owner, onboard_studio(owner, name="Mine", email="mine@x.test"))
    tid = conn.execute("SELECT tenant_id FROM users WHERE email = 'mine@x.test'").fetchone()["tenant_id"]
    _g, img = _img(conn, storage, tid, published=False, title="Owner Draft")  # unpublished
    # The owner's own dashboard renders storage-key URLs; the session must serve them.
    assert owner.get(f"/media/{img['storage_key']}").status_code == 200
    assert owner.get(f"/media/{img['access_token']}").status_code == 200


def test_client_gallery_renders_capability_urls_not_keys(client, conn, storage):
    t = create_tenant(conn, name="Render Studio", shoot_type="wedding")
    g, img = _img(conn, storage, t["id"], published=True, title="Finals")
    page = client.get(f"/g/{t['slug']}/{g['slug']}").text
    assert f"/media/{img['access_token']}" in page          # capability URL rendered
    assert f"/media/{img['storage_key']}" not in page       # never the enumerable key
