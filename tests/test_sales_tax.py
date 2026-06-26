"""Sales tax — additive tax on invoices that never inflates revenue.

amount_cents stays the pre-tax subtotal (what revenue/P&L count); tax_cents is added
on top; total = subtotal + tax is what the client pays. Default rate 0 → no tax."""

from conftest import login_owner, onboard_studio

from hestia.db import connect
from hestia.finances import revenue_total
from hestia.invoices import create_invoice, get_invoice, tax_for
from hestia.reports import tax_collected
from hestia.tenants import create_tenant, get_tenant, set_tax_rate


def test_tax_for_and_rate_clamp(conn):
    assert tax_for(10000, 850) == 850          # 8.5% of $100.00
    assert tax_for(10000, 0) == 0              # no rate → no tax
    assert tax_for(12345, 850) == 1049         # round(1049.325)
    t = create_tenant(conn, name="C", shoot_type="wedding")
    set_tax_rate(conn, t["id"], 20000)         # over 100% → clamped
    assert get_tenant(conn, t["id"])["tax_rate_bps"] == 10000
    set_tax_rate(conn, t["id"], -5)            # negative → 0
    assert get_tenant(conn, t["id"])["tax_rate_bps"] == 0


def test_invoice_stores_tax_and_total(conn, settings):
    t = create_tenant(conn, name="T", shoot_type="wedding")
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="Pkg",
                         amount_cents=10000, tax_cents=850)
    got = get_invoice(conn, t["id"], inv["id"])
    assert got["amount_cents"] == 10000 and got["tax_cents"] == 850
    assert got["total_cents"] == 10850
    assert got["tax_display"] == "$8.50" and got["total_display"] == "$108.50"


def test_revenue_counts_subtotal_not_tax(conn, settings):
    """Collected tax is owed to the state, not income — revenue must count the
    pre-tax subtotal only."""
    t = create_tenant(conn, name="R", shoot_type="wedding")
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="P",
                         amount_cents=10000, tax_cents=850)
    conn.execute("UPDATE invoices SET status = 'paid' WHERE id = ?", (inv["id"],))
    conn.commit()
    assert revenue_total(conn, t["id"]) == 10000     # not 10850


def test_no_rate_means_no_tax(conn, settings):
    t = create_tenant(conn, name="Z", shoot_type="wedding")
    assert get_tenant(conn, t["id"])["tax_rate_bps"] == 0          # default
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="P", amount_cents=5000)
    got = get_invoice(conn, t["id"], inv["id"])
    assert got["tax_cents"] == 0 and got["total_cents"] == 5000    # unchanged from before tax existed


# --- HTTP -------------------------------------------------------------------

def test_settings_tax_route_sets_bps(client, app):
    creds = onboard_studio(client, email="rate@example.com")
    login_owner(client, creds)
    client.post("/settings/tax", data={"tax_rate": "7.25"})        # 7.25%
    conn = connect(app.state.settings.db_path)
    try:
        bps = conn.execute("SELECT tax_rate_bps FROM tenants LIMIT 1").fetchone()["tax_rate_bps"]
    finally:
        conn.close()
    assert bps == 725


def test_settings_tax_tolerates_bad_input(client, app):
    creds = onboard_studio(client, email="bad@example.com")
    login_owner(client, creds)
    for bad in ("inf", "nan", "abc"):
        assert client.post("/settings/tax", data={"tax_rate": bad}).status_code in (200, 303)
    conn = connect(app.state.settings.db_path)
    try:
        bps = conn.execute("SELECT tax_rate_bps FROM tenants LIMIT 1").fetchone()["tax_rate_bps"]
    finally:
        conn.close()
    assert bps == 0                                                # non-finite/garbage → 0, no 500


def test_new_invoice_applies_studio_rate(client, app):
    creds = onboard_studio(client, email="tax@example.com")
    login_owner(client, creds)
    client.post("/settings/tax", data={"tax_rate": "8.5"})
    r = client.post("/invoices", data={"title": "Pkg", "amount": "100.00"})
    iid = r.url.path.rstrip("/").split("/")[-1]
    conn = connect(app.state.settings.db_path)
    try:
        row = conn.execute("SELECT amount_cents, tax_cents FROM invoices WHERE id = ?",
                           (iid,)).fetchone()
    finally:
        conn.close()
    assert row["amount_cents"] == 10000 and row["tax_cents"] == 850   # 8.5% applied on top


def test_pay_page_shows_tax_breakdown_and_total(client, app):
    creds = onboard_studio(client, email="pay@example.com")
    login_owner(client, creds)
    client.post("/settings/tax", data={"tax_rate": "10"})
    r = client.post("/invoices", data={"title": "Shoot", "amount": "200"})
    iid = r.url.path.rstrip("/").split("/")[-1]
    detail = client.get(f"/invoices/{iid}")
    token = detail.text.split("/pay/")[1].split('"')[0].split("<")[0].strip()
    client.post(f"/invoices/{iid}/send")
    page = client.get(f"/pay/{token}")
    assert "Subtotal" in page.text and "Tax" in page.text
    assert "$20.00" in page.text and "$220.00" in page.text          # tax + total (200 + 10%)


def test_tax_collected_sums_paid_invoice_tax(conn, settings):
    t = create_tenant(conn, name="TC", shoot_type="wedding")
    a = create_invoice(conn, settings, tenant_id=t["id"], title="A", amount_cents=10000, tax_cents=850)
    b = create_invoice(conn, settings, tenant_id=t["id"], title="B", amount_cents=20000, tax_cents=1700)
    create_invoice(conn, settings, tenant_id=t["id"], title="U", amount_cents=5000, tax_cents=425)  # unpaid
    conn.execute("UPDATE invoices SET status = 'paid' WHERE id IN (?, ?)", (a["id"], b["id"]))
    conn.commit()
    tc = tax_collected(conn, t["id"])
    assert tc["cents"] == 2550 and tc["display"] == "$25.50"         # only the two paid invoices' tax


def test_reports_page_shows_tax_collected(client, app):
    creds = onboard_studio(client, email="tc@example.com")
    login_owner(client, creds)
    client.post("/settings/tax", data={"tax_rate": "10"})
    r = client.post("/invoices", data={"title": "Shoot", "amount": "100"})
    iid = r.url.path.rstrip("/").split("/")[-1]
    detail = client.get(f"/invoices/{iid}")
    token = detail.text.split("/pay/")[1].split('"')[0].split("<")[0].strip()
    client.post(f"/invoices/{iid}/send")
    client.post(f"/pay/{token}/checkout")                            # mock settles → paid
    page = client.get("/finances/reports")
    assert "Sales tax collected" in page.text and "$10.00" in page.text   # 10% of $100
