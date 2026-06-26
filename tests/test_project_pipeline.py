"""Projects pipeline — the portfolio funnel grouped by stage."""

from conftest import login_owner, onboard_studio

from hestia.crm import PROJECT_STATUSES, create_client, create_project, project_pipeline
from hestia.db import connect
from hestia.invoices import create_invoice
from hestia.tenants import create_tenant


def test_pipeline_groups_by_stage_with_collected(conn, settings):
    t = create_tenant(conn, name="P", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Sarah")
    create_project(conn, tenant_id=t["id"], name="Lead1", client_id=c["id"], status="lead")
    create_project(conn, tenant_id=t["id"], name="Booked1", client_id=c["id"], status="booked")
    done = create_project(conn, tenant_id=t["id"], name="Done1", client_id=c["id"], status="delivered")
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="Bal", amount_cents=200000,
                         project_id=done["id"])
    conn.execute("UPDATE invoices SET status = 'paid' WHERE id = ?", (inv["id"],))
    conn.commit()

    stages = project_pipeline(conn, t["id"])
    assert [s["stage"] for s in stages] == list(PROJECT_STATUSES)       # always all stages, in order
    by = {s["stage"]: s for s in stages}
    assert by["lead"]["count"] == 1 and by["booked"]["count"] == 1
    assert by["shooting"]["count"] == 0 and by["archived"]["count"] == 0
    assert by["delivered"]["count"] == 1
    assert by["delivered"]["collected_cents"] == 200000               # paid invoice on that project
    assert by["delivered"]["collected_display"] == "$2,000.00"
    assert by["delivered"]["projects"][0]["name"] == "Done1"


def test_pipeline_is_tenant_scoped(conn):
    a = create_tenant(conn, name="A", shoot_type="wedding")
    b = create_tenant(conn, name="B", shoot_type="wedding")
    create_project(conn, tenant_id=b["id"], name="B-proj", client_id=None, status="booked")
    conn.commit()
    assert all(s["count"] == 0 for s in project_pipeline(conn, a["id"]))  # no cross-tenant projects


def test_pipeline_page_renders(client, app):
    creds = onboard_studio(client, email="pipe@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        create_project(conn, tenant_id=tid, name="MyShoot", client_id=None, status="booked")
        conn.commit()
    finally:
        conn.close()
    page = client.get("/pipeline")
    assert page.status_code == 200 and "Pipeline" in page.text
    assert "MyShoot" in page.text and "booked" in page.text


def test_pipeline_requires_login(client):
    assert client.get("/pipeline", follow_redirects=False).status_code == 303
