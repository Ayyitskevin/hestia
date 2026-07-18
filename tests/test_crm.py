"""CRM module — clients, projects, gallery linkage, tenant isolation."""

import threading
from concurrent.futures import ThreadPoolExecutor

from conftest import CSRFClient, login_owner, onboard_studio

from hestia.crm import (
    assign_gallery_to_project,
    create_client,
    create_project,
    galleries_for_project,
    get_client,
    get_project,
    list_clients,
    list_projects,
    set_project_status,
    update_client,
)
from hestia.db import connect
from hestia.email import list_emails
from hestia.galleries import create_gallery, get_gallery, publish_gallery
from hestia.portal import enable_portal
from hestia.proofing import selection_packet
from hestia.routes import galleries as gallery_routes
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


def test_update_client_is_bounded_clearable_and_tenant_scoped(conn):
    owner = _tenant(conn, "Owner")
    other = _tenant(conn, "Other")
    c = create_client(
        conn,
        tenant_id=owner["id"],
        name="Original",
        email="old@example.com",
        phone="555-0100",
        notes="Original note",
    )

    assert update_client(
        conn,
        owner["id"],
        c["id"],
        name="  " + ("N" * 205),
        email="e" * 260,
        phone="1" * 45,
        notes="x" * 20_005,
    )
    updated = get_client(conn, owner["id"], c["id"])
    assert len(updated["name"]) == 200
    assert len(updated["email"]) == 254
    assert len(updated["phone"]) == 40
    assert len(updated["notes"]) == 20_000

    assert not update_client(conn, other["id"], c["id"], name="Hijacked")
    assert get_client(conn, owner["id"], c["id"])["name"] == "N" * 200
    assert not update_client(conn, owner["id"], c["id"], name="   ")
    assert update_client(conn, owner["id"], c["id"], name="Corrected")
    cleared = get_client(conn, owner["id"], c["id"])
    assert cleared["name"] == "Corrected"
    assert cleared["email"] == cleared["phone"] == cleared["notes"] == ""


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


def test_gallery_project_assignment_moves_clears_and_is_idempotent(conn):
    owner = _tenant(conn, "Owner")
    foreign = _tenant(conn, "Foreign")
    first = create_project(conn, tenant_id=owner["id"], name="First")
    second = create_project(conn, tenant_id=owner["id"], name="Second")
    foreign_project = create_project(conn, tenant_id=foreign["id"], name="Foreign")
    gallery = create_gallery(conn, tenant_id=owner["id"], title="Repair me")

    assert assign_gallery_to_project(conn, owner["id"], gallery["id"], first["id"])
    assert not assign_gallery_to_project(conn, owner["id"], gallery["id"], first["id"])
    assert assign_gallery_to_project(conn, owner["id"], gallery["id"], second["id"])
    assert galleries_for_project(conn, owner["id"], first["id"]) == []
    assert [g["id"] for g in galleries_for_project(conn, owner["id"], second["id"])] == [
        gallery["id"]
    ]

    assert not assign_gallery_to_project(
        conn, owner["id"], gallery["id"], foreign_project["id"]
    )
    assert get_gallery(conn, owner["id"], gallery["id"])["project_id"] == second["id"]
    assert not assign_gallery_to_project(conn, foreign["id"], gallery["id"], None)

    assert assign_gallery_to_project(conn, owner["id"], gallery["id"], None)
    assert not assign_gallery_to_project(conn, owner["id"], gallery["id"], None)
    assert get_gallery(conn, owner["id"], gallery["id"])["project_id"] is None


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


def test_http_gallery_project_repair_moves_portal_visibility(client, app):
    creds = onboard_studio(client, email="gallery-repair@example.com")
    login_owner(client, creds)

    with connect(app.state.settings.db_path) as conn:
        tenant_id = conn.execute("SELECT id FROM tenants").fetchone()["id"]
        first_client = create_client(
            conn, tenant_id=tenant_id, name="First Client", email="first@example.com"
        )
        second_client = create_client(
            conn, tenant_id=tenant_id, name="Second Client", email="second@example.com"
        )
        first = create_project(
            conn, tenant_id=tenant_id, name="First Project", client_id=first_client["id"]
        )
        second = create_project(
            conn, tenant_id=tenant_id, name="Second Project", client_id=second_client["id"]
        )
        gallery = create_gallery(
            conn,
            tenant_id=tenant_id,
            title="Published Gallery",
            client_name="Legacy Client",
        )
        assign_gallery_to_project(conn, tenant_id, gallery["id"], first["id"])
        publish_gallery(conn, tenant_id, gallery["id"])
        first_token = enable_portal(conn, tenant_id, first_client["id"])
        second_token = enable_portal(conn, tenant_id, second_client["id"])
        tenant_slug = conn.execute(
            "SELECT slug FROM tenants WHERE id = ?", (tenant_id,)
        ).fetchone()["slug"]

    detail = client.get(f"/galleries/{gallery['id']}")
    assert f'action="/galleries/{gallery["id"]}/project"' in detail.text
    assert 'value="{}" selected'.format(first["id"]) in detail.text
    assert "Second Project — Second Client" in detail.text
    assert "Published Gallery" in client.get(f"/portal/{first_token}").text
    assert "Published Gallery" not in client.get(f"/portal/{second_token}").text

    moved = client.post(
        f"/galleries/{gallery['id']}/project",
        data={"project_id": str(second["id"])},
        follow_redirects=False,
    )
    assert moved.status_code == 303
    assert moved.headers["location"] == f"/galleries/{gallery['id']}"
    assert "Published Gallery" not in client.get(f"/portal/{first_token}").text
    assert "Published Gallery" in client.get(f"/portal/{second_token}").text

    with connect(app.state.settings.db_path) as conn:
        assert selection_packet(conn, tenant_id, gallery["id"])["client_name"] == "Second Client"

    submitted = client.post(
        f"/g/{tenant_slug}/{gallery['slug']}/submit", follow_redirects=False
    )
    assert submitted.status_code == 303
    with connect(app.state.settings.db_path) as conn:
        owner_messages = list_emails(conn, tenant_id)
    assert any("Second Client sent their favorites" in message["subject"] for message in owner_messages)
    assert all("Legacy Client" not in message["subject"] for message in owner_messages)

    cleared = client.post(
        f"/galleries/{gallery['id']}/project",
        data={"project_id": ""},
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    assert "Published Gallery" not in client.get(f"/portal/{second_token}").text

    with connect(app.state.settings.db_path) as conn:
        assert get_gallery(conn, tenant_id, gallery["id"])["project_id"] is None
        events = conn.execute(
            "SELECT detail FROM audit_log WHERE tenant_id = ? "
            "AND action = 'gallery.project_changed' ORDER BY id",
            (tenant_id,),
        ).fetchall()
    assert len(events) == 2
    assert f"project #{first['id']} -> #{second['id']}" in events[0]["detail"]
    assert f"project #{second['id']} -> none" in events[1]["detail"]


def test_http_gallery_project_repair_rejects_malformed_ids(client, app):
    creds = onboard_studio(client, email="gallery-repair-invalid@example.com")
    login_owner(client, creds)

    with connect(app.state.settings.db_path) as conn:
        tenant_id = conn.execute("SELECT id FROM tenants").fetchone()["id"]
        project = create_project(conn, tenant_id=tenant_id, name="Keep me")
        gallery = create_gallery(conn, tenant_id=tenant_id, title="Stay linked")
        assign_gallery_to_project(conn, tenant_id, gallery["id"], project["id"])

    for invalid_id in ("²", str(1 << 63), "9" * 5000):
        response = client.post(
            f"/galleries/{gallery['id']}/project",
            data={"project_id": invalid_id},
            follow_redirects=False,
        )
        assert response.status_code == 303

    with connect(app.state.settings.db_path) as conn:
        assert get_gallery(conn, tenant_id, gallery["id"])["project_id"] == project["id"]
        count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE tenant_id = ? "
            "AND action = 'gallery.project_changed'",
            (tenant_id,),
        ).fetchone()[0]
    assert count == 0


def test_http_gallery_project_repair_serializes_audit_chain(app, monkeypatch):
    setup_client = CSRFClient(app)
    creds = onboard_studio(setup_client, email="gallery-repair-race@example.com")
    login_owner(setup_client, creds)

    with connect(app.state.settings.db_path) as conn:
        tenant_id = conn.execute("SELECT id FROM tenants").fetchone()["id"]
        original = create_project(conn, tenant_id=tenant_id, name="Original")
        left = create_project(conn, tenant_id=tenant_id, name="Left")
        right = create_project(conn, tenant_id=tenant_id, name="Right")
        gallery = create_gallery(conn, tenant_id=tenant_id, title="Concurrent repair")
        assign_gallery_to_project(conn, tenant_id, gallery["id"], original["id"])

    left_client = login_owner(CSRFClient(app), creds)
    right_client = login_owner(CSRFClient(app), creds)
    real_get_gallery = gallery_routes.get_gallery
    read_barrier = threading.Barrier(2)

    def synchronized_get_gallery(conn, owner_tenant_id, gallery_id):
        row = real_get_gallery(conn, owner_tenant_id, gallery_id)
        try:
            read_barrier.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return row

    monkeypatch.setattr(gallery_routes, "get_gallery", synchronized_get_gallery)
    start_barrier = threading.Barrier(3)

    def move(client, target_id):
        start_barrier.wait()
        return client.post(
            f"/galleries/{gallery['id']}/project",
            data={"project_id": str(target_id)},
            follow_redirects=False,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        left_result = pool.submit(move, left_client, left["id"])
        right_result = pool.submit(move, right_client, right["id"])
        start_barrier.wait()
        responses = [left_result.result(), right_result.result()]
    assert [response.status_code for response in responses] == [303, 303]

    with connect(app.state.settings.db_path) as conn:
        final_project_id = get_gallery(conn, tenant_id, gallery["id"])["project_id"]
        events = [
            row["detail"]
            for row in conn.execute(
                "SELECT detail FROM audit_log WHERE tenant_id = ? "
                "AND action = 'gallery.project_changed' ORDER BY id",
                (tenant_id,),
            ).fetchall()
        ]
    left_then_right = [
        f"gallery #{gallery['id']} · project #{original['id']} -> #{left['id']}",
        f"gallery #{gallery['id']} · project #{left['id']} -> #{right['id']}",
    ]
    right_then_left = [
        f"gallery #{gallery['id']} · project #{original['id']} -> #{right['id']}",
        f"gallery #{gallery['id']} · project #{right['id']} -> #{left['id']}",
    ]
    assert events in (left_then_right, right_then_left)
    assert final_project_id == (right["id"] if events == left_then_right else left["id"])


def test_http_edit_client_recovers_missing_contact_details(client):
    creds = onboard_studio(client, email="client-edit@example.com")
    login_owner(client, creds)
    created = client.post("/clients", data={"name": "Tyop Name"})
    client_id = created.url.path.rstrip("/").split("/")[-1]

    detail = client.get(f"/clients/{client_id}")
    assert f'href="/clients/{client_id}/edit"' in detail.text
    edit = client.get(f"/clients/{client_id}/edit")
    assert edit.status_code == 200
    assert 'value="Tyop Name"' in edit.text

    saved = client.post(
        f"/clients/{client_id}/edit",
        data={
            "name": "Taylor Name",
            "email": "taylor@example.com",
            "phone": "555-0199",
            "notes": "Corrected after the inquiry.",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == f"/clients/{client_id}"
    updated = client.get(saved.headers["location"])
    assert "Taylor Name" in updated.text
    assert "taylor@example.com" in updated.text
    assert "555-0199" in updated.text
    assert "Corrected after the inquiry." in updated.text
    assert f'href="/clients/{client_id}/email"' in updated.text
