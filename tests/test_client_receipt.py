"""Client-facing printable receipt — a paid invoice the client can view and save.

Public and token-based (same token as the pay page). Only a paid invoice shows a
receipt; anything else falls back to the pay page (which 404s for void/unknown).
"""

from conftest import login_owner, onboard_studio


def _paid_invoice(client, conn, *, email="buyer@example.com", amount="500"):
    r = client.post("/clients", data={"name": "Pat Buyer", "email": email})
    cid = r.url.path.rstrip("/").split("/")[-1]
    r = client.post("/invoices", data={"title": "Wedding deposit", "amount": amount, "client_id": cid})
    iid = r.url.path.rstrip("/").split("/")[-1]
    client.post(f"/invoices/{iid}/record-payment", data={"method": "cash"})
    tok = conn.execute("SELECT token FROM invoices WHERE id = ?", (iid,)).fetchone()["token"]
    return iid, tok


def test_receipt_renders_for_paid_invoice(client, conn):
    creds = onboard_studio(client, name="Lumen Studio", email="rc1@example.com")
    login_owner(client, creds)
    iid, tok = _paid_invoice(client, conn)
    r = client.get(f"/pay/{tok}/receipt")
    assert r.status_code == 200
    assert "Receipt" in r.text and "Lumen Studio" in r.text
    assert "Pat Buyer" in r.text and "$500.00" in r.text and f"#{iid}" in r.text


def test_receipt_redirects_when_unpaid(client, conn):
    creds = onboard_studio(client, email="rc2@example.com")
    login_owner(client, creds)
    r = client.post("/clients", data={"name": "Una Paid", "email": "u@example.com"})
    cid = r.url.path.rstrip("/").split("/")[-1]
    r = client.post("/invoices", data={"title": "Still open", "amount": "100", "client_id": cid})
    iid = r.url.path.rstrip("/").split("/")[-1]
    tok = conn.execute("SELECT token FROM invoices WHERE id = ?", (iid,)).fetchone()["token"]
    r = client.get(f"/pay/{tok}/receipt")                 # unpaid → bounces to the pay page
    assert r.status_code == 200 and "Pay $100.00" in r.text


def test_unknown_token_404s(client):
    creds = onboard_studio(client, email="rc3@example.com")
    login_owner(client, creds)
    # unknown token → fall back to pay page → 404
    assert client.get("/pay/nope-not-a-real-token/receipt").status_code == 404


def test_pay_page_links_receipt_once_paid(client, conn):
    creds = onboard_studio(client, email="rc4@example.com")
    login_owner(client, creds)
    iid, tok = _paid_invoice(client, conn)
    page = client.get(f"/pay/{tok}")                      # public pay page, now settled
    assert f"/pay/{tok}/receipt" in page.text and "View receipt" in page.text


def test_receipt_shows_tax_breakdown(client, conn):
    creds = onboard_studio(client, email="rc5@example.com")
    login_owner(client, creds)
    client.post("/settings/tax", data={"tax_rate": "10"})  # 10% sales tax
    iid, tok = _paid_invoice(client, conn, amount="200")
    body = client.get(f"/pay/{tok}/receipt").text
    assert "Subtotal" in body and "Tax" in body
    assert "$200.00" in body and "$220.00" in body         # subtotal + 10% = total
