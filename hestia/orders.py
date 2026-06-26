"""Orders — a client reserving a bundle from the offer, settled via the pay flow.

Ordering reuses the money spine: an order creates a regular invoice (its own
idempotent /pay link), and when that invoice is paid the order flips to paid and
a fulfillment job is enqueued. Prices are recomputed server-side from the offer's
stored bundles (or the live favorites package) with any active sale applied — the
client's posted price is never trusted. Tenant-scoped throughout.
"""

from __future__ import annotations

import sqlite3

from .campaigns import apply_discount, get_active_campaign
from .config import Settings
from .db import audit
from .invoices import create_invoice, get_invoice_by_token, money, tax_for
from .jobs import enqueue
from .proofing import favorite_count
from .sales import favorites_package


def _resolve_bundle(conn: sqlite3.Connection, offer: dict, sku: str) -> dict | None:
    """The orderable item for ``sku`` — a stored offer bundle, or the live
    favorites package (whose price depends on the current favorite count)."""
    if sku == "favorites":
        return favorites_package(favorite_count(conn, offer["gallery_id"]))
    return next((b for b in offer.get("bundles", []) if b.get("sku") == sku), None)


def _client_project_for_gallery(conn: sqlite3.Connection, tenant_id: str, gallery_id: int):
    row = conn.execute(
        "SELECT p.id AS project_id, p.client_id AS client_id FROM galleries g "
        "LEFT JOIN projects p ON p.id = g.project_id AND p.tenant_id = g.tenant_id "
        "WHERE g.id = ? AND g.tenant_id = ?",
        (gallery_id, tenant_id),
    ).fetchone()
    if not row:
        return None, None
    return row["client_id"], row["project_id"]


def create_order(conn: sqlite3.Connection, settings: Settings, *, tenant: dict, offer: dict,
                 sku: str) -> dict | None:
    """Create an order + its invoice. Returns ``{order, invoice}`` or None for an
    unknown/unavailable sku."""
    bundle = _resolve_bundle(conn, offer, sku)
    if not bundle:
        return None
    amount = bundle["price_cents"]
    campaign = get_active_campaign(conn, offer["gallery_id"])
    if campaign and campaign["discount_pct"]:
        amount = apply_discount(amount, campaign["discount_pct"])
    client_id, project_id = _client_project_for_gallery(conn, tenant["id"], offer["gallery_id"])
    # print sales are taxable goods — add the studio's sales tax on top of the price
    tax = tax_for(amount, tenant.get("tax_rate_bps") or 0)
    invoice = create_invoice(conn, settings, tenant_id=tenant["id"], title=bundle["name"],
                             amount_cents=amount, client_id=client_id, project_id=project_id,
                             tax_cents=tax)
    cur = conn.execute(
        "INSERT INTO orders (tenant_id, offer_id, gallery_id, invoice_id, sku, name, "
        "amount_cents, currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tenant["id"], offer["id"], offer["gallery_id"], invoice["id"], sku, bundle["name"],
         amount, settings.currency),
    )
    audit(conn, actor="client", action="order.created", tenant_id=tenant["id"],
          detail=f"{bundle['name']} · {money(amount, settings.currency)}")
    return {"order": get_order(conn, tenant["id"], cur.lastrowid), "invoice": invoice}


def _hydrate(row: dict) -> dict:
    row["amount_display"] = money(row["amount_cents"], row.get("currency", "usd"))
    return row


def get_order(conn: sqlite3.Connection, tenant_id: str, order_id: int) -> dict | None:
    row = conn.execute(
        "SELECT o.*, i.token AS invoice_token, i.status AS invoice_status "
        "FROM orders o LEFT JOIN invoices i ON i.id = o.invoice_id AND i.tenant_id = o.tenant_id "
        "WHERE o.id = ? AND o.tenant_id = ?",
        (order_id, tenant_id),
    ).fetchone()
    return _hydrate(dict(row)) if row else None


def list_orders(conn: sqlite3.Connection, tenant_id: str, *, gallery_id: int | None = None) -> list[dict]:
    sql = ("SELECT o.*, i.token AS invoice_token, i.status AS invoice_status "
           "FROM orders o LEFT JOIN invoices i ON i.id = o.invoice_id AND i.tenant_id = o.tenant_id "
           "WHERE o.tenant_id = ?")
    params: list = [tenant_id]
    if gallery_id is not None:
        sql += " AND o.gallery_id = ?"
        params.append(gallery_id)
    sql += " ORDER BY o.created_at DESC"
    return [_hydrate(dict(r)) for r in conn.execute(sql, params).fetchall()]


def fulfill_for_invoice_token(conn: sqlite3.Connection, token: str) -> bool:
    """After an invoice is paid: if it backs an order, mark the order paid and
    enqueue fulfillment. Idempotent — only the pending→paid transition enqueues,
    so a duplicate payment callback never double-submits to the lab."""
    inv = get_invoice_by_token(conn, token)
    if not inv:
        return False
    cur = conn.execute(
        "UPDATE orders SET status = 'paid' WHERE invoice_id = ? AND status = 'pending'",
        (inv["id"],),
    )
    if cur.rowcount == 0:
        return False
    row = conn.execute(
        "SELECT id, tenant_id FROM orders WHERE invoice_id = ?", (inv["id"],)
    ).fetchone()
    enqueue(conn, kind="fulfillment.submit", tenant_id=row["tenant_id"],
            payload={"order_id": row["id"]})
    audit(conn, actor="system", action="order.paid", tenant_id=row["tenant_id"],
          detail=f"order #{row['id']}")
    return True
