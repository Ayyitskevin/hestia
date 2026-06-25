"""Audit trail — enrichment at key events + the owner activity feed."""

from conftest import login_owner, onboard_studio

from hestia.db import audit, list_audit
from hestia.invoices import create_invoice, mark_paid
from hestia.tenants import create_tenant


def test_list_audit_is_tenant_scoped_and_ordered(conn):
    a = create_tenant(conn, name="Studio A", shoot_type="other")
    b = create_tenant(conn, name="Studio B", shoot_type="other")
    audit(conn, actor="owner", action="first", tenant_id=a["id"])
    audit(conn, actor="owner", action="second", tenant_id=a["id"])
    audit(conn, actor="owner", action="elsewhere", tenant_id=b["id"])
    conn.commit()
    events = list_audit(conn, a["id"])
    assert [e["action"] for e in events] == ["second", "first"]  # most-recent-first
    assert all(e["action"] != "elsewhere" for e in events)       # tenant isolation


def test_mark_paid_audits_once(conn, settings):
    t = create_tenant(conn, name="Pay Co", shoot_type="other")
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="Balance", amount_cents=5000)
    assert mark_paid(conn, token=inv["token"], provider="mock", ref="r1") is True
    conn.commit()
    paid = [e for e in list_audit(conn, t["id"]) if e["action"] == "invoice.paid"]
    assert len(paid) == 1 and "Balance" in paid[0]["detail"]
    # idempotent: a duplicate settle must not re-audit
    assert mark_paid(conn, token=inv["token"], provider="mock", ref="r2") is False
    conn.commit()
    assert len([e for e in list_audit(conn, t["id"]) if e["action"] == "invoice.paid"]) == 1


def test_invoice_send_and_void_are_audited(client, conn):
    login_owner(client, onboard_studio(client, email="aud@inv.com"))
    iid = int(str(client.post("/invoices", data={"title": "Shoot", "amount": "300"}).url)
              .rstrip("/").split("/")[-1])
    client.post(f"/invoices/{iid}/send")
    client.post(f"/invoices/{iid}/void")
    tid = conn.execute("SELECT tenant_id FROM invoices WHERE id = ?", (iid,)).fetchone()["tenant_id"]
    actions = [e["action"] for e in list_audit(conn, tid)]
    assert "invoice.sent" in actions and "invoice.void" in actions


def test_gallery_publish_is_audited(client, conn):
    login_owner(client, onboard_studio(client, email="aud@gal.com"))
    gid = client.post("/galleries", data={"title": "Wedding"}).url.path.rstrip("/").split("/")[-1]
    client.post(f"/galleries/{gid}/publish")
    tid = conn.execute("SELECT tenant_id FROM galleries WHERE id = ?", (gid,)).fetchone()["tenant_id"]
    pub = [e for e in list_audit(conn, tid) if e["action"] == "gallery.published"]
    assert len(pub) == 1 and pub[0]["detail"] == "Wedding"


def test_activity_view_renders_humanized(client, conn):
    login_owner(client, onboard_studio(client, email="aud@act.com"))
    gid = client.post("/galleries", data={"title": "Seaside"}).url.path.rstrip("/").split("/")[-1]
    client.post(f"/galleries/{gid}/publish")
    page = client.get("/settings/activity")
    assert page.status_code == 200
    assert "Gallery published" in page.text  # humanized label, not the raw action key
    assert "Seaside" in page.text
