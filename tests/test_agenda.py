"""Schedule agenda — upcoming confirmed sessions grouped by day."""

import datetime

from conftest import login_owner, onboard_studio

from hestia.crm import create_client
from hestia.db import connect
from hestia.scheduler import agenda, create_appointment
from hestia.tenants import create_tenant


def _future(days: int, hhmm: str = "10:00") -> str:
    d = datetime.date.today() + datetime.timedelta(days=days)
    return f"{d.isoformat()} {hhmm}"


def _confirmed(conn, tenant_id, title, when, client_id=None):
    a = create_appointment(conn, tenant_id=tenant_id, title=title, options=["x"], client_id=client_id)
    conn.execute("UPDATE appointments SET status = 'confirmed', starts_at = ? WHERE id = ?",
                 (when, a["id"]))
    return a


def test_agenda_groups_confirmed_upcoming_by_day(conn):
    t = create_tenant(conn, name="Sch", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Sarah")
    _confirmed(conn, t["id"], "Engagement", _future(0, "09:00"), c["id"])   # today, earlier
    _confirmed(conn, t["id"], "Wedding", _future(0, "14:00"))               # today, later
    _confirmed(conn, t["id"], "Consult", _future(3, "11:00"))              # +3 days
    # excluded: a still-proposed session, one beyond the window, and a past one
    prop = create_appointment(conn, tenant_id=t["id"], title="Maybe", options=["y"])
    conn.execute("UPDATE appointments SET starts_at = ? WHERE id = ?", (_future(1), prop["id"]))
    _confirmed(conn, t["id"], "FarOff", _future(60))
    _confirmed(conn, t["id"], "Past", _future(-2))
    conn.commit()

    ag = agenda(conn, t["id"])
    assert len(ag) == 2                                                     # Today + the +3-day group
    assert ag[0]["label"] == "Today" and len(ag[0]["appointments"]) == 2
    assert [a["title"] for a in ag[0]["appointments"]] == ["Engagement", "Wedding"]  # by time
    assert ag[0]["appointments"][0]["at_time"] == "09:00"
    titles = [a["title"] for g in ag for a in g["appointments"]]
    assert "Consult" in titles
    assert "Maybe" not in titles and "FarOff" not in titles and "Past" not in titles  # all excluded


def test_agenda_is_tenant_scoped(conn):
    a = create_tenant(conn, name="A", shoot_type="wedding")
    b = create_tenant(conn, name="B", shoot_type="wedding")
    _confirmed(conn, b["id"], "B-shoot", _future(0))
    conn.commit()
    assert agenda(conn, a["id"]) == []                                     # B's sessions don't show


def test_schedule_page_shows_agenda(client, app):
    creds = onboard_studio(client, email="ag@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        a = create_appointment(conn, tenant_id=tid, title="ShootDay", options=["x"])
        conn.execute("UPDATE appointments SET status = 'confirmed', starts_at = ? WHERE id = ?",
                     (_future(0, "15:30"), a["id"]))
        conn.commit()
    finally:
        conn.close()
    page = client.get("/schedule")
    assert page.status_code == 200 and "Agenda" in page.text
    assert "ShootDay" in page.text and "15:30" in page.text and "Today" in page.text
