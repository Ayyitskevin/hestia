"""CRM module — clients, projects, gallery linkage, tenant isolation."""

from conftest import login_owner, onboard_studio

from hestia.crm import (
    assign_gallery_to_project,
    create_client,
    create_project,
    galleries_for_project,
    get_project,
    list_clients,
    list_projects,
    set_project_status,
)
from hestia.galleries import create_gallery
from hestia.tenants import create_tenant


def _tenant(conn, name="CRM Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def test_client_crud_and_project_count(conn):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah Lin", email="s@example.com")
    assert c["name"] == "Sarah Lin"
    rows = list_clients(conn, t["id"])
    assert len(rows) == 1 and rows[0]["project_count"] == 0
    create_project(conn, tenant_id=t["id"], name="Wedding", client_id=c["id"])
    assert list_clients(conn, t["id"])[0]["project_count"] == 1


def test_project_join_and_status(conn):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Pat")
    p = create_project(conn, tenant_id=t["id"], name="Engagement", client_id=c["id"],
                       shoot_type="portrait")
    got = get_project(conn, t["id"], p["id"])
    assert got["client_name"] == "Pat"
    assert got["shoot_type"] == "portrait"
    set_project_status(conn, t["id"], p["id"], "booked")
    assert get_project(conn, t["id"], p["id"])["status"] == "booked"
    # invalid status is ignored
    set_project_status(conn, t["id"], p["id"], "bogus")
    assert get_project(conn, t["id"], p["id"])["status"] == "booked"


def test_project_create_drops_foreign_client_id(conn):
    a = _tenant(conn, "A")
    b = _tenant(conn, "B")
    foreign = create_client(conn, tenant_id=a["id"], name="Foreign")
    p = create_project(conn, tenant_id=b["id"], name="B Project", client_id=foreign["id"])
    assert p["client_id"] is None
    assert get_project(conn, b["id"], p["id"])["client_name"] is None


def test_gallery_links_to_project(conn):
    t = _tenant(conn)
    p = create_project(conn, tenant_id=t["id"], name="Shoot")
    g = create_gallery(conn, tenant_id=t["id"], title="Gallery A")
    assign_gallery_to_project(conn, t["id"], g["id"], p["id"])
    linked = galleries_for_project(conn, t["id"], p["id"])
    assert len(linked) == 1 and linked[0]["id"] == g["id"]
    assert list_projects(conn, t["id"])[0]["gallery_count"] == 1


def test_assign_rejects_foreign_project(conn):
    t1 = _tenant(conn, "T1")
    t2 = _tenant(conn, "T2")
    p2 = create_project(conn, tenant_id=t2["id"], name="T2 project")
    g1 = create_gallery(conn, tenant_id=t1["id"], title="T1 gallery")
    # t1 cannot attach its gallery to t2's project
    assign_gallery_to_project(conn, t1["id"], g1["id"], p2["id"])
    assert galleries_for_project(conn, t2["id"], p2["id"]) == []


def test_tenant_isolation_on_clients(conn):
    t1 = _tenant(conn, "A")
    t2 = _tenant(conn, "B")
    create_client(conn, tenant_id=t1["id"], name="A-client")
    assert list_clients(conn, t2["id"]) == []


def test_http_create_client_and_project_and_link(client):
    creds = onboard_studio(client, email="crm@example.com")
    login_owner(client, creds)

    r = client.post("/clients", data={"name": "Acme Corp", "email": "hi@acme.test"})
    cid = r.url.path.rstrip("/").split("/")[-1]
    assert client.get(f"/clients/{cid}").status_code == 200

    r = client.post("/projects", data={"name": "Spring Campaign", "client_id": cid,
                                        "shoot_type": "commercial", "status": "booked"})
    pid = r.url.path.rstrip("/").split("/")[-1]
    assert client.get(f"/projects/{pid}").status_code == 200

    # create a gallery attached to the project
    client.post("/galleries", data={"title": "Campaign Gallery", "project_id": pid})
    page = client.get(f"/projects/{pid}")
    assert "Campaign Gallery" in page.text  # gallery shows under its project
