"""Block off time — a confirmed, client-less calendar entry for personal/busy time.

Shows on the schedule, the agenda, and the subscribe-able calendar feed (all of which
key off confirmed appointments). Not a bookable kind; visibility only.
"""

from conftest import login_owner, onboard_studio

from hestia.scheduler import agenda, create_block, schedule_ics
from hestia.tenants import create_tenant


def _studio(conn):
    t = create_tenant(conn, name="Block Studio", shoot_type="wedding")
    conn.commit()
    return t


def test_create_block_is_confirmed_and_clientless(conn):
    t = _studio(conn)
    b = create_block(conn, tenant_id=t["id"], title="Editing day",
                     starts_at="2026-07-01T14:00", duration_minutes=120)
    assert b["status"] == "confirmed" and b["kind"] == "blocked"
    assert b["client_id"] is None
    assert b["starts_at"] == "2026-07-01 14:00"          # datetime-local 'T' normalized


def test_block_appears_in_agenda_and_feed(conn):
    t = _studio(conn)
    future = conn.execute("SELECT datetime('now', '+3 days')").fetchone()[0]
    create_block(conn, tenant_id=t["id"], title="Vacation", starts_at=future)
    conn.commit()
    titles = [a["title"] for g in agenda(conn, t["id"]) for a in g["appointments"]]
    assert "Vacation" in titles
    assert "Vacation" in schedule_ics(conn, t["id"])      # subscribe-able feed includes it


def test_blank_title_defaults_to_busy(conn):
    t = _studio(conn)
    b = create_block(conn, tenant_id=t["id"], title="   ", starts_at="2026-07-01 09:00")
    assert b["title"] == "Busy"


def test_http_block_flow(client, conn):
    creds = onboard_studio(client, email="blk@example.com")
    login_owner(client, creds)
    assert "/schedule/block" in client.get("/schedule").text          # the action is offered
    future = conn.execute("SELECT datetime('now', '+2 days')").fetchone()[0].replace(" ", "T")[:16]
    client.post("/schedule/block",
                data={"title": "Personal day", "starts_at": future, "duration_minutes": "90"})
    assert "Personal day" in client.get("/schedule").text


def test_http_block_requires_a_time(client):
    creds = onboard_studio(client, email="blk2@example.com")
    login_owner(client, creds)
    client.post("/schedule/block", data={"title": "No time given", "starts_at": ""})
    assert "No time given" not in client.get("/schedule").text        # nothing created
