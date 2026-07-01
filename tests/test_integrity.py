"""Owner-facing integrity audit and repair for malformed legacy parent links."""

import io

from conftest import ADMIN_TOKEN, CSRFClient, login_owner, onboard_studio

from hestia.crm import create_client, create_project
from hestia.db import connect
from hestia.finances import create_expense
from hestia.galleries import add_image, create_gallery
from hestia.integrity import integrity_report, repair_integrity, tenant_integrity_overview
from hestia.invoices import create_invoice
from hestia.tenants import create_tenant


def _paid_invoice(conn, settings, *, tenant_id, cents, client_id=None, project_id=None):
    inv = create_invoice(conn, settings, tenant_id=tenant_id, title="Pkg", amount_cents=cents,
                         client_id=client_id, project_id=project_id)
    conn.execute("UPDATE invoices SET status = 'paid' WHERE id = ?", (inv["id"],))
    return inv


def _dirty_tenant_with_manual_and_repairable_issue(conn, settings):
    clean = create_tenant(conn, name="Clean Studio", shoot_type="wedding")
    dirty = create_tenant(conn, name="Dirty Studio", shoot_type="wedding")
    sarah = create_client(conn, tenant_id=dirty["id"], name="Sarah")
    bob = create_client(conn, tenant_id=dirty["id"], name="Bob")
    bob_project = create_project(conn, tenant_id=dirty["id"], name="Bob Shoot", client_id=bob["id"],
                                 shoot_type="wedding", status="booked")
    inv = _paid_invoice(conn, settings, tenant_id=dirty["id"], cents=100000, client_id=sarah["id"])
    conn.execute("UPDATE invoices SET project_id = ? WHERE id = ?", (bob_project["id"], inv["id"]))
    clean_gallery = create_gallery(conn, tenant_id=clean["id"], title="Clean Gallery")
    conn.execute(
        "INSERT INTO images (gallery_id, tenant_id, filename, storage_key) VALUES (?, ?, 'bad.jpg', 'bad')",
        (clean_gallery["id"], dirty["id"]),
    )
    conn.commit()
    return clean, dirty


def test_tenant_integrity_overview_summarizes_all_studios(conn, settings):
    clean, dirty = _dirty_tenant_with_manual_and_repairable_issue(conn, settings)

    overview = tenant_integrity_overview(conn)
    by_id = {t["id"]: t for t in overview["tenants"]}
    assert overview["tenant_count"] == 2
    assert overview["dirty_tenants"] == 1
    assert overview["total"] == 2
    assert overview["repairable_total"] == 1
    assert overview["manual_total"] == 1
    assert by_id[clean["id"]]["clean"] is True
    assert by_id[dirty["id"]]["total"] == 2
    assert by_id[dirty["id"]]["manual_rules"][0]["label"] == "Images: invalid gallery links"
    assert by_id[dirty["id"]]["repairable_rules"][0]["label"] == "Invoices: client/project mismatches"
    assert overview["tenants"][0]["id"] == dirty["id"]            # dirty studios sort first


def test_integrity_repair_clears_optional_legacy_links(conn, settings, storage):
    mine = create_tenant(conn, name="Mine", shoot_type="wedding")
    theirs = create_tenant(conn, name="Theirs", shoot_type="wedding")
    sarah = create_client(conn, tenant_id=mine["id"], name="Sarah")
    bob = create_client(conn, tenant_id=mine["id"], name="Bob")
    bob_project = create_project(conn, tenant_id=mine["id"], name="Bob Shoot", client_id=bob["id"],
                                 shoot_type="wedding", status="booked")
    foreign_project = create_project(conn, tenant_id=theirs["id"], name="Foreign",
                                     shoot_type="wedding", status="booked")
    inv = _paid_invoice(conn, settings, tenant_id=mine["id"], cents=100000, client_id=sarah["id"])
    conn.execute("UPDATE invoices SET project_id = ? WHERE id = ?", (bob_project["id"], inv["id"]))
    expense = create_expense(conn, tenant_id=mine["id"], amount_cents=7500, category="gear")
    conn.execute("UPDATE expenses SET project_id = ? WHERE id = ?", (foreign_project["id"], expense["id"]))

    gallery = create_gallery(conn, tenant_id=mine["id"], title="Mine")
    foreign_gallery = create_gallery(conn, tenant_id=theirs["id"], title="Theirs")
    foreign_image = add_image(conn, storage, tenant_id=theirs["id"], gallery_id=foreign_gallery["id"],
                              filename="x.jpg", fileobj=io.BytesIO(b"x"))
    conn.execute("UPDATE galleries SET cover_image_id = ? WHERE id = ?", (foreign_image["id"], gallery["id"]))
    conn.commit()

    report = integrity_report(conn, mine["id"])
    assert report["repairable_total"] == 3
    assert {r["code"] for r in report["active_rules"]} == {
        "invoices.mismatched_project",
        "expenses.invalid_project",
        "galleries.invalid_cover",
    }

    result = repair_integrity(conn, mine["id"])
    assert result["fixed_total"] == 3
    assert result["report"]["total"] == 0
    row = conn.execute("SELECT client_id, project_id FROM invoices WHERE id = ?", (inv["id"],)).fetchone()
    assert row["client_id"] == sarah["id"] and row["project_id"] is None
    assert conn.execute("SELECT project_id FROM expenses WHERE id = ?", (expense["id"],)).fetchone()[0] is None
    assert conn.execute("SELECT cover_image_id FROM galleries WHERE id = ?", (gallery["id"],)).fetchone()[0] is None


def test_integrity_repair_deletes_invalid_proofing_rows(conn, storage):
    owner = create_tenant(conn, name="Owner", shoot_type="wedding")
    other = create_tenant(conn, name="Other", shoot_type="wedding")
    gallery = create_gallery(conn, tenant_id=owner["id"], title="Owner gallery")
    image = add_image(conn, storage, tenant_id=owner["id"], gallery_id=gallery["id"],
                      filename="a.jpg", fileobj=io.BytesIO(b"x"))
    conn.execute(
        "INSERT INTO image_favorites (tenant_id, gallery_id, image_id) VALUES (?, ?, ?)",
        (other["id"], gallery["id"], image["id"]),
    )
    conn.execute(
        "INSERT INTO image_comments (tenant_id, gallery_id, image_id, body) VALUES (?, ?, ?, 'x')",
        (other["id"], gallery["id"], image["id"]),
    )
    conn.commit()

    report = integrity_report(conn, other["id"])
    assert report["repairable_total"] == 2
    result = repair_integrity(conn, other["id"])
    assert result["fixed_total"] == 2
    assert conn.execute("SELECT COUNT(*) FROM image_favorites").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM image_comments").fetchone()[0] == 0


def test_integrity_page_reports_and_repairs(client, app):
    creds = onboard_studio(client, email="integrity@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        sarah = create_client(conn, tenant_id=tid, name="Sarah")
        bob = create_client(conn, tenant_id=tid, name="Bob")
        project = create_project(conn, tenant_id=tid, name="Bob Shoot", client_id=bob["id"],
                                 shoot_type="wedding", status="booked")
        inv = _paid_invoice(conn, app.state.settings, tenant_id=tid, cents=100000,
                            client_id=sarah["id"])
        conn.execute("UPDATE invoices SET project_id = ? WHERE id = ?", (project["id"], inv["id"]))
        conn.commit()
    finally:
        conn.close()

    settings_page = client.get("/settings/site")
    assert settings_page.status_code == 200
    assert "Data integrity" in settings_page.text
    assert "1 hidden relationship issue" in settings_page.text

    detail = client.get("/settings/integrity")
    assert detail.status_code == 200
    assert "Invoices: client/project mismatches" in detail.text
    assert "Repair 1 issue" in detail.text

    repaired = client.post("/settings/integrity/repair")
    assert repaired.status_code == 200
    assert "1 issue repaired" in repaired.text
    assert "All clear" in repaired.text

    conn = connect(app.state.settings.db_path)
    try:
        assert conn.execute("SELECT project_id FROM invoices WHERE id = ?", (inv["id"],)).fetchone()[0] is None
        audit = conn.execute(
            "SELECT action, detail FROM audit_log WHERE tenant_id = ? ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert audit["action"] == "integrity.repaired"
        assert "1 hidden relationship issue" in audit["detail"]
    finally:
        conn.close()


def test_admin_integrity_overview_reports_dirty_tenants(app):
    admin = CSRFClient(app)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})
    conn = connect(app.state.settings.db_path)
    try:
        _clean, dirty = _dirty_tenant_with_manual_and_repairable_issue(conn, app.state.settings)
    finally:
        conn.close()

    page = admin.get("/admin/integrity")
    assert page.status_code == 200
    assert "Fleet summary" in page.text
    assert "Dirty Studio" in page.text
    assert "Images: invalid gallery links" in page.text
    assert "Invoices: client/project mismatches" in page.text
    assert f"/admin/tenants/{dirty['id']}" in page.text
    assert 'href="/admin/integrity"' in admin.get("/admin/tenants").text


def test_admin_integrity_requires_admin(client):
    r = client.get("/admin/integrity", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
