"""Gift cards — studio-issued stored value a client redeems at checkout.

A gift card is POST-TAX stored value applied to an invoice's grand total as a payment, not
a pre-tax discount. So :func:`apply_card_to_invoice` never touches ``amount_cents`` /
``tax_cents`` (that would corrupt revenue + sales tax); it draws the card's balance down,
records the draw in the ``gift_card_redemptions`` ledger, and adds to
``invoices.gift_credit_cents`` — the amount the provider charges then becomes
``total − gift_credit`` (see :func:`hestia.invoices._hydrate`'s ``amount_due_cents``).

The apply runs under the caller's ``BEGIN IMMEDIATE`` (same lock the discount apply uses)
so concurrent redemptions serialize; the balance draw is an atomic guarded UPDATE and the
ledger has a UNIQUE(gift_card_id, invoice_id), so a card can't be over-redeemed or applied
twice to one invoice. :func:`release_for_invoice` reverses the draw (idempotently) when an
invoice is voided, returning the value to the card.
"""

from __future__ import annotations

import datetime
import sqlite3

from .crypto import new_session_token
from .db import audit


class GiftCardError(Exception):
    """Raised on a post-claim apply anomaly so the caller's transaction rolls back
    (undoing the balance draw + ledger row) rather than committing a half-applied card."""


def normalize_code(code: str) -> str:
    return (code or "").strip().upper()[:40]


def _generate_code() -> str:
    """A bearer code for an issued card — unguessable (a gift card is real money), unlike a
    typed vanity discount code. 12 alphanumeric upper-case chars."""
    return "".join(c for c in new_session_token() if c.isalnum())[:12].upper()


# ── owner CRUD ────────────────────────────────────────────────────────────────


def create_gift_card(conn: sqlite3.Connection, *, tenant_id: str, initial_cents: int,
                     code: str = "", currency: str = "usd", expires_on: str = "",
                     note: str = "") -> dict | None:
    """Issue a card with a starting balance. Returns None for a non-positive amount or a
    duplicate code. A blank code is auto-generated (the bearer code to share)."""
    initial = max(0, int(initial_cents or 0))
    if initial <= 0:
        return None
    code = normalize_code(code) or _generate_code()
    try:
        cur = conn.execute(
            "INSERT INTO gift_cards (tenant_id, code, initial_cents, balance_cents, currency, "
            "expires_on, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, code, initial, initial, (currency or "usd").lower(),
             (expires_on or "").strip(), (note or "").strip()[:200]),
        )
    except sqlite3.IntegrityError:                      # code already exists for this tenant
        return None
    return get_gift_card(conn, tenant_id, cur.lastrowid)


def get_gift_card(conn: sqlite3.Connection, tenant_id: str, card_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM gift_cards WHERE id = ? AND tenant_id = ?", (card_id, tenant_id)
    ).fetchone()
    return dict(row) if row else None


def list_gift_cards(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM gift_cards WHERE tenant_id = ? ORDER BY active DESC, created_at DESC, id DESC",
        (tenant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_gift_card_active(conn: sqlite3.Connection, tenant_id: str, card_id: int,
                         active: bool) -> None:
    conn.execute(
        "UPDATE gift_cards SET active = ? WHERE id = ? AND tenant_id = ?",
        (1 if active else 0, card_id, tenant_id),
    )


# ── redemption (money path) ───────────────────────────────────────────────────


def apply_card_to_invoice(conn: sqlite3.Connection, *, invoice_token: str, code: str) -> dict:
    """Redeem a gift card against the unpaid invoice with this public token. Draws
    ``min(balance, amount still due)`` off the card and credits the invoice, in one
    transaction. Returns ``{"ok": True, "draw_cents", "code"}`` or ``{"ok": False, "error"}``;
    all not-ok branches happen before any write. A post-claim anomaly raises
    :class:`GiftCardError` so the caller rolls back. Run under the caller's ``BEGIN IMMEDIATE``."""
    code = normalize_code(code)
    if not code:
        return {"ok": False, "error": "Enter a gift card code."}
    inv = conn.execute(
        "SELECT id, tenant_id, currency, amount_cents, tax_cents, status, "
        "       COALESCE(gift_credit_cents, 0) AS gift_credit FROM invoices WHERE token = ?",
        (invoice_token,),
    ).fetchone()
    if not inv:
        return {"ok": False, "error": "We couldn't find that invoice."}
    if inv["status"] in ("paid", "void"):
        return {"ok": False, "error": "This invoice can no longer take a gift card."}
    remaining = (inv["amount_cents"] + int(inv["tax_cents"] or 0)) - int(inv["gift_credit"] or 0)
    if remaining <= 0:
        return {"ok": False, "error": "This invoice is already fully covered."}
    today = datetime.date.today().isoformat()
    card = conn.execute(
        "SELECT id, balance_cents, currency, active, expires_on FROM gift_cards "
        "WHERE tenant_id = ? AND code = ?",
        (inv["tenant_id"], code),
    ).fetchone()
    if (not card or not card["active"] or int(card["balance_cents"]) <= 0
            or (card["expires_on"] and card["expires_on"] < today)):
        return {"ok": False, "error": "That gift card isn't valid."}
    if (card["currency"] or "").lower() != (inv["currency"] or "").lower():
        return {"ok": False, "error": "That gift card is in a different currency."}
    if conn.execute(
        "SELECT 1 FROM gift_card_redemptions WHERE gift_card_id = ? AND invoice_id = ? "
        "AND status = 'applied'", (card["id"], inv["id"]),
    ).fetchone():
        return {"ok": False, "error": "That card is already applied to this invoice."}
    draw = min(int(card["balance_cents"]), remaining)
    if draw <= 0:
        return {"ok": False, "error": "That gift card has no balance left."}

    # atomic balance draw (mirrors the discount used_count claim) — guarded so a stale read
    # can't overdraw; the caller's write lock makes the checks above authoritative.
    drawn = conn.execute(
        "UPDATE gift_cards SET balance_cents = balance_cents - ? "
        "WHERE id = ? AND active = 1 AND balance_cents >= ? AND (expires_on = '' OR expires_on >= ?)",
        (draw, card["id"], draw, today),
    )
    try:
        conn.execute(
            "INSERT INTO gift_card_redemptions (tenant_id, gift_card_id, invoice_id, amount_cents, "
            "status) VALUES (?, ?, ?, ?, 'applied')",
            (inv["tenant_id"], card["id"], inv["id"], draw),
        )
    except sqlite3.IntegrityError:                      # raced to the same card+invoice
        raise GiftCardError("duplicate redemption") from None
    bumped = conn.execute(
        "UPDATE invoices SET gift_credit_cents = COALESCE(gift_credit_cents, 0) + ? "
        "WHERE token = ? AND status NOT IN ('paid', 'void')",
        (draw, invoice_token),
    )
    if drawn.rowcount != 1 or bumped.rowcount != 1:    # lost a race we thought we'd won
        raise GiftCardError("apply failed")
    audit(conn, actor="public", action="giftcard.redeemed", tenant_id=inv["tenant_id"],
          detail=f"{code} · {draw}")
    return {"ok": True, "draw_cents": draw, "code": code}


def release_for_invoice(conn: sqlite3.Connection, tenant_id: str, invoice_id: int) -> None:
    """Return any gift credit drawn against an invoice back to the card(s) — called inside
    the void/refund transaction. Idempotent: each redemption is flipped to 'reversed' under
    a rowcount-gated UPDATE, so a double call can't restore a balance twice."""
    rows = conn.execute(
        "SELECT id, gift_card_id, amount_cents FROM gift_card_redemptions "
        "WHERE invoice_id = ? AND tenant_id = ? AND status = 'applied'",
        (invoice_id, tenant_id),
    ).fetchall()
    for r in rows:
        flipped = conn.execute(
            "UPDATE gift_card_redemptions SET status = 'reversed' WHERE id = ? AND status = 'applied'",
            (r["id"],),
        )
        if flipped.rowcount == 1:                       # claim the reversal exactly once
            conn.execute(
                "UPDATE gift_cards SET balance_cents = balance_cents + ? WHERE id = ?",
                (int(r["amount_cents"]), r["gift_card_id"]),
            )
    conn.execute(
        "UPDATE invoices SET gift_credit_cents = 0 WHERE id = ? AND tenant_id = ?",
        (invoice_id, tenant_id),
    )
