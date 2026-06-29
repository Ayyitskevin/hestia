"""Gift cards — issuance, the redemption money path (partial/equal/over, the tax+revenue
invariant, guards, multi-card, over-draw), void release, and the public pay-page flow."""

from conftest import CSRFClient, login_owner, onboard_studio

from hestia.db import connect
from hestia.discounts import apply_code_to_invoice, create_discount
from hestia.giftcards import (
    apply_card_to_invoice,
    create_gift_card,
    get_gift_card,
    list_gift_cards,
    release_for_invoice,
)
from hestia.invoices import (
    create_invoice,
    get_invoice,
    get_invoice_by_token,
    send_invoice,
    void_invoice,
)
from hestia.tenants import create_tenant


def _tenant(conn, name="Gift Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def _tid_of(conn, email):
    return conn.execute(
        "SELECT t.id FROM tenants t JOIN users u ON u.tenant_id = t.id WHERE u.email = ?",
        (email,),
    ).fetchone()["id"]


def _invoice(conn, settings, tenant_id, *, amount=10000, tax=0):
    return create_invoice(conn, settings, tenant_id=tenant_id, title="Session",
                          amount_cents=amount, tax_cents=tax)


def _card(conn, tenant_id, **kw):
    c = create_gift_card(conn, tenant_id=tenant_id, **kw)
    return c


# ── issuance ──────────────────────────────────────────────────────────────────


def test_create_validates_and_autogenerates(conn):
    t = _tenant(conn)
    a = _card(conn, t["id"], initial_cents=10000)
    assert a and a["code"] and a["balance_cents"] == 10000 and a["initial_cents"] == 10000
    b = _card(conn, t["id"], initial_cents=5000, code="welcome")
    assert b["code"] == "WELCOME"                                   # normalized upper
    assert _card(conn, t["id"], initial_cents=0) is None            # non-positive rejected
    assert _card(conn, t["id"], initial_cents=5000, code="welcome") is None  # dup code


def test_cards_tenant_scoped(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    _card(conn, t1["id"], initial_cents=5000, code="X")
    assert _card(conn, t2["id"], initial_cents=5000, code="X")      # same code free in another tenant
    assert len(list_gift_cards(conn, t1["id"])) == 1


# ── redemption math + the tax/revenue invariant ────────────────────────────────


def test_apply_partial_draw(conn, settings):
    t = _tenant(conn)
    inv = _invoice(conn, settings, t["id"], amount=10000)
    c = _card(conn, t["id"], initial_cents=3000, code="GC")
    conn.commit()
    res = apply_card_to_invoice(conn, invoice_token=inv["token"], code="gc")
    assert res["ok"] and res["draw_cents"] == 3000
    fresh = get_invoice(conn, t["id"], inv["id"])
    assert fresh["gift_credit_cents"] == 3000
    assert get_gift_card(conn, t["id"], c["id"])["balance_cents"] == 0     # fully drawn
    hydrated = get_invoice_by_token(conn, inv["token"])
    assert hydrated["amount_due_cents"] == 7000                            # total − credit


def test_apply_preserves_tax_and_revenue(conn, settings):
    """The central invariant: a gift card NEVER changes amount_cents/tax_cents/total_cents —
    only the cash due. So revenue + sales tax stay exactly right."""
    t = _tenant(conn)
    inv = _invoice(conn, settings, t["id"], amount=10000, tax=850)
    _card(conn, t["id"], initial_cents=5000, code="HALF")
    conn.commit()
    before = get_invoice(conn, t["id"], inv["id"])
    apply_card_to_invoice(conn, invoice_token=inv["token"], code="HALF")
    after = get_invoice(conn, t["id"], inv["id"])
    assert after["amount_cents"] == before["amount_cents"] == 10000        # subtotal untouched
    assert after["tax_cents"] == before["tax_cents"] == 850                # tax untouched
    hydrated = get_invoice_by_token(conn, inv["token"])
    assert hydrated["total_cents"] == 10850 and hydrated["amount_due_cents"] == 5850


def test_apply_over_balance_keeps_leftover(conn, settings):
    t = _tenant(conn)
    inv = _invoice(conn, settings, t["id"], amount=4000)
    c = _card(conn, t["id"], initial_cents=10000, code="BIG")
    conn.commit()
    res = apply_card_to_invoice(conn, invoice_token=inv["token"], code="BIG")
    assert res["draw_cents"] == 4000                                       # only what's due
    assert get_gift_card(conn, t["id"], c["id"])["balance_cents"] == 6000  # leftover retained
    assert get_invoice_by_token(conn, inv["token"])["amount_due_cents"] == 0


def test_apply_guards(conn, settings):
    t = _tenant(conn)
    inv = _invoice(conn, settings, t["id"], amount=10000)
    conn.commit()
    assert apply_card_to_invoice(conn, invoice_token=inv["token"], code="NOPE")["ok"] is False
    c = _card(conn, t["id"], initial_cents=5000, code="OFF")
    conn.execute("UPDATE gift_cards SET active = 0 WHERE id = ?", (c["id"],))
    assert apply_card_to_invoice(conn, invoice_token=inv["token"], code="OFF")["ok"] is False
    _card(conn, t["id"], initial_cents=5000, code="OLD", expires_on="2000-01-01")
    assert apply_card_to_invoice(conn, invoice_token=inv["token"], code="OLD")["ok"] is False
    assert get_invoice(conn, t["id"], inv["id"])["gift_credit_cents"] == 0  # nothing applied


def test_apply_same_card_once_per_invoice(conn, settings):
    t = _tenant(conn)
    inv = _invoice(conn, settings, t["id"], amount=10000)
    _card(conn, t["id"], initial_cents=2000, code="GC")
    conn.commit()
    assert apply_card_to_invoice(conn, invoice_token=inv["token"], code="GC")["ok"]
    # the 2000 card is now spent; re-applying can't draw again or double the credit
    again = apply_card_to_invoice(conn, invoice_token=inv["token"], code="GC")
    assert again["ok"] is False
    assert get_invoice(conn, t["id"], inv["id"])["gift_credit_cents"] == 2000   # not doubled


def test_redeem_currency_match_is_case_insensitive(conn, settings):
    """Invoices store the currency verbatim from config (e.g. 'USD'); cards normalize to
    lower-case. Redemption must match case-insensitively or it would reject every card."""
    t = _tenant(conn)
    inv = _invoice(conn, settings, t["id"], amount=10000)
    _card(conn, t["id"], initial_cents=5000, code="UC")          # stored as 'usd'
    conn.execute("UPDATE invoices SET currency = 'USD' WHERE id = ?", (inv["id"],))  # uppercase deploy
    conn.commit()
    res = apply_card_to_invoice(conn, invoice_token=inv["token"], code="UC")
    assert res["ok"] and res["draw_cents"] == 5000              # matched despite case


def test_multiple_cards_stack(conn, settings):
    t = _tenant(conn)
    inv = _invoice(conn, settings, t["id"], amount=10000)
    _card(conn, t["id"], initial_cents=3000, code="A")
    _card(conn, t["id"], initial_cents=4000, code="B")
    conn.commit()
    apply_card_to_invoice(conn, invoice_token=inv["token"], code="A")
    apply_card_to_invoice(conn, invoice_token=inv["token"], code="B")
    assert get_invoice(conn, t["id"], inv["id"])["gift_credit_cents"] == 7000
    assert get_invoice_by_token(conn, inv["token"])["amount_due_cents"] == 3000


def test_one_card_across_invoices_cannot_overdraw(conn, settings):
    t = _tenant(conn)
    c = _card(conn, t["id"], initial_cents=10000, code="WALLET")
    i1 = _invoice(conn, settings, t["id"], amount=6000)
    i2 = _invoice(conn, settings, t["id"], amount=6000)
    i3 = _invoice(conn, settings, t["id"], amount=6000)
    conn.commit()
    assert apply_card_to_invoice(conn, invoice_token=i1["token"], code="WALLET")["draw_cents"] == 6000
    assert apply_card_to_invoice(conn, invoice_token=i2["token"], code="WALLET")["draw_cents"] == 4000
    assert get_gift_card(conn, t["id"], c["id"])["balance_cents"] == 0
    assert apply_card_to_invoice(conn, invoice_token=i3["token"], code="WALLET")["ok"] is False  # empty


def test_void_releases_credit_back_to_card(conn, settings):
    t = _tenant(conn)
    inv = _invoice(conn, settings, t["id"], amount=10000)
    c = _card(conn, t["id"], initial_cents=4000, code="GC")
    conn.commit()
    apply_card_to_invoice(conn, invoice_token=inv["token"], code="GC")
    assert get_gift_card(conn, t["id"], c["id"])["balance_cents"] == 0
    void_invoice(conn, t["id"], inv["id"])
    assert get_gift_card(conn, t["id"], c["id"])["balance_cents"] == 4000        # restored
    assert get_invoice(conn, t["id"], inv["id"])["gift_credit_cents"] == 0
    # idempotent: a second release doesn't double-restore
    release_for_invoice(conn, t["id"], inv["id"])
    assert get_gift_card(conn, t["id"], c["id"])["balance_cents"] == 4000


def test_gift_card_locks_out_discount(conn, settings):
    t = _tenant(conn)
    inv = _invoice(conn, settings, t["id"], amount=10000)
    _card(conn, t["id"], initial_cents=2000, code="GC")
    create_discount(conn, tenant_id=t["id"], code="SAVE", kind="percent", value=10)
    conn.commit()
    apply_card_to_invoice(conn, invoice_token=inv["token"], code="GC")
    blocked = apply_code_to_invoice(conn, invoice_token=inv["token"], code="SAVE")
    assert blocked["ok"] is False and "gift card" in blocked["error"].lower()


# ── HTTP ──────────────────────────────────────────────────────────────────────


def test_owner_crud_http(client, app):
    creds = onboard_studio(client, email="gc_owner@example.com")
    login_owner(client, creds)
    client.post("/settings/giftcards", data={"amount": "100.00", "code": "HOLIDAY", "note": "Promo"})
    page = client.get("/settings/giftcards").text
    assert "HOLIDAY" in page and "$100.00" in page
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        cid = list_gift_cards(conn, tid)[0]["id"]
        assert list_gift_cards(conn, tid)[0]["balance_cents"] == 10000
    finally:
        conn.close()
    client.post(f"/settings/giftcards/{cid}/toggle")
    conn = connect(app.state.settings.db_path)
    try:
        assert get_gift_card(conn, tid, cid)["active"] == 0
    finally:
        conn.close()


def test_public_partial_redeem_then_pay(client, app):
    creds = onboard_studio(client, email="gc_partial@example.com")
    login_owner(client, creds)
    client.post("/settings/giftcards", data={"amount": "30.00", "code": "GC30"})
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        inv = create_invoice(conn, app.state.settings, tenant_id=tid, title="Shoot",
                             amount_cents=10000)
        send_invoice(conn, tid, inv["id"])
        conn.commit()
        token, iid = inv["token"], inv["id"]
    finally:
        conn.close()
    pub = CSRFClient(app)
    assert "Gift card code" in pub.get(f"/pay/{token}").text
    r = pub.post(f"/pay/{token}/giftcard", data={"code": "gc30"}, follow_redirects=False)
    assert r.status_code == 303
    page = pub.get(f"/pay/{token}").text
    assert "$70.00" in page and "GC30" not in page          # amount due shown; code not leaked on page
    # pay the remainder (mock settles it); revenue/total stay the full sale
    pub.post(f"/pay/{token}/checkout")
    conn = connect(app.state.settings.db_path)
    try:
        row = get_invoice(conn, tid, iid)
        assert row["status"] == "paid" and row["amount_cents"] == 10000 and row["gift_credit_cents"] == 3000
    finally:
        conn.close()


def test_public_full_cover_settles_as_giftcard(client, app):
    creds = onboard_studio(client, email="gc_full@example.com")
    login_owner(client, creds)
    client.post("/settings/giftcards", data={"amount": "100.00", "code": "FULL100"})
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        inv = create_invoice(conn, app.state.settings, tenant_id=tid, title="Mini",
                             amount_cents=10000)
        send_invoice(conn, tid, inv["id"])
        conn.commit()
        token, iid = inv["token"], inv["id"]
    finally:
        conn.close()
    pub = CSRFClient(app)
    pub.post(f"/pay/{token}/giftcard", data={"code": "FULL100"})
    pub.post(f"/pay/{token}/checkout")                      # amount due 0 → direct settle
    conn = connect(app.state.settings.db_path)
    try:
        row = get_invoice(conn, tid, iid)
        assert row["status"] == "paid" and row["provider"] == "giftcard"   # settled by card, not charged
    finally:
        conn.close()
    assert "Paid by card" in pub.get(f"/pay/{token}/receipt").text


def test_public_invalid_card_shows_error(client, app):
    creds = onboard_studio(client, email="gc_bad@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        inv = create_invoice(conn, app.state.settings, tenant_id=tid, title="X", amount_cents=10000)
        send_invoice(conn, tid, inv["id"])
        conn.commit()
        token, iid = inv["token"], inv["id"]
    finally:
        conn.close()
    pub = CSRFClient(app)
    r = pub.post(f"/pay/{token}/giftcard", data={"code": "WRONG"})
    assert r.status_code == 400 and "alert error" in r.text
    conn = connect(app.state.settings.db_path)
    try:
        assert get_invoice(conn, tid, iid)["gift_credit_cents"] == 0
    finally:
        conn.close()
