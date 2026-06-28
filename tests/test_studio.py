"""Public studio site — profile, and the inquiry → CRM lead loop."""

from conftest import CSRFClient, login_owner, onboard_studio

from hestia.crm import list_clients, list_projects
from hestia.db import connect
from hestia.packages import list_packages
from hestia.studio import create_inquiry, get_profile, upsert_profile
from hestia.tenants import create_tenant, slugify


def _tid_of(conn, email):
    return conn.execute(
        "SELECT t.id FROM tenants t JOIN users u ON u.tenant_id = t.id WHERE u.email = ?",
        (email,),
    ).fetchone()["id"]


def _latest_notes(conn, tenant_id):
    return conn.execute(
        "SELECT notes FROM projects WHERE tenant_id = ? ORDER BY id DESC LIMIT 1", (tenant_id,)
    ).fetchone()["notes"]


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


# ── Packages on the public site ───────────────────────────────────────────────


def test_public_site_shows_packages(client):
    creds = onboard_studio(client, name="Menu Studio", email="menu@example.com")
    login_owner(client, creds)
    slug = slugify("Menu Studio")
    client.post("/settings/site", data={"headline": "x", "about": "y", "contact_email": "",
                                        "published": "1"})
    client.post("/packages", data={"name": "Gold Wedding", "description": "Full day coverage",
                                   "price": "5000", "deposit": "1500"})
    page = client.get(f"/studio/{slug}").text
    assert "Gold Wedding" in page and "$5,000.00" in page    # public pricing section
    assert "Interested in a package?" in page                # inquiry-form picker


def test_public_inquiry_with_package_folds_into_lead(client, app):
    creds = onboard_studio(client, name="Fold Studio", email="fold@example.com")
    login_owner(client, creds)
    slug = slugify("Fold Studio")
    client.post("/settings/site", data={"headline": "x", "about": "y", "contact_email": "",
                                        "published": "1"})
    client.post("/packages", data={"name": "Elopement", "price": "2000"})
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        pid = list_packages(conn, tid)[0]["id"]
    finally:
        conn.close()

    pub = CSRFClient(app)  # fresh, unauthenticated visitor
    r = pub.post(f"/studio/{slug}/inquire", data={"name": "Dana", "email": "dana@example.com",
                                                  "message": "Sept elopement", "shoot_type": "wedding",
                                                  "package_id": str(pid)})
    assert r.status_code == 200
    conn = connect(app.state.settings.db_path)
    try:
        notes = _latest_notes(conn, tid)
        assert "Interested in: Elopement" in notes and "Sept elopement" in notes
    finally:
        conn.close()


def test_public_inquiry_foreign_package_id_ignored(client, app):
    # a package_id from another tenant must not leak into this studio's lead
    a = onboard_studio(client, name="Studio One", email="one@example.com")
    login_owner(client, a)
    client.post("/packages", data={"name": "One-Only", "price": "999"})
    conn = connect(app.state.settings.db_path)
    try:
        a_pid = list_packages(conn, _tid_of(conn, a["email"]))[0]["id"]
    finally:
        conn.close()

    b_client = CSRFClient(app)
    b = onboard_studio(b_client, name="Studio Two", email="two@example.com")
    login_owner(b_client, b)
    b_client.post("/settings/site", data={"headline": "x", "about": "y", "contact_email": "",
                                          "published": "1"})
    slug = slugify("Studio Two")

    pub = CSRFClient(app)
    pub.post(f"/studio/{slug}/inquire", data={"name": "Vic", "email": "vic@example.com",
                                              "message": "hello", "package_id": str(a_pid)})
    conn = connect(app.state.settings.db_path)
    try:
        notes = _latest_notes(conn, _tid_of(conn, b["email"]))
        assert "One-Only" not in notes                       # foreign package ignored
    finally:
        conn.close()


# ── lead source on the inquiry ─────────────────────────────────────────────────


def test_inquiry_records_lead_source(conn):
    t = _tenant(conn)
    p = create_inquiry(conn, tenant=t, name="Pat", email="p@x.com", shoot_type="wedding",
                       lead_source="Instagram")
    assert p["lead_source"] == "Instagram"


def test_public_inquiry_captures_lead_source(client, app):
    creds = onboard_studio(client, name="Src Studio", email="src@example.com")
    login_owner(client, creds)
    slug = slugify("Src Studio")
    client.post("/settings/site", data={"headline": "x", "about": "y", "contact_email": "",
                                        "published": "1"})
    page = client.get(f"/studio/{slug}").text
    assert "How did you hear about us?" in page                 # the public select renders

    pub = CSRFClient(app)
    pub.post(f"/studio/{slug}/inquire", data={"name": "Dana", "email": "d@x.com",
                                              "shoot_type": "wedding", "lead_source": "Google search"})
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        row = conn.execute("SELECT lead_source FROM projects WHERE tenant_id=? ORDER BY id DESC LIMIT 1",
                           (tid,)).fetchone()
        assert row["lead_source"] == "Google search"
    finally:
        conn.close()
    assert "Lead sources" in client.get("/finances/reports").text   # surfaces in the report
