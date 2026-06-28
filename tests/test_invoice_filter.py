"""Invoice list status filter — draft / sent / paid / void, plus an 'overdue' pseudo-status.

Tenant-scoped (inherits list_invoices' scoping); 'overdue' = still 'sent' and past a real
due date. Unknown/blank status returns everything.
"""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client
from hestia.invoices import create_invoice, list_invoices, send_invoice
from hestia.tenants import create_tenant


def _studio(conn):
    t = create_tenant(conn, name="Filter Studio", shoot_type="wedding")
    conn.commit()
    return t


def test_status_filter_data_layer(conn, settings):
    t = _studio(conn)
    c = create_client(conn, tenant_id=t["id"], name="Cli", email="c@x.com")
    draft = create_invoice(conn, settings, tenant_id=t["id"], title="Draft one",
                           amount_cents=100, client_id=c["id"])
    sent = create_invoice(conn, settings, tenant_id=t["id"], title="Sent one",
                          amount_cents=200, client_id=c["id"])
    send_invoice(conn, t["id"], sent["id"])
    conn.commit()
    assert [i["title"] for i in list_invoices(conn, t["id"], status="draft")] == ["Draft one"]
    assert [i["title"] for i in list_invoices(conn, t["id"], status="sent")] == ["Sent one"]
    assert list_invoices(conn, t["id"], status="paid") == []
    assert len(list_invoices(conn, t["id"])) == 2          # no filter → all
    assert draft["id"] != sent["id"]


def test_overdue_pseudo_status(conn, settings):
    t = _studio(conn)
    c = create_client(conn, tenant_id=t["id"], name="Cli", email="c@x.com")
    od = create_invoice(conn, settings, tenant_id=t["id"], title="Past due",
                        amount_cents=500, client_id=c["id"])
    send_invoice(conn, t["id"], od["id"])
    conn.execute("UPDATE invoices SET due_date = date('now','-5 days') WHERE id = ?", (od["id"],))
    # a sent-but-not-yet-due invoice must NOT be overdue
    fut = create_invoice(conn, settings, tenant_id=t["id"], title="Future due",
                         amount_cents=500, client_id=c["id"])
    send_invoice(conn, t["id"], fut["id"])
    conn.execute("UPDATE invoices SET due_date = date('now','+5 days') WHERE id = ?", (fut["id"],))
    conn.commit()
    assert [i["title"] for i in list_invoices(conn, t["id"], status="overdue")] == ["Past due"]


def test_http_filter_pills_and_results(client):
    creds = onboard_studio(client, email="filt@example.com")
    login_owner(client, creds)
    r = client.post("/invoices", data={"title": "SentInvoice", "amount": "200"})
    sent_id = r.url.path.rstrip("/").split("/")[-1]
    client.post(f"/invoices/{sent_id}/send")               # this one is sent
    client.post("/invoices", data={"title": "DraftInvoice", "amount": "100"})   # stays draft
    sent_page = client.get("/invoices?status=sent").text
    assert "SentInvoice" in sent_page and "DraftInvoice" not in sent_page
    draft_page = client.get("/invoices?status=draft").text
    assert "DraftInvoice" in draft_page and "SentInvoice" not in draft_page
    assert 'href="/invoices?status=overdue"' in sent_page  # filter pills rendered


def test_http_empty_filter_message(client):
    creds = onboard_studio(client, email="filt2@example.com")
    login_owner(client, creds)
    assert "No paid invoices" in client.get("/invoices?status=paid").text
