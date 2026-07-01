"""Public beta interest capture for the hosted $40/month launch."""

from __future__ import annotations

import sqlite3

from .config import Settings
from .email import notify
from .features import SHOOT_TYPE_LABELS, normalize_shoot_type
from .tenants import signup_attribution


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
            status = 'new',
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


def beta_interest_summary(conn: sqlite3.Connection, *, limit: int = 8) -> dict:
    totals = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN datetime(updated_at) >= datetime('now', '-7 days') THEN 1 ELSE 0 END)
                AS last_7_days
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
    return {
        "total": int(totals["total"] or 0),
        "last_7_days": int(totals["last_7_days"] or 0),
        "recent": recent,
        "sources": sources,
        "niches": niches,
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
    return row


def _label(field: str, value: str) -> str:
    raw = (value or "").strip()
    if field == "shoot_type":
        return SHOOT_TYPE_LABELS.get(normalize_shoot_type(raw), "Other / Mixed")
    return {
        "landing": "Landing",
        "pricing": "Pricing",
        "demo": "Demo",
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


def _clean(value: str, limit: int) -> str:
    return (value or "").strip()[:limit]
