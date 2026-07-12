"""Image delivery — the launch-blocker cliff, closed. Uploads get a downscaled browse
thumbnail; grids serve it (a client paging a big gallery pulls a few MB, not a few GB);
serving streams from disk with revocation-safe caching (private, no-cache) + ETag/304
revalidation; and the thumbnail obeys the exact same access control as the full frame."""

import io

from PIL import Image

from hestia.galleries import (
    _THUMB_MAX_EDGE,
    add_image,
    create_gallery,
    publish_gallery,
    set_image_hidden,
)
from hestia.tenants import create_tenant


def _jpeg(width: int, height: int) -> bytes:
    """A real, decodable JPEG (Pillow must be able to thumbnail it)."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (90, 120, 200)).save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _upload(conn, storage, tenant_id, *, published=True, hidden=False, big=True):
    g = create_gallery(conn, tenant_id=tenant_id, title="Delivery")
    data = _jpeg(2400, 1600) if big else _jpeg(64, 64)
    img = add_image(conn, storage, tenant_id=tenant_id, gallery_id=g["id"],
                    filename="frame.jpg", fileobj=io.BytesIO(data), content_type="image/jpeg")
    if published:
        publish_gallery(conn, tenant_id, g["id"])
    if hidden:
        set_image_hidden(conn, tenant_id, img["id"], True)
    conn.commit()
    return g, img, data


def test_upload_generates_a_smaller_thumbnail(conn, storage):
    t = create_tenant(conn, name="Thumb Studio", shoot_type="wedding")
    _g, img, original = _upload(conn, storage, t["id"])
    assert img["thumb_key"] and img["thumb_key"] != img["storage_key"]

    thumb_bytes = storage.open(img["thumb_key"])
    assert 0 < len(thumb_bytes) < len(original)                 # materially smaller
    with Image.open(io.BytesIO(thumb_bytes)) as im:
        assert max(im.size) <= _THUMB_MAX_EDGE                  # longest edge capped


def test_thumb_url_serves_the_thumbnail_with_revalidated_cache(client, conn, storage):
    t = create_tenant(conn, name="Serve Studio", shoot_type="wedding")
    _g, img, original = _upload(conn, storage, t["id"], published=True)

    r = client.get(f"/media/{img['access_token']}?s=t")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")
    # Revocation-safe caching: stored but revalidated (not the middleware's no-store),
    # and private so shared proxies don't retain a client's frames.
    assert "no-cache" in r.headers["cache-control"] and "private" in r.headers["cache-control"]
    assert "no-store" not in r.headers["cache-control"]
    assert len(r.content) < len(original)                       # the small one, not the original

    full = client.get(f"/media/{img['access_token']}")          # no ?s=t → the original
    assert full.status_code == 200 and len(full.content) >= len(r.content)


def test_conditional_request_revalidates_to_304(client, conn, storage):
    t = create_tenant(conn, name="Etag Studio", shoot_type="wedding")
    _g, img, _ = _upload(conn, storage, t["id"], published=True)

    first = client.get(f"/media/{img['access_token']}?s=t")
    etag = first.headers["etag"]
    again = client.get(f"/media/{img['access_token']}?s=t", headers={"If-None-Match": etag})
    assert again.status_code == 304                             # browser reuses its cache


def test_revalidation_honors_revocation_not_a_stale_304(client, conn, storage):
    """The reason media is `no-cache` not `immutable`: after a frame is culled/revoked,
    a conditional re-request must re-run access control and 403 — never hand back a 304
    that lets the browser show the stale cached frame."""
    t = create_tenant(conn, name="Revoke Studio", shoot_type="wedding")
    g, img, _ = _upload(conn, storage, t["id"], published=True)
    first = client.get(f"/media/{img['access_token']}?s=t")
    etag = first.headers["etag"]

    set_image_hidden(conn, t["id"], img["id"], True)            # revoke (cull) the frame
    conn.commit()
    revalidated = client.get(f"/media/{img['access_token']}?s=t", headers={"If-None-Match": etag})
    assert revalidated.status_code == 403                       # access re-checked before 304


def test_thumbnail_obeys_the_same_access_control_as_the_full_frame(client, conn, storage):
    """A culled/unpublished frame must not leak via the thumbnail either."""
    t = create_tenant(conn, name="Guard Studio", shoot_type="wedding")
    _g1, hidden_img, _ = _upload(conn, storage, t["id"], published=True, hidden=True)
    _g2, draft_img, _ = _upload(conn, storage, t["id"], published=False)
    assert client.get(f"/media/{hidden_img['access_token']}?s=t").status_code == 403
    assert client.get(f"/media/{draft_img['access_token']}?s=t").status_code == 403


def test_serving_falls_back_to_original_when_no_thumbnail(client, conn, storage):
    """Pre-migration rows (and frames whose thumbnailing failed) have thumb_key NULL —
    ?s=t must still serve the full frame, not 404."""
    t = create_tenant(conn, name="Legacy Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="Legacy")
    # Non-decodable bytes → _make_thumbnail returns None → thumb_key stays NULL.
    img = add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"],
                    filename="raw.bin", fileobj=io.BytesIO(b"not an image" * 8),
                    content_type="image/jpeg")
    publish_gallery(conn, t["id"], g["id"])
    conn.commit()
    assert img["thumb_key"] is None
    r = client.get(f"/media/{img['access_token']}?s=t")         # asks for thumb, gets original
    assert r.status_code == 200 and r.content.startswith(b"not an image")
