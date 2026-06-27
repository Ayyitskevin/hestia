"""Reconnect — surface past clients who've gone quiet, for a retention nudge.

A client qualifies when their most recent project is older than the quiet cutoff
(~10 months) and they have an email to reach. Clients with a recent project, no
project, or no email don't surface. Tenant-scoped. The dashboard renders the list
with a one-click email (reusing the per-client email feature).
"""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client, create_project
from hestia.dashboard import reconnect_due
from hestia.tenants import create_tenant


def _backdate(conn, project_id, days):
    conn.execute("UPDATE projects SET created_at = datetime('now', ?) WHERE id = ?",
                 (f"-{days} days", project_id))


def test_surfaces_quiet_client_with_email(conn):
    t = create_tenant(conn, name="Studio", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Quiet Quinn", email="quinn@x.com")
    p = create_project(conn, tenant_id=t["id"], name="Wedding 2024", client_id=c["id"])
    _backdate(conn, p["id"], 400)                     # last booked ~13 months ago
    conn.commit()
    due = reconnect_due(conn, t["id"])
    assert [d["name"] for d in due] == ["Quiet Quinn"]
    assert due[0]["email"] == "quinn@x.com"


def test_recent_client_excluded(conn):
    t = create_tenant(conn, name="Studio", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Recent Rae", email="rae@x.com")
    create_project(conn, tenant_id=t["id"], name="Just booked", client_id=c["id"])
    conn.commit()
    assert reconnect_due(conn, t["id"]) == []


def test_quiet_client_without_email_excluded(conn):
    t = create_tenant(conn, name="Studio", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="No Email Ned", email="")
    p = create_project(conn, tenant_id=t["id"], name="Old", client_id=c["id"])
    _backdate(conn, p["id"], 400)
    conn.commit()
    assert reconnect_due(conn, t["id"]) == []


def test_client_with_no_project_excluded(conn):
    t = create_tenant(conn, name="Studio", shoot_type="wedding")
    create_client(conn, tenant_id=t["id"], name="Lead Only", email="lead@x.com")
    conn.commit()
    assert reconnect_due(conn, t["id"]) == []


def test_a_recent_project_keeps_client_active(conn):
    """A client with an old project AND a recent one is still active — MAX date wins."""
    t = create_tenant(conn, name="Studio", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Repeat Robin", email="robin@x.com")
    old = create_project(conn, tenant_id=t["id"], name="2023 shoot", client_id=c["id"])
    _backdate(conn, old["id"], 400)
    create_project(conn, tenant_id=t["id"], name="2026 shoot", client_id=c["id"])
    conn.commit()
    assert reconnect_due(conn, t["id"]) == []


def test_tenant_scoped(conn):
    a = create_tenant(conn, name="A", shoot_type="wedding")
    b = create_tenant(conn, name="B", shoot_type="wedding")
    c = create_client(conn, tenant_id=a["id"], name="A's client", email="a@x.com")
    p = create_project(conn, tenant_id=a["id"], name="Old", client_id=c["id"])
    _backdate(conn, p["id"], 400)
    conn.commit()
    assert reconnect_due(conn, b["id"]) == []
    assert [d["name"] for d in reconnect_due(conn, a["id"])] == ["A's client"]


def test_oldest_quiet_first(conn):
    t = create_tenant(conn, name="Studio", shoot_type="wedding")
    for name, days in [("Six months", 350), ("Two years", 730), ("One year", 400)]:
        cl = create_client(conn, tenant_id=t["id"], name=name, email=f"{days}@x.com")
        pr = create_project(conn, tenant_id=t["id"], name="p", client_id=cl["id"])
        _backdate(conn, pr["id"], days)
    conn.commit()
    assert [d["name"] for d in reconnect_due(conn, t["id"])] == ["Two years", "One year", "Six months"]


def test_dashboard_shows_reconnect_card(client, conn):
    creds = onboard_studio(client, email="rc@example.com")
    login_owner(client, creds)
    r = client.post("/clients", data={"name": "Nostalgia Nan", "email": "nan@example.com"})
    cid = r.url.path.rstrip("/").split("/")[-1]
    client.post("/projects", data={"name": "Old wedding", "client_id": cid})
    conn.execute("UPDATE projects SET created_at = datetime('now','-400 days') WHERE client_id = ?",
                 (cid,))
    conn.commit()
    page = client.get("/dashboard")
    assert "Reconnect" in page.text and "Nostalgia Nan" in page.text
    assert f"/clients/{cid}/email" in page.text
