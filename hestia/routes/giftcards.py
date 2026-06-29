"""Gift-card routes (owner side) — issue and manage stored-value cards.

The public side (a client redeeming a card) lives on the pay flow in ``routes/pay.py``.
"""

from __future__ import annotations

import datetime
import math

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..giftcards import (
    create_gift_card,
    create_purchase,
    find_card_by_code,
    list_gift_cards,
    set_gift_card_active,
)
from ..invoices import create_invoice, money
from ..ratelimit import enforce
from ..studio import get_profile
from ..tenants import get_tenant_by_slug
from .deps import db_conn, render, settings_of

_MAX_GIFT_CENTS = 1_000_000   # $10k cap on a single gift-card purchase

router = APIRouter()


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


def _to_cents(raw: str) -> int:
    try:
        cents = float(raw.replace("$", "").replace(",", "").strip()) * 100
        return int(round(cents)) if math.isfinite(cents) else 0
    except (ValueError, AttributeError, OverflowError):
        return 0


@router.get("/settings/giftcards")
def giftcards_list(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        currency = settings_of(request).currency
        cards = list_gift_cards(conn, auth.tenant["id"])
        for c in cards:
            c["balance_display"] = money(c["balance_cents"], c.get("currency") or currency)
            c["initial_display"] = money(c["initial_cents"], c.get("currency") or currency)
    return render(request, "studio/giftcards.html", auth=auth, cards=cards)


@router.post("/settings/giftcards")
def giftcard_create(request: Request, amount: str = Form("0"), code: str = Form(""),
                    expires_on: str = Form(""), note: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        create_gift_card(conn, tenant_id=auth.tenant["id"], initial_cents=_to_cents(amount),
                         code=code, currency=settings_of(request).currency,
                         expires_on=expires_on, note=note)
    return RedirectResponse("/settings/giftcards", status_code=303)


@router.post("/settings/giftcards/{card_id}/toggle")
def giftcard_toggle(request: Request, card_id: int, active: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_gift_card_active(conn, auth.tenant["id"], card_id, bool(active.strip()))
    return RedirectResponse("/settings/giftcards", status_code=303)


# ── Public: buy a gift card ───────────────────────────────────────────────────


def _published_tenant(conn, slug: str):
    tenant = get_tenant_by_slug(conn, slug)
    if not tenant:
        return None, None
    profile = get_profile(conn, tenant["id"])
    return (tenant, profile) if profile["published"] else (None, None)


@router.get("/studio/{slug}/gift")
def public_gift_page(request: Request, slug: str):
    with db_conn(request) as conn:
        tenant, profile = _published_tenant(conn, slug)
        if not tenant:
            t = get_tenant_by_slug(conn, slug)
            if t:
                return render(request, "studio/coming_soon.html", auth=None, tenant=t)
            return render(request, "offer_missing.html", auth=None, status_code=404)
    return render(request, "studio/gift_buy.html", auth=None, tenant=tenant, profile=profile)


@router.post("/studio/{slug}/gift")
def public_gift_buy(request: Request, slug: str, amount: str = Form(""),
                    recipient_name: str = Form(""), recipient_email: str = Form(""),
                    buyer_name: str = Form(""), buyer_email: str = Form(""), message: str = Form("")):
    """A visitor buys a gift card: create an invoice + a pending purchase, then send them to
    pay. The card is issued (and emailed to the recipient) only once the invoice is paid."""
    enforce(request, "inquiry")
    settings = settings_of(request)
    with db_conn(request) as conn:
        tenant, profile = _published_tenant(conn, slug)
        if not tenant:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        cents = _to_cents(amount)
        if cents <= 0 or cents > _MAX_GIFT_CENTS or not (buyer_email or "").strip():
            err = ("Please enter an amount up to "
                   f"{money(_MAX_GIFT_CENTS, settings.currency)}." if cents <= 0 or cents > _MAX_GIFT_CENTS
                   else "Please enter your email so we can send a receipt.")
            return render(request, "studio/gift_buy.html", auth=None, tenant=tenant,
                          profile=profile, error=err, status_code=400)
        inv = create_invoice(conn, settings, tenant_id=tenant["id"],
                             title=f"Gift card — {money(cents, settings.currency)}", amount_cents=cents,
                             note=(f"Gift card for {recipient_name.strip()}" if recipient_name.strip()
                                   else "Gift card purchase"))
        create_purchase(conn, tenant_id=tenant["id"], invoice_id=inv["id"], amount_cents=cents,
                        recipient_name=recipient_name, recipient_email=recipient_email,
                        buyer_name=buyer_name, buyer_email=buyer_email, message=message)
        conn.commit()
        token = inv["token"]
    return RedirectResponse(f"/pay/{token}", status_code=303)


def _balance_result(card: dict | None, currency: str) -> dict:
    """A display-ready balance lookup result for the public check."""
    if not card:
        return {"found": False, "message": "We couldn't find a gift card with that code."}
    today = datetime.date.today().isoformat()
    bal = int(card["balance_cents"])
    if not card["active"]:
        msg = "This gift card is no longer active."
    elif card["expires_on"] and card["expires_on"] < today:
        msg = f"This gift card expired on {card['expires_on']}."
    elif bal <= 0:
        msg = "This gift card has been fully used."
    else:
        msg = (f"Balance: {money(bal, card.get('currency') or currency)}"
               + (f" · expires {card['expires_on']}" if card["expires_on"] else ""))
    return {"found": True, "message": msg}


@router.get("/studio/{slug}/gift/balance")
def public_gift_balance_page(request: Request, slug: str):
    with db_conn(request) as conn:
        tenant, _ = _published_tenant(conn, slug)
        if not tenant:
            t = get_tenant_by_slug(conn, slug)
            if t:
                return render(request, "studio/coming_soon.html", auth=None, tenant=t)
            return render(request, "offer_missing.html", auth=None, status_code=404)
    return render(request, "studio/gift_balance.html", auth=None, tenant=tenant, result=None)


@router.post("/studio/{slug}/gift/balance")
def public_gift_balance_check(request: Request, slug: str, code: str = Form("")):
    enforce(request, "inquiry")                        # rate-limit to deter code enumeration
    with db_conn(request) as conn:
        tenant, _ = _published_tenant(conn, slug)
        if not tenant:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        card = find_card_by_code(conn, tenant["id"], code)
        result = _balance_result(card, settings_of(request).currency)
    return render(request, "studio/gift_balance.html", auth=None, tenant=tenant, result=result)
