"""Client portal — one branded, unguessable link per client.

A portal is a read-only hub: the client sees their projects, contracts to sign,
payment schedule, and galleries in one place, and acts through the flows that
already exist (``/sign/{token}``, ``/pay/{token}``, ``/g/{slug}/{gallery}``).
The portal itself mutates nothing, so it adds no new public write surface.

Access is a per-client token in the URL — the same unguessable-link model as
offers and pay links, no client passwords. The token is opt-in (nullable) and
rotatable: regenerating mints a fresh one and instantly revokes the old link.
"""

from __future__ import annotations

import sqlite3

from .config import Settings
from .contracts import contract_public_url, list_contracts
from .crm import galleries_for_client, get_client, list_projects
from .crypto import new_session_token
from .delivery import delivery_url
from .invoices import client_statement, invoice_public_url, list_invoices
from .payment_plans import get_payment_plan, list_payment_plans
from .questionnaires import list_questionnaires
from .scheduler import appointment_ics_url, list_appointments
from .tenants import get_tenant
from .testimonials import pending_testimonial, testimonial_public_url


def enable_portal(conn: sqlite3.Connection, tenant_id: str, client_id: int) -> str | None:
    """Ensure the client has a portal token, minting one if absent. Idempotent —
    an existing token is preserved (the link the client already has keeps working)."""
    client = get_client(conn, tenant_id, client_id)
    if not client:
        return None
    if client.get("portal_token"):
        return client["portal_token"]
    token = new_session_token()
    conn.execute(
        "UPDATE clients SET portal_token = ? WHERE id = ? AND tenant_id = ?",
        (token, client_id, tenant_id),
    )
    return token


def regenerate_portal_token(conn: sqlite3.Connection, tenant_id: str, client_id: int) -> str | None:
    """Rotate the portal token, revoking the previous link."""
    if not get_client(conn, tenant_id, client_id):
        return None
    token = new_session_token()
    conn.execute(
        "UPDATE clients SET portal_token = ? WHERE id = ? AND tenant_id = ?",
        (token, client_id, tenant_id),
    )
    return token


def get_client_by_portal_token(conn: sqlite3.Connection, token: str) -> dict | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT * FROM clients WHERE portal_token = ?", (token,)
    ).fetchone()
    return dict(row) if row else None


def portal_url(settings: Settings, token: str) -> str:
    return f"{settings.public_url.rstrip('/')}/portal/{token}"


def assemble_portal(conn: sqlite3.Connection, settings: Settings, client: dict) -> dict:
    """Gather everything the client should see, with the action URLs precomputed."""
    tenant_id = client["tenant_id"]
    tenant = get_tenant(conn, tenant_id)
    slug = tenant["slug"] if tenant else ""

    contracts = list_contracts(conn, tenant_id, client_id=client["id"])
    for ct in contracts:
        ct["sign_url"] = contract_public_url(settings, ct["token"])

    # Full plans (with installments) so each installment can carry its pay link.
    plans = []
    for p in list_payment_plans(conn, tenant_id, client_id=client["id"]):
        full = get_payment_plan(conn, tenant_id, p["id"])
        for inst in full["installments"]:
            inst["pay_url"] = invoice_public_url(settings, inst["token"])
        plans.append(full)

    invoices = list_invoices(conn, tenant_id, client_id=client["id"], standalone_only=True)
    for inv in invoices:
        inv["pay_url"] = invoice_public_url(settings, inv["token"])

    galleries = [g for g in galleries_for_client(conn, tenant_id, client["id"])
                 if g["status"] == "published"]
    for g in galleries:
        g["view_url"] = f"{settings.public_url.rstrip('/')}/g/{slug}/{g['slug']}"
        # If the studio has enabled digital delivery, the client downloads here too.
        g["download_url"] = delivery_url(settings, g["delivery_token"]) if g.get("delivery_token") else None

    questionnaires = [q for q in list_questionnaires(conn, tenant_id, client_id=client["id"])
                      if q["status"] in ("sent", "completed")]
    for q in questionnaires:
        q["fill_url"] = f"{settings.public_url.rstrip('/')}/q/{q['token']}"

    appointments = [a for a in list_appointments(conn, tenant_id, client_id=client["id"])
                    if a["status"] in ("proposed", "confirmed")]
    for a in appointments:
        a["book_url"] = f"{settings.public_url.rstrip('/')}/book/{a['token']}"
        # a confirmed session can be added to the client's own calendar
        a["calendar_url"] = appointment_ics_url(settings, a["token"]) if a["status"] == "confirmed" else None

    pending = pending_testimonial(conn, tenant_id, client["id"])
    review_url = testimonial_public_url(settings, pending["token"]) if pending else None

    return {
        "tenant": tenant,
        "projects": list_projects(conn, tenant_id, client_id=client["id"]),
        "contracts": contracts,
        "plans": plans,
        "invoices": invoices,
        "galleries": galleries,
        "questionnaires": questionnaires,
        "appointments": appointments,
        "review_url": review_url,
        # billed / paid / outstanding across all the client's issued invoices + installments
        "statement": client_statement(conn, tenant_id, client["id"]),
    }
