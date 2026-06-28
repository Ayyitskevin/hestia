"""Service-package routes (studio side) — the reusable 'service menu' that pre-fills quotes."""

from __future__ import annotations

import math

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..invoices import money
from ..packages import (
    create_package,
    get_package,
    list_packages,
    set_package_active,
    update_package,
)
from .deps import db_conn, render, settings_of

router = APIRouter(prefix="/packages")


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


def _to_cents(raw: str) -> int:
    try:
        # finiteness AFTER the * 100: a huge-but-finite input (1e308) overflows to inf
        # only once multiplied, which round() can't convert — floor it to 0 instead.
        cents = float(raw.replace("$", "").replace(",", "").strip()) * 100
        return int(round(cents)) if math.isfinite(cents) else 0
    except (ValueError, AttributeError, OverflowError):
        return 0


def _hydrate(packages: list[dict], currency: str) -> list[dict]:
    for p in packages:
        p["price_display"] = money(p["price_cents"], currency)
        p["deposit_display"] = money(p["deposit_cents"], currency) if p["deposit_cents"] else ""
    return packages


@router.get("")
def packages_list(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        packages = _hydrate(list_packages(conn, auth.tenant["id"]), settings_of(request).currency)
    return render(request, "packages/packages.html", auth=auth, packages=packages)


@router.post("")
def package_create(request: Request, name: str = Form(...), description: str = Form(""),
                   price: str = Form("0"), deposit: str = Form("0")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        create_package(conn, tenant_id=auth.tenant["id"], name=name, description=description,
                       price_cents=_to_cents(price), deposit_cents=_to_cents(deposit))
    return RedirectResponse("/packages", status_code=303)


@router.get("/{package_id}")
def package_edit(request: Request, package_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        pkg = get_package(conn, auth.tenant["id"], package_id)
        if not pkg:
            return RedirectResponse("/packages", status_code=303)
    return render(request, "packages/package_edit.html", auth=auth, pkg=pkg)


@router.post("/{package_id}")
def package_update(request: Request, package_id: int, name: str = Form(...),
                   description: str = Form(""), price: str = Form("0"), deposit: str = Form("0")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        update_package(conn, auth.tenant["id"], package_id, name=name, description=description,
                       price_cents=_to_cents(price), deposit_cents=_to_cents(deposit))
    return RedirectResponse("/packages", status_code=303)


@router.post("/{package_id}/archive")
def package_archive(request: Request, package_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_package_active(conn, auth.tenant["id"], package_id, False)
    return RedirectResponse("/packages", status_code=303)


@router.post("/{package_id}/restore")
def package_restore(request: Request, package_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_package_active(conn, auth.tenant["id"], package_id, True)
    return RedirectResponse("/packages", status_code=303)
