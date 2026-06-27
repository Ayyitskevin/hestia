"""Invoice note — a studio's personal message on an invoice: stored display-only
(never touches amounts), shown on the pay page, carried in the send email, editable."""

from conftest import login_owner, onboard_studio

from hestia.invoices import create_invoice, get_invoice, set_invoice_note
from hestia.tenants import create_tenant

# ── data layer (unit) ────────────────────────────────────────────────────────


def test_create_invoice_stores_and_strips_note(conn, settings):
    t = create_tenant(conn, name="Inv", shoot_type="other")
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="Balance",
                         amount_cents=250000, note="  Thanks so much!  ")
    conn.commit()
    assert inv["note"] == "Thanks so much!"                       # stripped on the way in
    assert get_invoice(conn, t["id"], inv["id"])["note"] == "Thanks so much!"


def test_note_does_not_touch_amounts(conn, settings):
    """The note is display-only — subtotal, tax, and total are exactly as billed."""
    t = create_tenant(conn, name="Inv2", shoot_type="other")
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="Bal", amount_cents=250000,
                         tax_cents=20000, note="Deposit is non-refundable.")
    assert (inv["amount_cents"], inv["tax_cents"], inv["total_cents"]) == (250000, 20000, 270000)


def test_set_invoice_note_is_tenant_scoped(conn, settings):
    a = create_tenant(conn, name="A", shoot_type="other")
    b = create_tenant(conn, name="B", shoot_type="other")
    inv = create_invoice(conn, settings, tenant_id=a["id"], title="Bal", amount_cents=1000)
    set_invoice_note(conn, b["id"], inv["id"], "not yours")       # wrong tenant → no-op
    conn.commit()
    assert get_invoice(conn, a["id"], inv["id"])["note"] == ""


# ── HTTP: pay page + send email + edit ───────────────────────────────────────


def test_note_shows_on_pay_page_and_in_email(client, conn):
    login_owner(client, onboard_studio(client, email="owner@note.com"))
    client.post("/clients", data={"name": "Pat", "email": "pat@note.com"})
    cid = conn.execute("SELECT id FROM clients WHERE email='pat@note.com'").fetchone()["id"]
    r = client.post("/invoices", data={"title": "Balance", "amount": "500", "client_id": str(cid),
                                       "note": "Venmo @studio also accepted"})
    iid = int(str(r.url).rstrip("/").split("/")[-1])
    token = conn.execute("SELECT token FROM invoices WHERE id=?", (iid,)).fetchone()["token"]

    pay = client.get(f"/pay/{token}")                             # client sees the note
    assert pay.status_code == 200 and "Venmo @studio also accepted" in pay.text

    client.post(f"/invoices/{iid}/send")                         # and it rides into the email
    body = conn.execute("SELECT body FROM emails WHERE to_addr='pat@note.com'").fetchone()["body"]
    assert "Venmo @studio also accepted" in body and "/pay/" in body


def test_note_can_be_edited_after_creation(client, conn):
    login_owner(client, onboard_studio(client, email="owner2@note.com"))
    r = client.post("/invoices", data={"title": "Shoot", "amount": "750"})   # created with no note
    iid = int(str(r.url).rstrip("/").split("/")[-1])
    client.post(f"/invoices/{iid}/note", data={"note": "Deposit is non-refundable."})
    detail = client.get(f"/invoices/{iid}")
    assert "Deposit is non-refundable." in detail.text
    assert conn.execute("SELECT note FROM invoices WHERE id=?", (iid,)).fetchone()["note"] \
        == "Deposit is non-refundable."


def test_no_note_means_no_note_on_pay_page(client, conn):
    login_owner(client, onboard_studio(client, email="owner3@note.com"))
    r = client.post("/invoices", data={"title": "Walk-in", "amount": "100"})  # no note
    iid = int(str(r.url).rstrip("/").split("/")[-1])
    token = conn.execute("SELECT token FROM invoices WHERE id=?", (iid,)).fetchone()["token"]
    pay = client.get(f"/pay/{token}")
    assert pay.status_code == 200 and "white-space:pre-line" not in pay.text  # note block absent
