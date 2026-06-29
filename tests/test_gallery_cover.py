"""AI cover — surface the gallery cover and let the owner set it to the AI's best hero pick.

hero_potential (the AI's cover-worthiness score) drove offers but the cover itself was set
once on upload and never changeable. This makes it actionable and visible.
"""

import io

from conftest import login_owner, onboard_studio

from hestia.galleries import (
    add_image,
    cover_storage_key,
    create_gallery,
    get_gallery,
    set_cover_image,
    set_image_hidden,
)
from hestia.tenants import create_tenant
from hestia.vision import hero_suggestions


def _img(conn, storage, t_id, g_id, name, data=b"x" * 16):
    return add_image(conn, storage, tenant_id=t_id, gallery_id=g_id,
                     filename=name, fileobj=io.BytesIO(data))


def _analyze(conn, t_id, g_id, image_id, *, hero=0.5, keeper=0.8, eyes=0.0, dup_key=None):
    conn.execute(
        "INSERT INTO image_analyses (image_id, gallery_id, tenant_id, keywords_json, "
        "keeper_score, hero_potential, shot_type, alt_text, eyes_closed, dup_key) "
        "VALUES (?, ?, ?, '[]', ?, ?, 'candid', '', ?, ?)",
        (image_id, g_id, t_id, keeper, hero, eyes, dup_key or f"solo-{image_id}"),
    )


# ── module logic ──────────────────────────────────────────────────────────────


def test_hero_suggestions_excludes_culled_and_hidden(conn, storage):
    t = create_tenant(conn, name="Hero Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    top = _img(conn, storage, t["id"], g["id"], "top.jpg")
    mid = _img(conn, storage, t["id"], g["id"], "mid.jpg")
    blink = _img(conn, storage, t["id"], g["id"], "blink.jpg")
    hidden = _img(conn, storage, t["id"], g["id"], "hidden.jpg")
    _analyze(conn, t["id"], g["id"], top["id"], hero=0.95)
    _analyze(conn, t["id"], g["id"], mid["id"], hero=0.60)
    _analyze(conn, t["id"], g["id"], blink["id"], hero=0.99, eyes=0.95)    # high hero but a blink
    _analyze(conn, t["id"], g["id"], hidden["id"], hero=0.90)
    set_image_hidden(conn, t["id"], hidden["id"], True)
    conn.commit()
    ids = hero_suggestions(conn, t["id"], g["id"])
    assert ids[0] == top["id"]                                 # best non-culled, non-hidden, first
    assert blink["id"] not in ids                              # culled (blink) excluded
    assert hidden["id"] not in ids                             # hidden excluded
    assert mid["id"] in ids


def test_set_cover_image_validates_and_scopes(conn, storage):
    t = create_tenant(conn, name="Cover Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    a = _img(conn, storage, t["id"], g["id"], "a.jpg")
    hidden = _img(conn, storage, t["id"], g["id"], "h.jpg")
    set_image_hidden(conn, t["id"], hidden["id"], True)
    conn.commit()
    assert set_cover_image(conn, t["id"], g["id"], a["id"]) is True
    assert get_gallery(conn, t["id"], g["id"])["cover_image_id"] == a["id"]
    assert set_cover_image(conn, t["id"], g["id"], hidden["id"]) is False        # hidden rejected
    other = create_tenant(conn, name="Other", shoot_type="portrait")
    assert set_cover_image(conn, other["id"], g["id"], a["id"]) is False         # foreign tenant
    g2 = create_gallery(conn, tenant_id=t["id"], title="G2")
    assert set_cover_image(conn, t["id"], g2["id"], a["id"]) is False            # wrong gallery
    assert get_gallery(conn, t["id"], g["id"])["cover_image_id"] == a["id"]      # unchanged by rejects


def test_cover_storage_key_prefers_cover_then_first_visible(conn, storage):
    t = create_tenant(conn, name="CK Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    a = _img(conn, storage, t["id"], g["id"], "a.jpg")         # first upload becomes the cover
    b = _img(conn, storage, t["id"], g["id"], "b.jpg")
    conn.commit()
    gal = get_gallery(conn, t["id"], g["id"])
    assert gal["cover_image_id"] == a["id"]
    assert cover_storage_key(conn, t["id"], gal) == a["storage_key"]
    set_image_hidden(conn, t["id"], a["id"], True)             # cover hidden → fall back to first visible
    gal = get_gallery(conn, t["id"], g["id"])
    assert cover_storage_key(conn, t["id"], gal) == b["storage_key"]


# ── routes ────────────────────────────────────────────────────────────────────


def test_owner_sets_cover_route(client, conn, storage):
    creds = onboard_studio(client, email="cover@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    g = create_gallery(conn, tenant_id=tid, title="Wedding")
    _img(conn, storage, tid, g["id"], "a.jpg")
    b = _img(conn, storage, tid, g["id"], "b.jpg")
    conn.commit()
    client.post(f"/galleries/{g['id']}/cover/{b['id']}")
    assert get_gallery(conn, tid, g["id"])["cover_image_id"] == b["id"]


def test_owner_use_ai_top_pick_button(client, conn, storage):
    creds = onboard_studio(client, email="aipick@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    g = create_gallery(conn, tenant_id=tid, title="Wedding")
    a = _img(conn, storage, tid, g["id"], "a.jpg")            # cover (first upload)
    star = _img(conn, storage, tid, g["id"], "star.jpg")
    _analyze(conn, tid, g["id"], a["id"], hero=0.20)
    _analyze(conn, tid, g["id"], star["id"], hero=0.97)       # the AI's top pick
    conn.commit()
    assert "Use the AI's top pick as the cover" in client.get(f"/galleries/{g['id']}").text
    client.post(f"/galleries/{g['id']}/cover/{star['id']}")
    assert get_gallery(conn, tid, g["id"])["cover_image_id"] == star["id"]


def test_owner_cannot_set_cover_on_foreign_gallery(client, conn, storage):
    creds = onboard_studio(client, email="a@cover.test", name="A")
    login_owner(client, creds)
    tb = create_tenant(conn, name="B", shoot_type="portrait")
    g = create_gallery(conn, tenant_id=tb["id"], title="B Gallery")
    _img(conn, storage, tb["id"], g["id"], "x.jpg")
    y = _img(conn, storage, tb["id"], g["id"], "y.jpg")
    conn.commit()
    before = get_gallery(conn, tb["id"], g["id"])["cover_image_id"]
    client.post(f"/galleries/{g['id']}/cover/{y['id']}")       # A tries to change B's cover
    assert get_gallery(conn, tb["id"], g["id"])["cover_image_id"] == before   # unchanged


def test_galleries_list_shows_cover_thumbnail(client, conn, storage):
    creds = onboard_studio(client, email="list@cover.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    g = create_gallery(conn, tenant_id=tid, title="Wedding")
    a = _img(conn, storage, tid, g["id"], "a.jpg")
    conn.commit()
    page = client.get("/galleries").text
    assert 'class="gallery-cover"' in page
    assert storage.public_path(a["storage_key"]) in page       # the cover thumbnail src
