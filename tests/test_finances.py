"""Studio finances — expense tracking + revenue-minus-expenses P&L."""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client, create_project
from hestia.db import connect
from hestia.finances import (
    create_expense,
    delete_expense,
    expenses_total,
    income_rows,
    list_expenses,
    profit_summary,
    project_pnl,
)
from hestia.invoices import create_invoice
from hestia.tenants import create_tenant


def _paid_order(conn, tenant_id, cents):
    conn.execute("INSERT INTO orders (tenant_id, sku, name, amount_cents, status) "
                 "VALUES (?, 'print-8x10', 'Print', ?, 'paid')", (tenant_id, cents))


def _paid_invoice(conn, settings, *, tenant_id, cents, project_id=None, client_id=None):
    inv = create_invoice(conn, settings, tenant_id=tenant_id, title="Pkg", amount_cents=cents,
                         client_id=client_id, project_id=project_id)
    conn.execute("UPDATE invoices SET status = 'paid' WHERE id = ?", (inv["id"],))
    return inv


# --- module logic -----------------------------------------------------------

def test_expense_crud_and_total(conn):
    t = create_tenant(conn, name="Fin Studio", shoot_type="wedding")
    e = create_expense(conn, tenant_id=t["id"], amount_cents=15000,
                       category="second_shooter", description="2nd shooter")
    conn.commit()
    assert e["amount_cents"] == 15000 and e["category"] == "second_shooter"
    lst = list_expenses(conn, t["id"])
    assert len(lst) == 1 and lst[0]["amount_display"] == "$150.00"
    assert expenses_total(conn, t["id"]) == 15000
    assert delete_expense(conn, t["id"], e["id"]) is True
    assert expenses_total(conn, t["id"]) == 0
    assert delete_expense(conn, t["id"], e["id"]) is False        # idempotent


def test_unknown_category_falls_back_to_other(conn):
    t = create_tenant(conn, name="Cat", shoot_type="wedding")
    assert create_expense(conn, tenant_id=t["id"], amount_cents=100, category="bogus")["category"] == "other"


def test_profit_summary_counts_paid_revenue_minus_expenses(conn, settings):
    t = create_tenant(conn, name="P Studio", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="C")
    p = create_project(conn, tenant_id=t["id"], name="Wedding", client_id=c["id"],
                       shoot_type="wedding", status="booked")
    _paid_invoice(conn, settings, tenant_id=t["id"], cents=300000, project_id=p["id"], client_id=c["id"])
    create_invoice(conn, settings, tenant_id=t["id"], title="Unpaid", amount_cents=100000,
                   project_id=p["id"])                            # unpaid → excluded
    _paid_order(conn, t["id"], 50000)                             # print sale
    create_expense(conn, tenant_id=t["id"], amount_cents=80000,
                   category="second_shooter", project_id=p["id"])
    conn.commit()

    s = profit_summary(conn, t["id"])
    assert s["revenue_cents"] == 350000                          # 300k invoice + 50k order
    assert s["expenses_cents"] == 80000 and s["profit_cents"] == 270000
    assert s["profit"] == "$2,700.00" and s["margin"] == round(100 * 270000 / 350000)
    # per-project counts invoiced revenue only (orders aren't project-tagged)
    ps = profit_summary(conn, t["id"], project_id=p["id"])
    assert ps["revenue_cents"] == 300000 and ps["profit_cents"] == 220000


def test_project_pnl_excludes_idle_and_surfaces_losses_first(conn, settings):
    t = create_tenant(conn, name="PnL", shoot_type="wedding")
    win = create_project(conn, tenant_id=t["id"], name="Win", client_id=None,
                         shoot_type="wedding", status="booked")
    _paid_invoice(conn, settings, tenant_id=t["id"], cents=200000, project_id=win["id"])
    loss = create_project(conn, tenant_id=t["id"], name="Loss", client_id=None,
                          shoot_type="wedding", status="booked")
    create_expense(conn, tenant_id=t["id"], amount_cents=90000, project_id=loss["id"])
    create_project(conn, tenant_id=t["id"], name="Idle", client_id=None,
                   shoot_type="wedding", status="lead")          # no activity
    conn.commit()
    pnl = project_pnl(conn, t["id"])
    names = [r["name"] for r in pnl]
    assert "Idle" not in names                                   # idle excluded
    assert names[0] == "Loss"                                    # loss sorts first
    assert {r["name"] for r in pnl} == {"Win", "Loss"}


def test_expenses_are_tenant_scoped(conn):
    t1 = create_tenant(conn, name="T1", shoot_type="wedding")
    t2 = create_tenant(conn, name="T2", shoot_type="wedding")
    e = create_expense(conn, tenant_id=t1["id"], amount_cents=5000)
    conn.commit()
    assert list_expenses(conn, t2["id"]) == []                   # not visible cross-tenant
    assert delete_expense(conn, t2["id"], e["id"]) is False      # not deletable cross-tenant
    assert expenses_total(conn, t1["id"]) == 5000


# --- HTTP -------------------------------------------------------------------

def test_finances_page_add_and_delete(client, app):
    creds = onboard_studio(client, email="fin@example.com")
    login_owner(client, creds)
    page = client.get("/finances")
    assert page.status_code == 200 and "Finances" in page.text and "No expenses logged" in page.text

    client.post("/finances/expenses", data={"amount": "150.50", "category": "gear",
                                            "description": "New lens"})
    page2 = client.get("/finances")
    assert "New lens" in page2.text and "$150.50" in page2.text

    conn = connect(app.state.settings.db_path)
    try:
        eid = conn.execute("SELECT id FROM expenses LIMIT 1").fetchone()["id"]
    finally:
        conn.close()
    client.post(f"/finances/expenses/{eid}/delete")
    assert "New lens" not in client.get("/finances").text


def test_finances_requires_login(client):
    assert client.get("/finances", follow_redirects=False).status_code == 303


# --- accountant export ------------------------------------------------------

def test_income_rows_are_paid_only(conn, settings):
    t = create_tenant(conn, name="Inc", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Cli")
    _paid_invoice(conn, settings, tenant_id=t["id"], cents=200000, client_id=c["id"])
    create_invoice(conn, settings, tenant_id=t["id"], title="Unpaid", amount_cents=99999, client_id=c["id"])
    _paid_order(conn, t["id"], 50000)
    conn.commit()
    rows = income_rows(conn, t["id"])
    assert sorted(r["type"] for r in rows) == ["invoice", "order"]      # both sources
    assert sum(r["amount_cents"] for r in rows) == 250000              # unpaid invoice excluded


def test_csv_exports(client, app):
    creds = onboard_studio(client, email="csv@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        create_expense(conn, tenant_id=tid, amount_cents=15000, category="gear", description="Lens")
        _paid_order(conn, tid, 50000)
        conn.commit()
    finally:
        conn.close()

    exp = client.get("/finances/export/expenses.csv")
    assert exp.status_code == 200 and exp.headers["content-type"].startswith("text/csv")
    assert "date,category,description,project,amount" in exp.text
    assert "Lens" in exp.text and "150.00" in exp.text

    inc = client.get("/finances/export/income.csv")
    assert inc.status_code == 200 and inc.headers["content-type"].startswith("text/csv")
    assert "Print" in inc.text and "500.00" in inc.text


def test_export_requires_login(client):
    assert client.get("/finances/export/expenses.csv", follow_redirects=False).status_code == 303
    assert client.get("/finances/export/income.csv", follow_redirects=False).status_code == 303
