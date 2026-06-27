"""Scheduler — sessions with client self-booking, confirmation + reminder emails.

The studio proposes one or more time options for an appointment; the client picks
one via a public link and it's confirmed. On confirmation Hestia enqueues a
confirmation email and a day-before reminder (the reminder via the job queue's
``run_at``), and emits ``appointment.confirmed`` so the workflow engine can react.

Booking is idempotent: the ``proposed → confirmed`` transition is guarded by
``WHERE status = 'proposed'``, so a double submit or a re-opened link never
rebooks. Tenant-scoped throughout; the reminder job no-ops if the appointment is
canceled before it fires.
"""

from __future__ import annotations

import datetime
import sqlite3

from .automations import emit_event
from .config import Settings
from .crypto import new_session_token
from .db import audit
from .email import notify
from .jobs import enqueue, register

APPOINTMENT_KINDS = ("consultation", "shoot", "call", "other")
KIND_LABELS = {
    "consultation": "Consultation",
    "shoot": "Shoot",
    "call": "Call",
    "other": "Other",
}


def create_appointment(
    conn: sqlite3.Connection, *, tenant_id: str, title: str, options: list[str],
    kind: str = "consultation", client_id: int | None = None, project_id: int | None = None,
    location: str = "", duration_minutes: int = 60, notes: str = "",
) -> dict:
    if kind not in APPOINTMENT_KINDS:
        kind = "other"
    token = new_session_token()[:28]
    cur = conn.execute(
        "INSERT INTO appointments (tenant_id, client_id, project_id, title, kind, location, "
        "duration_minutes, token, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tenant_id, client_id, project_id, title.strip(), kind, location.strip(),
         max(0, int(duration_minutes)), token, notes.strip()),
    )
    appt_id = cur.lastrowid
    for seq, starts_at in enumerate((o.strip() for o in options if o.strip()), start=1):
        conn.execute(
            "INSERT INTO appointment_options (appointment_id, tenant_id, sequence, starts_at) "
            "VALUES (?, ?, ?, ?)",
            (appt_id, tenant_id, seq, starts_at),
        )
    return get_appointment(conn, tenant_id, appt_id)


def _options(conn: sqlite3.Connection, tenant_id: str, appt_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM appointment_options WHERE tenant_id = ? AND appointment_id = ? "
        "ORDER BY sequence, id",
        (tenant_id, appt_id),
    ).fetchall()
    return [dict(r) for r in rows]


def get_appointment(conn: sqlite3.Connection, tenant_id: str, appt_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT a.*, c.name AS client_name, c.email AS client_email, p.name AS project_name
          FROM appointments a
          LEFT JOIN clients c ON c.id = a.client_id AND c.tenant_id = a.tenant_id
          LEFT JOIN projects p ON p.id = a.project_id AND p.tenant_id = a.tenant_id
         WHERE a.id = ? AND a.tenant_id = ?
        """,
        (appt_id, tenant_id),
    ).fetchone()
    if not row:
        return None
    a = dict(row)
    a["options"] = _options(conn, tenant_id, appt_id)
    a["kind_label"] = KIND_LABELS.get(a["kind"], a["kind"])
    return a


def get_appointment_by_token(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute(
        """
        SELECT a.*, c.name AS client_name, p.name AS project_name
          FROM appointments a
          LEFT JOIN clients c ON c.id = a.client_id AND c.tenant_id = a.tenant_id
          LEFT JOIN projects p ON p.id = a.project_id AND p.tenant_id = a.tenant_id
         WHERE a.token = ?
        """,
        (token,),
    ).fetchone()
    if not row:
        return None
    a = dict(row)
    a["options"] = _options(conn, a["tenant_id"], a["id"])
    a["kind_label"] = KIND_LABELS.get(a["kind"], a["kind"])
    return a


def _get_by_id(conn: sqlite3.Connection, appt_id: int) -> dict | None:
    """Tenant-agnostic fetch for the job handler (appointment ids are global)."""
    row = conn.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone()
    return dict(row) if row else None


def list_appointments(
    conn: sqlite3.Connection, tenant_id: str, *,
    project_id: int | None = None, client_id: int | None = None,
) -> list[dict]:
    sql = (
        "SELECT a.*, c.name AS client_name, p.name AS project_name, "
        "       (SELECT COUNT(*) FROM appointment_options o WHERE o.appointment_id = a.id) AS option_count "
        "  FROM appointments a "
        "  LEFT JOIN clients c ON c.id = a.client_id AND c.tenant_id = a.tenant_id "
        "  LEFT JOIN projects p ON p.id = a.project_id AND p.tenant_id = a.tenant_id "
        " WHERE a.tenant_id = ?"
    )
    params: list = [tenant_id]
    if project_id is not None:
        sql += " AND a.project_id = ?"
        params.append(project_id)
    if client_id is not None:
        sql += " AND a.client_id = ?"
        params.append(client_id)
    # Confirmed first (soonest at top), then still-proposed, then canceled.
    sql += (" ORDER BY CASE a.status WHEN 'confirmed' THEN 0 WHEN 'proposed' THEN 1 ELSE 2 END, "
            "a.starts_at, a.created_at DESC")
    out = []
    for r in conn.execute(sql, params).fetchall():
        a = dict(r)
        a["kind_label"] = KIND_LABELS.get(a["kind"], a["kind"])
        out.append(a)
    return out


def agenda(conn: sqlite3.Connection, tenant_id: str, *, days: int = 21) -> list[dict]:
    """Upcoming confirmed sessions grouped by day (soonest first) over the next
    ``days`` — an at-a-glance week view. Sessions with a free-text/unparseable time
    are excluded (they can't be placed on a day)."""
    rows = conn.execute(
        "SELECT a.id, a.title, a.kind, a.starts_at, "
        "       strftime('%H:%M', a.starts_at) AS at_time, date(a.starts_at) AS day, "
        "       c.name AS client_name "
        "FROM appointments a "
        "LEFT JOIN clients c ON c.id = a.client_id AND c.tenant_id = a.tenant_id "
        "WHERE a.tenant_id = ? AND a.status = 'confirmed' AND datetime(a.starts_at) IS NOT NULL "
        "  AND date(a.starts_at) >= date('now') AND date(a.starts_at) <= date('now', ?) "
        "ORDER BY datetime(a.starts_at)",
        (tenant_id, f"+{int(days)} days"),
    ).fetchall()
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    groups: list[dict] = []
    for r in rows:
        d = dict(r)
        d["kind_label"] = KIND_LABELS.get(d["kind"], d["kind"])
        if not groups or groups[-1]["day"] != d["day"]:
            try:
                dd = datetime.date.fromisoformat(d["day"])
                label = ("Today" if dd == today else "Tomorrow" if dd == tomorrow
                         else dd.strftime("%a %b %d"))
            except (ValueError, TypeError):
                label = d["day"]
            groups.append({"day": d["day"], "label": label, "appointments": []})
        groups[-1]["appointments"].append(d)
    return groups


def book_appointment(conn: sqlite3.Connection, *, token: str, option_id: int) -> bool:
    """Client self-booking. Idempotent: the proposed→confirmed transition lands
    once (guarded by ``WHERE status = 'proposed'``); later submits are no-ops."""
    appt = get_appointment_by_token(conn, token)
    if not appt or appt["status"] != "proposed":
        return False
    opt = conn.execute(
        "SELECT starts_at FROM appointment_options WHERE id = ? AND appointment_id = ?",
        (option_id, appt["id"]),
    ).fetchone()
    if not opt:
        return False
    cur = conn.execute(
        "UPDATE appointments SET status = 'confirmed', starts_at = ?, updated_at = datetime('now') "
        "WHERE id = ? AND status = 'proposed'",
        (opt["starts_at"], appt["id"]),
    )
    if cur.rowcount == 0:
        return False
    confirmed = get_appointment(conn, appt["tenant_id"], appt["id"])
    _on_confirmed(conn, appt["tenant_id"], confirmed)
    audit(conn, actor="client", action="appointment.booked", tenant_id=appt["tenant_id"],
          detail=f"{appt['title']} · {opt['starts_at']}")
    return True


def confirm_appointment(
    conn: sqlite3.Connection, tenant_id: str, appt_id: int, starts_at: str
) -> bool:
    """Owner confirms a time directly (proposed→confirmed, once)."""
    when = starts_at.strip()
    if not when:
        return False
    cur = conn.execute(
        "UPDATE appointments SET status = 'confirmed', starts_at = ?, updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ? AND status = 'proposed'",
        (when, appt_id, tenant_id),
    )
    if cur.rowcount == 0:
        return False
    confirmed = get_appointment(conn, tenant_id, appt_id)
    _on_confirmed(conn, tenant_id, confirmed)
    audit(conn, actor="owner", action="appointment.confirmed", tenant_id=tenant_id,
          detail=f"{confirmed['title']} · {when}")
    return True


def cancel_appointment(conn: sqlite3.Connection, tenant_id: str, appt_id: int) -> None:
    conn.execute(
        "UPDATE appointments SET status = 'canceled', updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ?",
        (appt_id, tenant_id),
    )


def _on_confirmed(conn: sqlite3.Connection, tenant_id: str, appt: dict) -> None:
    """Side effects of a confirmed booking: confirmation email, reminder, event."""
    enqueue(conn, kind="scheduler.notify", tenant_id=tenant_id,
            payload={"appointment_id": appt["id"], "kind": "confirm"})
    _schedule_reminder(conn, tenant_id, appt)
    emit_event(conn, tenant_id=tenant_id, event="appointment.confirmed",
               context={"client_id": appt.get("client_id"), "project_id": appt.get("project_id"),
                        "title": appt["title"]})


def _schedule_reminder(conn: sqlite3.Connection, tenant_id: str, appt: dict) -> None:
    """Queue a day-before reminder, letting SQLite parse the time. Skips silently
    if the time is unparseable or already in the past."""
    starts_at = appt.get("starts_at") or ""
    if not starts_at:
        return
    row = conn.execute(
        "SELECT datetime(?) AS parsed, datetime(?, '-1 day') AS remind, datetime('now') AS now",
        (starts_at, starts_at),
    ).fetchone()
    if not row["parsed"] or row["parsed"] <= row["now"]:
        return
    run_at = row["remind"] if row["remind"] and row["remind"] > row["now"] else row["now"]
    enqueue(conn, kind="scheduler.notify", tenant_id=tenant_id,
            payload={"appointment_id": appt["id"], "kind": "reminder"}, run_at=run_at)


@register("scheduler.notify")
def _notify(settings: Settings, payload: dict) -> None:
    """Send a confirmation or reminder email for an appointment, if still on."""
    from .crm import get_client
    from .db import get_db
    from .tenants import get_tenant

    appt_id = int(payload["appointment_id"])
    kind = payload.get("kind", "confirm")
    with get_db(settings.db_path) as conn:
        appt = _get_by_id(conn, appt_id)
        if not appt or appt["status"] != "confirmed":
            return  # canceled or gone between scheduling and firing → no email
        client = get_client(conn, appt["tenant_id"], appt["client_id"]) if appt["client_id"] else None
        to = (client or {}).get("email", "")
        if not to:
            return
        tenant = get_tenant(conn, appt["tenant_id"])
        studio = (tenant or {}).get("name", "your photographer")
        who = (client or {}).get("name") or "there"
        when = appt["starts_at"]
        if kind == "reminder":
            subject = f"Reminder: {appt['title']} on {when}"
            opener = f"A friendly reminder that your {appt['title'].lower()} with {studio} is coming up"
        else:
            subject = f"Confirmed: {appt['title']} on {when}"
            opener = f"Your {appt['title'].lower()} with {studio} is confirmed"
        body = f"Hi {who},\n\n{opener} on {when}."
        if appt["location"]:
            body += f"\nLocation: {appt['location']}"
        body += f"\n\nAdd to your calendar: {appointment_ics_url(settings, appt['token'])}"
        body += "\n\nSee you then!"
        notify(conn, settings, to=to, subject=subject, body=body, tenant_id=appt["tenant_id"])


def appointment_public_url(settings: Settings, token: str) -> str:
    return f"{settings.public_url.rstrip('/')}/book/{token}"


def appointment_ics_url(settings: Settings, token: str) -> str:
    return f"{settings.public_url.rstrip('/')}/book/{token}/calendar.ics"


def _ics_escape(text: str) -> str:
    """Escape a value for an iCalendar text field (RFC 5545 §3.3.11)."""
    return (str(text).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def appointment_ics(conn: sqlite3.Connection, appt: dict) -> str | None:
    """An iCalendar (.ics) VEVENT for a confirmed appointment, or None when its
    free-text ``starts_at`` can't be parsed into a calendar timestamp. The event
    time is floating (no zone) — exactly the local time the studio typed — while
    DTSTAMP is the UTC build time. Duration comes from the appointment."""
    if appt.get("status") != "confirmed":
        return None
    starts_at = (appt.get("starts_at") or "").strip()
    if not starts_at:
        return None
    minutes = max(1, int(appt.get("duration_minutes") or 60))
    row = conn.execute(
        "SELECT strftime('%Y%m%dT%H%M%S', datetime(?))    AS dtstart, "
        "       strftime('%Y%m%dT%H%M%S', datetime(?, ?)) AS dtend, "
        "       strftime('%Y%m%dT%H%M%SZ', 'now')         AS dtstamp",
        (starts_at, starts_at, f"+{minutes} minutes"),
    ).fetchone()
    if not row or not row["dtstart"]:
        return None                                        # unparseable time → no event
    desc = []
    if appt.get("kind_label"):
        desc.append(appt["kind_label"])
    if appt.get("client_name"):
        desc.append(f"with {appt['client_name']}")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Hestia//Scheduler//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "BEGIN:VEVENT",
        f"UID:hestia-appt-{appt['id']}@hestia",
        f"DTSTAMP:{row['dtstamp']}",
        f"DTSTART:{row['dtstart']}",
        f"DTEND:{row['dtend'] or row['dtstart']}",
        f"SUMMARY:{_ics_escape(appt.get('title') or 'Session')}",
    ]
    if appt.get("location"):
        lines.append(f"LOCATION:{_ics_escape(appt['location'])}")
    if desc:
        lines.append(f"DESCRIPTION:{_ics_escape(' · '.join(desc))}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"
