"""The owner's 'today' view — what needs attention across the studio, gathered into
one home screen so nothing slips: new leads to answer, invoices to chase, sessions
coming up, and finished galleries still waiting to be delivered. Pure read-side
aggregation over the modules that already own each thing."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

from .config import Settings
from .email import notify
from .invoices import accounts_receivable, money
from .presets import preset_applied
from .proposals import proposal_followups
from .reports import monthly_pnl

_SOURCE_WEIGHTS = {
    "mini_session": 45,
    "booking": 35,
    "referral": 40,
    "friend or family": 35,
    "venue or vendor": 30,
    "google search": 25,
    "wedding directory": 20,
    "instagram": 15,
    "other": 8,
}

_SOURCE_LABELS = {
    "mini_session": "Mini-session claim",
    "booking": "Booking page",
    "referral": "Referral source",
    "friend or family": "Friend/family source",
    "venue or vendor": "Venue/vendor source",
    "google search": "Google search lead",
    "wedding directory": "Directory lead",
    "instagram": "Instagram lead",
    "other": "Other source",
}


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
        "       AND date(i.due_date) < date('now', 'localtime') THEN 1 ELSE 0 END AS is_overdue "
        "FROM invoices i LEFT JOIN clients c ON c.id = i.client_id AND c.tenant_id = i.tenant_id "
        # plan_id IS NULL: installments live under their payment plan, not this list,
        # so they don't get double-counted here and under /payment-plans
        "WHERE i.tenant_id = ? AND i.status IN ('draft', 'sent') AND i.plan_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM proposals pr WHERE pr.invoice_id = i.id "
        "                AND pr.tenant_id = i.tenant_id AND pr.status IN ('sent', 'accepted')) "
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
        # a 'blocked' entry is the studio's own busy-time, not a client session — exclude it
        "WHERE a.tenant_id = ? AND a.status != 'canceled' AND a.kind != 'blocked' "
        "AND datetime(a.starts_at) IS NOT NULL "
        "AND datetime(a.starts_at) >= datetime('now', 'localtime') "
        "ORDER BY datetime(a.starts_at) ASC LIMIT ?",
        (tenant_id, limit))]

    # Published galleries the client can see but can't yet download — finish the job.
    to_deliver = [dict(r) for r in conn.execute(
        "SELECT id, title FROM galleries "
        "WHERE tenant_id = ? AND status = 'published' "
        "AND (delivery_token IS NULL OR delivery_token = '') "
        "ORDER BY id DESC LIMIT ?",
        (tenant_id, limit))]

    # Sessions still awaiting a confirmed time: every 'proposed' appointment — a public
    # booking request the studio should confirm, or an owner-sent time-picker the client
    # hasn't answered (worth a nudge either way). These have no starts_at, so the
    # "upcoming" query above can never surface them — without this list they're
    # invisible until the owner happens to open the schedule.
    to_confirm = [dict(r) for r in conn.execute(
        "SELECT a.id, a.title, a.created_at, c.name AS client_name "
        "FROM appointments a LEFT JOIN clients c ON c.id = a.client_id AND c.tenant_id = a.tenant_id "
        "WHERE a.tenant_id = ? AND a.status = 'proposed' AND a.kind != 'blocked' "
        "ORDER BY a.created_at ASC LIMIT ?",  # oldest waiting first
        (tenant_id, limit))]

    # Albums where the client asked for changes — the ball is in the studio's court.
    # Gallery join tenant-matched like every other join here: albums.gallery_id is
    # written tenant-scoped today, but the FK alone doesn't force tenant agreement.
    album_changes = [dict(r) for r in conn.execute(
        "SELECT al.id, al.title, al.change_request, g.title AS gallery_title "
        "FROM albums al JOIN galleries g ON g.id = al.gallery_id AND g.tenant_id = al.tenant_id "
        "WHERE al.tenant_id = ? AND al.change_request IS NOT NULL "
        "ORDER BY al.change_requested_at ASC LIMIT ?",
        (tenant_id, limit))]

    # Contracts sent but not yet signed — the booking can't proceed until they are.
    # Client join tenant-matched so a stray cross-tenant client_id can't surface a name.
    awaiting_contract = [dict(r) for r in conn.execute(
        "SELECT ct.id, ct.title, c.name AS client_name FROM contracts ct "
        "LEFT JOIN clients c ON c.id = ct.client_id AND c.tenant_id = ct.tenant_id "
        "WHERE ct.tenant_id = ? AND ct.status = 'sent' "
        "AND NOT EXISTS (SELECT 1 FROM proposals pr WHERE pr.contract_id = ct.id "
        "                AND pr.tenant_id = ct.tenant_id AND pr.status IN ('sent', 'accepted')) "
        "ORDER BY ct.created_at ASC LIMIT ?",  # oldest unsigned first
        (tenant_id, limit))]

    # Questionnaires sent but not yet completed — chase the details you need to shoot.
    awaiting_questionnaire = [dict(r) for r in conn.execute(
        "SELECT q.id, q.title, c.name AS client_name FROM questionnaires q "
        "LEFT JOIN clients c ON c.id = q.client_id AND c.tenant_id = q.tenant_id "
        "WHERE q.tenant_id = ? AND q.status = 'sent' "
        "ORDER BY q.created_at ASC LIMIT ?",
        (tenant_id, limit))]
    proposal_followup = proposal_followups(conn, tenant_id, limit=limit)

    return {
        "leads": leads,
        "unpaid": unpaid,
        "upcoming": upcoming,
        "to_deliver": to_deliver,
        "to_confirm": to_confirm,
        "album_changes": album_changes,
        "awaiting_contract": awaiting_contract,
        "awaiting_questionnaire": awaiting_questionnaire,
        "proposal_acceptance": proposal_followup["awaiting_acceptance"],
        "proposal_booking": proposal_followup["finish_booking"],
        "proposal_followup": proposal_followup,
        "total": (len(leads) + len(unpaid) + len(upcoming) + len(to_deliver)
                  + len(to_confirm) + len(album_changes)
                  + len(awaiting_contract) + len(awaiting_questionnaire)
                  + proposal_followup["total"]),
    }


def hot_leads(conn: sqlite3.Connection, tenant_id: str, *, limit: int = 5) -> list[dict]:
    """Rank open leads by clear sales signals.

    This is deliberately deterministic and explainable: no model call, no hidden state.
    It uses the data Hestia already owns (source, freshness, event date, client email,
    linked sessions, proposals, and retainer invoices) so the owner can decide who gets
    the next reply without scanning every project.
    """
    rows = conn.execute(
        """
        SELECT
            p.id, p.name, p.shoot_type, p.event_date, p.created_at, p.lead_source, p.notes,
            c.id AS client_id, c.name AS client_name, c.email AS client_email,
            COALESCE((SELECT COUNT(*) FROM appointments a
                       WHERE a.tenant_id = p.tenant_id AND a.project_id = p.id
                         AND a.status = 'confirmed'), 0) AS confirmed_sessions,
            COALESCE((SELECT COUNT(*) FROM appointments a
                       WHERE a.tenant_id = p.tenant_id AND a.project_id = p.id
                         AND a.status = 'proposed'), 0) AS proposed_sessions,
            COALESCE((SELECT COUNT(*) FROM proposals pr
                       WHERE pr.tenant_id = p.tenant_id AND pr.project_id = p.id
                         AND pr.status IN ('sent', 'accepted')), 0) AS active_proposals,
            COALESCE((SELECT COUNT(*) FROM invoices i
                       WHERE i.tenant_id = p.tenant_id AND i.project_id = p.id
                         AND i.status = 'sent'), 0) AS sent_invoices,
            COALESCE((SELECT SUM(i.amount_cents) FROM invoices i
                       WHERE i.tenant_id = p.tenant_id AND i.project_id = p.id
                         AND i.status IN ('sent', 'paid')), 0) AS intent_cents
          FROM projects p
          LEFT JOIN clients c ON c.id = p.client_id AND c.tenant_id = p.tenant_id
         WHERE p.tenant_id = ?
           AND p.status = 'lead'
         ORDER BY p.created_at DESC, p.id DESC
        """,
        (tenant_id,),
    ).fetchall()
    scored = [_score_lead(dict(row)) for row in rows]
    scored.sort(key=lambda row: (row["score"], row.get("created_sort") or datetime.min.replace(tzinfo=UTC)),
                reverse=True)
    for row in scored:
        row.pop("created_sort", None)
    return scored[: max(0, int(limit))]


def _score_lead(row: dict) -> dict:
    score = 10
    reasons: list[str] = []

    source_key = _source_key(row.get("lead_source"))
    source_score = _SOURCE_WEIGHTS.get(source_key, 5)
    score += source_score
    reasons.append(_SOURCE_LABELS.get(source_key, "Source captured" if source_key else "Unknown source"))

    created = _parse_created_at(row.get("created_at"))
    row["created_sort"] = created
    age_days = _age_days(created)
    if age_days is not None:
        if age_days <= 1:
            score += 25
            reasons.append("Fresh inquiry")
        elif age_days <= 3:
            score += 18
            reasons.append("New this week")
        elif age_days <= 7:
            score += 10
            reasons.append("Still warm")
        elif age_days <= 14:
            score += 4
            reasons.append("Needs follow-up")

    days_until_event = _days_until_event(row.get("event_date"))
    if days_until_event is not None:
        if 0 <= days_until_event <= 30:
            score += 20
            reasons.append("Event soon")
        elif days_until_event <= 90:
            score += 12
            reasons.append("Date in next 90 days")
        elif days_until_event <= 180:
            score += 6
            reasons.append("Date on calendar")

    if (row.get("client_email") or "").strip():
        score += 10
        reasons.append("Email on file")

    if int(row.get("confirmed_sessions") or 0):
        score += 25
        reasons.append("Confirmed session")
    elif int(row.get("proposed_sessions") or 0):
        score += 15
        reasons.append("Proposed session")

    if int(row.get("active_proposals") or 0):
        score += 20
        reasons.append("Proposal in motion")

    if int(row.get("sent_invoices") or 0):
        score += 25
        reasons.append("Retainer open")
    elif int(row.get("intent_cents") or 0):
        score += 10
        reasons.append("Money intent")

    if len((row.get("notes") or "").strip()) >= 80:
        score += 5
        reasons.append("Detailed message")

    row["score"] = min(100, score)
    row["priority"] = "Hot lead" if row["score"] >= 75 else "Warm lead" if row["score"] >= 50 else "Watch"
    row["priority_class"] = "on" if row["score"] >= 75 else "off"
    row["reasons"] = _unique(reasons)[:5]
    row["reason_line"] = " · ".join(row["reasons"])
    row["source_label"] = _SOURCE_LABELS.get(source_key, row.get("lead_source") or "Unknown source")
    row["next_action"] = _lead_next_action(row)
    row["intent_value"] = money(int(row.get("intent_cents") or 0))
    row["href"] = f"/projects/{row['id']}"
    row["client_href"] = f"/clients/{row['client_id']}" if row.get("client_id") else ""
    return row


def _source_key(source: str | None) -> str:
    return (source or "").strip().lower().replace("-", "_")


def _age_days(created: datetime | None) -> int | None:
    if created is None:
        return None
    return max(0, (datetime.now(UTC) - created).days)


def _days_until_event(value: str | None) -> int | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        event = date.fromisoformat(raw[:10])
    except ValueError:
        return None
    return (event - datetime.now(UTC).date()).days


def _lead_next_action(row: dict) -> str:
    if not (row.get("client_email") or "").strip():
        return "Add an email before follow-up"
    if int(row.get("sent_invoices") or 0):
        return "Collect retainer"
    if int(row.get("active_proposals") or 0):
        return "Follow proposal"
    if int(row.get("proposed_sessions") or 0):
        return "Confirm session"
    if int(row.get("confirmed_sessions") or 0):
        return "Send prep and next steps"
    return "Reply within 24 hours"


def _unique(items: list[str]) -> list[str]:
    out = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def money_snapshot(conn: sqlite3.Connection, tenant_id: str) -> dict:
    """Money at a glance for the dashboard: this calendar month's revenue and profit,
    plus what's still outstanding (and the overdue slice). Reuses the finances reports
    and A/R, so the figures match the Finances page exactly — revenue counts paid work
    once, profit nets expenses, outstanding is sent-unpaid (plan installments excluded)."""
    month = monthly_pnl(conn, tenant_id, months=1)[0]   # current month, with displays
    ar = accounts_receivable(conn, tenant_id)
    return {"month": month, "ar": ar}


def _parse_created_at(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw) if "T" in raw else datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _days_left(started_at: str | None, trial_days: int) -> int:
    started = _parse_created_at(started_at)
    if started is None:
        return max(0, trial_days)
    remaining = (started + timedelta(days=trial_days)) - datetime.now(UTC)
    if remaining.total_seconds() <= 0:
        return 0
    return max(1, (remaining.days + (1 if remaining.seconds or remaining.microseconds else 0)))


def _flat_price(settings: Settings) -> str:
    cents = int(settings.flat_price_cents)
    if settings.currency.lower() == "usd" and cents % 100 == 0:
        return f"${cents // 100}/month"
    return f"{money(cents, settings.currency)}/month"


def trial_cockpit(
    tenant: dict,
    subscription: dict | None,
    settings: Settings,
    setup: dict,
) -> dict:
    """Hosted-SaaS trial/billing summary for the owner dashboard."""
    trial_days = max(0, int(settings.trial_days))
    price = _flat_price(settings)
    plan = tenant.get("plan", "beta")
    if plan == "studio_pro":
        plan = "studio"
    status = (subscription or {}).get("status") or ("active" if plan == "studio" else "ready")

    if plan == "studio" and status == "trialing":
        days = _days_left((subscription or {}).get("created_at"), trial_days)
        return {
            "title": "Trial active",
            "message": f"Your {trial_days}-day trial is active. {days} days left before {price}. Cancel anytime.",
            "price": price,
            "trial_days": trial_days,
            "billing_label": "Manage billing",
            "next": setup.get("next"),
        }
    if plan == "studio":
        return {
            "title": "Hestia Studio active",
            "message": f"You are on the flat {price} studio plan. Every module is included.",
            "price": price,
            "trial_days": trial_days,
            "billing_label": "Manage billing",
            "next": setup.get("next"),
        }
    return {
        "title": f"{trial_days}-day trial ready",
        "message": f"Start the hosted {trial_days}-day trial when ready; after that Hestia is {price}. No tiers.",
        "price": price,
        "trial_days": trial_days,
        "billing_label": f"Start {trial_days}-day trial",
        "next": setup.get("next"),
    }


def _has_any(conn: sqlite3.Connection, tenant_id: str, table: str) -> bool:
    """Whether the tenant owns at least one row in ``table`` (table name is a fixed
    literal from the caller, never user input)."""
    return conn.execute(
        f"SELECT 1 FROM {table} WHERE tenant_id = ? LIMIT 1", (tenant_id,)
    ).fetchone() is not None


def _has_active_booking_type(conn: sqlite3.Connection, tenant_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM booking_types WHERE tenant_id = ? AND active = 1 LIMIT 1",
        (tenant_id,),
    ).fetchone() is not None


def _has_uploaded_gallery(conn: sqlite3.Connection, tenant_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM images WHERE tenant_id = ? LIMIT 1",
        (tenant_id,),
    ).fetchone() is not None


def _has_active_offer(conn: sqlite3.Connection, tenant_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM offers WHERE tenant_id = ? AND status = 'active' LIMIT 1",
        (tenant_id,),
    ).fetchone() is not None


def _has_money_link(conn: sqlite3.Connection, tenant_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM invoices WHERE tenant_id = ? AND status IN ('sent', 'paid') LIMIT 1",
        (tenant_id,),
    ).fetchone() is not None


def setup_checklist(conn: sqlite3.Connection, tenant_id: str, *, published: bool) -> dict:
    """A studio's commercial activation path — the first loop from public presence to
    a shareable offer and money link. These checks intentionally use existing tenant
    data instead of a separate onboarding state machine, so the checklist stays honest
    when owners skip around."""
    steps = [
        {"stage": "Preset", "label": "Choose a studio preset", "done": preset_applied(conn, tenant_id),
         "href": "/onboarding", "value": "booking, packages, forms"},
        {"stage": "Launch", "label": "Publish studio site", "done": bool(published),
         "href": "/settings/site", "value": "public lead capture"},
        {"stage": "Book", "label": "Add a bookable session", "done": _has_active_booking_type(conn, tenant_id),
         "href": "/settings/booking-types", "value": "self-serve booking path"},
        {"stage": "Client", "label": "Start a client project", "done": _has_any(conn, tenant_id, "projects"),
         "href": "/projects/new", "value": "CRM spine"},
        {"stage": "Deliver", "label": "Upload gallery images", "done": _has_uploaded_gallery(conn, tenant_id),
         "href": "/galleries/new", "value": "client delivery"},
        {"stage": "Sell", "label": "Generate an offer link", "done": _has_active_offer(conn, tenant_id),
         "href": "/galleries", "value": "print and album upsell"},
        {"stage": "Collect", "label": "Send an invoice or retainer", "done": _has_money_link(conn, tenant_id),
         "href": "/invoices/new", "value": "cash collection"},
    ]
    done = sum(1 for s in steps if s["done"])
    next_step = next((s for s in steps if not s["done"]), None)
    return {
        "steps": steps,
        "done": done,
        "total": len(steps),
        "remaining": len(steps) - done,
        "complete": done == len(steps),
        "next": next_step,
    }


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
    section("⏳", "Awaiting a confirmed time", att["to_confirm"],
            lambda x: x["title"] + (f" — {x['client_name']}" if x.get("client_name") else ""))
    section("\U0001f4d6", "Album change requests", att["album_changes"],
            lambda x: f"{x['gallery_title']}: “{x['change_request']}”")
    section("\U0001f4e6", "Ready to deliver", att["to_deliver"], lambda x: x["title"])
    section("✍️", "Awaiting signature", att["awaiting_contract"],
            lambda x: x["title"] + (f" — {x['client_name']}" if x.get("client_name") else ""))
    section("Proposal follow-up", "Awaiting proposal acceptance", att["proposal_acceptance"],
            lambda x: x["title"] + (f" — {x['client_name']}" if x.get("client_name") else ""))
    section("Finish booking", "Accepted proposals to finish", att["proposal_booking"],
            lambda x: f"{x['title']} — {x['followup_label']}")
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
