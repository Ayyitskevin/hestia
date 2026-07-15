"""Weekly availability + open-slot generation for self-serve booking.

A studio defines recurring weekly windows (e.g. Tue 09:00–17:00). For a chosen session
type, :func:`available_slots` turns those windows into concrete open slots over the next
couple of weeks — stepped by the session's length and excluding any time that collides
with an existing session (proposed or confirmed) or a personal block. The visitor picks a
real slot and it's confirmed on the spot (the route guards against the slot being taken in
between via :func:`is_slot_open`).

Times are minutes-since-midnight / naive local datetimes, matching how appointment times
are stored elsewhere — no timezone handling, deliberately, so this stays consistent with
the rest of the scheduler. ``today``/``now`` are injectable so slot generation is testable
without touching the wall clock.
"""

from __future__ import annotations

import datetime
import sqlite3

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_DEFAULT_DAYS = 14          # how far ahead the booking page offers slots
_DISPLAY_LIMIT = 200        # cap slots shown so a wide-open studio can't render thousands
_SLOT_FMT = "%Y-%m-%d %H:%M"


def hhmm_to_minutes(s: str) -> int | None:
    """'HH:MM' → minutes since midnight, or None if malformed / out of range."""
    try:
        h_str, m_str = (s or "").strip().split(":")
        h, m = int(h_str), int(m_str)
    except (ValueError, AttributeError):
        return None
    return h * 60 + m if 0 <= h <= 23 and 0 <= m <= 59 else None


def _minutes_label(m: int) -> str:
    """720 → '12:00 PM'."""
    h, mm = divmod(int(m), 60)
    ampm = "AM" if h < 12 else "PM"
    return f"{h % 12 or 12}:{mm:02d} {ampm}"


# ── window CRUD ──────────────────────────────────────────────────────────────


def add_window(conn: sqlite3.Connection, *, tenant_id: str, weekday: int,
               start_minute: int, end_minute: int) -> dict | None:
    """Add a weekly availability window. Returns None for an invalid weekday or a
    non-positive / out-of-day range (so a bad form submit is a no-op, not a 500)."""
    weekday, start_minute, end_minute = int(weekday), int(start_minute), int(end_minute)
    if not (0 <= weekday <= 6) or not (0 <= start_minute < end_minute <= 24 * 60):
        return None
    cur = conn.execute(
        "INSERT INTO availability_windows (tenant_id, weekday, start_minute, end_minute) "
        "VALUES (?, ?, ?, ?)",
        (tenant_id, weekday, start_minute, end_minute),
    )
    row = conn.execute(
        "SELECT * FROM availability_windows WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return dict(row) if row else None


def list_windows(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    """The tenant's windows, ordered by weekday then time, each with display labels."""
    rows = conn.execute(
        "SELECT * FROM availability_windows WHERE tenant_id = ? ORDER BY weekday, start_minute, id",
        (tenant_id,),
    ).fetchall()
    out = []
    for r in rows:
        w = dict(r)
        w["weekday_label"] = WEEKDAYS[w["weekday"]] if 0 <= w["weekday"] <= 6 else "?"
        w["start_label"] = _minutes_label(w["start_minute"])
        w["end_label"] = _minutes_label(w["end_minute"])
        out.append(w)
    return out


def delete_window(conn: sqlite3.Connection, tenant_id: str, window_id: int) -> None:
    conn.execute(
        "DELETE FROM availability_windows WHERE id = ? AND tenant_id = ?", (window_id, tenant_id)
    )


def has_availability(conn: sqlite3.Connection, tenant_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM availability_windows WHERE tenant_id = ? LIMIT 1", (tenant_id,)
    ).fetchone() is not None


# ── slot generation ──────────────────────────────────────────────────────────


def _parse_dt(s: str) -> datetime.datetime | None:
    """Parse a stored/naive datetime string ('YYYY-MM-DD HH:MM[:SS]', T or space), or None."""
    s = (s or "").strip().replace("T", " ")
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _busy_intervals(conn: sqlite3.Connection, tenant_id: str,
                    today: datetime.date) -> list[tuple[datetime.datetime, datetime.datetime]]:
    """(start, end) of sessions that occupy time — proposed/confirmed appointments and
    personal blocks — from today onward, skipping any with an unparseable start."""
    rows = conn.execute(
        "SELECT starts_at, duration_minutes FROM appointments "
        "WHERE tenant_id = ? AND status IN ('proposed', 'confirmed') "
        "  AND starts_at IS NOT NULL AND TRIM(starts_at) != '' "
        "  AND date(starts_at) >= date(?)",
        (tenant_id, today.isoformat()),
    ).fetchall()
    out = []
    for r in rows:
        start = _parse_dt(r["starts_at"])
        if not start:
            continue
        dur = max(1, int(r["duration_minutes"] or 60))
        out.append((start, start + datetime.timedelta(minutes=dur)))
    return out


def _overlaps(start: datetime.datetime, end: datetime.datetime, busy) -> bool:
    return any(start < b_end and b_start < end for (b_start, b_end) in busy)


def _generate(conn: sqlite3.Connection, tenant_id: str, *, duration_minutes: int,
              days: int, today: datetime.date, now: datetime.datetime) -> list[datetime.datetime]:
    """All unique open slot starts for the window, sorted — stepped by duration,
    future-only, excluding times that overlap an existing session."""
    dur = max(1, int(duration_minutes or 60))
    windows: dict[int, list[tuple[int, int]]] = {}
    for r in conn.execute(
        "SELECT weekday, start_minute, end_minute FROM availability_windows WHERE tenant_id = ?",
        (tenant_id,),
    ).fetchall():
        windows.setdefault(int(r["weekday"]), []).append((int(r["start_minute"]), int(r["end_minute"])))
    if not windows:
        return []
    min_notice, buffer_min = _booking_rules(conn, tenant_id)
    # min notice pushes the earliest bookable moment out; with 0 this is the plain "future"
    # cutoff (a slot exactly at `now` is excluded, as before).
    earliest = now + datetime.timedelta(hours=min_notice)
    buffer_td = datetime.timedelta(minutes=buffer_min)
    # pad each busy interval by the buffer so slots can't butt right up against a session
    busy = [(bs - buffer_td, be + buffer_td) for bs, be in _busy_intervals(conn, tenant_id, today)]
    # Repeated or aligned windows can describe the same start. That is one
    # bookable option, and duplicates must not consume the display limit.
    slots: set[datetime.datetime] = set()
    for offset in range(days + 1):
        d = today + datetime.timedelta(days=offset)
        for ws, we in sorted(windows.get(d.weekday(), [])):
            t = ws
            while t + dur <= we:                       # the whole session must fit in the window
                start = datetime.datetime.combine(d, datetime.time(t // 60, t % 60))
                end = start + datetime.timedelta(minutes=dur)
                if start > earliest and not _overlaps(start, end, busy):
                    slots.add(start)
                t += dur
    return sorted(slots)


def _booking_rules(conn: sqlite3.Connection, tenant_id: str) -> tuple[int, int]:
    """(min_notice_hours, buffer_minutes) for the tenant — both default to 0."""
    row = conn.execute(
        "SELECT booking_min_notice_hours, booking_buffer_minutes FROM tenants WHERE id = ?",
        (tenant_id,),
    ).fetchone()
    if not row:
        return 0, 0
    return max(0, int(row["booking_min_notice_hours"] or 0)), max(0, int(row["booking_buffer_minutes"] or 0))


def available_slots(conn: sqlite3.Connection, tenant_id: str, *, duration_minutes: int,
                    days: int = _DEFAULT_DAYS, today: datetime.date | None = None,
                    now: datetime.datetime | None = None) -> list[dict]:
    """Open slots grouped by day for the booking page: ``[{day, label, slots:[{value,label}]}]``.
    Capped at a sane display limit."""
    today = today or datetime.date.today()
    now = now or datetime.datetime.now()
    groups: list[dict] = []
    for s in _generate(conn, tenant_id, duration_minutes=duration_minutes, days=days,
                       today=today, now=now)[:_DISPLAY_LIMIT]:
        key = s.date().isoformat()
        if not groups or groups[-1]["day"] != key:
            groups.append({"day": key, "label": _day_label(s.date(), today), "slots": []})
        groups[-1]["slots"].append(
            {"value": s.strftime(_SLOT_FMT), "label": _minutes_label(s.hour * 60 + s.minute)})
    return groups


def _day_label(d: datetime.date, today: datetime.date) -> str:
    if d == today:
        return "Today"
    if d == today + datetime.timedelta(days=1):
        return "Tomorrow"
    return d.strftime("%a %b %d")


def is_slot_open(conn: sqlite3.Connection, tenant_id: str, *, duration_minutes: int, slot: str,
                 days: int = _DEFAULT_DAYS, today: datetime.date | None = None,
                 now: datetime.datetime | None = None) -> bool:
    """Whether ``slot`` is still a real, open slot for this session type — the booking-time
    re-check that guards against booking a time that's off-grid, in the past, or taken since
    the page loaded."""
    today = today or datetime.date.today()
    now = now or datetime.datetime.now()
    target = _parse_dt(slot)
    if not target:
        return False
    valid = {s.strftime(_SLOT_FMT) for s in
             _generate(conn, tenant_id, duration_minutes=duration_minutes, days=days,
                       today=today, now=now)}
    return target.strftime(_SLOT_FMT) in valid
