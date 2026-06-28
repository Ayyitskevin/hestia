"""Discount-code routes (owner side) — manage the studio's promo codes.

The public side (a client applying a code) lives on the pay flow in ``routes/pay.py``.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..discounts import create_discount, delete_discount, list_discounts, set_discount_active
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


def _describe(d: dict, currency: str) -> str:
    return f"{d['value']}% off" if d["kind"] == "percent" else f"{money(d['value'], currency)} off"


@router.get("/settings/discounts")
def discounts_list(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        currency = settings_of(request).currency
        codes = list_discounts(conn, auth.tenant["id"])
        for d in codes:
            d["amount_label"] = _describe(d, currency)
            d["uses_label"] = (f"{d['used_count']} / {d['max_uses']}" if d["max_uses"]
                               else f"{d['used_count']} · unlimited")
    return render(request, "studio/discounts.html", auth=auth, codes=codes)


@router.post("/settings/discounts")
def discount_create(request: Request, code: str = Form(...), kind: str = Form("percent"),
                    value: str = Form("0"), max_uses: str = Form("0"), expires_on: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        # percent → a plain integer (20 = 20%); fixed → a money amount in dollars → cents
        amount = int(value) if (kind == "percent" and value.strip().lstrip("-").isdigit()) \
            else _to_cents(value)
        uses = int(max_uses) if max_uses.strip().isdigit() else 0
        create_discount(conn, tenant_id=auth.tenant["id"], code=code, kind=kind, value=amount,
                        max_uses=uses, expires_on=expires_on)
    return RedirectResponse("/settings/discounts", status_code=303)


@router.post("/settings/discounts/{discount_id}/toggle")
def discount_toggle(request: Request, discount_id: int, active: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_discount_active(conn, auth.tenant["id"], discount_id, bool(active.strip()))
    return RedirectResponse("/settings/discounts", status_code=303)


@router.post("/settings/discounts/{discount_id}/delete")
def discount_delete(request: Request, discount_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        delete_discount(conn, auth.tenant["id"], discount_id)
    return RedirectResponse("/settings/discounts", status_code=303)
