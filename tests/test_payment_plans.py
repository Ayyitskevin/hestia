"""Payment plans — installment generation, derived progress, idempotent settle."""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client, create_project
from hestia.email import list_emails
from hestia.invoices import get_invoice_by_token, list_invoices, mark_paid
from hestia.payment_plans import (
    create_payment_plan,
    deposit_balance_installments,
    get_payment_plan,
    list_payment_plans,
    void_payment_plan,
)
from hestia.tenants import create_tenant


def _tenant(conn, name="Plan Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def test_deposit_balance_helper():
    out = deposit_balance_installments(total_cents=400000, deposit_cents=100000,
                                       balance_due_date="2026-08-15")
    assert [i["amount_cents"] for i in out] == [100000, 300000]
    assert out[0]["label"] == "Deposit" and out[1]["due_date"] == "2026-08-15"


def test_deposit_clamped_and_balance_omitted():
    # A deposit covering the whole total → single installment, no zero balance.
    out = deposit_balance_installments(total_cents=200000, deposit_cents=500000)
    assert len(out) == 1 and out[0]["amount_cents"] == 200000


def test_create_plan_builds_installments(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah")
    p = create_project(conn, tenant_id=t["id"], name="Wedding", client_id=c["id"])
    plan = create_payment_plan(
        conn, settings, tenant_id=t["id"], title="Wedding", client_id=c["id"], project_id=p["id"],
        installments=deposit_balance_installments(total_cents=400000, deposit_cents=100000,
                                                  balance_due_date="2026-08-15"),
    )
    assert plan["total_cents"] == 400000
    assert plan["progress"] == "open" and plan["paid_cents"] == 0
    assert [i["sequence"] for i in plan["installments"]] == [1, 2]
    assert plan["installments"][0]["title"] == "Wedding — Deposit"
    assert plan["installments"][1]["due_date"] == "2026-08-15"
    assert plan["client_name"] == "Sarah" and plan["project_name"] == "Wedding"


def test_create_plan_drops_foreign_parent_ids(conn, settings):
    a = _tenant(conn, "A")
    b = _tenant(conn, "B")
    foreign_client = create_client(conn, tenant_id=a["id"], name="Foreign")
    foreign_project = create_project(conn, tenant_id=a["id"], name="Foreign Project")
    plan = create_payment_plan(
        conn, settings, tenant_id=b["id"], title="B Plan",
        client_id=foreign_client["id"], project_id=foreign_project["id"],
        installments=deposit_balance_installments(total_cents=100000, deposit_cents=25000),
    )
    assert plan["client_id"] is None and plan["project_id"] is None
    assert all(i["client_id"] is None and i["project_id"] is None for i in plan["installments"])


def test_create_plan_drops_project_for_wrong_same_tenant_client(conn, settings):
    t = _tenant(conn)
    sarah = create_client(conn, tenant_id=t["id"], name="Sarah")
    bob = create_client(conn, tenant_id=t["id"], name="Bob")
    bob_project = create_project(conn, tenant_id=t["id"], name="Bob shoot", client_id=bob["id"])
    plan = create_payment_plan(
        conn, settings, tenant_id=t["id"], title="Sarah Plan",
        client_id=sarah["id"], project_id=bob_project["id"],
        installments=deposit_balance_installments(total_cents=100000, deposit_cents=25000),
    )
    assert plan["client_id"] == sarah["id"]
    assert plan["project_id"] is None
    assert all(i["client_id"] == sarah["id"] and i["project_id"] is None
               for i in plan["installments"])


def test_progress_open_partial_paid(conn, settings):
    t = _tenant(conn)
    plan = create_payment_plan(
        conn, settings, tenant_id=t["id"], title="Booking",
        installments=deposit_balance_installments(total_cents=400000, deposit_cents=100000),
    )
    deposit, balance = plan["installments"]
    # pay deposit → partial
    assert mark_paid(conn, token=deposit["token"], provider="mock", ref="d") is True
    mid = get_payment_plan(conn, t["id"], plan["id"])
    assert mid["progress"] == "partial" and mid["paid_cents"] == 100000
    assert mid["remaining_cents"] == 300000
    # pay balance → paid
    mark_paid(conn, token=balance["token"], provider="mock", ref="b")
    done = get_payment_plan(conn, t["id"], plan["id"])
    assert done["progress"] == "paid" and done["remaining_cents"] == 0


def test_settle_is_idempotent(conn, settings):
    t = _tenant(conn)
    plan = create_payment_plan(
        conn, settings, tenant_id=t["id"], title="X",
        installments=deposit_balance_installments(total_cents=200000, deposit_cents=50000),
    )
    tok = plan["installments"][0]["token"]
    assert mark_paid(conn, token=tok, provider="mock", ref="r1") is True
    # second settle is a no-op — plan still shows just the one paid installment
    assert mark_paid(conn, token=tok, provider="mock", ref="r2") is False
    again = get_payment_plan(conn, t["id"], plan["id"])
    assert again["paid_cents"] == 50000


def test_installments_excluded_from_standalone_list(conn, settings):
    t = _tenant(conn)
    create_payment_plan(
        conn, settings, tenant_id=t["id"], title="Plan",
        installments=deposit_balance_installments(total_cents=200000, deposit_cents=50000),
    )
    # the two installments exist as invoices...
    assert len(list_invoices(conn, t["id"])) == 2
    # ...but the flat (standalone) invoice list hides them
    assert list_invoices(conn, t["id"], standalone_only=True) == []


def test_void_plan_voids_unpaid_only(conn, settings):
    t = _tenant(conn)
    plan = create_payment_plan(
        conn, settings, tenant_id=t["id"], title="X",
        installments=deposit_balance_installments(total_cents=400000, deposit_cents=100000),
    )
    deposit, balance = plan["installments"]
    mark_paid(conn, token=deposit["token"], provider="mock", ref="d")  # deposit paid
    void_payment_plan(conn, t["id"], plan["id"])
    voided = get_payment_plan(conn, t["id"], plan["id"])
    assert voided["status"] == "void"
    # paid deposit stands; unpaid balance is voided
    assert get_invoice_by_token(conn, deposit["token"])["status"] == "paid"
    assert get_invoice_by_token(conn, balance["token"])["status"] == "void"


def test_tenant_isolation(conn, settings):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    create_payment_plan(conn, settings, tenant_id=t1["id"], title="A-plan",
                        installments=deposit_balance_installments(total_cents=100, deposit_cents=50))
    assert list_payment_plans(conn, t2["id"]) == []
    assert list_invoices(conn, t2["id"]) == []


def test_http_plan_and_pay_flow(client):
    creds = onboard_studio(client, email="plan@example.com")
    login_owner(client, creds)
    r = client.post("/payment-plans", data={
        "title": "Wedding", "total": "4,000.00", "deposit": "1,000.00",
        "balance_due_date": "2026-08-15",
    })
    pid = r.url.path.rstrip("/").split("/")[-1]
    detail = client.get(f"/payment-plans/{pid}")
    assert detail.status_code == 200
    assert "$4,000.00" in detail.text and "$1,000.00" in detail.text
    assert "open" in detail.text

    # first /pay/ link in the table is the deposit installment
    token = detail.text.split("/pay/")[1].split('"')[0].split("<")[0].strip()
    client.post(f"/pay/{token}/checkout")  # mock settles immediately
    after = client.get(f"/payment-plans/{pid}")
    assert "partial" in after.text and "$1,000.00" in after.text


def test_http_send_schedule_emails_links(client, app):
    creds = onboard_studio(client, email="send@example.com")
    login_owner(client, creds)
    # a client with an email so the schedule can be sent; the create redirect
    # lands on /clients/{id}, so read the id straight off the final URL.
    rc = client.post("/clients", data={"name": "Sarah", "email": "sarah@example.com"})
    cid = rc.url.path.rstrip("/").split("/")[-1]
    r = client.post("/payment-plans", data={
        "title": "Wedding", "total": "2000", "deposit": "500", "client_id": cid,
    })
    pid = r.url.path.rstrip("/").split("/")[-1]
    client.post(f"/payment-plans/{pid}/send")

    from hestia.db import connect
    conn = connect(app.state.settings.db_path)
    try:
        tenant_id = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        outbox = list_emails(conn, tenant_id)
    finally:
        conn.close()
    assert any("payment schedule" in m["subject"].lower() for m in outbox)
    assert any("/pay/" in m["body"] for m in outbox)


def test_to_cents_handles_non_finite_money():
    """The amount parser degrades to 0 on non-finite input instead of a 500 (it
    catches OverflowError via the math.isfinite guard), matching the invoices route."""
    from hestia.routes.payment_plans import _to_cents

    assert _to_cents("2,500.50") == 250050          # normal money still parses
    assert _to_cents("inf") == 0                     # would OverflowError in int(round(inf))
    assert _to_cents("-inf") == 0
    assert _to_cents("1e400") == 0                   # parses to inf
    assert _to_cents("nan") == 0
    assert _to_cents("not money") == 0
