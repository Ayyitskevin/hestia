"""Payment receipt — owner emails the client a paid confirmation.

Works however the invoice was settled (online or recorded offline). A no-op when the
invoice isn't paid or the client has no email. Rendered from the customizable
"Payment receipt" template and sent through the usual notify() chokepoint.
"""

from conftest import login_owner, onboard_studio

from hestia.email import list_emails


def _paid_invoice(client, *, email="client@example.com", amount="500"):
    r = client.post("/clients", data={"name": "Pat Client", "email": email})
    cid = r.url.path.rstrip("/").split("/")[-1]
    r = client.post("/invoices", data={"title": "Session fee", "amount": amount, "client_id": cid})
    iid = r.url.path.rstrip("/").split("/")[-1]
    client.post(f"/invoices/{iid}/record-payment", data={"method": "cash"})
    return iid


def _tenant_id(conn):
    return conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]


def test_receipt_emails_client(client, conn):
    creds = onboard_studio(client, name="Lumen Studio", email="rcpt1@example.com")
    login_owner(client, creds)
    iid = _paid_invoice(client)
    assert "Send receipt" in client.get(f"/invoices/{iid}").text   # control shown once paid
    client.post(f"/invoices/{iid}/receipt")
    receipts = [m for m in list_emails(conn, _tenant_id(conn))
                if m["to_addr"] == "client@example.com" and "Receipt" in m["subject"]]
    assert receipts and "$500.00" in receipts[0]["body"]           # amount filled in


def test_no_receipt_for_unpaid_invoice(client, conn):
    creds = onboard_studio(client, email="rcpt2@example.com")
    login_owner(client, creds)
    r = client.post("/clients", data={"name": "Pat", "email": "p@example.com"})
    cid = r.url.path.rstrip("/").split("/")[-1]
    r = client.post("/invoices", data={"title": "Unpaid", "amount": "100", "client_id": cid})
    iid = r.url.path.rstrip("/").split("/")[-1]
    assert "Send receipt" not in client.get(f"/invoices/{iid}").text
    client.post(f"/invoices/{iid}/receipt")                        # no-op
    assert not any("Receipt" in m["subject"] for m in list_emails(conn, _tenant_id(conn)))


def test_no_receipt_without_client_email(client, conn):
    creds = onboard_studio(client, email="rcpt3@example.com")
    login_owner(client, creds)
    iid = _paid_invoice(client, email="")                          # paid, but no address
    assert "Send receipt" not in client.get(f"/invoices/{iid}").text
    client.post(f"/invoices/{iid}/receipt")
    assert not any("Receipt" in m["subject"] for m in list_emails(conn, _tenant_id(conn)))


def test_receipt_template_is_customizable(client):
    creds = onboard_studio(client, email="rcpt4@example.com")
    login_owner(client, creds)
    assert "Payment receipt" in client.get("/settings/messages").text


def test_foreign_invoice_is_safe_noop(client):
    creds = onboard_studio(client, email="rcpt5@example.com")
    login_owner(client, creds)
    assert client.post("/invoices/99999/receipt").status_code in (200, 303)
