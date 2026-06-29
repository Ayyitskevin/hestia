"""Client portal — surface the client's shared albums (review link + state), tying the
album review/approve flow into the client's hub. Read-only; album review is independent of
the gallery's publish state.
"""

import io

from hestia.albums import (
    approve_album,
    enable_album_review,
    generate_album,
    request_album_changes,
)
from hestia.crm import assign_gallery_to_project, create_client, create_project
from hestia.galleries import add_image, create_gallery
from hestia.portal import assemble_portal, enable_portal, get_client_by_portal_token
from hestia.tenants import create_tenant


def _client_gallery_album(conn, storage, settings, t, *, title="Wedding Gallery", name="Sarah"):
    c = create_client(conn, tenant_id=t["id"], name=name)
    p = create_project(conn, tenant_id=t["id"], name="Wedding", client_id=c["id"])
    g = create_gallery(conn, tenant_id=t["id"], title=title)
    assign_gallery_to_project(conn, t["id"], g["id"], p["id"])
    for i in range(4):
        add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"], filename=f"f{i}.jpg",
                  fileobj=io.BytesIO(bytes([i + 1]) * 20), content_type="image/jpeg")
    conn.commit()
    return c, g, generate_album(conn, settings, tenant=t, gallery=g)


def _portal_albums(conn, settings, t, c):
    client = get_client_by_portal_token(conn, enable_portal(conn, t["id"], c["id"]))
    return assemble_portal(conn, settings, client)["albums"]


def test_only_shared_album_appears(conn, storage, settings):
    t = create_tenant(conn, name="Portal Albums", shoot_type="wedding")
    conn.commit()
    c, g, album = _client_gallery_album(conn, storage, settings, t)
    assert _portal_albums(conn, settings, t, c) == []          # not shared yet → hidden
    tok = enable_album_review(conn, t["id"], album["id"])
    conn.commit()
    albums = _portal_albums(conn, settings, t, c)
    assert len(albums) == 1
    a = albums[0]
    assert a["gallery_title"] == "Wedding Gallery"
    assert a["state"] == "review"                              # shared, awaiting the client
    assert tok in a["review_url"]                              # links into the review flow


def test_album_state_tracks_review(conn, storage, settings):
    t = create_tenant(conn, name="States", shoot_type="wedding")
    conn.commit()
    c, g, album = _client_gallery_album(conn, storage, settings, t)
    tok = enable_album_review(conn, t["id"], album["id"])
    request_album_changes(conn, tok, "brighter please")
    conn.commit()
    assert _portal_albums(conn, settings, t, c)[0]["state"] == "changes"
    approve_album(conn, tok)
    conn.commit()
    assert _portal_albums(conn, settings, t, c)[0]["state"] == "approved"


def test_portal_album_is_client_scoped(conn, storage, settings):
    t = create_tenant(conn, name="Scope", shoot_type="wedding")
    conn.commit()
    c1, g1, album1 = _client_gallery_album(conn, storage, settings, t)
    enable_album_review(conn, t["id"], album1["id"])
    c2, g2, album2 = _client_gallery_album(conn, storage, settings, t,
                                           title="Other Gallery", name="Other")
    enable_album_review(conn, t["id"], album2["id"])
    conn.commit()
    assert [a["gallery_title"] for a in _portal_albums(conn, settings, t, c1)] == ["Wedding Gallery"]
    assert [a["gallery_title"] for a in _portal_albums(conn, settings, t, c2)] == ["Other Gallery"]


def test_portal_page_renders_album(client, conn, storage, settings):
    t = create_tenant(conn, name="Page", shoot_type="wedding")
    conn.commit()
    c, g, album = _client_gallery_album(conn, storage, settings, t)
    tok = enable_album_review(conn, t["id"], album["id"])      # shared but gallery NOT published
    ptoken = enable_portal(conn, t["id"], c["id"])
    conn.commit()
    page = client.get(f"/portal/{ptoken}").text
    assert "Your albums" in page and f"/a/{tok}" in page       # album shows even if gallery unpublished


def test_review_album_appears_in_attention(client, conn, storage, settings):
    t = create_tenant(conn, name="Attention", shoot_type="wedding")
    conn.commit()
    c, g, album = _client_gallery_album(conn, storage, settings, t)
    tok = enable_album_review(conn, t["id"], album["id"])
    ptoken = enable_portal(conn, t["id"], c["id"])
    conn.commit()
    page = client.get(f"/portal/{ptoken}").text
    assert "Needs your attention" in page                      # an album awaiting review is a to-do
    assert "Review your album" in page and f"/a/{tok}" in page


def test_approved_album_not_a_todo(client, conn, storage, settings):
    t = create_tenant(conn, name="Done", shoot_type="wedding")
    conn.commit()
    c, g, album = _client_gallery_album(conn, storage, settings, t)
    tok = enable_album_review(conn, t["id"], album["id"])
    approve_album(conn, tok)
    ptoken = enable_portal(conn, t["id"], c["id"])
    conn.commit()
    page = client.get(f"/portal/{ptoken}").text
    assert "Review your album" not in page                     # approved → no longer a to-do...
    assert "Your albums" in page                               # ...but still listed (as approved)
