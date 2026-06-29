"""Album designer — the owner reorders spreads (sequencing the story their way). Locked
once the client approves; tenant-scoped.
"""

import io

from conftest import login_owner, onboard_studio

from hestia.albums import (
    approve_album,
    enable_album_review,
    generate_album,
    get_album,
    move_spread,
)
from hestia.galleries import add_image, create_gallery
from hestia.tenants import create_tenant


def _img(conn, storage, t_id, g_id, name, data=b"jpg"):
    return add_image(conn, storage, tenant_id=t_id, gallery_id=g_id, filename=name,
                     fileobj=io.BytesIO(data), content_type="image/jpeg")


def _album(conn, storage, settings, tenant, *, n=12):
    g = create_gallery(conn, tenant_id=tenant["id"], title="Wedding")
    for i in range(n):
        _img(conn, storage, tenant["id"], g["id"], f"f{i}.jpg", data=bytes([i + 1]) * 20)
    conn.commit()
    return g, generate_album(conn, settings, tenant=tenant, gallery=g)


def test_move_spread(conn, storage, settings):
    t = create_tenant(conn, name="Reorder", shoot_type="wedding")
    g, album = _album(conn, storage, settings, t, n=12)         # 3 spreads of 4
    before = [sp["photo_ids"] for sp in album["spreads"]]
    assert len(before) == 3
    assert move_spread(conn, t["id"], album["id"], 2, "up") is True
    after = get_album(conn, t["id"], album["id"])["spreads"]
    assert [sp["position"] for sp in after] == [1, 2, 3]        # renumbered contiguously
    assert after[0]["photo_ids"] == before[1]                  # old spread 2 is now first
    assert after[1]["photo_ids"] == before[0]
    assert after[2]["photo_ids"] == before[2]                  # spread 3 untouched
    assert move_spread(conn, t["id"], album["id"], 1, "up") is False     # first can't go up
    assert move_spread(conn, t["id"], album["id"], 3, "down") is False   # last can't go down
    assert move_spread(conn, t["id"], album["id"], 99, "up") is False    # bad position
    assert move_spread(conn, t["id"], album["id"], 1, "sideways") is False   # bad direction
    other = create_tenant(conn, name="Other", shoot_type="portrait")
    assert move_spread(conn, other["id"], album["id"], 2, "up") is False     # tenant-scoped


def test_move_spread_blocked_when_approved(conn, storage, settings):
    t = create_tenant(conn, name="Locked", shoot_type="wedding")
    g, album = _album(conn, storage, settings, t, n=8)
    tok = enable_album_review(conn, t["id"], album["id"])
    approve_album(conn, tok)
    conn.commit()
    assert move_spread(conn, t["id"], album["id"], 2, "up") is False    # approved → locked


def test_owner_move_spread_route(client, conn, storage, settings):
    creds = onboard_studio(client, email="reorder@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    tenant = dict(conn.execute("SELECT * FROM tenants WHERE id = ?", (tid,)).fetchone())
    g, album = _album(conn, storage, settings, tenant, n=8)
    before = [sp["photo_ids"] for sp in album["spreads"]]
    client.post(f"/albums/{album['id']}/spreads/2/move/up")
    after = get_album(conn, tid, album["id"])["spreads"]
    assert after[0]["photo_ids"] == before[1]                  # spread 2 moved to first


def test_owner_cannot_reorder_foreign_album(client, conn, storage, settings):
    creds = onboard_studio(client, email="a@reorder.test", name="A")
    login_owner(client, creds)
    tb = create_tenant(conn, name="B", shoot_type="portrait")
    g, album = _album(conn, storage, settings, tb, n=8)
    before = [sp["photo_ids"] for sp in album["spreads"]]
    client.post(f"/albums/{album['id']}/spreads/2/move/up")
    after = get_album(conn, tb["id"], album["id"])["spreads"]
    assert [sp["photo_ids"] for sp in after] == before         # unchanged
