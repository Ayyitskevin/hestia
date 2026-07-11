"""Email seam — transactional notifications (invoice pay links, inquiry alerts).

Pluggable, same shape as the payments and storage seams:

- ``mock``  — records every message to the ``emails`` outbox table and sends
  nothing. The default: the whole flow is testable in CI, and the studio owner
  can see exactly what Hestia *would* send (``/settings/outbox``). Honest —
  nothing ever leaves the box.
- ``smtp``  — actually delivers over SMTP (host/port/user/password) *and* still
  records to the outbox for an audit trail. Only active with real config; a send
  failure is captured as the row's status, never raised into the request.

Every send is recorded, so a notification can never silently vanish.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import Settings


@dataclass
class EmailMessage:
    to: str
    subject: str
    body: str
    tenant_id: str | None = None


def _record(conn: sqlite3.Connection, msg: EmailMessage, backend: str, status: str) -> None:
    conn.execute(
        "INSERT INTO emails (tenant_id, to_addr, subject, body, backend, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (msg.tenant_id, msg.to, msg.subject, msg.body, backend, status),
    )


class MockEmailer:
    backend = "mock"

    def send(self, conn: sqlite3.Connection, msg: EmailMessage) -> str:
        _record(conn, msg, self.backend, "recorded")
        return "recorded"


class SmtpEmailer:
    backend = "smtp"

    def __init__(self, settings: Settings):
        self.settings = settings

    def send(self, conn: sqlite3.Connection, msg: EmailMessage) -> str:
        status = "sent"
        try:
            self._deliver(msg)
        except Exception as exc:  # noqa: BLE001 - a mail miss must not break the request
            status = f"error: {exc}"
        _record(conn, msg, self.backend, status)
        return status

    def _deliver(self, msg: EmailMessage) -> None:
        import smtplib
        from email.message import EmailMessage as MIME

        s = self.settings
        if not s.smtp_host:
            raise RuntimeError("HESTIA_SMTP_HOST not set for smtp backend")
        mime = MIME()
        mime["From"] = s.smtp_from or s.smtp_user or "no-reply@hestia.local"
        mime["To"] = msg.to
        mime["Subject"] = msg.subject
        mime.set_content(msg.body)
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as srv:
            srv.starttls()
            if s.smtp_user:
                srv.login(s.smtp_user, s.smtp_password)
            srv.send_message(mime)


def build_emailer(settings: Settings):
    if settings.email_backend == "smtp":
        return SmtpEmailer(settings)
    return MockEmailer()


def _with_signature(conn: sqlite3.Connection, tenant_id: str, body: str) -> str:
    """Append the studio's email signature to a client-facing body, if it has one.
    No signature set → body unchanged, so existing mail is untouched."""
    row = conn.execute(
        "SELECT email_signature FROM tenants WHERE id = ?", (tenant_id,)
    ).fetchone()
    sig = ((row["email_signature"] if row else "") or "").strip()
    return f"{body.rstrip()}\n\n—\n{sig}" if sig else body


def notify(conn: sqlite3.Connection, settings: Settings, *, to: str, subject: str,
           body: str, tenant_id: str | None = None, signed: bool = True) -> str | None:
    """Record/send one message. No-op (returns None) when there's no recipient.

    Client-facing mail is signed with the studio's signature (``signed=True``, the
    default). Pass ``signed=False`` for platform/owner mail — signup verification,
    password resets, lead alerts — which shouldn't carry the studio's client-facing
    sign-off.

    Success statuses are ``"sent"`` (SMTP) and ``"recorded"`` (mock). Failures
    return ``"error: …"`` — callers must use :func:`delivery_ok` before treating
    a send as successful (audit rows, cooldowns, counters).
    """
    if not to or not to.strip():
        return None
    if signed and tenant_id:
        body = _with_signature(conn, tenant_id, body)
    msg = EmailMessage(to=to.strip(), subject=subject, body=body, tenant_id=tenant_id)
    return build_emailer(settings).send(conn, msg)


def delivery_ok(status: str | None) -> bool:
    """True when ``notify`` actually delivered (or mock-recorded) the message."""
    return status in ("sent", "recorded")


def list_emails(conn: sqlite3.Connection, tenant_id: str, *, limit: int = 50,
                to_addr: str | None = None) -> list[dict]:
    """Recent emails for a tenant, newest first. With ``to_addr``, the result is scoped to
    one recipient BEFORE the limit — so a client's history isn't truncated by tenant-wide
    volume (matched by address, case-insensitive)."""
    if to_addr and to_addr.strip():
        rows = conn.execute(
            "SELECT * FROM emails WHERE tenant_id = ? AND lower(to_addr) = lower(?) "
            "ORDER BY id DESC LIMIT ?",
            (tenant_id, to_addr.strip(), limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM emails WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
            (tenant_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
