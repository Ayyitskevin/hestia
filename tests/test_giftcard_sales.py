"""Selling gift cards online — purchase → pay → card issued (on every settle path),
idempotent fulfillment, recipient delivery email, and the public buy flow + gating."""

from conftest import CSRFClient, login_owner, onboard_studio

from hestia.db import connect
from hestia.discounts import apply_code_to_invoice, create_discount
from hestia.giftcards import (
    apply_card_to_invoice,
    create_gift_card,
    create_purchase,
    fulfill_purchase,
    list_gift_cards,
)
from hestia.invoices import create_invoice, get_invoice, mark_paid
from hestia.jobs import HANDLERS
from hestia.tenants import create_tenant, slugify


def _tenant(conn, name="Sales Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def _tid_of(conn, email):
    return conn.execute(
        "SELECT t.id FROM tenants t JOIN users u ON u.tenant_id = t.id WHERE u.email = ?",
        (email,),
    ).fetchone()["id"]


def _publish(client):
    client.post("/settings/site", data={"headline": "x", "about": "y", "contact_email": "",
                                        "published": "1"})


def _deliver_jobs(conn, tenant_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE kind = 'giftcard.deliver' AND tenant_id = ?",
        (tenant_id,),
    ).fetchone()["n"]


# ── module: fulfillment ─────────────────────────────────────────────────────────


def test_fulfill_issues_card_idempotently(conn, settings):
    t = _tenant(conn)
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="Gift card", amount_cents=10000)
    create_purchase(conn, tenant_id=t["id"], invoice_id=inv["id"], amount_cents=10000,
                    recipient_name="Dana", recipient_email="dana@example.com", buyer_name="Sam")
    conn.commit()
    fulfill_purchase(conn, t["id"], inv["id"])
    cards = list_gift_cards(conn, t["id"])
    assert len(cards) == 1 and cards[0]["balance_cents"] == 10000      # card issued for the amount
    assert _deliver_jobs(conn, t["id"]) == 1                           # recipient email queued
    pur = conn.execute("SELECT status, gift_card_id FROM gift_card_purchases WHERE invoice_id = ?",
                       (inv["id"],)).fetchone()
    assert pur["status"] == "fulfilled" and pur["gift_card_id"] == cards[0]["id"]
    # idempotent: a second fulfill issues nothing more
    fulfill_purchase(conn, t["id"], inv["id"])
    assert len(list_gift_cards(conn, t["id"])) == 1 and _deliver_jobs(conn, t["id"]) == 1


def test_gift_purchase_invoice_rejects_codes_and_cards(conn, settings):
    """You buy stored value with real money: a gift-card purchase invoice must reject both a
    discount (would sell a card below face) and a gift-card redemption (would shuffle value)."""
    t = _tenant(conn)
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="Gift card", amount_cents=10000)
    create_purchase(conn, tenant_id=t["id"], invoice_id=inv["id"], amount_cents=10000,
                    buyer_email="s@example.com")
    create_discount(conn, tenant_id=t["id"], code="SAVE", kind="percent", value=20)
    create_gift_card(conn, tenant_id=t["id"], initial_cents=5000, code="PAY")
    conn.commit()
    assert apply_code_to_invoice(conn, invoice_token=inv["token"], code="SAVE")["ok"] is False
    assert apply_card_to_invoice(conn, invoice_token=inv["token"], code="PAY")["ok"] is False
    fresh = get_invoice(conn, t["id"], inv["id"])
    assert fresh["amount_cents"] == 10000 and fresh["gift_credit_cents"] == 0   # untouched


def test_mark_paid_issues_purchased_card(conn, settings):
    t = _tenant(conn)
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="Gift card", amount_cents=5000)
    create_purchase(conn, tenant_id=t["id"], invoice_id=inv["id"], amount_cents=5000,
                    buyer_name="Sam", buyer_email="sam@example.com")
    conn.commit()
    assert mark_paid(conn, token=inv["token"], provider="mock", ref="x") is True
    assert list_gift_cards(conn, t["id"])[0]["balance_cents"] == 5000   # issued by the mark_paid hook


def test_deliver_emails_recipient(conn, settings):
    t = _tenant(conn)
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="Gift card", amount_cents=7500)
    p = create_purchase(conn, tenant_id=t["id"], invoice_id=inv["id"], amount_cents=7500,
                        recipient_name="Dana", recipient_email="dana@example.com", buyer_name="Sam")
    fulfill_purchase(conn, t["id"], inv["id"])
    conn.commit()
    code = list_gift_cards(conn, t["id"])[0]["code"]
    HANDLERS["giftcard.deliver"](settings, {"purchase_id": p["id"]})    # run the queued handler
    row = conn.execute("SELECT subject, body FROM emails WHERE to_addr = 'dana@example.com'").fetchone()
    assert row and code in row["body"] and "gift card" in row["subject"].lower()


# ── HTTP: the public buy flow ───────────────────────────────────────────────────


def test_public_buy_then_pay_issues_card(client, app):
    creds = onboard_studio(client, name="Gift Shop", email="sales@example.com")
    login_owner(client, creds)
    slug = slugify("Gift Shop")
    _publish(client)
    assert "Give a gift card" in client.get(f"/studio/{slug}").text     # cross-linked on the site
    assert "Amount" in client.get(f"/studio/{slug}/gift").text

    pub = CSRFClient(app)
    r = pub.post(f"/studio/{slug}/gift",
                 data={"amount": "100.00", "recipient_name": "Dana",
                       "recipient_email": "dana@example.com", "buyer_name": "Sam",
                       "buyer_email": "sam@example.com", "message": "Enjoy!"},
                 follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/pay/")   # sent to pay
    token = r.headers["location"].split("/pay/")[1]
    pub.post(f"/pay/{token}/checkout")                                  # mock settles it
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        cards = list_gift_cards(conn, tid)
        assert len(cards) == 1 and cards[0]["balance_cents"] == 10000   # $100 card issued on payment
        assert _deliver_jobs(conn, tid) == 1                           # recipient delivery queued
    finally:
        conn.close()


def test_public_buy_gated_and_validated(client, app):
    creds = onboard_studio(client, name="Hidden Shop", email="hidden@example.com")
    login_owner(client, creds)
    slug = slugify("Hidden Shop")
    # unpublished → coming soon, can't buy
    assert "coming soon" in client.get(f"/studio/{slug}/gift").text.lower()
    _publish(client)
    pub = CSRFClient(app)
    assert pub.post(f"/studio/{slug}/gift",
                    data={"amount": "0", "buyer_email": "x@example.com"}).status_code == 400
    assert pub.post(f"/studio/{slug}/gift",
                    data={"amount": "50", "buyer_email": ""}).status_code == 400   # email required
    conn = connect(app.state.settings.db_path)
    try:
        assert list_gift_cards(conn, _tid_of(conn, creds["email"])) == []   # nothing issued
    finally:
        conn.close()
