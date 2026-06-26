"""Accounts receivable — overdue detection, the A/R rollup, and idempotent
past-due reminders (manual nudge + the worker's cooldown-gated auto-sweep)."""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client
from hestia.dashboard import needs_attention
from hestia.db import connect
from hestia.invoices import (
    accounts_receivable,
    create_invoice,
    list_invoices,
    record_invoice_reminder,
    send_invoice,
    send_overdue_reminders,
)
from hestia.payment_plans import create_payment_plan, deposit_balance_installments
from hestia.tenants import create_tenant

PAST = "2020-01-01"
FUTURE = "2099-01-01"


def _sent(conn, settings, *, tenant_id, cents, due_date="", client_id=None):
    inv = create_invoice(conn, settings, tenant_id=tenant_id, title="Bill", amount_cents=cents,
                         client_id=client_id, due_date=due_date)
    send_invoice(conn, tenant_id, inv["id"])
    return inv


def _emails(conn, tenant_id):
    return conn.execute("SELECT * FROM emails WHERE tenant_id = ? ORDER BY id", (tenant_id,)).fetchall()


# --- overdue detection + A/R rollup -----------------------------------------

def test_overdue_flagged_only_for_sent_past_due(conn, settings):
    t = create_tenant(conn, name="OD", shoot_type="wedding")
    od = _sent(conn, settings, tenant_id=t["id"], cents=10000, due_date=PAST)
    future = _sent(conn, settings, tenant_id=t["id"], cents=20000, due_date=FUTURE)
    nodate = _sent(conn, settings, tenant_id=t["id"], cents=30000, due_date="")
    draft = create_invoice(conn, settings, tenant_id=t["id"], title="Draft",
                           amount_cents=40000, due_date=PAST)              # still draft, not sent
    conn.commit()
    by_id = {i["id"]: i for i in list_invoices(conn, t["id"])}
    assert by_id[od["id"]]["is_overdue"] == 1 and by_id[od["id"]]["days_overdue"] > 0
    assert by_id[future["id"]]["is_overdue"] == 0                          # due in the future
    assert by_id[nodate["id"]]["is_overdue"] == 0                          # empty/free-text due date
    assert by_id[draft["id"]]["is_overdue"] == 0                          # draft isn't outstanding yet


def test_accounts_receivable_rollup(conn, settings):
    t = create_tenant(conn, name="AR", shoot_type="wedding")
    _sent(conn, settings, tenant_id=t["id"], cents=10000, due_date=PAST)      # overdue
    _sent(conn, settings, tenant_id=t["id"], cents=25000, due_date=FUTURE)    # outstanding, not overdue
    paid = create_invoice(conn, settings, tenant_id=t["id"], title="Paid", amount_cents=99999)
    conn.execute("UPDATE invoices SET status = 'paid' WHERE id = ?", (paid["id"],))
    create_invoice(conn, settings, tenant_id=t["id"], title="Draft", amount_cents=88888)  # draft
    conn.commit()
    ar = accounts_receivable(conn, t["id"])
    assert ar["outstanding_cents"] == 35000 and ar["outstanding_count"] == 2   # paid + draft excluded
    assert ar["overdue_cents"] == 10000 and ar["overdue_count"] == 1
    assert ar["outstanding"] == "$350.00" and ar["overdue"] == "$100.00"


def test_ar_is_tenant_scoped(conn, settings):
    t1 = create_tenant(conn, name="T1", shoot_type="wedding")
    t2 = create_tenant(conn, name="T2", shoot_type="wedding")
    _sent(conn, settings, tenant_id=t1["id"], cents=10000, due_date=PAST)
    conn.commit()
    assert accounts_receivable(conn, t2["id"])["outstanding_cents"] == 0


# --- the automated reminder sweep -------------------------------------------

def test_sweep_reminds_overdue_once_then_cools_down(conn, settings):
    t = create_tenant(conn, name="Sweep", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Late Client", email="late@example.com")
    _sent(conn, settings, tenant_id=t["id"], cents=15000, due_date=PAST, client_id=c["id"])
    conn.commit()

    assert send_overdue_reminders(conn, settings) == 1            # first sweep nudges
    conn.commit()
    assert len(_emails(conn, t["id"])) == 1
    row = conn.execute("SELECT reminder_count, last_reminder_at FROM invoices "
                       "WHERE tenant_id = ?", (t["id"],)).fetchone()
    assert row["reminder_count"] == 1 and row["last_reminder_at"]

    assert send_overdue_reminders(conn, settings) == 0            # cooldown: no second nudge
    conn.commit()
    assert len(_emails(conn, t["id"])) == 1                       # still just one email


def test_sweep_resends_after_cooldown_window(conn, settings):
    t = create_tenant(conn, name="Cool", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="C", email="c@example.com")
    _sent(conn, settings, tenant_id=t["id"], cents=15000, due_date=PAST, client_id=c["id"])
    conn.commit()
    assert send_overdue_reminders(conn, settings, cooldown_days=7) == 1
    conn.commit()
    conn.execute("UPDATE invoices SET last_reminder_at = datetime('now', '-10 days')")  # past the window
    conn.commit()
    assert send_overdue_reminders(conn, settings, cooldown_days=7) == 1


def test_sweep_ignores_future_paid_and_emailless(conn, settings):
    t = create_tenant(conn, name="Skip", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="HasMail", email="h@example.com")
    noemail = create_client(conn, tenant_id=t["id"], name="NoMail")             # no email on file
    _sent(conn, settings, tenant_id=t["id"], cents=10000, due_date=FUTURE, client_id=c["id"])   # not due
    _sent(conn, settings, tenant_id=t["id"], cents=20000, due_date=PAST, client_id=noemail["id"])  # no email
    paid = create_invoice(conn, settings, tenant_id=t["id"], title="P", amount_cents=30000,
                          due_date=PAST, client_id=c["id"])
    conn.execute("UPDATE invoices SET status = 'paid' WHERE id = ?", (paid["id"],))
    conn.commit()
    assert send_overdue_reminders(conn, settings) == 0
    conn.commit()
    assert len(_emails(conn, t["id"])) == 0


def test_record_reminder_only_sent_and_tenant_scoped(conn, settings):
    t1 = create_tenant(conn, name="A", shoot_type="wedding")
    t2 = create_tenant(conn, name="B", shoot_type="wedding")
    inv = _sent(conn, settings, tenant_id=t1["id"], cents=10000, due_date=PAST)
    draft = create_invoice(conn, settings, tenant_id=t1["id"], title="D", amount_cents=5000)
    conn.commit()
    assert record_invoice_reminder(conn, t2["id"], inv["id"]) is False         # wrong tenant
    assert record_invoice_reminder(conn, t1["id"], draft["id"]) is False        # not 'sent'
    assert record_invoice_reminder(conn, t1["id"], inv["id"]) is True


# --- HTTP -------------------------------------------------------------------

def test_manual_remind_emails_client_and_counts(client, app):
    creds = onboard_studio(client, email="ar@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        c = create_client(conn, tenant_id=tid, name="Client", email="client@example.com")
        inv = create_invoice(conn, app.state.settings, tenant_id=tid, title="Overdue bill",
                             amount_cents=12300, client_id=c["id"], due_date=PAST)
        send_invoice(conn, tid, inv["id"])
        conn.commit()
        iid = inv["id"]
    finally:
        conn.close()

    page = client.get("/invoices")
    assert "overdue" in page.text and "outstanding" in page.text          # badge + A/R strip

    assert client.post(f"/invoices/{iid}/remind").status_code in (200, 303)
    conn = connect(app.state.settings.db_path)
    try:
        em = conn.execute("SELECT subject, body FROM emails ORDER BY id DESC LIMIT 1").fetchone()
        cnt = conn.execute("SELECT reminder_count FROM invoices WHERE id = ?", (iid,)).fetchone()
    finally:
        conn.close()
    assert em and "past due" in em["subject"] and "/pay/" in em["body"]
    assert cnt["reminder_count"] == 1
    assert "Remind again" in client.get("/invoices").text                 # reflects the nudge


def test_remind_requires_login(client):
    assert client.post("/invoices/1/remind", follow_redirects=False).status_code == 303


# --- audit hardening --------------------------------------------------------

def test_sweep_skips_emailless_without_rescanning(conn, settings):
    """An overdue invoice whose client has no email is never selected by the sweep,
    so it isn't stamped — and isn't pointlessly rescanned every cadence."""
    t = create_tenant(conn, name="NoMail", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="NoMail")              # no email
    inv = _sent(conn, settings, tenant_id=t["id"], cents=10000, due_date=PAST, client_id=c["id"])
    conn.commit()
    assert send_overdue_reminders(conn, settings) == 0
    assert send_overdue_reminders(conn, settings) == 0                     # still not selected
    conn.commit()
    row = conn.execute("SELECT last_reminder_at, reminder_count FROM invoices WHERE id = ?",
                       (inv["id"],)).fetchone()
    assert row["last_reminder_at"] is None and row["reminder_count"] == 0  # never touched


def test_dashboard_does_not_leak_cross_tenant_client(conn, settings):
    """A cross-tenant client_id on an invoice must not surface the other studio's
    client name in the dashboard's needs-attention list."""
    a = create_tenant(conn, name="A", shoot_type="wedding")
    b = create_tenant(conn, name="B", shoot_type="wedding")
    ca = create_client(conn, tenant_id=a["id"], name="SECRET-A")
    inv = create_invoice(conn, settings, tenant_id=b["id"], title="X", amount_cents=100,
                         client_id=ca["id"])
    conn.execute("UPDATE invoices SET status = 'sent' WHERE id = ?", (inv["id"],))
    conn.commit()
    att = needs_attention(conn, b["id"])
    assert att["unpaid"] and att["unpaid"][0]["client_name"] is None       # A's name not surfaced


def test_plan_installments_excluded_from_ar(conn, settings):
    """Plan installments live under their payment plan, so A/R (which mirrors the flat
    invoices list) must not count them — even once they're sent."""
    t = create_tenant(conn, name="Plan", shoot_type="wedding")
    _sent(conn, settings, tenant_id=t["id"], cents=10000, due_date=FUTURE)  # one standalone
    create_payment_plan(conn, settings, tenant_id=t["id"], title="Wedding", client_id=None,
                        installments=deposit_balance_installments(total_cents=400000,
                                                                  deposit_cents=100000))
    conn.execute("UPDATE invoices SET status = 'sent' WHERE plan_id IS NOT NULL")
    conn.commit()
    ar = accounts_receivable(conn, t["id"])
    assert ar["outstanding_count"] == 1 and ar["outstanding_cents"] == 10000  # only the standalone


def test_manual_remind_wording_matches_overdue_state(client, app):
    """A nudge on a not-yet-due invoice must not falsely say 'past due'; an overdue
    one must."""
    creds = onboard_studio(client, email="word@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        c = create_client(conn, tenant_id=tid, name="C", email="c@example.com")
        fut = create_invoice(conn, app.state.settings, tenant_id=tid, title="Upcoming",
                             amount_cents=5000, client_id=c["id"], due_date=FUTURE)
        send_invoice(conn, tid, fut["id"])
        late = create_invoice(conn, app.state.settings, tenant_id=tid, title="Latebill",
                              amount_cents=6000, client_id=c["id"], due_date=PAST)
        send_invoice(conn, tid, late["id"])
        conn.commit()
        fid, lid = fut["id"], late["id"]
    finally:
        conn.close()
    client.post(f"/invoices/{fid}/remind")
    client.post(f"/invoices/{lid}/remind")
    conn = connect(app.state.settings.db_path)
    try:
        subjects = [r["subject"] for r in conn.execute("SELECT subject FROM emails").fetchall()]
    finally:
        conn.close()
    assert any("Upcoming" in s and "past due" not in s for s in subjects)   # future → neutral
    assert any("Latebill" in s and "past due" in s for s in subjects)       # overdue → past due


def test_manual_remind_no_email_records_skip(client, app):
    """Reminding an invoice whose client has no email leaves an audit trail instead of
    a silent no-op, and does not increment the reminder count or send anything."""
    creds = onboard_studio(client, email="skip@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        c = create_client(conn, tenant_id=tid, name="NoMail")              # no email
        inv = create_invoice(conn, app.state.settings, tenant_id=tid, title="Skipbill",
                             amount_cents=5000, client_id=c["id"], due_date=PAST)
        send_invoice(conn, tid, inv["id"])
        conn.commit()
        iid = inv["id"]
    finally:
        conn.close()
    client.post(f"/invoices/{iid}/remind")
    conn = connect(app.state.settings.db_path)
    try:
        cnt = conn.execute("SELECT reminder_count FROM invoices WHERE id = ?", (iid,)).fetchone()
        skipped = conn.execute("SELECT COUNT(*) AS n FROM audit_log "
                               "WHERE action = 'invoice.remind_skipped'").fetchone()["n"]
        mailed = conn.execute("SELECT COUNT(*) AS n FROM emails WHERE subject LIKE '%Skipbill%'").fetchone()["n"]
    finally:
        conn.close()
    assert cnt["reminder_count"] == 0 and skipped == 1 and mailed == 0
