"""Itemized invoices — optional line items whose sum becomes the subtotal.

amount_cents stays authoritative (tax, totals, pay, plans, A/R unchanged); the items are
an additive breakdown shown on the invoice, pay page, and receipt. A flat single-amount
invoice has no items and behaves exactly as before.
"""

from conftest import login_owner, onboard_studio

from hestia.routes.invoices import _parse_line_items


def _itemized(client, items_text, *, title="Wedding package"):
    r = client.post("/clients", data={"name": "Buyer", "email": "buyer@example.com"})
    cid = r.url.path.rstrip("/").split("/")[-1]
    r = client.post("/invoices", data={"title": title, "items": items_text, "client_id": cid})
    return r.url.path.rstrip("/").split("/")[-1]


def test_parse_line_items_skips_stray_and_empty():
    out = _parse_line_items("Coverage | 2500\n\nstray note\nAlbum | 400.50\n| 99")
    assert out == [("Coverage", 250000), ("Album", 40050)]   # no-pipe + empty-desc skipped


def test_itemized_create_sums_to_subtotal(client):
    creds = onboard_studio(client, email="li1@example.com")
    login_owner(client, creds)
    iid = _itemized(client, "Coverage | 2500\nAlbum | 400\nPrints | 100")
    detail = client.get(f"/invoices/{iid}").text
    assert "Line items" in detail
    assert "Coverage" in detail and "Album" in detail and "Prints" in detail
    assert "$3,000.00" in detail                              # 2500 + 400 + 100


def test_tax_applies_to_itemized_subtotal(client):
    creds = onboard_studio(client, email="li2@example.com")
    login_owner(client, creds)
    client.post("/settings/tax", data={"tax_rate": "10"})
    iid = _itemized(client, "A | 100\nB | 100")
    detail = client.get(f"/invoices/{iid}").text
    assert "$200.00" in detail and "$220.00" in detail       # subtotal + 10% tax


def test_flat_invoice_unchanged(client):
    creds = onboard_studio(client, email="li3@example.com")
    login_owner(client, creds)
    r = client.post("/invoices", data={"title": "Flat", "amount": "750"})
    iid = r.url.path.rstrip("/").split("/")[-1]
    detail = client.get(f"/invoices/{iid}").text
    assert "$750.00" in detail and "Line items" not in detail  # no items card


def test_items_show_on_pay_and_receipt(client, conn):
    creds = onboard_studio(client, email="li4@example.com")
    login_owner(client, creds)
    iid = _itemized(client, "Coverage | 1000\nAlbum | 500")
    tok = conn.execute("SELECT token FROM invoices WHERE id = ?", (iid,)).fetchone()["token"]
    pay = client.get(f"/pay/{tok}").text
    assert "Coverage" in pay and "Album" in pay
    client.post(f"/invoices/{iid}/record-payment", data={"method": "cash"})
    receipt = client.get(f"/pay/{tok}/receipt").text
    assert "Coverage" in receipt and "Album" in receipt and "$1,500.00" in receipt


def test_items_are_tenant_scoped(client, conn):
    from hestia.invoices import invoice_items
    creds = onboard_studio(client, email="li5@example.com")
    login_owner(client, creds)
    iid = _itemized(client, "Secret line | 999")
    tid = conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]
    assert [i["description"] for i in invoice_items(conn, tid, int(iid))] == ["Secret line"]
    assert invoice_items(conn, "some-other-tenant", int(iid)) == []   # foreign tenant sees none
