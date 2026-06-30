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
    revenue_total,
)
from hestia.invoices import create_invoice
from hestia.tenants import create_tenant


def _paid_order(conn, tenant_id, cents):
    # a STANDALONE paid order (no backing invoice) — counts on its own
    conn.execute("INSERT INTO orders (tenant_id, sku, name, amount_cents, status) "
                 "VALUES (?, 'print-8x10', 'Print', ?, 'paid')", (tenant_id, cents))


def _paid_invoice(conn, settings, *, tenant_id, cents, project_id=None, client_id=None):
    inv = create_invoice(conn, settings, tenant_id=tenant_id, title="Pkg", amount_cents=cents,
                         client_id=client_id, project_id=project_id)
    conn.execute("UPDATE invoices SET status = 'paid' WHERE id = ?", (inv["id"],))
    return inv


def _paid_gallery_sale(conn, settings, *, tenant_id, cents, project_id=None, client_id=None):
    """Reproduce a real gallery sale: orders.create_order pairs an order with a backing
    invoice of the SAME amount, and the pay flow marks both paid. Returns the order id."""
    inv = _paid_invoice(conn, settings, tenant_id=tenant_id, cents=cents,
                        project_id=project_id, client_id=client_id)
    cur = conn.execute(
        "INSERT INTO orders (tenant_id, invoice_id, sku, name, amount_cents, status) "
        "VALUES (?, ?, 'favorites', 'Gallery sale', ?, 'paid')", (tenant_id, inv["id"], cents))
    return cur.lastrowid


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


def test_expense_create_drops_foreign_project_id(conn):
    a = create_tenant(conn, name="A", shoot_type="wedding")
    b = create_tenant(conn, name="B", shoot_type="wedding")
    foreign_project = create_project(conn, tenant_id=a["id"], name="Foreign Project")
    e = create_expense(conn, tenant_id=b["id"], amount_cents=100, project_id=foreign_project["id"])
    assert e["project_id"] is None


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


# --- revenue counts a paired sale once (regression) -------------------------

def test_gallery_sale_counts_once_not_twice(conn, settings):
    """A gallery sale is a paired invoice + order of the same amount, both marked paid.
    Counting both would double the sale; tenant-wide revenue and the income export
    must report it once."""
    t = create_tenant(conn, name="Sale", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Buyer")
    _paid_gallery_sale(conn, settings, tenant_id=t["id"], cents=50000, client_id=c["id"])
    conn.commit()
    assert revenue_total(conn, t["id"]) == 50000                 # $500 once, not $1,000
    assert profit_summary(conn, t["id"])["revenue_cents"] == 50000
    rows = income_rows(conn, t["id"])
    assert len(rows) == 1                                        # backing invoice not a 2nd row
    assert rows[0]["type"] == "order" and rows[0]["amount_cents"] == 50000


def test_revenue_mixes_standalone_invoice_and_gallery_sale(conn, settings):
    """A normal package invoice and a gallery sale each count once — the sale's backing
    invoice is excluded, the standalone invoice is not."""
    t = create_tenant(conn, name="Mix", shoot_type="wedding")
    _paid_invoice(conn, settings, tenant_id=t["id"], cents=300000)        # package invoice
    _paid_gallery_sale(conn, settings, tenant_id=t["id"], cents=50000)    # gallery print sale
    conn.commit()
    assert revenue_total(conn, t["id"]) == 350000                # 300k + 50k, each once
    rows = income_rows(conn, t["id"])
    assert len(rows) == 2 and sum(r["amount_cents"] for r in rows) == 350000


def test_project_pnl_subqueries_are_tenant_scoped(conn, settings):
    """A foreign studio's invoice/expense that (wrongly) references my project id must
    not bleed into my project's P&L — the subqueries are tenant-scoped."""
    mine = create_tenant(conn, name="Mine", shoot_type="wedding")
    theirs = create_tenant(conn, name="Theirs", shoot_type="wedding")
    p = create_project(conn, tenant_id=mine["id"], name="Shoot", client_id=None,
                       shoot_type="wedding", status="booked")
    _paid_invoice(conn, settings, tenant_id=mine["id"], cents=100000, project_id=p["id"])
    # foreign rows pointed at my project id
    _paid_invoice(conn, settings, tenant_id=theirs["id"], cents=999999, project_id=p["id"])
    create_expense(conn, tenant_id=theirs["id"], amount_cents=777777, project_id=p["id"])
    conn.commit()
    row = next(r for r in project_pnl(conn, mine["id"]) if r["name"] == "Shoot")
    assert row["revenue"] == "$1,000.00"                         # only my 100k
    assert row["profit_cents"] == 100000                         # foreign expense excluded too


def test_project_revenue_ignores_mismatched_client_project_invoice(conn, settings):
    """A paid invoice belongs in tenant-wide cash totals, but it must not inflate a
    project whose client does not match the invoice client."""
    t = create_tenant(conn, name="Mismatch", shoot_type="wedding")
    sarah = create_client(conn, tenant_id=t["id"], name="Sarah")
    bob = create_client(conn, tenant_id=t["id"], name="Bob")
    shoot = create_project(conn, tenant_id=t["id"], name="Bob Shoot", client_id=bob["id"],
                           shoot_type="wedding", status="booked")
    mismatched = _paid_invoice(conn, settings, tenant_id=t["id"], cents=100000,
                               client_id=sarah["id"])
    conn.execute("UPDATE invoices SET project_id = ? WHERE id = ?", (shoot["id"], mismatched["id"]))
    _paid_invoice(conn, settings, tenant_id=t["id"], cents=200000,
                  project_id=shoot["id"], client_id=bob["id"])
    conn.commit()

    assert revenue_total(conn, t["id"]) == 300000
    assert revenue_total(conn, t["id"], project_id=shoot["id"]) == 200000
    assert profit_summary(conn, t["id"], project_id=shoot["id"])["revenue_cents"] == 200000
    row = next(r for r in project_pnl(conn, t["id"]) if r["name"] == "Bob Shoot")
    assert row["revenue"] == "$2,000.00"
    assert row["profit_cents"] == 200000


def test_expense_reads_ignore_foreign_project_link(conn):
    """A stale expense.project_id that points at another tenant is still this tenant's
    expense, but project-scoped reads should not attribute it to that project."""
    mine = create_tenant(conn, name="Mine", shoot_type="wedding")
    theirs = create_tenant(conn, name="Theirs", shoot_type="wedding")
    foreign = create_project(conn, tenant_id=theirs["id"], name="Foreign Project",
                             shoot_type="wedding", status="booked")
    e = create_expense(conn, tenant_id=mine["id"], amount_cents=7500, category="gear")
    conn.execute("UPDATE expenses SET project_id = ? WHERE id = ?", (foreign["id"], e["id"]))
    conn.commit()

    rows = list_expenses(conn, mine["id"])
    assert rows[0]["project_id"] is None
    assert rows[0]["project_name"] is None
    assert expenses_total(conn, mine["id"]) == 7500
    assert list_expenses(conn, mine["id"], project_id=foreign["id"]) == []
    assert expenses_total(conn, mine["id"], project_id=foreign["id"]) == 0


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


# --- hardening (manual review of the new code) ------------------------------

def test_csv_export_neutralizes_formula_injection(client, app):
    creds = onboard_studio(client, email="inj@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        create_expense(conn, tenant_id=tid, amount_cents=100, category="gear", description="=2+5+cmd()")
        conn.commit()
    finally:
        conn.close()
    text = client.get("/finances/export/expenses.csv").text
    assert "'=2+5+cmd()" in text          # quoted → spreadsheet treats it as text
    assert ",=2+5" not in text            # never a bare leading-'=' formula cell


def test_add_expense_ignores_a_foreign_project(client, app):
    creds = onboard_studio(client, email="own@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        my_tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        other = create_tenant(conn, name="Other Studio", shoot_type="wedding")
        foreign = create_project(conn, tenant_id=other["id"], name="Foreign",
                                 client_id=None, shoot_type="wedding", status="lead")
        conn.commit()
        fpid = foreign["id"]
    finally:
        conn.close()
    client.post("/finances/expenses", data={"amount": "10", "category": "gear",
                                            "description": "x", "project_id": str(fpid)})
    conn = connect(app.state.settings.db_path)
    try:
        row = conn.execute("SELECT project_id FROM expenses WHERE tenant_id = ?", (my_tid,)).fetchone()
    finally:
        conn.close()
    assert row["project_id"] is None      # a project this studio doesn't own is dropped


def test_add_expense_tolerates_non_finite_amount(client, app):
    """'inf'/'1e400'/'nan' parse as floats but overflow round()-to-int — a non-finite
    amount must be ignored (no expense), not 500 the page."""
    creds = onboard_studio(client, email="inf@example.com")
    login_owner(client, creds)
    for bad in ("inf", "1e400", "nan", "-inf"):
        r = client.post("/finances/expenses", data={"amount": bad, "category": "gear",
                                                    "description": "boom"})
        assert r.status_code in (200, 303)        # redirect back, never a 500
    conn = connect(app.state.settings.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM expenses").fetchone()["n"]
    finally:
        conn.close()
    assert n == 0                                  # nothing logged from a non-finite amount
