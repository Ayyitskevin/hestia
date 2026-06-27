"""Offline payment — record cash/check/transfer taken outside the online pay link.

Mirrors mark_paid's idempotent claim, but owner-initiated and id/tenant-scoped: only a
draft or sent invoice settles, exactly once, firing invoice.paid like an online payment.
"""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client
from hestia.db import list_audit
from hestia.invoices import (
    create_invoice,
    get_invoice,
    record_offline_payment,
    send_invoice,
    void_invoice,
)
from hestia.tenants import create_tenant


def _studio(conn, name="Pay Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def _invoice(conn, settings, t, *, status="draft"):
    c = create_client(conn, tenant_id=t["id"], name="Cli", email="c@x.com")
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="Session fee",
                         amount_cents=20000, client_id=c["id"])
    if status == "sent":
        send_invoice(conn, t["id"], inv["id"])
    conn.commit()
    return inv


def test_records_and_settles(conn, settings):
    t = _studio(conn)
    inv = _invoice(conn, settings, t)
    assert record_offline_payment(conn, t["id"], inv["id"], method="cash") is True
    got = get_invoice(conn, t["id"], inv["id"])
    assert got["status"] == "paid" and got["provider"] == "cash" and got["paid_at"]


def test_idempotent_double_submit(conn, settings):
    t = _studio(conn)
    inv = _invoice(conn, settings, t)
    assert record_offline_payment(conn, t["id"], inv["id"], method="cash") is True
    assert record_offline_payment(conn, t["id"], inv["id"], method="cash") is False
    paid = [a for a in list_audit(conn, t["id"]) if a["action"] == "invoice.paid"]
    assert len(paid) == 1                       # settled (and audited/emitted) exactly once


def test_unknown_method_falls_back_to_other(conn, settings):
    t = _studio(conn)
    inv = _invoice(conn, settings, t)
    assert record_offline_payment(conn, t["id"], inv["id"], method="venmo") is True
    assert get_invoice(conn, t["id"], inv["id"])["provider"] == "other"


def test_sent_invoice_can_be_recorded(conn, settings):
    t = _studio(conn)
    inv = _invoice(conn, settings, t, status="sent")
    assert record_offline_payment(conn, t["id"], inv["id"], method="check") is True
    assert get_invoice(conn, t["id"], inv["id"])["status"] == "paid"


def test_void_invoice_cannot_be_paid(conn, settings):
    t = _studio(conn)
    inv = _invoice(conn, settings, t)
    void_invoice(conn, t["id"], inv["id"])
    conn.commit()
    assert record_offline_payment(conn, t["id"], inv["id"], method="cash") is False
    assert get_invoice(conn, t["id"], inv["id"])["status"] == "void"


def test_tenant_scoped(conn, settings):
    a = _studio(conn, "A Studio")
    b = _studio(conn, "B Studio")
    inv = _invoice(conn, settings, a)
    assert record_offline_payment(conn, b["id"], inv["id"], method="cash") is False
    assert get_invoice(conn, a["id"], inv["id"])["status"] != "paid"


def test_http_record_payment(client):
    creds = onboard_studio(client, email="pay@example.com")
    login_owner(client, creds)
    r = client.post("/invoices", data={"title": "Deposit", "amount": "500"})
    iid = r.url.path.rstrip("/").split("/")[-1]
    assert "Record payment" in client.get(f"/invoices/{iid}").text
    client.post(f"/invoices/{iid}/record-payment", data={"method": "cash"})
    paid = client.get(f"/invoices/{iid}")
    assert "paid" in paid.text and "via cash" in paid.text
    assert "Record payment" not in paid.text     # control gone once settled
