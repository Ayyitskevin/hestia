"""Per-client account statement — billed/paid/outstanding across all issued invoices."""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client
from hestia.db import connect
from hestia.invoices import client_statement, create_invoice, send_invoice
from hestia.payment_plans import create_payment_plan, deposit_balance_installments
from hestia.tenants import create_tenant


def _tenant(conn, name="Statement Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def test_statement_totals_across_invoices_plans_and_excludes_draft_void(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Acme", email="a@x.com")

    i1 = create_invoice(conn, settings, tenant_id=t["id"], title="Sitting fee",
                        amount_cents=10000, client_id=c["id"])
    send_invoice(conn, t["id"], i1["id"])                                   # sent, unpaid
    i2 = create_invoice(conn, settings, tenant_id=t["id"], title="Album",
                        amount_cents=20000, client_id=c["id"])
    conn.execute("UPDATE invoices SET status='paid', paid_at=datetime('now') WHERE id=?", (i2["id"],))
    create_invoice(conn, settings, tenant_id=t["id"], title="Draft", amount_cents=88888,
                   client_id=c["id"])                                       # draft → excluded
    iv = create_invoice(conn, settings, tenant_id=t["id"], title="Void", amount_cents=99999,
                        client_id=c["id"])
    conn.execute("UPDATE invoices SET status='void' WHERE id=?", (iv["id"],))

    # a payment plan: both installments issued, the deposit paid
    create_payment_plan(conn, settings, tenant_id=t["id"], title="Wedding", client_id=c["id"],
                        installments=deposit_balance_installments(total_cents=400000, deposit_cents=100000))
    conn.execute("UPDATE invoices SET status='sent' WHERE tenant_id=? AND plan_id IS NOT NULL", (t["id"],))
    conn.execute("UPDATE invoices SET status='paid', paid_at=datetime('now') "
                 "WHERE tenant_id=? AND plan_id IS NOT NULL AND amount_cents=100000", (t["id"],))
    conn.commit()

    s = client_statement(conn, t["id"], c["id"])
    assert s["billed_cents"] == 10000 + 20000 + 100000 + 300000            # draft + void excluded
    assert s["paid_cents"] == 20000 + 100000
    assert s["outstanding_cents"] == s["billed_cents"] - s["paid_cents"]
    titles = {it["title"] for it in s["lines"]}
    assert "Draft" not in titles and "Void" not in titles


def test_statement_tenant_scoped(conn, settings):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    c1 = create_client(conn, tenant_id=t1["id"], name="A1", email="a1@x.com")
    i = create_invoice(conn, settings, tenant_id=t1["id"], title="X", amount_cents=10000,
                       client_id=c1["id"])
    send_invoice(conn, t1["id"], i["id"])
    conn.commit()
    # the other tenant gets nothing for t1's client id
    s = client_statement(conn, t2["id"], c1["id"])
    assert s["billed_cents"] == 0 and s["lines"] == []


def test_http_statement_page_and_link(client, app):
    creds = onboard_studio(client, email="st@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        c = create_client(conn, tenant_id=tid, name="Bea", email="bea@x.com")
        inv = create_invoice(conn, app.state.settings, tenant_id=tid, title="Deposit",
                             amount_cents=15000, client_id=c["id"])
        send_invoice(conn, tid, inv["id"])
        conn.commit()
        cid = c["id"]
    finally:
        conn.close()

    page = client.get(f"/clients/{cid}/statement")
    assert page.status_code == 200
    assert "Account statement" in page.text and "Deposit" in page.text and "150.00" in page.text
    assert f"/clients/{cid}/statement" in client.get(f"/clients/{cid}").text   # linked from detail
