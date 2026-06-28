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
    # not a bookable kind — set only by create_block (personal/busy time)
    "blocked": "Blocked",
}

# proposed → confirmed → (completed | no_show), or canceled at any point before.
STATUS_LABELS = {
    "proposed": "Proposed",
    "confirmed": "Confirmed",
    "canceled": "Canceled",
    "completed": "Completed",
    "no_show": "No-show",
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


def create_block(conn: sqlite3.Connection, *, tenant_id: str, title: str, starts_at: str,
                 duration_minutes: int = 60, notes: str = "") -> dict:
    """Block off personal/busy time — a confirmed calendar entry with no client and no
    public booking. It shows on the schedule, the agenda, and the subscribe-able feed so
    the studio can see it; visibility only (it doesn't auto-prevent a client from booking
    an overlapping proposed time)."""
    token = new_session_token()[:28]
    when = starts_at.replace("T", " ").strip()      # accept datetime-local; store space-separated
    cur = conn.execute(
        "INSERT INTO appointments (tenant_id, title, kind, status, starts_at, duration_minutes, "
        "token, notes) VALUES (?, ?, 'blocked', 'confirmed', ?, ?, ?, ?)",
        (tenant_id, title.strip() or "Busy", when, max(0, int(duration_minutes)), token, notes.strip()),
    )
    return get_appointment(conn, tenant_id, cur.lastrowid)


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
    a["status_label"] = STATUS_LABELS.get(a["status"], a["status"])
    return a


def get_appointment_by_token(conn: sqlite3.Connection, token: str) -> dict | None:
    # kind != 'blocked': a personal time-block carries a token too, but it's never a
    # client booking — keep it out of the public book/cancel/calendar flow entirely.
    row = conn.execute(
        """
        SELECT a.*, c.name AS client_name, p.name AS project_name
          FROM appointments a
          LEFT JOIN clients c ON c.id = a.client_id AND c.tenant_id = a.tenant_id
          LEFT JOIN projects p ON p.id = a.project_id AND p.tenant_id = a.tenant_id
         WHERE a.token = ? AND a.kind != 'blocked'
        """,
        (token,),
    ).fetchone()
    if not row:
        return None
    a = dict(row)
    a["options"] = _options(conn, a["tenant_id"], a["id"])
    a["kind_label"] = KIND_LABELS.get(a["kind"], a["kind"])
    a["status_label"] = STATUS_LABELS.get(a["status"], a["status"])
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
        a["status_label"] = STATUS_LABELS.get(a["status"], a["status"])
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


def complete_appointment(conn: sqlite3.Connection, tenant_id: str, appt_id: int) -> bool:
    """Close out a confirmed session as completed (once). True iff a row changed."""
    cur = conn.execute(
        "UPDATE appointments SET status = 'completed', updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ? AND status = 'confirmed'",
        (appt_id, tenant_id),
    )
    return cur.rowcount > 0


def mark_no_show(conn: sqlite3.Connection, tenant_id: str, appt_id: int) -> bool:
    """Mark a confirmed session as a no-show (once). True iff a row changed."""
    cur = conn.execute(
        "UPDATE appointments SET status = 'no_show', updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ? AND status = 'confirmed'",
        (appt_id, tenant_id),
    )
    return cur.rowcount > 0


def cancel_by_token(conn: sqlite3.Connection, settings: Settings, token: str) -> bool:
    """Client self-cancel via their booking link. Moves a proposed/confirmed session
    to canceled (once, guarded by the status set) and alerts the studio that the time
    has freed up. Idempotent — a second submit or a re-opened link is a no-op."""
    appt = get_appointment_by_token(conn, token)
    if not appt or appt["status"] not in ("proposed", "confirmed"):
        return False
    cur = conn.execute(
        "UPDATE appointments SET status = 'canceled', updated_at = datetime('now') "
        "WHERE id = ? AND status IN ('proposed', 'confirmed')",
        (appt["id"],),
    )
    if cur.rowcount == 0:
        return False
    tenant_id = appt["tenant_id"]
    audit(conn, actor="client", action="appointment.canceled_by_client", tenant_id=tenant_id,
          detail=f"{appt['title']} · {appt.get('starts_at') or ''}".strip(" ·"))
    emit_event(conn, tenant_id=tenant_id, event="appointment.canceled",
               context={"client_id": appt.get("client_id"), "project_id": appt.get("project_id"),
                        "title": appt["title"]})
    _alert_cancellation(conn, settings, tenant_id, appt)
    return True


def _alert_cancellation(conn: sqlite3.Connection, settings: Settings, tenant_id: str,
                        appt: dict) -> None:
    """Tell the studio owner a client canceled, so the freed time gets noticed."""
    row = conn.execute(
        "SELECT email FROM users WHERE tenant_id = ? AND role = 'owner' ORDER BY id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    to = row["email"] if row else ""
    if not to:
        return
    who = appt.get("client_name") or "A client"
    when = appt.get("starts_at") or "their session"
    notify(conn, settings, to=to, signed=False,        # owner-facing alert → unsigned
           subject=f"Canceled: {appt['title']}",
           body=f"{who} canceled their {appt['title']} ({when}). That time is now open again.",
           tenant_id=tenant_id)


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
    from . import messaging
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
        ctx = {
            "client": (client or {}).get("name") or "there",
            "studio": (tenant or {}).get("name", "your photographer"),
            "title": appt["title"], "when": appt["starts_at"],
            "location": f"\nLocation: {appt['location']}" if appt["location"] else "",
            "calendar_url": appointment_ics_url(settings, appt["token"]),
        }
        tpl_kind = "appointment_reminder" if kind == "reminder" else "appointment_confirm"
        msg = messaging.render(conn, appt["tenant_id"], tpl_kind, ctx)
        notify(conn, settings, to=to, subject=msg["subject"], body=msg["body"],
               tenant_id=appt["tenant_id"])


def appointment_public_url(settings: Settings, token: str) -> str:
    return f"{settings.public_url.rstrip('/')}/book/{token}"


def appointment_ics_url(settings: Settings, token: str) -> str:
    return f"{settings.public_url.rstrip('/')}/book/{token}/calendar.ics"


def _ics_escape(text: str) -> str:
    """Escape a value for an iCalendar text field (RFC 5545 §3.3.11). Backslash first,
    then fold any CR / CRLF to a single newline so an embedded carriage return in
    owner-entered text can't inject extra calendar lines, then escape ; , and newline."""
    s = str(text).replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n")
    return s.replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _vevent_lines(conn: sqlite3.Connection, appt: dict) -> list[str] | None:
    """The VEVENT block for one appointment, or None when its free-text ``starts_at``
    won't parse. Floating local time (as typed); DTEND from duration; DTSTAMP UTC."""
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
        "BEGIN:VEVENT",
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
    lines.append("END:VEVENT")
    return lines


def _wrap_calendar(event_blocks: list[list[str]]) -> str:
    """Wrap zero or more VEVENT blocks in a VCALENDAR (CRLF-terminated per RFC 5545)."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Hestia//Scheduler//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    for block in event_blocks:
        lines += block
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def appointment_ics(conn: sqlite3.Connection, appt: dict) -> str | None:
    """An iCalendar (.ics) for a single confirmed appointment, or None when it's not
    confirmed or its time can't be placed on a calendar."""
    if appt.get("status") != "confirmed":
        return None
    block = _vevent_lines(conn, appt)
    return _wrap_calendar([block]) if block else None


def ensure_calendar_token(conn: sqlite3.Connection, tenant_id: str) -> str:
    """The studio's calendar-feed token, minting one on first use. Stable thereafter so
    a calendar app that's already subscribed keeps working."""
    row = conn.execute("SELECT calendar_token FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    tok = (row["calendar_token"] if row else None) or ""
    if tok:
        return tok
    tok = new_session_token()
    conn.execute("UPDATE tenants SET calendar_token = ? WHERE id = ?", (tok, tenant_id))
    return tok


def regenerate_calendar_token(conn: sqlite3.Connection, tenant_id: str) -> str:
    """Mint a fresh token, revoking the old feed URL (any existing subscriptions break)."""
    tok = new_session_token()
    conn.execute("UPDATE tenants SET calendar_token = ? WHERE id = ?", (tok, tenant_id))
    return tok


def get_tenant_by_calendar_token(conn: sqlite3.Connection, token: str) -> dict | None:
    if not token or not token.strip():
        return None
    row = conn.execute(
        "SELECT * FROM tenants WHERE calendar_token = ?", (token.strip(),)
    ).fetchone()
    return dict(row) if row else None


def calendar_feed_url(settings: Settings, token: str) -> str:
    return f"{settings.public_url.rstrip('/')}/calendar/{token}.ics"


def schedule_ics(conn: sqlite3.Connection, tenant_id: str, *, days: int = 120) -> str:
    """A subscribe-able calendar of the studio's confirmed sessions — recent and
    upcoming within the window. Tenant-scoped (client join tenant-matched); sessions
    with unparseable times are skipped. Always returns a valid (possibly empty) feed."""
    rows = conn.execute(
        "SELECT a.*, c.name AS client_name "
        "  FROM appointments a "
        "  LEFT JOIN clients c ON c.id = a.client_id AND c.tenant_id = a.tenant_id "
        " WHERE a.tenant_id = ? AND a.status = 'confirmed' "
        "   AND datetime(a.starts_at) IS NOT NULL "
        "   AND datetime(a.starts_at) >= datetime('now', '-30 days') "
        "   AND datetime(a.starts_at) <= datetime('now', ?) "
        " ORDER BY datetime(a.starts_at)",
        (tenant_id, f"+{int(days)} days"),
    ).fetchall()
    blocks = []
    for r in rows:
        a = dict(r)
        a["kind_label"] = KIND_LABELS.get(a["kind"], a["kind"])
        block = _vevent_lines(conn, a)
        if block:
            blocks.append(block)
    return _wrap_calendar(blocks)
