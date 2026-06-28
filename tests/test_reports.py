"""Finance reports — A/R aging buckets and expense-by-category breakdown."""

import datetime

from conftest import login_owner, onboard_studio

from hestia.crm import create_project
from hestia.db import connect
from hestia.finances import create_expense
from hestia.invoices import create_invoice, send_invoice
from hestia.payment_plans import create_payment_plan, deposit_balance_installments
from hestia.reports import (
    ar_aging,
    booking_funnel,
    expense_breakdown,
    monthly_pnl,
    tax_by_period,
)
from hestia.tenants import create_tenant

TODAY = datetime.date.today()


def _due(days_ago: int) -> str:
    """A due date `days_ago` in the past (negative = future), as ISO text."""
    return (TODAY - datetime.timedelta(days=days_ago)).isoformat()


def _sent(conn, settings, tenant_id, cents, due):
    inv = create_invoice(conn, settings, tenant_id=tenant_id, title="I", amount_cents=cents, due_date=due)
    send_invoice(conn, tenant_id, inv["id"])
    return inv


def test_ar_aging_buckets(conn, settings):
    t = create_tenant(conn, name="Aging", shoot_type="wedding")
    _sent(conn, settings, t["id"], 10000, _due(-10))   # not yet due (10 days out)
    _sent(conn, settings, t["id"], 20000, _due(15))    # 1–30
    _sent(conn, settings, t["id"], 30000, _due(45))    # 31–60
    _sent(conn, settings, t["id"], 40000, _due(75))    # 61–90
    _sent(conn, settings, t["id"], 50000, _due(120))   # 90+
    conn.commit()
    b = ar_aging(conn, t["id"])["buckets"]              # fixed order: not-due,1-30,31-60,61-90,90+
    assert [x["cents"] for x in b] == [10000, 20000, 30000, 40000, 50000]
    assert [x["count"] for x in b] == [1, 1, 1, 1, 1]
    ag = ar_aging(conn, t["id"])
    assert ag["total_cents"] == 150000 and ag["overdue_cents"] == 140000   # all but not-yet-due


def test_ar_aging_excludes_plan_paid_and_draft(conn, settings):
    t = create_tenant(conn, name="X", shoot_type="wedding")
    _sent(conn, settings, t["id"], 10000, _due(45))                        # the only one that counts
    paid = create_invoice(conn, settings, tenant_id=t["id"], title="P", amount_cents=99999, due_date=_due(45))
    conn.execute("UPDATE invoices SET status = 'paid' WHERE id = ?", (paid["id"],))
    create_invoice(conn, settings, tenant_id=t["id"], title="D", amount_cents=88888, due_date=_due(45))  # draft
    create_payment_plan(conn, settings, tenant_id=t["id"], title="Plan", client_id=None,
                        installments=deposit_balance_installments(total_cents=400000, deposit_cents=100000))
    conn.execute("UPDATE invoices SET status = 'sent' WHERE plan_id IS NOT NULL")
    conn.commit()
    ag = ar_aging(conn, t["id"])
    assert ag["total_cents"] == 10000                                      # paid/draft/plan all excluded


def test_expense_breakdown_groups_and_pcts(conn):
    t = create_tenant(conn, name="Spend", shoot_type="wedding")
    create_expense(conn, tenant_id=t["id"], amount_cents=7500, category="gear")
    create_expense(conn, tenant_id=t["id"], amount_cents=2500, category="gear")
    create_expense(conn, tenant_id=t["id"], amount_cents=10000, category="travel")
    conn.commit()
    bd = expense_breakdown(conn, t["id"])
    assert bd["total_cents"] == 20000
    rows = {r["category"]: r for r in bd["rows"]}
    assert rows["gear"]["cents"] == 10000 and rows["gear"]["count"] == 2 and rows["gear"]["pct"] == 50
    assert rows["travel"]["pct"] == 50
    assert bd["rows"][0]["cents"] == 10000                                 # biggest first


def test_expense_breakdown_is_tenant_scoped(conn):
    t1 = create_tenant(conn, name="A", shoot_type="wedding")
    t2 = create_tenant(conn, name="B", shoot_type="wedding")
    create_expense(conn, tenant_id=t1["id"], amount_cents=5000, category="gear")
    conn.commit()
    assert expense_breakdown(conn, t2["id"])["total_cents"] == 0


# --- HTTP -------------------------------------------------------------------

def test_reports_page_renders(client, app):
    creds = onboard_studio(client, email="rep@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        inv = create_invoice(conn, app.state.settings, tenant_id=tid, title="Late",
                             amount_cents=12300, due_date=_due(45))
        send_invoice(conn, tid, inv["id"])
        create_expense(conn, tenant_id=tid, amount_cents=5000, category="gear", description="Lens")
        conn.commit()
    finally:
        conn.close()
    page = client.get("/finances/reports")
    assert page.status_code == 200
    assert "A/R aging" in page.text and "Expenses by category" in page.text
    assert "123.00" in page.text and "gear" in page.text


def test_reports_requires_login(client):
    assert client.get("/finances/reports", follow_redirects=False).status_code == 303


# --- monthly trend ----------------------------------------------------------

def _paid_invoice(conn, settings, tenant_id, cents):
    inv = create_invoice(conn, settings, tenant_id=tenant_id, title="Pkg", amount_cents=cents)
    conn.execute("UPDATE invoices SET status = 'paid' WHERE id = ?", (inv["id"],))
    return inv


def test_monthly_pnl_counts_each_sale_once(conn, settings):
    t = create_tenant(conn, name="Trend", shoot_type="wedding")
    _paid_invoice(conn, settings, t["id"], 300000)                     # standalone package
    binv = _paid_invoice(conn, settings, t["id"], 50000)              # a gallery sale: paired
    conn.execute("INSERT INTO orders (tenant_id, invoice_id, sku, name, amount_cents, status) "
                 "VALUES (?, ?, 'favorites', 'Sale', 50000, 'paid')", (t["id"], binv["id"]))
    create_expense(conn, tenant_id=t["id"], amount_cents=80000, category="gear")
    conn.commit()
    trend = monthly_pnl(conn, t["id"], months=6)
    assert len(trend) == 6
    cur = trend[-1]                                                   # this month
    assert cur["revenue_cents"] == 350000                            # 300k + 50k once (not 400k)
    assert cur["expenses_cents"] == 80000 and cur["profit_cents"] == 270000


def test_monthly_pnl_empty_is_zeroed(conn):
    t = create_tenant(conn, name="Quiet", shoot_type="wedding")
    conn.commit()
    trend = monthly_pnl(conn, t["id"], months=3)
    assert len(trend) == 3
    assert all(m["revenue_cents"] == 0 and m["profit_cents"] == 0 for m in trend)


# ── tax by period ─────────────────────────────────────────────────────────────


def _ym(d):
    return d.strftime("%Y-%m")


def test_tax_by_period_buckets_by_paid_month(conn, settings):
    t = create_tenant(conn, name="Tax", shoot_type="wedding")
    i1 = create_invoice(conn, settings, tenant_id=t["id"], title="A", amount_cents=10000, tax_cents=800)
    i2 = create_invoice(conn, settings, tenant_id=t["id"], title="B", amount_cents=20000, tax_cents=1600)
    i3 = create_invoice(conn, settings, tenant_id=t["id"], title="C", amount_cents=5000, tax_cents=0)
    create_invoice(conn, settings, tenant_id=t["id"], title="D", amount_cents=9000, tax_cents=700)  # unpaid
    this_m = _ym(TODAY)
    last_m = _ym(TODAY.replace(day=1) - datetime.timedelta(days=1))
    conn.execute("UPDATE invoices SET status='paid', paid_at=? WHERE id=?", (f"{this_m}-15 10:00:00", i1["id"]))
    conn.execute("UPDATE invoices SET status='paid', paid_at=? WHERE id=?", (f"{last_m}-15 10:00:00", i2["id"]))
    conn.execute("UPDATE invoices SET status='paid', paid_at=? WHERE id=?", (f"{this_m}-16 10:00:00", i3["id"]))
    conn.commit()
    rep = tax_by_period(conn, t["id"], months=4)
    by = {r["month"]: r["cents"] for r in rep["rows"]}
    assert by.get(this_m) == 800 and by.get(last_m) == 1600   # zero-tax + unpaid excluded
    assert rep["total_cents"] == 2400


# ── booking funnel ────────────────────────────────────────────────────────────


def test_booking_funnel_counts_and_conversions(conn):
    t = create_tenant(conn, name="Funnel", shoot_type="wedding")
    for name, status in [("L1", "lead"), ("L2", "lead"), ("B", "booked"),
                         ("S", "shooting"), ("D", "delivered"), ("Ar", "archived")]:
        create_project(conn, tenant_id=t["id"], name=name, status=status)
    conn.commit()
    f = booking_funnel(conn, t["id"])
    assert f["total"] == 6
    assert f["booked"] == 4 and f["delivered"] == 2          # booked = past-lead; delivered = delivered+archived
    assert f["lead_to_booked_pct"] == round(100 * 4 / 6)
    assert f["booked_to_delivered_pct"] == 50
    assert f["overall_pct"] == round(100 * 2 / 6)
    assert f["by_status"]["lead"] == 2


def test_reports_tenant_scoped(conn, settings):
    t1 = create_tenant(conn, name="A", shoot_type="wedding")
    t2 = create_tenant(conn, name="B", shoot_type="wedding")
    create_project(conn, tenant_id=t1["id"], name="P", status="booked")
    i = create_invoice(conn, settings, tenant_id=t1["id"], title="X", amount_cents=10000, tax_cents=900)
    conn.execute("UPDATE invoices SET status='paid', paid_at=datetime('now') WHERE id=?", (i["id"],))
    conn.commit()
    assert booking_funnel(conn, t2["id"])["total"] == 0
    assert tax_by_period(conn, t2["id"])["total_cents"] == 0


def test_http_reports_page_and_tax_export(client):
    creds = onboard_studio(client, email="rep@example.com")
    login_owner(client, creds)
    page = client.get("/finances/reports")
    assert page.status_code == 200 and "Booking funnel" in page.text
    csv = client.get("/finances/export/tax.csv")
    assert csv.status_code == 200 and "month,tax_collected" in csv.text
