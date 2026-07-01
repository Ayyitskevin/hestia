"""Public beta interest capture for the hosted $40/month launch."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from .config import Settings
from .crypto import hash_api_key, new_session_token
from .email import notify
from .features import SHOOT_TYPE_LABELS, normalize_shoot_type
from .tenants import signup_attribution

BETA_INVITE_TTL = timedelta(days=7)


def record_beta_interest(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    name: str = "",
    studio_name: str = "",
    email: str,
    shoot_type: str = "other",
    note: str = "",
    source: str = "",
    landing_path: str = "",
) -> dict:
    email_norm = (email or "").strip().lower()
    if "@" not in email_norm or "." not in email_norm.rsplit("@", 1)[-1]:
        raise ValueError("Enter a valid email address.")
    attribution = signup_attribution(source, landing_path)
    normalized_shoot_type = normalize_shoot_type(shoot_type)
    conn.execute(
        """
        INSERT INTO beta_interests
            (name, studio_name, email, shoot_type, source, landing_path, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            name = excluded.name,
            studio_name = excluded.studio_name,
            shoot_type = excluded.shoot_type,
            source = excluded.source,
            landing_path = excluded.landing_path,
            note = excluded.note,
            status = CASE
                WHEN beta_interests.status IN ('invited', 'converted')
                THEN beta_interests.status
                ELSE 'new'
            END,
            updated_at = datetime('now')
        """,
        (
            _clean(name, 120),
            _clean(studio_name, 160),
            email_norm,
            normalized_shoot_type,
            attribution["source"],
            attribution["landing_path"],
            _clean(note, 800),
        ),
    )
    row = conn.execute("SELECT * FROM beta_interests WHERE email = ?", (email_norm,)).fetchone()
    interest = _hydrate(dict(row))
    status = _notify_operator(conn, settings, interest)
    return {**interest, "email_status": status}


def send_beta_interest_invite(
    conn: sqlite3.Connection,
    settings: Settings,
    interest_id: int,
    *,
    ttl: timedelta = BETA_INVITE_TTL,
) -> dict | None:
    row = conn.execute("SELECT * FROM beta_interests WHERE id = ?", (int(interest_id),)).fetchone()
    if not row:
        return None
    interest = _hydrate(dict(row))
    if interest.get("tenant_id"):
        return {**interest, "email_status": "converted", "skipped": True}

    token = new_session_token()
    token_hash = hash_api_key(token, settings.tenant_key_pepper)
    expires_at = (_now() + ttl).isoformat()
    invite_url = f"{settings.public_url.rstrip('/')}/invite/{token}"
    status = notify(
        conn,
        settings,
        to=interest["email"],
        signed=False,
        subject="You're invited to start your Hestia studio beta",
        body=_invite_body(interest, invite_url),
    )
    conn.execute(
        """
        UPDATE beta_interests
           SET status = 'invited',
               invite_token_hash = ?,
               invited_at = datetime('now'),
               invite_expires_at = ?,
               invite_email_status = ?,
               updated_at = datetime('now')
         WHERE id = ?
        """,
        (token_hash, expires_at, status or "", interest["id"]),
    )
    row = conn.execute("SELECT * FROM beta_interests WHERE id = ?", (interest["id"],)).fetchone()
    return {**_hydrate(dict(row)), "email_status": status, "invite_url": invite_url}


def find_beta_interest_invite(
    conn: sqlite3.Connection,
    settings: Settings,
    token: str,
) -> dict | None:
    token_hash = hash_api_key(token or "", settings.tenant_key_pepper)
    row = conn.execute(
        """
        SELECT * FROM beta_interests
         WHERE invite_token_hash = ?
           AND invite_token_hash != ''
           AND tenant_id = ''
        """,
        (token_hash,),
    ).fetchone()
    if not row:
        return None
    interest = _hydrate(dict(row))
    expires = _parse_time(interest.get("invite_expires_at"))
    if not expires or expires < _now():
        return None
    return interest


def mark_beta_interest_converted(
    conn: sqlite3.Connection,
    interest_id: int,
    tenant_id: str,
) -> None:
    conn.execute(
        """
        UPDATE beta_interests
           SET status = 'converted',
               tenant_id = ?,
               converted_at = datetime('now'),
               invite_token_hash = '',
               updated_at = datetime('now')
         WHERE id = ?
        """,
        (tenant_id, int(interest_id)),
    )


def mark_beta_interest_converted_by_email(
    conn: sqlite3.Connection,
    email: str,
    tenant_id: str,
) -> None:
    email_norm = (email or "").strip().lower()
    if not email_norm:
        return
    conn.execute(
        """
        UPDATE beta_interests
           SET status = 'converted',
               tenant_id = ?,
               converted_at = COALESCE(converted_at, datetime('now')),
               invite_token_hash = '',
               updated_at = datetime('now')
         WHERE lower(email) = ?
           AND tenant_id = ''
        """,
        (tenant_id, email_norm),
    )


def beta_interest_summary(conn: sqlite3.Connection, *, limit: int = 8) -> dict:
    totals = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN datetime(updated_at) >= datetime('now', '-7 days') THEN 1 ELSE 0 END)
                AS last_7_days,
            SUM(CASE WHEN status = 'invited' THEN 1 ELSE 0 END) AS invited_total,
            SUM(CASE WHEN status = 'converted' THEN 1 ELSE 0 END) AS converted_total,
            SUM(CASE WHEN status != 'converted' THEN 1 ELSE 0 END) AS open_total
          FROM beta_interests
        """
    ).fetchone()
    recent = [
        _hydrate(dict(row))
        for row in conn.execute(
            "SELECT * FROM beta_interests ORDER BY updated_at DESC, id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    ]
    sources = _counts(conn, "source")
    niches = _counts(conn, "shoot_type")
    statuses = _counts(conn, "status")
    return {
        "total": int(totals["total"] or 0),
        "last_7_days": int(totals["last_7_days"] or 0),
        "invited_total": int(totals["invited_total"] or 0),
        "converted_total": int(totals["converted_total"] or 0),
        "open_total": int(totals["open_total"] or 0),
        "recent": recent,
        "sources": sources,
        "niches": niches,
        "statuses": statuses,
    }


def _counts(conn: sqlite3.Connection, field: str) -> list[dict]:
    if field == "source":
        rows = conn.execute(
            """
            SELECT source AS label, COUNT(*) AS count
              FROM beta_interests
             GROUP BY source
             ORDER BY count DESC, label ASC
            """
        ).fetchall()
    elif field == "shoot_type":
        rows = conn.execute(
            """
            SELECT shoot_type AS label, COUNT(*) AS count
              FROM beta_interests
             GROUP BY shoot_type
             ORDER BY count DESC, label ASC
            """
        ).fetchall()
    elif field == "status":
        rows = conn.execute(
            """
            SELECT status AS label, COUNT(*) AS count
              FROM beta_interests
             GROUP BY status
             ORDER BY count DESC, label ASC
            """
        ).fetchall()
    else:
        raise ValueError(f"Unsupported beta interest summary field: {field}")
    total = sum(int(row["count"] or 0) for row in rows)
    return [
        {
            "label": _label(field, row["label"]),
            "count": int(row["count"] or 0),
            "percent": round(100 * int(row["count"] or 0) / max(1, total)),
        }
        for row in rows
    ]


def _hydrate(row: dict) -> dict:
    shoot_type = normalize_shoot_type(row.get("shoot_type"))
    row["shoot_type"] = shoot_type
    row["shoot_type_label"] = SHOOT_TYPE_LABELS.get(shoot_type, shoot_type.title())
    row["source_label"] = _label("source", row.get("source") or "")
    row["status"] = (row.get("status") or "new").strip().lower()
    row["status_label"] = _label("status", row["status"])
    row["invite_available"] = row["status"] != "converted" and not row.get("tenant_id")
    row["invite_button_label"] = "Resend invite" if row["status"] == "invited" else "Send invite"
    return row


def _label(field: str, value: str) -> str:
    raw = (value or "").strip()
    if field == "shoot_type":
        return SHOOT_TYPE_LABELS.get(normalize_shoot_type(raw), "Other / Mixed")
    if field == "status":
        return {
            "new": "New",
            "invited": "Invited",
            "converted": "Converted",
        }.get(raw, raw.title() if raw else "New")
    return {
        "landing": "Landing",
        "pricing": "Pricing",
        "demo": "Demo",
        "interest": "Beta interest",
        "": "Direct / unknown",
    }.get(raw, raw.title() if raw else "Direct / unknown")


def _notify_operator(conn: sqlite3.Connection, settings: Settings, interest: dict) -> str | None:
    to = (settings.smtp_from or settings.smtp_user or "").strip()
    if not to:
        return None
    studio = interest["studio_name"] or interest["name"] or interest["email"]
    subject = f"New Hestia beta interest: {studio}"
    body = "\n".join([
        "New Hestia beta interest:",
        "",
        f"Name: {interest['name'] or '-'}",
        f"Studio: {interest['studio_name'] or '-'}",
        f"Email: {interest['email']}",
        f"Niche: {interest['shoot_type_label']}",
        f"Source: {interest['source_label']} {interest['landing_path'] or ''}".strip(),
        "",
        interest["note"] or "No note.",
    ])
    return notify(conn, settings, to=to, subject=subject, body=body, signed=False)


def _invite_body(interest: dict, invite_url: str) -> str:
    studio = interest["studio_name"] or interest["name"] or "your studio"
    return "\n".join([
        f"You're invited to start {studio} on Hestia.",
        "",
        "Use this private beta invite to create your hosted photography studio:",
        invite_url,
        "",
        "Your first 14 days are free. After that, Hestia is exactly $40/month with no tiers.",
        "It brings booking, contracts, galleries, invoices, AI offers, and follow-up into one studio command center.",
        "",
        "If the invite expires, reply and Kevin can send a fresh one.",
    ])


def _clean(value: str, limit: int) -> str:
    return (value or "").strip()[:limit]


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
