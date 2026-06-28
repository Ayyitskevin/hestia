"""Duplicate invoice — clone into a fresh draft for repeat/retainer billing.

Copies title, amounts, tax, client/project, note, and line items; new token, no
payment state. Tenant-scoped.
"""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client
from hestia.invoices import (
    create_invoice,
    duplicate_invoice,
    invoice_items,
    mark_paid,
)
from hestia.tenants import create_tenant


def _studio(conn, name="Dup Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def test_duplicate_copies_fields_and_resets_state(conn, settings):
    t = _studio(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sam", email="s@x.com")
    src = create_invoice(conn, settings, tenant_id=t["id"], title="Retainer",
                         amount_cents=50000, client_id=c["id"], tax_cents=5000, note="Monthly")
    mark_paid(conn, token=src["token"], provider="stripe", ref="x")     # source is paid
    conn.commit()
    new = duplicate_invoice(conn, settings, t["id"], src["id"])
    assert new["id"] != src["id"]
    assert new["title"] == "Retainer" and new["amount_cents"] == 50000 and new["tax_cents"] == 5000
    assert new["client_id"] == c["id"] and new["note"] == "Monthly"
    assert new["status"] == "draft" and new["token"] != src["token"]     # fresh, unpaid


def test_duplicate_copies_line_items(conn, settings):
    t = _studio(conn)
    from hestia.invoices import add_invoice_items
    src = create_invoice(conn, settings, tenant_id=t["id"], title="Package", amount_cents=3000)
    add_invoice_items(conn, t["id"], src["id"], [("Coverage", 2000), ("Album", 1000)])
    conn.commit()
    new = duplicate_invoice(conn, settings, t["id"], src["id"])
    items = invoice_items(conn, t["id"], new["id"])
    assert [(i["description"], i["amount_cents"]) for i in items] == [("Coverage", 2000), ("Album", 1000)]


def test_duplicate_is_tenant_scoped(conn, settings):
    a = _studio(conn, "A")
    b = _studio(conn, "B")
    src = create_invoice(conn, settings, tenant_id=a["id"], title="A's", amount_cents=100)
    conn.commit()
    assert duplicate_invoice(conn, settings, b["id"], src["id"]) is None   # B can't clone A's


def test_http_duplicate_redirects_to_new_draft(client, conn):
    creds = onboard_studio(client, email="dup@example.com")
    login_owner(client, creds)
    r = client.post("/invoices", data={"title": "Rebill me", "items": "Coverage | 1200"})
    iid = r.url.path.rstrip("/").split("/")[-1]
    assert "Duplicate" in client.get(f"/invoices/{iid}").text
    r = client.post(f"/invoices/{iid}/duplicate")
    new_id = r.url.path.rstrip("/").split("/")[-1]
    assert new_id != iid
    detail = client.get(f"/invoices/{new_id}").text
    assert "Rebill me" in detail and "Coverage" in detail and "draft" in detail
