"""Public studio site — profile, and the inquiry → CRM lead loop."""

from conftest import login_owner, onboard_studio

from hestia.crm import list_clients, list_projects
from hestia.studio import create_inquiry, get_profile, upsert_profile
from hestia.tenants import create_tenant, slugify


def _tenant(conn, name="Site Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def test_profile_default_and_upsert(conn):
    t = _tenant(conn)
    p = get_profile(conn, t["id"])
    assert p["published"] == 0 and p["headline"] == ""
    upsert_profile(conn, tenant_id=t["id"], headline="Hi", about="A studio",
                   contact_email="hi@studio.test", published=True)
    p = get_profile(conn, t["id"])
    assert p["headline"] == "Hi" and p["published"] == 1
    # update in place (no duplicate row)
    upsert_profile(conn, tenant_id=t["id"], headline="Hi2", about="x", contact_email="", published=False)
    assert get_profile(conn, t["id"])["published"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM studio_profiles").fetchone()["n"] == 1


def test_inquiry_creates_client_and_lead(conn):
    t = _tenant(conn)
    project = create_inquiry(conn, tenant=t, name="Pat Jones", email="pat@example.com",
                             message="Need a wedding shooter", shoot_type="wedding",
                             event_date="2026-09-12")
    assert project["status"] == "lead"
    assert project["shoot_type"] == "wedding"
    clients = list_clients(conn, t["id"])
    assert any(c["name"] == "Pat Jones" for c in clients)
    projects = list_projects(conn, t["id"])
    assert projects and projects[0]["status"] == "lead"


def test_inquiry_tenant_scoped(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    create_inquiry(conn, tenant=t1, name="Lead A", email="a@x.com")
    assert list_clients(conn, t2["id"]) == []


def test_public_site_unpublished_then_published(client):
    creds = onboard_studio(client, name="PNW Weddings", email="pnw@example.com")
    login_owner(client, creds)
    slug = slugify("PNW Weddings")

    # unpublished → coming soon, no inquiry form
    page = client.get(f"/studio/{slug}")
    assert page.status_code == 200 and "coming soon" in page.text.lower()
    assert "Send inquiry" not in page.text

    # owner publishes
    assert client.get("/settings/site").status_code == 200
    client.post("/settings/site", data={"headline": "Timeless", "about": "We shoot love",
                                         "contact_email": "hi@pnw.test", "published": "1"})
    live = client.get(f"/studio/{slug}")
    assert live.status_code == 200 and "Send inquiry" in live.text


def test_public_inquiry_becomes_lead(client):
    creds = onboard_studio(client, name="Inquiry Studio", email="inq@example.com")
    login_owner(client, creds)
    slug = slugify("Inquiry Studio")
    client.post("/settings/site", data={"headline": "x", "about": "y", "contact_email": "",
                                        "published": "1"})

    pub = client.__class__(client.app)  # fresh, unauthenticated visitor
    r = pub.post(f"/studio/{slug}/inquire", data={"name": "Sam Visitor", "email": "sam@example.com",
                                                  "message": "June wedding", "shoot_type": "wedding"})
    assert r.status_code == 200 and "Inquiry received" in r.text

    # the lead shows up in the studio's CRM
    assert "Sam Visitor" in client.get("/clients").text
