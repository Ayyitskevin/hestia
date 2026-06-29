"""Discount codes — CRUD/validation, the apply money-path (subtotal cut + proportional tax,
usage limits, every guard, tenant isolation), and the public pay-page flow."""

from conftest import CSRFClient, login_owner, onboard_studio

from hestia.crm import create_client
from hestia.db import connect
from hestia.discounts import (
    apply_code_to_invoice,
    create_discount,
    discount_amount,
    get_discount,
    list_discounts,
    normalize_code,
)
from hestia.invoices import create_invoice, get_invoice, send_invoice
from hestia.tenants import create_tenant


def _tenant(conn, name="Promo Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def _tid_of(conn, email):
    return conn.execute(
        "SELECT t.id FROM tenants t JOIN users u ON u.tenant_id = t.id WHERE u.email = ?",
        (email,),
    ).fetchone()["id"]


def _invoice(conn, settings, tenant_id, *, amount=10000, tax=0, client_id=None):
    return create_invoice(conn, settings, tenant_id=tenant_id, title="Session",
                          amount_cents=amount, tax_cents=tax, client_id=client_id)


# ── CRUD + validation ─────────────────────────────────────────────────────────


def test_create_validates_and_normalizes(conn):
    t = _tenant(conn)
    d = create_discount(conn, tenant_id=t["id"], code=" save25 ", kind="percent", value=25)
    assert d and d["code"] == "SAVE25"                          # normalized upper, trimmed
    assert create_discount(conn, tenant_id=t["id"], code="", kind="percent", value=10) is None
    assert create_discount(conn, tenant_id=t["id"], code="X", kind="percent", value=0) is None     # 0%
    assert create_discount(conn, tenant_id=t["id"], code="X", kind="percent", value=150) is None   # >100%
    assert create_discount(conn, tenant_id=t["id"], code="X", kind="fixed", value=0) is None       # $0
    assert create_discount(conn, tenant_id=t["id"], code="SAVE25", kind="fixed", value=500) is None  # dup


def test_codes_tenant_scoped(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    create_discount(conn, tenant_id=t1["id"], code="SHARED", kind="percent", value=10)
    # the SAME string is free in another tenant (unique is per-tenant)
    assert create_discount(conn, tenant_id=t2["id"], code="SHARED", kind="fixed", value=500)
    assert [d["code"] for d in list_discounts(conn, t2["id"])] == ["SHARED"]
    assert len(list_discounts(conn, t1["id"])) == 1


def test_discount_amount_math():
    assert discount_amount("percent", 25, 10000) == 2500
    assert discount_amount("percent", 100, 10000) == 10000           # capped at subtotal
    assert discount_amount("fixed", 1500, 10000) == 1500
    assert discount_amount("fixed", 99999, 10000) == 10000           # never more than subtotal
    assert normalize_code("  abc ") == "ABC"


# ── apply: the money path ─────────────────────────────────────────────────────


def test_apply_percent_reduces_subtotal(conn, settings):
    t = _tenant(conn)
    inv = _invoice(conn, settings, t["id"], amount=10000)
    create_discount(conn, tenant_id=t["id"], code="SAVE25", kind="percent", value=25)
    conn.commit()
    res = apply_code_to_invoice(conn, invoice_token=inv["token"], code="save25")
    assert res["ok"] and res["discount_cents"] == 2500
    fresh = get_invoice(conn, t["id"], inv["id"])
    assert fresh["amount_cents"] == 7500 and fresh["discount_cents"] == 2500
    assert fresh["discount_code"] == "SAVE25"
    assert get_discount(conn, t["id"], list_discounts(conn, t["id"])[0]["id"])["used_count"] == 1


def test_apply_scales_tax_proportionally(conn, settings):
    t = _tenant(conn)
    inv = _invoice(conn, settings, t["id"], amount=10000, tax=1000)    # 10% tax
    create_discount(conn, tenant_id=t["id"], code="HALF", kind="percent", value=50)
    conn.commit()
    apply_code_to_invoice(conn, invoice_token=inv["token"], code="HALF")
    fresh = get_invoice(conn, t["id"], inv["id"])
    assert fresh["amount_cents"] == 5000 and fresh["tax_cents"] == 500   # tax scaled with subtotal


def test_apply_fixed_amount(conn, settings):
    t = _tenant(conn)
    inv = _invoice(conn, settings, t["id"], amount=10000)
    create_discount(conn, tenant_id=t["id"], code="TWENTY", kind="fixed", value=2000)
    conn.commit()
    apply_code_to_invoice(conn, invoice_token=inv["token"], code="TWENTY")
    assert get_invoice(conn, t["id"], inv["id"])["amount_cents"] == 8000


def test_apply_guards(conn, settings):
    t = _tenant(conn)
    inv = _invoice(conn, settings, t["id"], amount=10000)
    conn.commit()
    # unknown code
    assert apply_code_to_invoice(conn, invoice_token=inv["token"], code="NOPE")["ok"] is False
    # blank
    assert apply_code_to_invoice(conn, invoice_token=inv["token"], code="")["ok"] is False
    # inactive
    d = create_discount(conn, tenant_id=t["id"], code="OFF", kind="percent", value=10)
    conn.execute("UPDATE discount_codes SET active = 0 WHERE id = ?", (d["id"],))
    assert apply_code_to_invoice(conn, invoice_token=inv["token"], code="OFF")["ok"] is False
    # expired
    create_discount(conn, tenant_id=t["id"], code="OLD", kind="percent", value=10,
                    expires_on="2000-01-01")
    assert apply_code_to_invoice(conn, invoice_token=inv["token"], code="OLD")["ok"] is False
    # nothing changed on the invoice through all the rejected attempts
    assert get_invoice(conn, t["id"], inv["id"])["amount_cents"] == 10000


def test_apply_only_once_per_invoice(conn, settings):
    t = _tenant(conn)
    inv = _invoice(conn, settings, t["id"], amount=10000)
    create_discount(conn, tenant_id=t["id"], code="A", kind="percent", value=10)
    create_discount(conn, tenant_id=t["id"], code="B", kind="percent", value=10)
    conn.commit()
    assert apply_code_to_invoice(conn, invoice_token=inv["token"], code="A")["ok"]
    second = apply_code_to_invoice(conn, invoice_token=inv["token"], code="B")
    assert second["ok"] is False and "already" in second["error"].lower()
    assert get_invoice(conn, t["id"], inv["id"])["amount_cents"] == 9000   # only the first applied


def test_apply_respects_usage_limit(conn, settings):
    t = _tenant(conn)
    create_discount(conn, tenant_id=t["id"], code="ONCE", kind="percent", value=10, max_uses=1)
    inv1 = _invoice(conn, settings, t["id"], amount=10000)
    inv2 = _invoice(conn, settings, t["id"], amount=10000)
    conn.commit()
    assert apply_code_to_invoice(conn, invoice_token=inv1["token"], code="ONCE")["ok"]
    exhausted = apply_code_to_invoice(conn, invoice_token=inv2["token"], code="ONCE")
    assert exhausted["ok"] is False and "limit" in exhausted["error"].lower()
    assert get_invoice(conn, t["id"], inv2["id"])["amount_cents"] == 10000   # not discounted


def test_apply_rejected_on_paid_invoice(conn, settings):
    t = _tenant(conn)
    inv = _invoice(conn, settings, t["id"], amount=10000)
    conn.execute("UPDATE invoices SET status='paid' WHERE id=?", (inv["id"],))
    create_discount(conn, tenant_id=t["id"], code="LATE", kind="percent", value=10)
    conn.commit()
    assert apply_code_to_invoice(conn, invoice_token=inv["token"], code="LATE")["ok"] is False


def test_apply_cross_tenant_code_invalid(conn, settings):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    create_discount(conn, tenant_id=t1["id"], code="AONLY", kind="percent", value=50)
    inv = _invoice(conn, settings, t2["id"], amount=10000)              # t2's invoice
    conn.commit()
    res = apply_code_to_invoice(conn, invoice_token=inv["token"], code="AONLY")
    assert res["ok"] is False                                            # t1's code can't touch t2
    assert get_invoice(conn, t2["id"], inv["id"])["amount_cents"] == 10000


# ── HTTP: owner CRUD + public apply on the pay page ───────────────────────────


def test_owner_crud_http(client, app):
    creds = onboard_studio(client, email="disc_owner@example.com")
    login_owner(client, creds)
    client.post("/settings/discounts", data={"code": "spring25", "kind": "percent", "value": "25"})
    page = client.get("/settings/discounts").text
    assert "SPRING25" in page and "25% off" in page
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        did = list_discounts(conn, tid)[0]["id"]
    finally:
        conn.close()
    client.post(f"/settings/discounts/{did}/toggle")               # disable
    conn = connect(app.state.settings.db_path)
    try:
        assert get_discount(conn, tid, did)["active"] == 0
    finally:
        conn.close()
    client.post(f"/settings/discounts/{did}/delete")
    conn = connect(app.state.settings.db_path)
    try:
        assert list_discounts(conn, tid) == []
    finally:
        conn.close()


def test_owner_create_fixed_amount_in_dollars(client, app):
    creds = onboard_studio(client, email="disc_fixed@example.com")
    login_owner(client, creds)
    client.post("/settings/discounts", data={"code": "TENOFF", "kind": "fixed", "value": "10.00"})
    conn = connect(app.state.settings.db_path)
    try:
        d = list_discounts(conn, _tid_of(conn, creds["email"]))[0]
        assert d["kind"] == "fixed" and d["value"] == 1000        # $10 → 1000 cents
    finally:
        conn.close()


def test_public_apply_on_pay_page(client, app):
    creds = onboard_studio(client, email="disc_pay@example.com")
    login_owner(client, creds)
    client.post("/settings/discounts", data={"code": "SAVE20", "kind": "percent", "value": "20"})
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        c = create_client(conn, tenant_id=tid, name="Payer", email="payer@example.com")
        inv = create_invoice(conn, app.state.settings, tenant_id=tid, title="Balance",
                             amount_cents=10000, client_id=c["id"])
        send_invoice(conn, tid, inv["id"])
        conn.commit()
        token = inv["token"]
        iid = inv["id"]
    finally:
        conn.close()

    pub = CSRFClient(app)
    assert "Discount code" in pub.get(f"/pay/{token}").text         # the entry form is offered
    r = pub.post(f"/pay/{token}/discount", data={"code": "save20"}, follow_redirects=False)
    assert r.status_code == 303
    page = pub.get(f"/pay/{token}").text
    assert "SAVE20" in page and "$80.00" in page                    # discount line + reduced total
    conn = connect(app.state.settings.db_path)
    try:
        assert get_invoice(conn, tid, iid)["amount_cents"] == 8000  # the charge is now reduced
    finally:
        conn.close()


def test_full_discount_settles_without_charging(client, app):
    """A 100%-off code zeroes the invoice; checkout settles it directly (no provider
    charge) instead of dead-ending on a zero-amount payment."""
    creds = onboard_studio(client, email="disc_free@example.com")
    login_owner(client, creds)
    client.post("/settings/discounts", data={"code": "COMP", "kind": "percent", "value": "100"})
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        inv = create_invoice(conn, app.state.settings, tenant_id=tid, title="Comp",
                             amount_cents=10000)
        send_invoice(conn, tid, inv["id"])
        conn.commit()
        token, iid = inv["token"], inv["id"]
    finally:
        conn.close()
    pub = CSRFClient(app)
    pub.post(f"/pay/{token}/discount", data={"code": "COMP"})
    conn = connect(app.state.settings.db_path)
    try:
        assert get_invoice(conn, tid, iid)["amount_cents"] == 0          # zeroed by the 100% code
    finally:
        conn.close()
    pub.post(f"/pay/{token}/checkout")
    conn = connect(app.state.settings.db_path)
    try:
        row = get_invoice(conn, tid, iid)
        assert row["status"] == "paid" and row["provider"] == "comp"   # settled, not charged
    finally:
        conn.close()


def test_public_apply_invalid_code_shows_error(client, app):
    creds = onboard_studio(client, email="disc_bad@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        inv = create_invoice(conn, app.state.settings, tenant_id=tid, title="Balance",
                             amount_cents=10000)
        send_invoice(conn, tid, inv["id"])
        conn.commit()
        token, iid = inv["token"], inv["id"]
    finally:
        conn.close()
    pub = CSRFClient(app)
    r = pub.post(f"/pay/{token}/discount", data={"code": "WRONG"})
    assert r.status_code == 400 and "alert error" in r.text and "valid" in r.text
    conn = connect(app.state.settings.db_path)
    try:
        assert get_invoice(conn, tid, iid)["amount_cents"] == 10000   # untouched
    finally:
        conn.close()


def test_receipt_itemizes_discount(client, app):
    """A paid, discounted invoice's printable receipt shows the code, original subtotal,
    and the amount saved — not just the reduced total."""
    creds = onboard_studio(client, email="disc_receipt@example.com")
    login_owner(client, creds)
    client.post("/settings/discounts", data={"code": "SAVE20", "kind": "percent", "value": "20"})
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        inv = create_invoice(conn, app.state.settings, tenant_id=tid, title="Session",
                             amount_cents=10000)
        send_invoice(conn, tid, inv["id"])
        conn.commit()
        token = inv["token"]
    finally:
        conn.close()
    pub = CSRFClient(app)
    pub.post(f"/pay/{token}/discount", data={"code": "SAVE20"})    # → $80.00 due
    pub.post(f"/pay/{token}/checkout")                             # mock backend settles it
    receipt = pub.get(f"/pay/{token}/receipt").text
    assert "SAVE20" in receipt and "$100.00" in receipt and "$20.00" in receipt   # code, original, saved
    assert "$80.00" in receipt                                    # the amount actually paid
