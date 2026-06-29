"""Album review — the client can request changes (a note back), not just approve. Notifies
the owner, keeps the album editable, and is cleared when the client later approves.
"""

import io

from conftest import login_owner, onboard_studio

from hestia.albums import (
    approve_album,
    enable_album_review,
    generate_album,
    get_album,
    request_album_changes,
)
from hestia.automations import TRIGGERS, create_automation
from hestia.galleries import add_image, create_gallery
from hestia.tenants import create_tenant


def _img(conn, storage, t_id, g_id, name, data=b"jpg"):
    return add_image(conn, storage, tenant_id=t_id, gallery_id=g_id, filename=name,
                     fileobj=io.BytesIO(data), content_type="image/jpeg")


def _album(conn, storage, settings, tenant, *, n=4):
    g = create_gallery(conn, tenant_id=tenant["id"], title="Wedding")
    for i in range(n):
        _img(conn, storage, tenant["id"], g["id"], f"f{i}.jpg", data=bytes([i + 1]) * 20)
    conn.commit()
    return g, generate_album(conn, settings, tenant=tenant, gallery=g)


def _automation_jobs(conn, tenant_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE tenant_id = ? AND kind = 'automation.run'",
        (tenant_id,),
    ).fetchone()["n"]


def test_changes_requested_trigger_registered():
    assert "album.changes_requested" in TRIGGERS


def test_request_changes_records_and_notifies(conn, storage, settings):
    t = create_tenant(conn, name="Notify", shoot_type="wedding")
    g, album = _album(conn, storage, settings, t)
    create_automation(conn, tenant_id=t["id"], name="Heads up", trigger="album.changes_requested",
                      subject="Changes requested", body="Client wants changes to {title}.")
    tok = enable_album_review(conn, t["id"], album["id"])
    conn.commit()
    assert request_album_changes(conn, tok, "  swap spreads 1 and 2  ") is True
    a = get_album(conn, t["id"], album["id"])
    assert a["change_request"] == "swap spreads 1 and 2"       # trimmed
    assert a["change_requested_at"]
    assert _automation_jobs(conn, t["id"]) == 1                # the owner's automation fired
    assert request_album_changes(conn, tok, "   ") is False    # empty note rejected
    assert request_album_changes(conn, "nope", "hi") is False  # unknown token


def test_request_changes_blocked_after_approval(conn, storage, settings):
    t = create_tenant(conn, name="Approved", shoot_type="wedding")
    g, album = _album(conn, storage, settings, t)
    tok = enable_album_review(conn, t["id"], album["id"])
    approve_album(conn, tok)
    conn.commit()
    assert request_album_changes(conn, tok, "too late") is False    # already approved


def test_approval_clears_change_request(conn, storage, settings):
    t = create_tenant(conn, name="Clear", shoot_type="wedding")
    g, album = _album(conn, storage, settings, t)
    tok = enable_album_review(conn, t["id"], album["id"])
    request_album_changes(conn, tok, "make it brighter")
    conn.commit()
    assert get_album(conn, t["id"], album["id"])["change_request"]
    approve_album(conn, tok)
    conn.commit()
    a = get_album(conn, t["id"], album["id"])
    assert a["approved_at"] and a["change_request"] is None     # resolved on approval


def test_public_request_changes_flow(client, conn, storage, settings):
    t = create_tenant(conn, name="Public", shoot_type="wedding")
    g, album = _album(conn, storage, settings, t)
    tok = enable_album_review(conn, t["id"], album["id"])
    conn.commit()
    client.post(f"/a/{tok}/request-changes", data={"note": "use the beach photo as the cover"})
    assert get_album(conn, t["id"], album["id"])["change_request"] == "use the beach photo as the cover"
    assert "use the beach photo as the cover" in client.get(f"/a/{tok}").text   # echoed to the client


def test_owner_sees_change_request(client, conn, storage, settings):
    creds = onboard_studio(client, email="cr@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    tenant = dict(conn.execute("SELECT * FROM tenants WHERE id = ?", (tid,)).fetchone())
    g, album = _album(conn, storage, settings, tenant)
    tok = enable_album_review(conn, tid, album["id"])
    request_album_changes(conn, tok, "swap photos 3 and 4")
    conn.commit()
    page = client.get(f"/albums/{album['id']}").text
    assert "requested changes" in page and "swap photos 3 and 4" in page


def test_change_request_is_escaped_not_injected(client, conn, storage, settings):
    creds = onboard_studio(client, email="xss@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    tenant = dict(conn.execute("SELECT * FROM tenants WHERE id = ?", (tid,)).fetchone())
    g, album = _album(conn, storage, settings, tenant)
    tok = enable_album_review(conn, tid, album["id"])
    conn.commit()
    client.post(f"/a/{tok}/request-changes", data={"note": "<script>alert(1)</script>"})
    page = client.get(f"/albums/{album['id']}").text
    assert "<script>alert(1)</script>" not in page          # client text is not injected raw...
    assert "&lt;script&gt;" in page                          # ...it's HTML-escaped
