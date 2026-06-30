"""Invoices — lifecycle, idempotent settlement, isolation, and the pay flow."""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client, create_project
from hestia.db import connect, list_audit
from hestia.invoices import (
    create_invoice,
    get_invoice,
    get_invoice_by_token,
    list_invoices,
    mark_paid,
    money,
    send_invoice,
    void_invoice,
)
from hestia.tenants import create_tenant


def _tenant(conn, name="Inv Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def test_money_format():
    assert money(250050, "usd") == "$2,500.50"
    assert money(0, "eur") == "€0.00"


def test_create_and_join(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah")
    p = create_project(conn, tenant_id=t["id"], name="Wedding", client_id=c["id"])
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="Balance",
                         amount_cents=250000, client_id=c["id"], project_id=p["id"])
    assert inv["status"] == "draft" and inv["token"]
    got = get_invoice(conn, t["id"], inv["id"])
    assert got["client_name"] == "Sarah" and got["project_name"] == "Wedding"
    assert got["amount_display"] == "$2,500.00"


def test_status_transitions(conn, settings):
    t = _tenant(conn)
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="Dep", amount_cents=5000)
    send_invoice(conn, t["id"], inv["id"])
    assert get_invoice(conn, t["id"], inv["id"])["status"] == "sent"
    void_invoice(conn, t["id"], inv["id"])
    assert get_invoice(conn, t["id"], inv["id"])["status"] == "void"


def test_mark_paid_idempotent(conn, settings):
    t = _tenant(conn)
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="X", amount_cents=9900)
    assert mark_paid(conn, token=inv["token"], provider="mock", ref="r1") is True
    assert get_invoice_by_token(conn, inv["token"])["status"] == "paid"
    # second settlement is a no-op (no double payment)
    assert mark_paid(conn, token=inv["token"], provider="mock", ref="r2") is False


def test_mark_paid_settles_exactly_once(conn, settings):
    """A duplicate callback never double-fires invoice.paid (audit/event once only).
    The UPDATE is guarded by 'status != paid' + rowcount, not just the pre-read."""
    t = _tenant(conn)
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="X", amount_cents=9900)
    assert mark_paid(conn, token=inv["token"], provider="mock", ref="r1") is True
    assert mark_paid(conn, token=inv["token"], provider="mock", ref="r2") is False
    paid = [a for a in list_audit(conn, t["id"]) if a["action"] == "invoice.paid"]
    assert len(paid) == 1                                       # settled, audited, emitted once


def test_paid_cannot_be_voided(conn, settings):
    t = _tenant(conn)
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="X", amount_cents=100)
    mark_paid(conn, token=inv["token"], provider="mock", ref="r")
    void_invoice(conn, t["id"], inv["id"])
    assert get_invoice(conn, t["id"], inv["id"])["status"] == "paid"  # unchanged


def test_tenant_isolation(conn, settings):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    create_invoice(conn, settings, tenant_id=t1["id"], title="A-inv", amount_cents=100)
    assert list_invoices(conn, t2["id"]) == []


def test_http_invoice_and_pay_flow(client):
    creds = onboard_studio(client, email="bill@example.com")
    login_owner(client, creds)
    r = client.post("/invoices", data={"title": "Wedding balance", "amount": "2,500.50"})
    iid = r.url.path.rstrip("/").split("/")[-1]
    detail = client.get(f"/invoices/{iid}")
    assert "$2,500.50" in detail.text
    # extract public pay token from the rendered pay link
    token = detail.text.split("/pay/")[1].split('"')[0].split("<")[0].strip()

    assert client.post(f"/invoices/{iid}/send").status_code in (200, 303)
    pay = client.get(f"/pay/{token}")
    assert pay.status_code == 200 and "Pay" in pay.text

    # mock checkout settles immediately
    client.post(f"/pay/{token}/checkout")
    assert "Paid" in client.get(f"/pay/{token}").text
    # paying again is idempotent — still one paid invoice, no error
    client.post(f"/pay/{token}/checkout")
    assert "Paid" in client.get(f"/pay/{token}").text


def test_pay_unknown_token_404(client):
    assert client.get("/pay/nope-not-a-token").status_code == 404


# --- cross-tenant isolation (audit hardening) -------------------------------

def test_invoice_join_does_not_leak_cross_tenant_client(conn, settings):
    """An invoice that (wrongly) carries another studio's client_id must not surface
    that studio's client name/email — the joins are tenant-matched."""
    a, b = _tenant(conn, "A"), _tenant(conn, "B")
    ca = create_client(conn, tenant_id=a["id"], name="SECRET-A", email="a@example.com")
    # tenant B's invoice references tenant A's client id (IDs are globally unique)
    inv = create_invoice(conn, settings, tenant_id=b["id"], title="X", amount_cents=100,
                         client_id=ca["id"])
    conn.commit()
    got = get_invoice(conn, b["id"], inv["id"])
    assert got["client_id"] is None
    assert got["client_name"] is None and got["client_email"] is None     # A's data not leaked
    listed = {i["id"]: i for i in list_invoices(conn, b["id"])}
    assert listed[inv["id"]]["client_name"] is None


def test_invoice_create_drops_foreign_client_and_project(client, app):
    """The create route must reject a client_id/project_id this studio doesn't own,
    so a stray cross-tenant reference can never ride along on the invoice."""
    creds = onboard_studio(client, email="iso@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        my_tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        other = create_tenant(conn, name="Other", shoot_type="wedding")
        fc = create_client(conn, tenant_id=other["id"], name="Foreign", email="f@example.com")
        fp = create_project(conn, tenant_id=other["id"], name="FP", client_id=None,
                            shoot_type="wedding", status="lead")
        conn.commit()
        fcid, fpid = fc["id"], fp["id"]
    finally:
        conn.close()
    client.post("/invoices", data={"title": "X", "amount": "100",
                                   "client_id": str(fcid), "project_id": str(fpid)})
    conn = connect(app.state.settings.db_path)
    try:
        row = conn.execute("SELECT client_id, project_id FROM invoices WHERE tenant_id = ?",
                           (my_tid,)).fetchone()
    finally:
        conn.close()
    assert row["client_id"] is None and row["project_id"] is None          # both foreign refs dropped
