"""Contract data access — draft terms, send for signature, capture an e-signature.

Statuses: ``draft → sent → signed`` (or ``void``). Each contract carries a public
``token`` for a shareable sign link. Signing is idempotent — the contract moves
``sent → signed`` exactly once, recording the typed signature, timestamp, and IP;
a second submit (or a re-opened link) never re-signs. A signed contract can be
neither re-signed nor voided. Tenant-scoped throughout.
"""

from __future__ import annotations

import sqlite3

from . import messaging
from .automations import emit_event
from .config import Settings
from .crypto import new_session_token
from .db import audit
from .email import notify
from .ownership import normalize_client_project_ids

CONTRACT_STATUSES = ("draft", "sent", "signed", "void")


def create_contract(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    title: str,
    body: str = "",
    client_id: int | None = None,
    project_id: int | None = None,
    signer_name: str = "",
    signer_email: str = "",
) -> dict:
    client_id, project_id = normalize_client_project_ids(conn, tenant_id, client_id, project_id)
    token = new_session_token()[:28]
    cur = conn.execute(
        """
        INSERT INTO contracts
            (tenant_id, client_id, project_id, title, body, token, signer_name, signer_email)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (tenant_id, client_id, project_id, title.strip(), body, token,
         signer_name.strip(), signer_email.strip()),
    )
    return get_contract(conn, tenant_id, cur.lastrowid)


def get_contract(conn: sqlite3.Connection, tenant_id: str, contract_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT ct.*, c.name AS client_name, c.email AS client_email, p.name AS project_name
          FROM contracts ct
          LEFT JOIN clients c ON c.id = ct.client_id AND c.tenant_id = ct.tenant_id
          LEFT JOIN projects p ON p.id = ct.project_id AND p.tenant_id = ct.tenant_id
         WHERE ct.id = ? AND ct.tenant_id = ?
        """,
        (contract_id, tenant_id),
    ).fetchone()
    return dict(row) if row else None


def get_contract_by_token(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute(
        """
        SELECT ct.*, c.name AS client_name, p.name AS project_name
          FROM contracts ct
          LEFT JOIN clients c ON c.id = ct.client_id AND c.tenant_id = ct.tenant_id
          LEFT JOIN projects p ON p.id = ct.project_id AND p.tenant_id = ct.tenant_id
         WHERE ct.token = ?
        """,
        (token,),
    ).fetchone()
    return dict(row) if row else None


def list_contracts(
    conn: sqlite3.Connection, tenant_id: str, *,
    project_id: int | None = None, client_id: int | None = None,
) -> list[dict]:
    sql = (
        "SELECT ct.*, c.name AS client_name, p.name AS project_name "
        "  FROM contracts ct "
        "  LEFT JOIN clients c ON c.id = ct.client_id AND c.tenant_id = ct.tenant_id "
        "  LEFT JOIN projects p ON p.id = ct.project_id AND p.tenant_id = ct.tenant_id "
        " WHERE ct.tenant_id = ?"
    )
    params: list = [tenant_id]
    if project_id is not None:
        sql += " AND ct.project_id = ?"
        params.append(project_id)
    if client_id is not None:
        sql += " AND ct.client_id = ?"
        params.append(client_id)
    sql += " ORDER BY ct.created_at DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def send_contract(conn: sqlite3.Connection, tenant_id: str, contract_id: int) -> None:
    """Make a contract available for signature (draft→sent). Re-sending is allowed
    while still sent (to re-email the link); a signed/void contract is untouched."""
    conn.execute(
        "UPDATE contracts SET status = 'sent', updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ? AND status IN ('draft', 'sent')",
        (contract_id, tenant_id),
    )


def void_contract(conn: sqlite3.Connection, tenant_id: str, contract_id: int) -> None:
    """Void a contract. A signed contract is legally executed and cannot be voided."""
    conn.execute(
        "UPDATE contracts SET status = 'void', updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ? AND status != 'signed'",
        (contract_id, tenant_id),
    )


def sign_contract(
    conn: sqlite3.Connection, *, token: str, signature_name: str, signer_ip: str = ""
) -> bool:
    """Idempotently capture a typed e-signature. Returns True only on the single
    ``sent → signed`` transition; every later submit is a no-op (returns False).

    The ``WHERE status = 'sent'`` guard makes the transition atomic — under a
    double submit only the first row update lands, so a contract is never
    re-signed and the original signature/timestamp stand.
    """
    signature = signature_name.strip()
    if not signature:
        return False
    cur = conn.execute(
        "UPDATE contracts SET status = 'signed', signature_name = ?, signed_ip = ?, "
        "signed_at = datetime('now'), updated_at = datetime('now') "
        "WHERE token = ? AND status = 'sent'",
        (signature, signer_ip, token),
    )
    if cur.rowcount == 0:
        return False
    row = conn.execute(
        "SELECT tenant_id, title, client_id, project_id FROM contracts WHERE token = ?", (token,)
    ).fetchone()
    audit(conn, actor="client", action="contract.signed", tenant_id=row["tenant_id"],
          detail=f"{row['title']} · signed by {signature}")
    emit_event(conn, tenant_id=row["tenant_id"], event="contract.signed",
               context={"client_id": row["client_id"], "project_id": row["project_id"],
                        "title": row["title"]})
    return True


def contract_public_url(settings: Settings, token: str) -> str:
    return f"{settings.public_url.rstrip('/')}/sign/{token}"


# --- chase signatures: nudge clients sitting on an unsigned contract -----------


def send_contract_reminder(conn: sqlite3.Connection, settings: Settings, contract: dict) -> str | None:
    """Email a friendly nudge with the sign link. Recipient is the named signer's
    email, else the client's. Returns the send status, or None with no address."""
    to = (contract.get("signer_email") or contract.get("client_email") or "").strip()
    if not to:
        return None
    trow = conn.execute("SELECT name FROM tenants WHERE id = ?", (contract["tenant_id"],)).fetchone()
    ctx = {
        "client": contract.get("signer_name") or contract.get("client_name") or "there",
        "studio": trow["name"] if trow else "your photographer",
        "title": contract["title"], "sign_url": contract_public_url(settings, contract["token"]),
    }
    msg = messaging.render(conn, contract["tenant_id"], "contract_reminder", ctx)
    return notify(conn, settings, to=to, subject=msg["subject"], body=msg["body"],
                  tenant_id=contract["tenant_id"])


def record_contract_reminder(conn: sqlite3.Connection, tenant_id: str, contract_id: int) -> bool:
    """Atomically stamp a reminder as sent — gates the next nudge. Only a still-'sent'
    contract is stamped; True iff a row changed (claim-before-send, no double nudge)."""
    cur = conn.execute(
        "UPDATE contracts SET last_reminder_at = datetime('now'), reminder_count = reminder_count + 1 "
        "WHERE id = ? AND tenant_id = ? AND status = 'sent'",
        (contract_id, tenant_id),
    )
    return cur.rowcount > 0


def send_unsigned_reminders(conn: sqlite3.Connection, settings: Settings, *,
                            cooldown_days: int = 7, limit: int = 500) -> int:
    """Across all tenants, nudge each unsigned ('sent') contract that has an email to
    reach (signer or client) and hasn't been reminded within the cooldown. Each is
    claimed first (an atomic UPDATE gated on status='sent'); only a successful claim
    sends, so a contract signed between this SELECT and the send gets no late nudge."""
    rows = conn.execute(
        "SELECT ct.id, ct.tenant_id, ct.title, ct.token, ct.signer_name, ct.signer_email, "
        "       c.name AS client_name, c.email AS client_email "
        "FROM contracts ct LEFT JOIN clients c ON c.id = ct.client_id AND c.tenant_id = ct.tenant_id "
        "WHERE ct.status = 'sent' "
        "  AND (TRIM(COALESCE(ct.signer_email, '')) <> '' OR TRIM(COALESCE(c.email, '')) <> '') "
        "  AND (ct.last_reminder_at IS NULL OR ct.last_reminder_at < datetime('now', ?)) "
        "ORDER BY ct.id LIMIT ?",
        (f"-{int(cooldown_days)} days", limit),
    ).fetchall()
    sent = 0
    for r in rows:
        c = dict(r)
        if record_contract_reminder(conn, c["tenant_id"], c["id"]):   # claim before send
            send_contract_reminder(conn, settings, c)
            sent += 1
    return sent


# --- reusable contract templates: save boilerplate, start a contract from it -----


def save_contract_template(conn: sqlite3.Connection, *, tenant_id: str, name: str,
                           body: str) -> dict | None:
    """Save a named reusable contract template. Empty name is ignored (returns None)."""
    label = (name or "").strip()
    if not label:
        return None
    cur = conn.execute(
        "INSERT INTO contract_templates (tenant_id, name, body) VALUES (?, ?, ?)",
        (tenant_id, label[:200], (body or "").strip()),
    )
    return get_contract_template(conn, tenant_id, cur.lastrowid)


def list_contract_templates(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM contract_templates WHERE tenant_id = ? ORDER BY name, id", (tenant_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_contract_template(conn: sqlite3.Connection, tenant_id: str, template_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM contract_templates WHERE id = ? AND tenant_id = ?", (template_id, tenant_id)
    ).fetchone()
    return dict(row) if row else None


def delete_contract_template(conn: sqlite3.Connection, tenant_id: str, template_id: int) -> None:
    conn.execute(
        "DELETE FROM contract_templates WHERE id = ? AND tenant_id = ?", (template_id, tenant_id)
    )
