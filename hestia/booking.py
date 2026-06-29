"""Self-serve booking — the studio's public "book me" menu of session types.

A studio publishes a small menu of bookable session types (a free consultation, a
mini-session, an engagement shoot). A visitor on the public studio site picks one and
requests a time; :func:`request_booking` turns that into a CRM lead (client + project)
plus a *proposed* appointment at the requested time, which the owner confirms with one
click — reusing the scheduler's existing confirm → confirmation-email → reminder →
calendar machinery. So the public "book me" page feeds the same pipeline as every other
lead, and nothing new touches the money path (price is display-only; deposits come later).

Session types are reference data: set up once, soft-archived (``active = 0``) rather than
deleted so the menu can be tidied without losing history. Everything is tenant-scoped.
"""

from __future__ import annotations

import sqlite3

from .config import Settings
from .crm import create_client, create_project
from .db import audit
from .email import notify
from .invoices import create_invoice, get_invoice, send_invoice
from .scheduler import (
    APPOINTMENT_KINDS,
    confirm_appointment,
    create_appointment,
    get_appointment,
)

_MAX_DURATION = 24 * 60   # a single session is at most a day; clamp absurd input


def _clean_kind(kind: str) -> str:
    return kind if kind in APPOINTMENT_KINDS else "consultation"


def create_booking_type(
    conn: sqlite3.Connection, *, tenant_id: str, title: str, description: str = "",
    kind: str = "consultation", duration_minutes: int = 60, price_cents: int = 0,
    deposit_cents: int = 0,
) -> dict | None:
    """Add a bookable session type to the studio's menu. Returns None for a blank title."""
    title = (title or "").strip()
    if not title:
        return None
    row = conn.execute(
        "SELECT COALESCE(MAX(position), 0) AS m FROM booking_types WHERE tenant_id = ?",
        (tenant_id,),
    ).fetchone()
    pos = (row["m"] if row else 0) + 1
    cur = conn.execute(
        "INSERT INTO booking_types (tenant_id, title, description, kind, duration_minutes, "
        "price_cents, deposit_cents, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tenant_id, title[:200], (description or "").strip()[:2000], _clean_kind(kind),
         min(_MAX_DURATION, max(1, int(duration_minutes or 0))), max(0, int(price_cents or 0)),
         max(0, int(deposit_cents or 0)), pos),
    )
    return get_booking_type(conn, tenant_id, cur.lastrowid)


def get_booking_type(conn: sqlite3.Connection, tenant_id: str, type_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM booking_types WHERE id = ? AND tenant_id = ?", (type_id, tenant_id)
    ).fetchone()
    return dict(row) if row else None


def list_booking_types(
    conn: sqlite3.Connection, tenant_id: str, *, active_only: bool = False
) -> list[dict]:
    """The tenant's session types — active first, then by position. ``active_only`` skips
    archived ones (used for the public booking page)."""
    sql = "SELECT * FROM booking_types WHERE tenant_id = ?"
    if active_only:
        sql += " AND active = 1"
    sql += " ORDER BY active DESC, position, id"
    return [dict(r) for r in conn.execute(sql, (tenant_id,)).fetchall()]


def update_booking_type(
    conn: sqlite3.Connection, tenant_id: str, type_id: int, *, title: str,
    description: str = "", kind: str = "consultation", duration_minutes: int = 60,
    price_cents: int = 0, deposit_cents: int = 0,
) -> bool:
    """Edit a session type in place. True iff a row of this tenant's changed; a blank
    title is rejected (returns False)."""
    title = (title or "").strip()
    if not title:
        return False
    cur = conn.execute(
        "UPDATE booking_types SET title = ?, description = ?, kind = ?, duration_minutes = ?, "
        "price_cents = ?, deposit_cents = ?, updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ?",
        (title[:200], (description or "").strip()[:2000], _clean_kind(kind),
         min(_MAX_DURATION, max(1, int(duration_minutes or 0))), max(0, int(price_cents or 0)),
         max(0, int(deposit_cents or 0)), type_id, tenant_id),
    )
    return cur.rowcount == 1


def set_booking_type_active(
    conn: sqlite3.Connection, tenant_id: str, type_id: int, active: bool
) -> None:
    """Archive (active=False) or restore (active=True) a session type — tenant-scoped."""
    conn.execute(
        "UPDATE booking_types SET active = ?, updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ?",
        (1 if active else 0, type_id, tenant_id),
    )


def delete_booking_type(conn: sqlite3.Connection, tenant_id: str, type_id: int) -> None:
    conn.execute("DELETE FROM booking_types WHERE id = ? AND tenant_id = ?", (type_id, tenant_id))


def request_booking(
    conn: sqlite3.Connection, settings: Settings, *, tenant: dict, booking_type: dict, name: str,
    email: str = "", requested_at: str = "", message: str = "", lead_source: str = "booking",
    confirm: bool = False,
) -> dict:
    """A public visitor books/requests a session of one published type. Creates a CRM lead
    (client + project) and an appointment at the requested time. When ``confirm`` is set
    (the visitor picked a real open availability slot), the appointment is confirmed on the
    spot — firing the usual confirmation email + reminder; otherwise it's left PROPOSED for
    the owner to confirm. If the session type carries a deposit, also raises a deposit
    invoice (paid through the existing /pay flow) to secure the booking. Returns
    ``{"project", "appointment", "client", "invoice"}`` (invoice is None when no deposit).
    No commit — the caller owns the transaction, so the lead, appointment, and deposit
    invoice all land together or not at all."""
    tenant_id = tenant["id"]
    who = (name or "").strip() or (email or "").strip() or "Booking request"
    when = (requested_at or "").replace("T", " ").strip()   # accept datetime-local; store space-separated
    client = create_client(conn, tenant_id=tenant_id, name=who, email=email)
    notes = (f"{'Booked' if confirm else 'Requested'}: {booking_type['title']}"
             + (f" · {when}" if when else "")
             + (f"\n\n{message.strip()}" if (message or "").strip() else ""))
    project = create_project(
        conn, tenant_id=tenant_id,
        name=f"{booking_type['title']} — {who}", client_id=client["id"],
        status="lead", notes=notes, lead_source=lead_source,
    )
    appt = create_appointment(
        conn, tenant_id=tenant_id, title=booking_type["title"],
        options=[when] if when else [], kind=booking_type.get("kind", "consultation"),
        client_id=client["id"], project_id=project["id"],
        duration_minutes=int(booking_type.get("duration_minutes") or 60),
    )
    deposit_cents = max(0, int(booking_type.get("deposit_cents") or 0))
    if confirm and when:                                # picked a real open slot → confirm it now
        confirm_appointment(conn, tenant_id, appt["id"], when)   # this emails them the confirmation
        appt = get_appointment(conn, tenant_id, appt["id"])
    elif (email or "").strip():
        # Not auto-confirmed (the studio hasn't set availability, or it's a free-text time
        # request) → acknowledge it to the visitor so they're not left hanging until the studio
        # confirms. A confirmed slot already emails them via confirm_appointment, so only here.
        studio = (tenant.get("name") or "the studio")
        notify(conn, settings, to=email.strip(), tenant_id=tenant_id, signed=True,
               subject=f"We received your booking request — {booking_type['title']}",
               body=(f"Hi {who},\n\nThanks for requesting a {booking_type['title']} with {studio}.\n"
                     + (f"\nYou asked for: {when}.\n" if when else "")
                     + "\nWe'll confirm your time shortly"
                     + (" — a deposit invoice is on its way to secure your spot."
                        if deposit_cents > 0 else ".")
                     + f"\n\nWarmly,\n{studio}"))
    invoice = None
    if deposit_cents > 0:
        invoice = create_invoice(
            conn, settings, tenant_id=tenant_id, title=f"Deposit — {booking_type['title']}",
            amount_cents=deposit_cents, client_id=client["id"], project_id=project["id"],
            note=("Deposit to secure your booking. Your session time will be confirmed "
                  "by the studio."),
        )
        # Issue it (draft → sent) so an unpaid deposit shows up in A/R and on the client's
        # statement — not just once it's paid. Payment works the same either way.
        send_invoice(conn, tenant_id, invoice["id"])
        invoice = get_invoice(conn, tenant_id, invoice["id"])
    audit(conn, actor="public", action="booking.confirmed" if (confirm and when) else "booking.requested",
          tenant_id=tenant_id, detail=f"{booking_type['title']} · {when or 'no time given'} · {email or who}")
    return {"project": project, "appointment": appt, "client": client, "invoice": invoice}
