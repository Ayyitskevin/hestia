"""Subscribe-able calendar feed — a token-authorized public .ics any calendar app follows.

The session-gated /schedule/calendar.ics is a browser download; a calendar app carries no
session, so it needs an unguessable public URL instead. That's /calendar/{token}.ics.
"""

from conftest import login_owner, onboard_studio

from hestia.scheduler import (
    ensure_calendar_token,
    get_tenant_by_calendar_token,
    regenerate_calendar_token,
)
from hestia.tenants import create_tenant


def _studio(conn, name="Cal Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def _confirmed(conn, tenant_id, *, title, when="+3 days", token):
    conn.execute(
        "INSERT INTO appointments (tenant_id, title, status, token, starts_at) "
        "VALUES (?, ?, 'confirmed', ?, datetime('now', ?))",
        (tenant_id, title, token, when),
    )
    conn.commit()


def test_ensure_token_is_stable(conn):
    t = _studio(conn)
    tok = ensure_calendar_token(conn, t["id"])
    assert tok and ensure_calendar_token(conn, t["id"]) == tok       # minted once, then stable


def test_regenerate_changes_and_revokes(conn):
    t = _studio(conn)
    tok = ensure_calendar_token(conn, t["id"])
    conn.commit()
    new = regenerate_calendar_token(conn, t["id"])
    assert new != tok
    assert get_tenant_by_calendar_token(conn, tok) is None           # old revoked
    assert get_tenant_by_calendar_token(conn, new)["id"] == t["id"]


def test_lookup_unknown_or_empty(conn):
    assert get_tenant_by_calendar_token(conn, "nope") is None
    assert get_tenant_by_calendar_token(conn, "") is None


def test_public_feed_needs_no_login(client, conn):
    t = _studio(conn)
    tok = ensure_calendar_token(conn, t["id"])
    conn.commit()
    _confirmed(conn, t["id"], title="Beach engagement", token="a1")
    r = client.get(f"/calendar/{tok}.ics")
    assert r.status_code == 200
    assert "text/calendar" in r.headers["content-type"]
    assert "BEGIN:VCALENDAR" in r.text and "Beach engagement" in r.text


def test_unknown_token_404(client):
    assert client.get("/calendar/not-a-real-token.ics").status_code == 404


def test_regenerate_breaks_old_url(client, conn):
    t = _studio(conn)
    tok = ensure_calendar_token(conn, t["id"])
    conn.commit()
    assert client.get(f"/calendar/{tok}.ics").status_code == 200
    new = regenerate_calendar_token(conn, t["id"])
    conn.commit()
    assert client.get(f"/calendar/{tok}.ics").status_code == 404
    assert client.get(f"/calendar/{new}.ics").status_code == 200


def test_feed_is_tenant_scoped(client, conn):
    a = _studio(conn, "A Studio")
    b = _studio(conn, "B Studio")
    atok = ensure_calendar_token(conn, a["id"])
    conn.commit()
    _confirmed(conn, a["id"], title="A session", token="a2")
    _confirmed(conn, b["id"], title="B session", token="b2")
    feed = client.get(f"/calendar/{atok}.ics").text
    assert "A session" in feed and "B session" not in feed


def test_schedule_page_offers_subscription(client):
    creds = onboard_studio(client, email="cal@example.com")
    login_owner(client, creds)
    page = client.get("/schedule").text
    assert "Subscribe to your calendar" in page
    assert "/calendar/" in page and ".ics" in page
