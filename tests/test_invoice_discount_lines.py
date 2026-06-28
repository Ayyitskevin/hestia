"""Discount line items — a negative line amount reduces the invoice subtotal.

Line amounts may be negative (a discount); the subtotal is floored at zero so tax and
totals never go negative. amount_cents stays authoritative.
"""

from conftest import login_owner, onboard_studio

from hestia.invoices import get_invoice, invoice_items


def _itemized(client, items_text):
    r = client.post("/clients", data={"name": "Buyer", "email": "b@example.com"})
    cid = r.url.path.rstrip("/").split("/")[-1]
    r = client.post("/invoices", data={"title": "Package", "items": items_text, "client_id": cid})
    return r.url.path.rstrip("/").split("/")[-1]


def _tid(conn):
    return conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]


def test_discount_line_reduces_subtotal(client, conn):
    creds = onboard_studio(client, email="disc1@example.com")
    login_owner(client, creds)
    iid = _itemized(client, "Coverage | 2000\nAlbum | 500\nLoyalty discount | -250")
    inv = get_invoice(conn, _tid(conn), int(iid))
    assert inv["amount_cents"] == 225000           # 2000 + 500 - 250 = 2250.00
    # the discount line is stored (negative) and shown
    items = invoice_items(conn, _tid(conn), int(iid))
    assert ("Loyalty discount", -25000) in [(i["description"], i["amount_cents"]) for i in items]
    assert "$2,250.00" in client.get(f"/invoices/{iid}").text


def test_subtotal_floored_at_zero(client, conn):
    creds = onboard_studio(client, email="disc2@example.com")
    login_owner(client, creds)
    iid = _itemized(client, "Tiny | 100\nBig discount | -500")
    assert get_invoice(conn, _tid(conn), int(iid))["amount_cents"] == 0   # not negative


def test_discount_shows_on_pay_page(client, conn):
    creds = onboard_studio(client, email="disc3@example.com")
    login_owner(client, creds)
    iid = _itemized(client, "Coverage | 1000\nReferral credit | -100")
    tok = conn.execute("SELECT token FROM invoices WHERE id = ?", (int(iid),)).fetchone()["token"]
    pay = client.get(f"/pay/{tok}").text
    assert "Referral credit" in pay
