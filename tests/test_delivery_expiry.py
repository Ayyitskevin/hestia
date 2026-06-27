"""Gallery delivery expiry — a download link can carry an expiry date: it works
through that date and 410s after. No date → never expires (existing links unchanged)."""

import datetime
import io

from conftest import login_owner, onboard_studio

from hestia.delivery import delivery_expired, enable_delivery, set_delivery_expiry
from hestia.galleries import add_image, create_gallery
from hestia.tenants import create_tenant


def _past(days: int = 2) -> str:
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def _future(days: int = 30) -> str:
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def _delivered_gallery(conn, storage, *, expires_at: str = ""):
    t = create_tenant(conn, name="Exp Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="Wedding Finals")
    add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"], filename="a.jpg",
              fileobj=io.BytesIO(b"AAA"))
    token = enable_delivery(conn, t["id"], g["id"])
    if expires_at:
        set_delivery_expiry(conn, t["id"], g["id"], expires_at)
    conn.commit()
    return t, g, token


# ── unit ─────────────────────────────────────────────────────────────────────


def test_delivery_expired_logic(conn):
    today = datetime.date.today().isoformat()
    assert delivery_expired(conn, {"delivery_expires_at": _past()}) is True
    assert delivery_expired(conn, {"delivery_expires_at": _future()}) is False
    assert delivery_expired(conn, {"delivery_expires_at": today}) is False     # through the date
    assert delivery_expired(conn, {"delivery_expires_at": None}) is False
    assert delivery_expired(conn, {"delivery_expires_at": ""}) is False
    assert delivery_expired(conn, {"delivery_expires_at": "not a date"}) is False  # forgiving


def test_set_and_clear_expiry_is_tenant_scoped(conn, storage):
    t, g, _token = _delivered_gallery(conn, storage)
    set_delivery_expiry(conn, t["id"], g["id"], _future())
    assert conn.execute("SELECT delivery_expires_at FROM galleries WHERE id=?",
                        (g["id"],)).fetchone()[0] == _future()
    other = create_tenant(conn, name="Other", shoot_type="other")
    set_delivery_expiry(conn, other["id"], g["id"], "")            # wrong tenant → no-op
    assert conn.execute("SELECT delivery_expires_at FROM galleries WHERE id=?",
                        (g["id"],)).fetchone()[0] == _future()
    set_delivery_expiry(conn, t["id"], g["id"], "")               # owner clears it
    assert conn.execute("SELECT delivery_expires_at FROM galleries WHERE id=?",
                        (g["id"],)).fetchone()[0] is None


# ── HTTP: live through the date, 410 (Gone) after ────────────────────────────


def test_expired_link_is_gone_on_every_endpoint(client, conn, storage):
    _t, g, token = _delivered_gallery(conn, storage, expires_at=_past())
    img_id = conn.execute("SELECT id FROM images WHERE gallery_id=?", (g["id"],)).fetchone()[0]

    page = client.get(f"/d/{token}")
    assert page.status_code == 410 and "expired" in page.text.lower()
    assert client.get(f"/d/{token}/all.zip").status_code == 410
    assert client.get(f"/d/{token}/{img_id}").status_code == 410
    assert client.get(f"/d/{token}/{img_id}/view").status_code == 410


def test_future_expiry_still_downloadable(client, conn, storage):
    _t, g, token = _delivered_gallery(conn, storage, expires_at=_future())
    page = client.get(f"/d/{token}")
    assert page.status_code == 200 and "Wedding Finals" in page.text


def test_owner_sets_expiry_via_route(client, conn, storage):
    login_owner(client, onboard_studio(client, email="exp@owner.com"))
    tid = conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()[0]
    g = create_gallery(conn, tenant_id=tid, title="Set Me")
    enable_delivery(conn, tid, g["id"])
    conn.commit()
    client.post(f"/galleries/{g['id']}/delivery/expiry", data={"expires_at": _future()})
    assert conn.execute("SELECT delivery_expires_at FROM galleries WHERE id=?",
                        (g["id"],)).fetchone()[0] == _future()
