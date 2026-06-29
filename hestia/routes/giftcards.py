"""Gift-card routes (owner side) — issue and manage stored-value cards.

The public side (a client redeeming a card) lives on the pay flow in ``routes/pay.py``.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..giftcards import create_gift_card, list_gift_cards, set_gift_card_active
from ..invoices import money
from .deps import db_conn, render, settings_of

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
