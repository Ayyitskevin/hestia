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
from .jobs import enqueue, register


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


def find_card_by_code(conn: sqlite3.Connection, tenant_id: str, code: str) -> dict | None:
    """Look up a card by its code within a studio — for the public balance check."""
    code = normalize_code(code)
    if not code:
        return None
    row = conn.execute(
        "SELECT * FROM gift_cards WHERE tenant_id = ? AND code = ?", (tenant_id, code)
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
    # You buy a gift card with real money — not with another gift card (a purchase invoice
    # issues a card at its face amount, so paying it with stored value would just shuffle it).
    if conn.execute("SELECT 1 FROM gift_card_purchases WHERE invoice_id = ?", (inv["id"],)).fetchone():
        return {"ok": False, "error": "A gift card can't be used to buy a gift card."}
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


def create_purchase(conn: sqlite3.Connection, *, tenant_id: str, invoice_id: int,
                    amount_cents: int, recipient_name: str = "", recipient_email: str = "",
                    buyer_name: str = "", buyer_email: str = "", message: str = "") -> dict | None:
    """Record a PENDING gift-card purchase tied to a (to-be-paid) invoice. The card itself is
    issued only once the invoice is paid — see :func:`fulfill_purchase`."""
    amount = max(0, int(amount_cents or 0))
    if amount <= 0:
        return None
    cur = conn.execute(
        "INSERT INTO gift_card_purchases (tenant_id, invoice_id, amount_cents, recipient_name, "
        "recipient_email, buyer_name, buyer_email, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tenant_id, invoice_id, amount, (recipient_name or "").strip()[:200],
         (recipient_email or "").strip()[:200], (buyer_name or "").strip()[:200],
         (buyer_email or "").strip()[:200], (message or "").strip()[:1000]),
    )
    row = conn.execute(
        "SELECT * FROM gift_card_purchases WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return dict(row) if row else None


def fulfill_purchase(conn: sqlite3.Connection, tenant_id: str, invoice_id: int) -> None:
    """Issue the purchased gift card once its invoice is paid — called from
    :func:`hestia.invoices.mark_paid`, so every settle path triggers it. Idempotent: the
    pending→fulfilled flip is a rowcount-gated claim, so the card is issued exactly once.
    Card issuance is DB-only here; the recipient email is enqueued for the worker."""
    claimed = conn.execute(
        "UPDATE gift_card_purchases SET status = 'fulfilled' "
        "WHERE invoice_id = ? AND tenant_id = ? AND status = 'pending'",
        (invoice_id, tenant_id),
    )
    if claimed.rowcount != 1:                           # no pending purchase, or already done
        return
    pur = conn.execute(
        "SELECT * FROM gift_card_purchases WHERE invoice_id = ? AND tenant_id = ?",
        (invoice_id, tenant_id),
    ).fetchone()
    irow = conn.execute(
        "SELECT currency FROM invoices WHERE id = ? AND tenant_id = ?", (invoice_id, tenant_id)
    ).fetchone()
    currency = (irow["currency"] if irow else "usd")
    who = pur["buyer_name"] or "Someone"
    note = f"Gift from {who}" + (f" for {pur['recipient_name']}" if pur["recipient_name"] else "")
    card = None
    for _ in range(5):                                  # a fresh code each try; collisions ~never
        card = create_gift_card(conn, tenant_id=tenant_id, initial_cents=int(pur["amount_cents"]),
                                currency=currency, note=note)
        if card:
            break
    if not card:
        # couldn't mint a unique code — roll the whole settle back so it's retried, rather
        # than leave a paid purchase fulfilled with no card.
        raise GiftCardError("could not issue purchased gift card")
    conn.execute("UPDATE gift_card_purchases SET gift_card_id = ? WHERE id = ?",
                 (card["id"], pur["id"]))
    enqueue(conn, kind="giftcard.deliver", tenant_id=tenant_id, payload={"purchase_id": pur["id"]})
    audit(conn, actor="public", action="giftcard.purchased", tenant_id=tenant_id,
          detail=f"{who} · {int(pur['amount_cents'])}")


@register("giftcard.deliver")
def _deliver(settings, payload: dict) -> None:
    """Email the gift-card code to the recipient (or the buyer) after a purchase settles."""
    from .db import get_db
    from .email import notify
    from .invoices import money
    from .tenants import get_tenant

    purchase_id = int(payload["purchase_id"])
    with get_db(settings.db_path) as conn:
        pur = conn.execute(
            "SELECT * FROM gift_card_purchases WHERE id = ?", (purchase_id,)
        ).fetchone()
        if not pur or not pur["gift_card_id"]:
            return
        card = conn.execute("SELECT * FROM gift_cards WHERE id = ?", (pur["gift_card_id"],)).fetchone()
        to = (pur["recipient_email"] or pur["buyer_email"] or "").strip()
        if not card or not to:
            return
        tenant = get_tenant(conn, pur["tenant_id"])
        studio = (tenant or {}).get("name") or "your studio"
        amount = money(int(card["balance_cents"]), card["currency"])
        who = pur["buyer_name"] or "Someone"
        body = (f"Hi {pur['recipient_name'] or 'there'},\n\n{who} has sent you a {amount} gift card "
                f"for {studio}!\n\nYour code: {card['code']}\n\n"
                + (f"Their message:\n{pur['message']}\n\n" if pur["message"] else "")
                + f"Redeem it at checkout when {studio} sends you an invoice — it can be used across "
                "multiple payments until the balance runs out.")
        notify(conn, settings, to=to, tenant_id=pur["tenant_id"],
               subject=f"You've received a gift card for {studio}", body=body)
        conn.commit()


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
