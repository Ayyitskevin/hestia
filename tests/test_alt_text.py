"""AI alt text — every delivered photo carries the vision pass's caption as its ``alt``
(accessibility + SEO), falling back to the filename for any frame without a caption.
"""

import io

from hestia.delivery import enable_delivery
from hestia.galleries import add_image, create_gallery, publish_gallery
from hestia.tenants import create_tenant
from hestia.vision import alt_text_map


def _img(conn, storage, t_id, g_id, name, data=b"x" * 16):
    return add_image(conn, storage, tenant_id=t_id, gallery_id=g_id,
                     filename=name, fileobj=io.BytesIO(data))


def _caption(conn, t_id, g_id, image_id, alt):
    conn.execute(
        "INSERT INTO image_analyses (image_id, gallery_id, tenant_id, keywords_json, alt_text) "
        "VALUES (?, ?, ?, '[]', ?)",
        (image_id, g_id, t_id, alt),
    )


def test_alt_text_map_only_returns_captioned_frames(conn, storage):
    t = create_tenant(conn, name="Alt Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    a = _img(conn, storage, t["id"], g["id"], "a.jpg")
    b = _img(conn, storage, t["id"], g["id"], "b.jpg")
    c = _img(conn, storage, t["id"], g["id"], "c.jpg")
    _caption(conn, t["id"], g["id"], a["id"], "a candid portrait at golden hour")
    _caption(conn, t["id"], g["id"], b["id"], "   ")        # blank caption → excluded
    conn.commit()
    m = alt_text_map(conn, g["id"])
    assert m == {a["id"]: "a candid portrait at golden hour"}
    assert c["id"] not in m                                  # no analysis row → filename fallback


def test_client_gallery_uses_ai_alt_text(client, conn, storage):
    t = create_tenant(conn, name="Alt Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="Wedding")
    a = _img(conn, storage, t["id"], g["id"], "a.jpg")
    _img(conn, storage, t["id"], g["id"], "b.jpg")          # uncaptioned
    _caption(conn, t["id"], g["id"], a["id"], "a candid portrait at golden hour")
    publish_gallery(conn, t["id"], g["id"])
    conn.commit()
    page = client.get(f"/g/{t['slug']}/{g['slug']}").text
    assert 'alt="a candid portrait at golden hour"' in page   # the AI caption
    assert 'alt="b.jpg"' in page                              # filename fallback


def test_delivery_page_uses_ai_alt_text(client, conn, storage):
    t = create_tenant(conn, name="Alt Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="Finals")
    a = _img(conn, storage, t["id"], g["id"], "a.jpg")
    _caption(conn, t["id"], g["id"], a["id"], "a sunset detail shot")
    token = enable_delivery(conn, t["id"], g["id"])
    conn.commit()
    assert 'alt="a sunset detail shot"' in client.get(f"/d/{token}").text
