"""Operator-loop closure — booking requests awaiting confirmation and album change
requests now surface in needs_attention, the dashboard, and the owner digest."""

import io

from conftest import login_owner, onboard_studio

from hestia.albums import enable_album_review, generate_album, request_album_changes
from hestia.booking import create_booking_type, request_booking
from hestia.dashboard import build_owner_digest, needs_attention
from hestia.galleries import add_image, create_gallery
from hestia.tenants import create_tenant


def _seed_states(conn, storage, settings, t):
    """One proposed booking request + one album change request for the tenant."""
    bt = create_booking_type(conn, tenant_id=t["id"], title="Engagement")
    request_booking(conn, settings, tenant=t, booking_type=bt, name="Sam",
                    email="sam@ex.com", requested_at="2031-01-01 10:00")   # proposed
    g = create_gallery(conn, tenant_id=t["id"], title="Wedding")
    for i in range(4):
        add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"], filename=f"f{i}.jpg",
                  fileobj=io.BytesIO(bytes([i + 1]) * 20), content_type="image/jpeg")
    conn.commit()
    album = generate_album(conn, settings, tenant=t, gallery=g)
    tok = enable_album_review(conn, t["id"], album["id"])
    request_album_changes(conn, tok, "swap spreads 1 and 2")
    conn.commit()
    return album


def test_needs_attention_surfaces_new_states(conn, storage, settings):
    t = create_tenant(conn, name="Ops Studio", shoot_type="wedding")
    conn.commit()
    _seed_states(conn, storage, settings, t)
    att = needs_attention(conn, t["id"])
    assert [a["title"] for a in att["to_confirm"]] == ["Engagement"]
    assert att["album_changes"][0]["change_request"] == "swap spreads 1 and 2"
    assert att["album_changes"][0]["gallery_title"] == "Wedding"
    assert att["total"] >= 2                        # both count toward the badge


def test_confirmed_and_resolved_states_drop_out(conn, storage, settings):
    from hestia.albums import approve_album
    from hestia.scheduler import confirm_appointment, list_appointments
    t = create_tenant(conn, name="Done Studio", shoot_type="wedding")
    conn.commit()
    album = _seed_states(conn, storage, settings, t)
    appt = list_appointments(conn, t["id"])[0]
    confirm_appointment(conn, t["id"], appt["id"], "2031-01-01 10:00")
    tok = conn.execute("SELECT review_token FROM albums WHERE id = ?",
                       (album["id"],)).fetchone()["review_token"]
    approve_album(conn, tok)                        # approval clears the change request
    conn.commit()
    att = needs_attention(conn, t["id"])
    assert att["to_confirm"] == [] and att["album_changes"] == []


def test_digest_includes_new_sections(conn, storage, settings):
    t = create_tenant(conn, name="Digest Studio", shoot_type="wedding")
    conn.commit()
    _seed_states(conn, storage, settings, t)
    digest = build_owner_digest(conn, t["id"], settings)
    assert "Awaiting a confirmed time" in digest["body"]
    assert "Album change requests" in digest["body"]
    assert "swap spreads 1 and 2" in digest["body"]


def test_dashboard_renders_new_sections(client, conn, storage, settings):
    creds = onboard_studio(client, email="ops@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    tenant = dict(conn.execute("SELECT * FROM tenants WHERE id = ?", (tid,)).fetchone())
    _seed_states(conn, storage, settings, tenant)
    page = client.get("/dashboard").text
    assert "Awaiting a confirmed time" in page
    assert "Album change requests" in page and "swap spreads 1 and 2" in page
