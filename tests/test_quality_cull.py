"""Quality cull — make the exposure/sharpness flags actionable: one-click hide of the
frames the AI flags as soft or poorly exposed. Reversible; reuses the ``hidden`` mechanism,
so flagged frames drop out of the client gallery and delivery like any other hidden frame.
"""

import io

from conftest import login_owner, onboard_studio

from hestia.galleries import add_image, apply_quality_cull, create_gallery, list_images
from hestia.tenants import create_tenant
from hestia.vision import flagged_image_ids


def _img(conn, storage, t_id, g_id, name, data=b"x" * 16):
    return add_image(conn, storage, tenant_id=t_id, gallery_id=g_id,
                     filename=name, fileobj=io.BytesIO(data))


def _row(conn, t_id, g_id, image_id, *, exposure=0.6, sharpness=0.6):
    conn.execute(
        "INSERT INTO image_analyses (image_id, gallery_id, tenant_id, keywords_json, "
        "keeper_score, hero_potential, shot_type, alt_text, exposure, sharpness) "
        "VALUES (?, ?, ?, '[]', 0.8, 0.5, 'candid', '', ?, ?)",
        (image_id, g_id, t_id, exposure, sharpness),
    )


def _seed(conn, storage):
    """A gallery with a soft frame, a dark frame, and a technically-fine frame."""
    t = create_tenant(conn, name="QC Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    soft = _img(conn, storage, t["id"], g["id"], "soft.jpg")
    dark = _img(conn, storage, t["id"], g["id"], "dark.jpg")
    fine = _img(conn, storage, t["id"], g["id"], "fine.jpg")
    _row(conn, t["id"], g["id"], soft["id"], sharpness=0.20, exposure=0.60)
    _row(conn, t["id"], g["id"], dark["id"], sharpness=0.80, exposure=0.20)
    _row(conn, t["id"], g["id"], fine["id"], sharpness=0.80, exposure=0.60)
    conn.commit()
    return t, g, {"soft": soft["id"], "dark": dark["id"], "fine": fine["id"]}


def test_flagged_image_ids_and_scope(conn, storage):
    t, g, ids = _seed(conn, storage)
    assert flagged_image_ids(conn, t["id"], g["id"]) == {ids["soft"], ids["dark"]}
    other = create_tenant(conn, name="Other", shoot_type="portrait")
    assert flagged_image_ids(conn, other["id"], g["id"]) == set()       # tenant-scoped


def test_apply_quality_cull_hides_flagged_only(conn, storage):
    t, g, ids = _seed(conn, storage)
    assert apply_quality_cull(conn, t["id"], g["id"]) == 2               # soft + dark
    visible = {im["id"] for im in list_images(conn, g["id"], include_hidden=False)}
    assert visible == {ids["fine"]}                                      # only the clean frame
    assert apply_quality_cull(conn, t["id"], g["id"]) == 0              # idempotent


def test_owner_quality_cull_route(client, conn, storage):
    creds = onboard_studio(client, email="qc@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    g = create_gallery(conn, tenant_id=tid, title="Wedding")
    soft = _img(conn, storage, tid, g["id"], "soft.jpg")
    fine = _img(conn, storage, tid, g["id"], "fine.jpg")
    _row(conn, tid, g["id"], soft["id"], sharpness=0.15, exposure=0.60)
    _row(conn, tid, g["id"], fine["id"], sharpness=0.80, exposure=0.60)
    conn.commit()
    assert "Hide 1 flagged reject" in client.get(f"/galleries/{g['id']}").text
    client.post(f"/galleries/{g['id']}/quality-cull/apply")
    assert conn.execute("SELECT hidden FROM images WHERE id = ?", (soft["id"],)).fetchone()["hidden"] == 1
    assert conn.execute("SELECT hidden FROM images WHERE id = ?", (fine["id"],)).fetchone()["hidden"] == 0


def test_owner_cannot_quality_cull_foreign_gallery(client, conn, storage):
    creds = onboard_studio(client, email="a@qc.test", name="A")
    login_owner(client, creds)
    tb = create_tenant(conn, name="B", shoot_type="portrait")
    g = create_gallery(conn, tenant_id=tb["id"], title="B Gallery")
    soft = _img(conn, storage, tb["id"], g["id"], "soft.jpg")
    _row(conn, tb["id"], g["id"], soft["id"], sharpness=0.10, exposure=0.60)
    conn.commit()
    client.post(f"/galleries/{g['id']}/quality-cull/apply")             # A logged in, B's gallery
    assert conn.execute("SELECT hidden FROM images WHERE id = ?", (soft["id"],)).fetchone()["hidden"] == 0
