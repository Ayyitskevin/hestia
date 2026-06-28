"""Recurring invoices — cadence math, claim-before-act idempotency, generation, scoping."""

from conftest import CSRFClient, login_owner, onboard_studio

from hestia.crm import create_client
from hestia.db import connect
from hestia.email import list_emails
from hestia.recurring import (
    _claim_due,
    create_recurring,
    get_recurring,
    list_recurring,
    run_recurring,
    set_recurring_active,
)
from hestia.tenants import create_tenant, set_tax_rate


def _tenant(conn, name="Rec Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def _today(conn):
    return conn.execute("SELECT date('now')").fetchone()[0]


def _date(conn, mod):
    return conn.execute("SELECT date('now', ?)", (mod,)).fetchone()[0]


def _invoices(conn, tenant_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM invoices WHERE tenant_id = ? ORDER BY id", (tenant_id,))]


# ── Module CRUD ───────────────────────────────────────────────────────────────


def test_create_and_list(conn):
    t = _tenant(conn)
    p = create_recurring(conn, tenant_id=t["id"], title="Retainer", amount_cents=50000,
                         cadence="monthly", next_run_at=_today(conn))
    assert p["title"] == "Retainer" and p["amount_cents"] == 50000 and p["active"] == 1
    assert [x["title"] for x in list_recurring(conn, t["id"])] == ["Retainer"]
    assert create_recurring(conn, tenant_id=t["id"], title="  ", amount_cents=1) is None  # blank
    # an unparseable start date falls back to today, never NULL
    p2 = create_recurring(conn, tenant_id=t["id"], title="X", amount_cents=1, next_run_at="not-a-date")
    assert p2["next_run_at"] == _today(conn)


def test_negative_amount_floored(conn):
    t = _tenant(conn)
    p = create_recurring(conn, tenant_id=t["id"], title="R", amount_cents=-5)
    assert p["amount_cents"] == 0


# ── Cadence advance + claim idempotency ───────────────────────────────────────


def test_claim_due_advances_by_cadence_then_not_due(conn):
    t = _tenant(conn)
    for cadence, mod in [("weekly", "+7 days"), ("monthly", "+1 month"), ("yearly", "+1 year")]:
        p = create_recurring(conn, tenant_id=t["id"], title=f"R-{cadence}", amount_cents=1000,
                             cadence=cadence, next_run_at=_today(conn))
        expected = conn.execute("SELECT date(?, ?)", (p["next_run_at"], mod)).fetchone()[0]
        assert _claim_due(conn, p["id"]) is True                       # first claim wins
        assert get_recurring(conn, t["id"], p["id"])["next_run_at"] == expected
        assert _claim_due(conn, p["id"]) is False                      # now future → no double-claim


# ── Generation + idempotency ──────────────────────────────────────────────────


def test_run_recurring_generates_once_then_not_due(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Acme", email="acme@x.com")
    create_recurring(conn, tenant_id=t["id"], title="Retainer", amount_cents=50000,
                     cadence="monthly", next_run_at=_today(conn), client_id=c["id"])
    assert run_recurring(conn, settings) == 1
    invs = _invoices(conn, t["id"])
    assert len(invs) == 1 and invs[0]["amount_cents"] == 50000 and invs[0]["status"] == "sent"
    assert invs[0]["client_id"] == c["id"]
    # immediately running again generates nothing — the profile was advanced past today
    assert run_recurring(conn, settings) == 0
    assert len(_invoices(conn, t["id"])) == 1


def test_generated_invoice_applies_tax_and_emails_client(conn, settings):
    t = _tenant(conn)
    set_tax_rate(conn, t["id"], 1000)                                  # 10%
    c = create_client(conn, tenant_id=t["id"], name="Acme", email="acme@x.com")
    create_recurring(conn, tenant_id=t["id"], title="Retainer", amount_cents=50000,
                     cadence="monthly", next_run_at=_today(conn), client_id=c["id"])
    assert run_recurring(conn, settings) == 1
    inv = _invoices(conn, t["id"])[0]
    assert inv["tax_cents"] == 5000                                    # 10% of 50000
    assert len(list_emails(conn, t["id"], to_addr="acme@x.com")) == 1  # pay link emailed


def test_run_recurring_catches_up_one_period_per_sweep(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Acme", email="a@x.com")
    p = create_recurring(conn, tenant_id=t["id"], title="Weekly", amount_cents=1000,
                         cadence="weekly", client_id=c["id"])
    # simulate the worker having been down: backdate next_run_at two periods (create()
    # itself floors a past start date to today, so set it directly here)
    conn.execute("UPDATE recurring_invoices SET next_run_at = ? WHERE id = ?",
                 (_date(conn, "-14 days"), p["id"]))
    conn.commit()
    # a profile two periods behind generates one invoice per sweep, not all at once
    assert run_recurring(conn, settings) == 1
    assert run_recurring(conn, settings) == 1
    assert len(_invoices(conn, t["id"])) == 2


def test_paused_profile_not_generated(conn, settings):
    t = _tenant(conn)
    p = create_recurring(conn, tenant_id=t["id"], title="R", amount_cents=1000,
                         next_run_at=_today(conn))
    set_recurring_active(conn, t["id"], p["id"], False)
    assert run_recurring(conn, settings) == 0
    assert _invoices(conn, t["id"]) == []


def test_future_profile_not_yet_due(conn, settings):
    t = _tenant(conn)
    create_recurring(conn, tenant_id=t["id"], title="R", amount_cents=1000,
                     next_run_at=_date(conn, "+10 days"))
    assert run_recurring(conn, settings) == 0


# ── Tenant scoping ────────────────────────────────────────────────────────────


def test_recurring_tenant_scoped(conn, settings):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    p = create_recurring(conn, tenant_id=t1["id"], title="A-ret", amount_cents=1000,
                         next_run_at=_today(conn))
    assert get_recurring(conn, t2["id"], p["id"]) is None
    assert list_recurring(conn, t2["id"]) == []
    set_recurring_active(conn, t2["id"], p["id"], False)               # cross-tenant no-op
    assert get_recurring(conn, t1["id"], p["id"])["active"] == 1
    # generation lands the invoice under the owning tenant only
    assert run_recurring(conn, settings) == 1
    assert len(_invoices(conn, t1["id"])) == 1 and _invoices(conn, t2["id"]) == []


# ── HTTP flow ─────────────────────────────────────────────────────────────────


def _tid(conn, email):
    return conn.execute(
        "SELECT t.id FROM tenants t JOIN users u ON u.tenant_id = t.id WHERE u.email = ?",
        (email,),
    ).fetchone()["id"]


def test_http_create_pause_resume(client, app):
    creds = onboard_studio(client, name="HTTP Rec", email="httprec@example.com")
    login_owner(client, creds)
    assert "Recurring invoices" in client.get("/recurring").text
    client.post("/recurring", data={"title": "Monthly retainer", "amount": "500",
                                    "cadence": "monthly"})
    assert "Monthly retainer" in client.get("/recurring").text

    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid(conn, creds["email"])
        rid = list_recurring(conn, tid)[0]["id"]
    finally:
        conn.close()

    client.post(f"/recurring/{rid}/pause")
    assert "Resume" in client.get("/recurring").text
    client.post(f"/recurring/{rid}/resume")
    assert "Pause" in client.get("/recurring").text


# ── Hardening from the adversarial money-path review ───────────────────────────


def test_monthly_advance_clamps_end_of_month_no_skip(conn):
    # Jan 31 must advance to Feb 28/29 (clamped), never overflow to March (a skipped month)
    t = _tenant(conn)
    p = create_recurring(conn, tenant_id=t["id"], title="EOM", amount_cents=1000, cadence="monthly")
    conn.execute("UPDATE recurring_invoices SET next_run_at = '2024-01-31' WHERE id = ?", (p["id"],))
    conn.commit()
    assert _claim_due(conn, p["id"]) is True
    assert get_recurring(conn, t["id"], p["id"])["next_run_at"] == "2024-02-29"  # leap Feb


def test_create_floors_backdated_start_to_today(conn):
    # a past 'Starting' date must not put the profile in arrears (no catch-up storm)
    t = _tenant(conn)
    p = create_recurring(conn, tenant_id=t["id"], title="P", amount_cents=1000,
                         next_run_at=_date(conn, "-30 days"))
    assert p["next_run_at"] == _today(conn)


def test_failed_profile_does_not_roll_back_or_rebill_others(conn, settings, monkeypatch):
    import hestia.recurring as rec
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="A", email="a@x.com")
    create_recurring(conn, tenant_id=t["id"], title="P1", amount_cents=1000,
                     next_run_at=_today(conn), client_id=c["id"])
    create_recurring(conn, tenant_id=t["id"], title="P2", amount_cents=2000,
                     next_run_at=_today(conn), client_id=c["id"])
    conn.commit()
    real = rec._bill_profile

    def flaky(conn_, settings_, prof):
        if prof["title"] == "P2":
            raise RuntimeError("boom")
        return real(conn_, settings_, prof)

    monkeypatch.setattr(rec, "_bill_profile", flaky)
    assert rec.run_recurring(conn, settings) == 1            # only the healthy profile billed
    invs = _invoices(conn, t["id"])
    assert len(invs) == 1 and invs[0]["amount_cents"] == 1000  # P1 committed despite P2 failing
    # P2's claim was rolled back, so it's still due and retries next sweep (not lost, not advanced)
    p2 = next(p for p in list_recurring(conn, t["id"]) if p["title"] == "P2")
    assert p2["next_run_at"] == _today(conn) and p2["invoice_count"] == 0


def test_email_failure_after_commit_does_not_roll_back_bill(conn, settings, monkeypatch):
    import hestia.recurring as rec
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="A", email="a@x.com")
    create_recurring(conn, tenant_id=t["id"], title="P", amount_cents=1000,
                     next_run_at=_today(conn), client_id=c["id"])
    conn.commit()

    def boom(*a, **k):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(rec, "_email_invoice", boom)
    # the bill is committed BEFORE the email, so a send failure leaves exactly one invoice
    assert rec.run_recurring(conn, settings) == 1
    assert len(_invoices(conn, t["id"])) == 1
    # and the profile is advanced, so the next sweep does NOT re-bill
    assert rec.run_recurring(conn, settings) == 0


def test_http_create_ignores_foreign_client_id(client, app):
    a = onboard_studio(client, name="Own A", email="owna@example.com")
    login_owner(client, a)
    conn = connect(app.state.settings.db_path)
    try:
        a_cid = create_client(conn, tenant_id=_tid(conn, a["email"]), name="A-client",
                              email="ac@x.com")["id"]
        conn.commit()
    finally:
        conn.close()

    b_client = CSRFClient(app)
    b = onboard_studio(b_client, name="Own B", email="ownb@example.com")
    login_owner(b_client, b)
    b_client.post("/recurring", data={"title": "R", "amount": "100", "cadence": "monthly",
                                      "client_id": str(a_cid)})
    conn = connect(app.state.settings.db_path)
    try:
        prof = list_recurring(conn, _tid(conn, b["email"]))[0]
        assert prof["client_id"] is None                   # foreign client id dropped
    finally:
        conn.close()


def test_http_delete(client, app):
    creds = onboard_studio(client, name="Del Studio", email="del@example.com")
    login_owner(client, creds)
    client.post("/recurring", data={"title": "Temp", "amount": "100", "cadence": "monthly"})
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid(conn, creds["email"])
        rid = list_recurring(conn, tid)[0]["id"]
    finally:
        conn.close()
    client.post(f"/recurring/{rid}/delete")
    conn = connect(app.state.settings.db_path)
    try:
        assert list_recurring(conn, _tid(conn, creds["email"])) == []
    finally:
        conn.close()
