"""AI cull-apply — make vision's cull suggestions actionable.

The vision pass flags near-duplicates and likely blinks (``cull_summary``); this lets the
studio APPLY those suggestions by hiding frames. A hidden frame stays in the library (and
in analysis) but is excluded from the client gallery and from delivery — reversibly, and
the original is never deleted.
"""

import io
import zipfile

from conftest import login_owner, onboard_studio

from hestia.delivery import enable_delivery
from hestia.galleries import (
    add_image,
    apply_cull,
    create_gallery,
    image_count,
    list_images,
    publish_gallery,
    set_image_hidden,
)
from hestia.tenants import create_tenant


def _img(conn, storage, t_id, g_id, name, data=b"x" * 32):
    return add_image(conn, storage, tenant_id=t_id, gallery_id=g_id,
                     filename=name, fileobj=io.BytesIO(data))


def _analyze(conn, t_id, g_id, image_id, *, keeper=0.8, eyes=0.0, dup_key=None):
    """Insert a controlled analysis row so the cull picture is deterministic
    (independent of the mock provider's filename hashing)."""
    conn.execute(
        "INSERT INTO image_analyses (image_id, gallery_id, tenant_id, keywords_json, "
        "keeper_score, hero_potential, shot_type, alt_text, eyes_closed, dup_key) "
        "VALUES (?, ?, ?, '[]', ?, 0.5, 'candid', '', ?, ?)",
        (image_id, g_id, t_id, keeper, eyes, dup_key or f"solo-{image_id}"),
    )


def _seed(conn, storage):
    """A gallery with four frames: a duplicate pair (A best, B cull), a likely
    blink (C), and a clean keeper (D). Returns (tenant, gallery, id-map)."""
    t = create_tenant(conn, name="Cull Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="Cull Gallery")
    a = _img(conn, storage, t["id"], g["id"], "a.jpg")
    b = _img(conn, storage, t["id"], g["id"], "b.jpg")
    c = _img(conn, storage, t["id"], g["id"], "c.jpg")
    d = _img(conn, storage, t["id"], g["id"], "d.jpg")
    _analyze(conn, t["id"], g["id"], a["id"], keeper=0.9, dup_key="pair")   # best of the pair → kept
    _analyze(conn, t["id"], g["id"], b["id"], keeper=0.4, dup_key="pair")   # worse dup → culled
    _analyze(conn, t["id"], g["id"], c["id"], keeper=0.8, eyes=0.95)        # blink → culled
    _analyze(conn, t["id"], g["id"], d["id"], keeper=0.95)                  # clean keeper → kept
    conn.commit()
    return t, g, {"a": a["id"], "b": b["id"], "c": c["id"], "d": d["id"]}


# ── module logic ──────────────────────────────────────────────────────────────


def test_apply_cull_hides_dups_and_blinks(conn, storage):
    t, g, ids = _seed(conn, storage)
    assert apply_cull(conn, t["id"], g["id"]) == 2              # the dup (b) + the blink (c)
    visible = {im["id"] for im in list_images(conn, g["id"], include_hidden=False)}
    assert visible == {ids["a"], ids["d"]}                      # best + clean keeper remain
    assert image_count(conn, g["id"], include_hidden=False) == 2
    assert image_count(conn, g["id"]) == 4                      # originals never deleted
    assert apply_cull(conn, t["id"], g["id"]) == 0             # idempotent: nothing new to hide


def test_apply_cull_no_flags_is_noop(conn, storage):
    t = create_tenant(conn, name="Clean Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="All Keepers")
    for i in range(3):
        im = _img(conn, storage, t["id"], g["id"], f"k{i}.jpg", data=bytes([i + 1]) * 16)
        _analyze(conn, t["id"], g["id"], im["id"], keeper=0.9)
    conn.commit()
    assert apply_cull(conn, t["id"], g["id"]) == 0
    assert image_count(conn, g["id"], include_hidden=False) == 3


def test_set_image_hidden_roundtrip_and_tenant_scoped(conn, storage):
    t, g, ids = _seed(conn, storage)
    set_image_hidden(conn, t["id"], ids["a"], True)
    assert {im["id"] for im in list_images(conn, g["id"], include_hidden=False)} == \
        {ids["b"], ids["c"], ids["d"]}
    set_image_hidden(conn, t["id"], ids["a"], False)            # restore
    assert ids["a"] in {im["id"] for im in list_images(conn, g["id"], include_hidden=False)}
    # another tenant cannot hide this tenant's image
    other = create_tenant(conn, name="Intruder", shoot_type="portrait")
    set_image_hidden(conn, other["id"], ids["a"], True)
    assert ids["a"] in {im["id"] for im in list_images(conn, g["id"], include_hidden=False)}
    # a route-level/gallery-scoped call cannot mutate another gallery in the same tenant
    g2 = create_gallery(conn, tenant_id=t["id"], title="Other Gallery")
    other_img = _img(conn, storage, t["id"], g2["id"], "other.jpg")
    assert set_image_hidden(conn, t["id"], other_img["id"], True, gallery_id=g["id"]) is False
    assert other_img["id"] in {
        im["id"] for im in list_images(conn, g2["id"], include_hidden=False)
    }


# ── public surfaces: hidden frames are gone from delivery & the client gallery ─


def test_hidden_excluded_from_delivery(client, conn, storage):
    t, g, ids = _seed(conn, storage)
    apply_cull(conn, t["id"], g["id"])                          # hides b + c
    token = enable_delivery(conn, t["id"], g["id"])
    conn.commit()

    page = client.get(f"/d/{token}")
    assert page.status_code == 200
    assert "a.jpg" in page.text and "d.jpg" in page.text
    assert "b.jpg" not in page.text and "c.jpg" not in page.text   # culled frames hidden

    z = client.get(f"/d/{token}/all.zip")                       # the whole-set zip skips them too
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    assert set(zf.namelist()) == {"a.jpg", "d.jpg"}

    # a hidden frame can't be pulled by direct id (download or inline view) either
    assert client.get(f"/d/{token}/{ids['b']}").status_code == 404
    assert client.get(f"/d/{token}/{ids['b']}/view").status_code == 404
    assert client.get(f"/d/{token}/{ids['a']}").status_code == 200   # a kept frame still downloads


def test_hidden_excluded_from_client_gallery(client, conn, storage):
    t, g, ids = _seed(conn, storage)
    apply_cull(conn, t["id"], g["id"])
    publish_gallery(conn, t["id"], g["id"])                     # client gallery requires published
    conn.commit()
    r = client.get(f"/g/{t['slug']}/{g['slug']}")
    assert r.status_code == 200
    assert f'id="img-{ids["a"]}"' in r.text and f'id="img-{ids["d"]}"' in r.text
    assert f'id="img-{ids["b"]}"' not in r.text and f'id="img-{ids["c"]}"' not in r.text


# ── owner routes ──────────────────────────────────────────────────────────────


def test_owner_cull_apply_route(client, conn, storage):
    creds = onboard_studio(client, email="cull@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    g = create_gallery(conn, tenant_id=tid, title="Owner Cull")
    a = _img(conn, storage, tid, g["id"], "a.jpg")
    b = _img(conn, storage, tid, g["id"], "b.jpg")
    _analyze(conn, tid, g["id"], a["id"], keeper=0.9, dup_key="pair")
    _analyze(conn, tid, g["id"], b["id"], keeper=0.4, dup_key="pair")
    conn.commit()
    gid = g["id"]

    assert "flagged frame" in client.get(f"/galleries/{gid}").text   # the apply-cull CTA

    client.post(f"/galleries/{gid}/cull/apply")
    assert conn.execute("SELECT hidden FROM images WHERE id = ?", (b["id"],)).fetchone()["hidden"] == 1
    assert conn.execute("SELECT hidden FROM images WHERE id = ?", (a["id"],)).fetchone()["hidden"] == 0


def test_owner_hide_unhide_route(client, conn, storage):
    creds = onboard_studio(client, email="hide@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    g = create_gallery(conn, tenant_id=tid, title="Hide Gallery")
    im = _img(conn, storage, tid, g["id"], "solo.jpg")
    conn.commit()
    gid, iid = g["id"], im["id"]

    client.post(f"/galleries/{gid}/images/{iid}/hide")
    assert conn.execute("SELECT hidden FROM images WHERE id = ?", (iid,)).fetchone()["hidden"] == 1
    client.post(f"/galleries/{gid}/images/{iid}/unhide")
    assert conn.execute("SELECT hidden FROM images WHERE id = ?", (iid,)).fetchone()["hidden"] == 0


def test_owner_hide_route_cannot_mutate_same_tenant_other_gallery(client, conn, storage):
    creds = onboard_studio(client, email="same-tenant-hide@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    g1 = create_gallery(conn, tenant_id=tid, title="Visible Gallery")
    g2 = create_gallery(conn, tenant_id=tid, title="Other Gallery")
    other = _img(conn, storage, tid, g2["id"], "other.jpg")
    conn.commit()

    client.post(f"/galleries/{g1['id']}/images/{other['id']}/hide")
    assert conn.execute("SELECT hidden FROM images WHERE id = ?", (other["id"],)).fetchone()[
        "hidden"
    ] == 0


def test_owner_cannot_cull_or_hide_foreign_gallery(client, conn, storage):
    creds = onboard_studio(client, email="a@studio.test")
    login_owner(client, creds)
    # a second studio's gallery, with a flagged duplicate, created directly
    tb = create_tenant(conn, name="Studio B", shoot_type="portrait")
    g = create_gallery(conn, tenant_id=tb["id"], title="B Gallery")
    x = _img(conn, storage, tb["id"], g["id"], "x.jpg")
    y = _img(conn, storage, tb["id"], g["id"], "y.jpg")
    _analyze(conn, tb["id"], g["id"], x["id"], keeper=0.9, dup_key="p")
    _analyze(conn, tb["id"], g["id"], y["id"], keeper=0.3, dup_key="p")
    conn.commit()

    # logged in as A, neither route may touch B's gallery
    client.post(f"/galleries/{g['id']}/cull/apply")
    assert conn.execute("SELECT COALESCE(SUM(hidden), 0) AS s FROM images WHERE gallery_id = ?",
                        (g["id"],)).fetchone()["s"] == 0
    client.post(f"/galleries/{g['id']}/images/{y['id']}/hide")
    assert conn.execute("SELECT hidden FROM images WHERE id = ?", (y["id"],)).fetchone()["hidden"] == 0


# ── leak guards: a hidden frame's bytes must not be reachable by the public ────
# The client gallery serves image bytes via the predictable /media/{key} URL, so
# excluding hidden frames from the *listing* is not enough — /media/ itself must
# refuse to serve a culled frame to anyone but the owner.


def _key(conn, image_id):
    return conn.execute("SELECT storage_key FROM images WHERE id = ?", (image_id,)).fetchone()["storage_key"]


def test_media_route_blocks_hidden_frame_for_public(client, conn, storage):
    t, g, ids = _seed(conn, storage)
    publish_gallery(conn, t["id"], g["id"])
    apply_cull(conn, t["id"], g["id"])                         # hides b + c
    conn.commit()
    assert client.get(f"/media/{_key(conn, ids['a'])}").status_code == 200   # kept frame public
    assert client.get(f"/media/{_key(conn, ids['b'])}").status_code == 403   # culled frame forbidden
    assert client.get(f"/media/{_key(conn, ids['c'])}").status_code == 403


def test_owner_still_sees_hidden_frame_via_media(client, conn, storage):
    from fastapi.testclient import TestClient
    creds = onboard_studio(client, email="media@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    g = create_gallery(conn, tenant_id=tid, title="Media Gallery")
    im = _img(conn, storage, tid, g["id"], "solo.jpg")
    publish_gallery(conn, tid, g["id"])
    set_image_hidden(conn, tid, im["id"], True)
    conn.commit()
    key = _key(conn, im["id"])
    assert client.get(f"/media/{key}").status_code == 200        # owner manages their hidden frame
    assert TestClient(client.app).get(f"/media/{key}").status_code == 403   # anon cannot


def test_offer_page_drops_hidden_hero_and_favorite(client, conn, storage):
    from hestia.proofing import toggle_favorite
    from hestia.sales import create_or_update_offer
    from hestia.tenants import tenant_flags
    t = create_tenant(conn, name="Offer Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="Offer Gallery")
    a = _img(conn, storage, t["id"], g["id"], "hero.jpg")
    b = _img(conn, storage, t["id"], g["id"], "kept-fav.jpg")
    publish_gallery(conn, t["id"], g["id"])
    vision = {"hero_image_ids": [a["id"], b["id"]], "keeper_count": 2}
    offer = create_or_update_offer(conn, tenant=dict(t), gallery=dict(g), run_id=None,
                                   vision_summary=vision, flags=tenant_flags(t))
    toggle_favorite(conn, tenant_id=t["id"], gallery_id=g["id"], image_id=a["id"])
    toggle_favorite(conn, tenant_id=t["id"], gallery_id=g["id"], image_id=b["id"])
    set_image_hidden(conn, t["id"], a["id"], True)             # cull a frame that's hero AND favorite
    conn.commit()
    page = client.get(f"/s/{t['slug']}/{offer['token']}")
    assert page.status_code == 200
    assert _key(conn, a["id"]) not in page.text               # culled frame gone from heroes + favorites
    assert _key(conn, b["id"]) in page.text                   # the kept frame still shows
