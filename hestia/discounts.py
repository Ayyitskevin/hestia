"""Discount / promo codes — studio-created codes a client applies at checkout.

A code is percent-off or a fixed amount off, optionally usage-limited and/or expiring,
unique per studio. :func:`apply_code_to_invoice` is the money path: it claims one use of
the code and reduces the invoice's subtotal (scaling tax proportionally) in one
transaction, so the charge, receipt, A/R, and statement all follow from the now-reduced
``amount_cents``/``tax_cents`` with nothing else to recompute. The apply is meant to run
under the caller's write lock (``BEGIN IMMEDIATE``) so concurrent attempts serialize and a
code can't be over-redeemed or applied twice to one invoice.
"""

from __future__ import annotations

import datetime
import sqlite3

KINDS = ("percent", "fixed")


class DiscountError(Exception):
    """Raised on a should-never-happen apply anomaly so the caller's transaction rolls
    back (undoing a claimed use) rather than committing a half-applied discount."""


def normalize_code(code: str) -> str:
    return (code or "").strip().upper()[:40]


# ── code CRUD (owner side) ────────────────────────────────────────────────────


def create_discount(conn: sqlite3.Connection, *, tenant_id: str, code: str,
                    kind: str = "percent", value: int = 0, max_uses: int = 0,
                    expires_on: str = "") -> dict | None:
    """Create a code. Returns None for a blank/duplicate code or an out-of-range value
    (percent must be 1–100; fixed must be > 0 cents)."""
    code = normalize_code(code)
    kind = kind if kind in KINDS else "percent"
    value = int(value or 0)
    if not code:
        return None
    if kind == "percent" and not (1 <= value <= 100):
        return None
    if kind == "fixed" and value <= 0:
        return None
    try:
        cur = conn.execute(
            "INSERT INTO discount_codes (tenant_id, code, kind, value, max_uses, expires_on) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tenant_id, code, kind, value, max(0, int(max_uses or 0)), (expires_on or "").strip()),
        )
    except sqlite3.IntegrityError:                      # code already exists for this tenant
        return None
    return get_discount(conn, tenant_id, cur.lastrowid)


def get_discount(conn: sqlite3.Connection, tenant_id: str, discount_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM discount_codes WHERE id = ? AND tenant_id = ?", (discount_id, tenant_id)
    ).fetchone()
    return dict(row) if row else None


def list_discounts(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM discount_codes WHERE tenant_id = ? ORDER BY active DESC, code",
        (tenant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_discount_active(conn: sqlite3.Connection, tenant_id: str, discount_id: int,
                        active: bool) -> None:
    conn.execute(
        "UPDATE discount_codes SET active = ? WHERE id = ? AND tenant_id = ?",
        (1 if active else 0, discount_id, tenant_id),
    )


def delete_discount(conn: sqlite3.Connection, tenant_id: str, discount_id: int) -> None:
    conn.execute(
        "DELETE FROM discount_codes WHERE id = ? AND tenant_id = ?", (discount_id, tenant_id)
    )


# ── applying a code (money path) ──────────────────────────────────────────────


def discount_amount(kind: str, value: int, subtotal_cents: int) -> int:
    """Cents to take off ``subtotal_cents`` — never more than the subtotal, never negative."""
    subtotal = max(0, int(subtotal_cents))
    if kind == "percent":
        return min(subtotal, subtotal * max(0, int(value)) // 100)
    return min(subtotal, max(0, int(value)))           # fixed


def apply_code_to_invoice(conn: sqlite3.Connection, *, invoice_token: str, code: str) -> dict:
    """Apply a code to the unpaid invoice with this public token. Returns
    ``{"ok": True, "discount_cents", "code", "amount_cents"}`` on success, or
    ``{"ok": False, "error"}``. All the not-ok branches happen *before* any write, so the
    caller can commit them harmlessly; a genuine anomaly after the claim raises
    :class:`DiscountError` so the caller rolls the claim back. Run under the caller's
    ``BEGIN IMMEDIATE`` so concurrent applies serialize."""
    code = normalize_code(code)
    if not code:
        return {"ok": False, "error": "Enter a discount code."}
    inv = conn.execute(
        "SELECT id, tenant_id, amount_cents, tax_cents, status, "
        "       COALESCE(discount_code, '') AS dc, COALESCE(gift_credit_cents, 0) AS gift "
        "FROM invoices WHERE token = ?",
        (invoice_token,),
    ).fetchone()
    if not inv:
        return {"ok": False, "error": "We couldn't find that invoice."}
    if inv["status"] in ("paid", "void"):
        return {"ok": False, "error": "This invoice can no longer take a discount."}
    # A gift-card purchase issues a card at its face amount, so discounting it would sell
    # stored value below face — exclude gift-card purchases from codes.
    if conn.execute("SELECT 1 FROM gift_card_purchases WHERE invoice_id = ?", (inv["id"],)).fetchone():
        return {"ok": False, "error": "A discount can't be applied to a gift-card purchase."}
    if inv["dc"]:
        return {"ok": False, "error": "A discount has already been applied."}
    # A discount reduces the (taxable) total; applying one after a gift card was drawn would
    # change what the card already covered, so lock it out — the card came first.
    if int(inv["gift"] or 0) > 0:
        return {"ok": False, "error": "A gift card is already applied — it can't be combined with a code."}
    today = datetime.date.today().isoformat()
    row = conn.execute(
        "SELECT id, kind, value, active, max_uses, used_count, expires_on "
        "FROM discount_codes WHERE tenant_id = ? AND code = ?",
        (inv["tenant_id"], code),
    ).fetchone()
    if not row or not row["active"] or (row["expires_on"] and row["expires_on"] < today):
        return {"ok": False, "error": "That code isn't valid."}
    if row["max_uses"] and row["used_count"] >= row["max_uses"]:
        return {"ok": False, "error": "That code has reached its limit."}
    disc = discount_amount(row["kind"], row["value"], inv["amount_cents"])
    if disc <= 0:
        return {"ok": False, "error": "This invoice has nothing to discount."}

    # Both writes are guarded so a stale state can't slip a second use / second discount
    # through; the caller's write lock makes the read-checks above authoritative.
    claimed = conn.execute(
        "UPDATE discount_codes SET used_count = used_count + 1 "
        "WHERE id = ? AND active = 1 AND (expires_on = '' OR expires_on >= ?) "
        "  AND (max_uses = 0 OR used_count < max_uses)",
        (row["id"], today),
    )
    new_amount = inv["amount_cents"] - disc
    new_tax = (round(int(inv["tax_cents"] or 0) * new_amount / inv["amount_cents"])
               if inv["amount_cents"] else 0)
    applied = conn.execute(
        "UPDATE invoices SET amount_cents = ?, tax_cents = ?, discount_cents = ?, discount_code = ? "
        "WHERE token = ? AND status NOT IN ('paid', 'void') AND COALESCE(discount_code, '') = ''",
        (new_amount, new_tax, disc, code, invoice_token),
    )
    if claimed.rowcount != 1 or applied.rowcount != 1:     # lost a race we thought we'd won
        raise DiscountError("apply failed")
    return {"ok": True, "discount_cents": disc, "code": code, "amount_cents": new_amount}
