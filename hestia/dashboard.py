"""The owner's 'today' view — what needs attention across the studio, gathered into
one home screen so nothing slips: new leads to answer, invoices to chase, sessions
coming up, and finished galleries still waiting to be delivered. Pure read-side
aggregation over the modules that already own each thing."""

from __future__ import annotations

import sqlite3

from .config import Settings
from .email import notify
from .invoices import accounts_receivable, money
from .reports import monthly_pnl


def needs_attention(conn: sqlite3.Connection, tenant_id: str, *, limit: int = 8) -> dict:
    """Actionable items for the dashboard, each scoped to the tenant."""
    leads = [dict(r) for r in conn.execute(
        "SELECT p.id, p.name, p.created_at, p.shoot_type, c.name AS client_name "
        "FROM projects p LEFT JOIN clients c ON c.id = p.client_id AND c.tenant_id = p.tenant_id "
        "WHERE p.tenant_id = ? AND p.status = 'lead' "
        "ORDER BY p.created_at ASC LIMIT ?",  # oldest unanswered first
        (tenant_id, limit))]

    unpaid = [dict(r) for r in conn.execute(
        "SELECT i.id, i.title, i.amount_cents, i.currency, i.status, c.name AS client_name, "
        # flag the overdue ones (sent, past a parseable due_date) and float them up
        "  CASE WHEN i.status = 'sent' AND date(i.due_date) IS NOT NULL "
        "       AND date(i.due_date) < date('now') THEN 1 ELSE 0 END AS is_overdue "
        "FROM invoices i LEFT JOIN clients c ON c.id = i.client_id AND c.tenant_id = i.tenant_id "
        # plan_id IS NULL: installments live under their payment plan, not this list,
        # so they don't get double-counted here and under /payment-plans
        "WHERE i.tenant_id = ? AND i.status IN ('draft', 'sent') AND i.plan_id IS NULL "
        "ORDER BY is_overdue DESC, i.id DESC LIMIT ?",
        (tenant_id, limit))]
    for inv in unpaid:
        inv["amount_display"] = money(inv["amount_cents"], inv.get("currency") or "usd")

    # starts_at is free-text (owners type it), so parse via datetime(): a real
    # timestamp compares chronologically; unparseable text yields NULL and is excluded
    # rather than mis-sorted by a lexicographic string compare.
    upcoming = [dict(r) for r in conn.execute(
        "SELECT a.id, a.title, a.starts_at, a.status, c.name AS client_name "
        "FROM appointments a LEFT JOIN clients c ON c.id = a.client_id AND c.tenant_id = a.tenant_id "
        "WHERE a.tenant_id = ? AND a.status != 'canceled' "
        "AND datetime(a.starts_at) IS NOT NULL AND datetime(a.starts_at) >= datetime('now') "
        "ORDER BY datetime(a.starts_at) ASC LIMIT ?",
        (tenant_id, limit))]

    # Published galleries the client can see but can't yet download — finish the job.
    to_deliver = [dict(r) for r in conn.execute(
        "SELECT id, title FROM galleries "
        "WHERE tenant_id = ? AND status = 'published' "
        "AND (delivery_token IS NULL OR delivery_token = '') "
        "ORDER BY id DESC LIMIT ?",
        (tenant_id, limit))]

    # Contracts sent but not yet signed — the booking can't proceed until they are.
    # Client join tenant-matched so a stray cross-tenant client_id can't surface a name.
    awaiting_contract = [dict(r) for r in conn.execute(
        "SELECT ct.id, ct.title, c.name AS client_name FROM contracts ct "
        "LEFT JOIN clients c ON c.id = ct.client_id AND c.tenant_id = ct.tenant_id "
        "WHERE ct.tenant_id = ? AND ct.status = 'sent' "
        "ORDER BY ct.created_at ASC LIMIT ?",  # oldest unsigned first
        (tenant_id, limit))]

    # Questionnaires sent but not yet completed — chase the details you need to shoot.
    awaiting_questionnaire = [dict(r) for r in conn.execute(
        "SELECT q.id, q.title, c.name AS client_name FROM questionnaires q "
        "LEFT JOIN clients c ON c.id = q.client_id AND c.tenant_id = q.tenant_id "
        "WHERE q.tenant_id = ? AND q.status = 'sent' "
        "ORDER BY q.created_at ASC LIMIT ?",
        (tenant_id, limit))]

    return {
        "leads": leads,
        "unpaid": unpaid,
        "upcoming": upcoming,
        "to_deliver": to_deliver,
        "awaiting_contract": awaiting_contract,
        "awaiting_questionnaire": awaiting_questionnaire,
        "total": (len(leads) + len(unpaid) + len(upcoming) + len(to_deliver)
                  + len(awaiting_contract) + len(awaiting_questionnaire)),
    }


def money_snapshot(conn: sqlite3.Connection, tenant_id: str) -> dict:
    """Money at a glance for the dashboard: this calendar month's revenue and profit,
    plus what's still outstanding (and the overdue slice). Reuses the finances reports
    and A/R, so the figures match the Finances page exactly — revenue counts paid work
    once, profit nets expenses, outstanding is sent-unpaid (plan installments excluded)."""
    month = monthly_pnl(conn, tenant_id, months=1)[0]   # current month, with displays
    ar = accounts_receivable(conn, tenant_id)
    return {"month": month, "ar": ar}


def _has_any(conn: sqlite3.Connection, tenant_id: str, table: str) -> bool:
    """Whether the tenant owns at least one row in ``table`` (table name is a fixed
    literal from the caller, never user input)."""
    return conn.execute(
        f"SELECT 1 FROM {table} WHERE tenant_id = ? LIMIT 1", (tenant_id,)
    ).fetchone() is not None


def setup_checklist(conn: sqlite3.Connection, tenant_id: str, *, published: bool) -> dict:
    """A new studio's activation steps — the first actions that turn an empty account
    into a working studio. Each step links to its action; once every step is done the
    dashboard stops showing the checklist, so an established studio never sees it."""
    steps = [
        {"label": "Add your first client", "done": _has_any(conn, tenant_id, "clients"),
         "href": "/clients/new"},
        {"label": "Start a project", "done": _has_any(conn, tenant_id, "projects"),
         "href": "/projects/new"},
        {"label": "Create a gallery", "done": _has_any(conn, tenant_id, "galleries"),
         "href": "/galleries/new"},
        {"label": "Send an invoice", "done": _has_any(conn, tenant_id, "invoices"),
         "href": "/invoices/new"},
        {"label": "Publish your studio site", "done": bool(published), "href": "/settings/site"},
    ]
    done = sum(1 for s in steps if s["done"])
    return {"steps": steps, "done": done, "total": len(steps), "complete": done == len(steps)}


def reconnect_due(conn: sqlite3.Connection, tenant_id: str, *,
                  limit: int = 6, quiet_days: int = 300) -> list[dict]:
    """Past clients who've gone quiet — their most recent project is older than
    ``quiet_days`` (≈10 months) and they have an email to reach. A gentle retention
    nudge so the studio reaches out before the client books their next shoot elsewhere.
    Only clients with at least one project qualify; oldest-quiet first. Tenant-scoped."""
    rows = conn.execute(
        "SELECT c.id, c.name, c.email, MAX(p.created_at) AS last_seen "
        "FROM clients c JOIN projects p ON p.client_id = c.id AND p.tenant_id = c.tenant_id "
        "WHERE c.tenant_id = ? AND TRIM(COALESCE(c.email, '')) <> '' "
        "GROUP BY c.id "
        "HAVING MAX(p.created_at) < datetime('now', ?) "  # quiet past the cutoff
        "ORDER BY last_seen ASC LIMIT ?",
        (tenant_id, f"-{int(quiet_days)} days", limit),
    ).fetchall()
    return [dict(r) for r in rows]


# --- owner digest: the dashboard, delivered as a periodic email ----------------


def owner_digest_recipient(conn: sqlite3.Connection, tenant_id: str) -> str:
    """Where the owner digest goes: the studio's stated contact email, else the owner's
    login. Empty string if neither exists (then no digest is sent)."""
    row = conn.execute(
        "SELECT contact_email FROM studio_profiles WHERE tenant_id = ?", (tenant_id,)
    ).fetchone()
    if row and (row["contact_email"] or "").strip():
        return row["contact_email"].strip()
    owner = conn.execute(
        "SELECT email FROM users WHERE tenant_id = ? AND role = 'owner' ORDER BY id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    return (owner["email"] if owner else "").strip()


def build_owner_digest(conn: sqlite3.Connection, tenant_id: str,
                       settings: Settings) -> dict | None:
    """Assemble the studio's 'what needs you' summary as a plain-text email. Returns
    ``{"subject", "body"}``, or None when there's nothing worth sending (so an idle
    studio is never pinged). Reuses the same aggregation as the dashboard."""
    att = needs_attention(conn, tenant_id)
    reconnect = reconnect_due(conn, tenant_id)
    count = att["total"] + len(reconnect)
    if count == 0:
        return None
    trow = conn.execute("SELECT name FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    studio = (trow["name"] if trow else "") or "your studio"
    base = settings.public_url.rstrip("/")
    snap = money_snapshot(conn, tenant_id)

    lines = [f"Here's what needs you at {studio}:", ""]

    def section(emoji, label, items, render):
        if items:
            lines.append(f"{emoji} {label} ({len(items)})")
            lines.extend(f" · {render(i)}" for i in items)
            lines.append("")

    section("\U0001f4e5", "New leads", att["leads"],
            lambda x: x["name"] + (f" — {x['client_name']}" if x.get("client_name") else ""))
    section("\U0001f4b8", "Unpaid invoices", att["unpaid"],
            lambda x: f"{x['title']} — {x['amount_display']}" + (" (overdue)" if x.get("is_overdue") else ""))
    section("\U0001f4c5", "Upcoming sessions", att["upcoming"],
            lambda x: f"{x['title']} — {x['starts_at']}")
    section("\U0001f4e6", "Ready to deliver", att["to_deliver"], lambda x: x["title"])
    section("✍️", "Awaiting signature", att["awaiting_contract"],
            lambda x: x["title"] + (f" — {x['client_name']}" if x.get("client_name") else ""))
    section("\U0001f4cb", "Awaiting questionnaire", att["awaiting_questionnaire"],
            lambda x: x["title"] + (f" — {x['client_name']}" if x.get("client_name") else ""))
    section("\U0001f91d", "Reconnect", reconnect,
            lambda x: f"{x['name']} — last booked {x['last_seen'][:10]}")

    lines.append(f"\U0001f4b0 This month: revenue {snap['month']['revenue']}, "
                 f"profit {snap['month']['profit']}; outstanding {snap['ar']['outstanding']}.")
    lines.append("")
    lines.append(f"Open your dashboard: {base}/dashboard")

    noun = "thing needs" if count == 1 else "things need"
    return {"subject": f"{studio}: {count} {noun} your attention", "body": "\n".join(lines)}


def send_owner_digest_now(conn: sqlite3.Connection, settings: Settings,
                          tenant_id: str) -> str | None:
    """Send the digest to one studio's owner immediately (the manual 'email me this'
    action). No-op (None) if there's no recipient or nothing to report."""
    to = owner_digest_recipient(conn, tenant_id)
    if not to:
        return None
    digest = build_owner_digest(conn, tenant_id, settings)
    if not digest:
        return None
    # Claim-before-send: an atomic stamp with a short window. A double-click (or retry)
    # within the window loses the claim (rowcount 0) and sends nothing — so the owner
    # never gets two copies. The stamp also gates the weekly sweep for the cooldown. A
    # deliberate manual resend after the window still works.
    cur = conn.execute(
        "UPDATE tenants SET last_digest_at = datetime('now') "
        "WHERE id = ? AND (last_digest_at IS NULL OR last_digest_at < datetime('now', '-1 minute'))",
        (tenant_id,),
    )
    if cur.rowcount == 0:
        return None
    return notify(conn, settings, to=to, subject=digest["subject"], body=digest["body"],
                  tenant_id=tenant_id, signed=False)


def set_digest_enabled(conn: sqlite3.Connection, tenant_id: str, enabled: bool) -> None:
    """Turn the weekly owner digest on or off for a studio."""
    conn.execute("UPDATE tenants SET digest_enabled = ? WHERE id = ?",
                 (1 if enabled else 0, tenant_id))


def _claim_digest(conn: sqlite3.Connection, tenant_id: str, cooldown_days: int) -> bool:
    """Atomically stamp the digest as sent — gates the next one. True iff this call won
    the claim (last_digest_at was null or older than the cooldown), so a second worker
    pass in the same window sends nothing."""
    cur = conn.execute(
        "UPDATE tenants SET last_digest_at = datetime('now') "
        "WHERE id = ? AND (last_digest_at IS NULL OR last_digest_at < datetime('now', ?))",
        (tenant_id, f"-{int(cooldown_days)} days"),
    )
    return cur.rowcount > 0


def send_owner_digests(conn: sqlite3.Connection, settings: Settings, *,
                       cooldown_days: int = 7, limit: int = 500) -> int:
    """Across all studios, email each owner their digest at most once per cooldown. A
    tenant with nothing to report (or no recipient) is skipped without being claimed, so
    it's revisited as soon as something comes up; one with content is claimed before the
    send. Returns the number sent."""
    rows = conn.execute(
        "SELECT id FROM tenants "
        "WHERE (last_digest_at IS NULL OR last_digest_at < datetime('now', ?)) "
        "  AND COALESCE(digest_enabled, 1) = 1 "       # honor the owner's opt-out
        "ORDER BY id LIMIT ?",
        (f"-{int(cooldown_days)} days", limit),
    ).fetchall()
    sent = 0
    for r in rows:
        tid = r["id"]
        to = owner_digest_recipient(conn, tid)
        digest = build_owner_digest(conn, tid, settings) if to else None
        if not digest:
            continue                                  # nothing to say / nowhere to send
        if _claim_digest(conn, tid, cooldown_days):   # claim before send
            notify(conn, settings, to=to, subject=digest["subject"], body=digest["body"],
                   tenant_id=tid, signed=False)
            sent += 1
    return sent
