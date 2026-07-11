"""Launch kit actions — nudges, digests, and billing outreach."""

from __future__ import annotations

import sqlite3

from ..config import Settings
from ..db import audit
from ..email import notify
from ..tenants import get_tenant
from ..trial_conversion import trial_conversion_cockpit, trial_conversion_for_tenant
from .kit import (
    LAUNCH_DIGEST_COOLDOWN_DAYS,
    beta_launch_kit,
    followup,
    launch_digest_recent,
    launch_digest_recipient,
    launch_nudge_activity,
    price_label,
)

DUNNING_COOLDOWN_DAYS = 4
DUNNING_ACTION = "billing.dunning_sent"


def send_beta_launch_nudge(
    conn: sqlite3.Connection,
    settings: Settings,
    tenant_id: str,
) -> dict | None:
    tenant = get_tenant(conn, tenant_id)
    if not tenant:
        return None
    studio = trial_conversion_for_tenant(conn, tenant, settings)
    activity = launch_nudge_activity(conn)
    item = followup(studio, nudge_activity=activity)
    if not item["owner_email"]:
        return None
    if item["nudge_cooling_down"]:
        return {**item, "email_status": "cooldown", "skipped": True}
    status = notify(
        conn,
        settings,
        to=item["owner_email"],
        tenant_id=tenant_id,
        signed=False,
        subject=item["email_subject"],
        body=item["email_body"],
    )
    return {**item, "email_status": status}


def send_trial_ending_nudges(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    days_threshold: int = 3,
    limit: int = 200,
) -> int:
    """Automatic trial-conversion sweep: every studio still trialing with
    ``days_threshold`` or fewer days left gets the same personalized follow-up the
    admin's manual nudge button sends — through the same audit-backed cooldown, so
    the sweep and a manual nudge can never double-send. The founder stops having to
    remember to click before each trial closes. Returns the number sent."""
    cockpit = trial_conversion_cockpit(conn, settings, limit=limit)
    sent = 0
    for studio in cockpit["studios"]:
        if studio["trial_state"] != "trialing":
            continue
        days_left = studio.get("trial_days_left")
        if days_left is None or days_left > max(0, int(days_threshold)):
            continue
        result = send_beta_launch_nudge(conn, settings, studio["tenant_id"])
        if not result or result.get("skipped") or not result.get("email_status"):
            continue
        audit(conn, actor="worker", action="launch.nudge_sent",
              tenant_id=studio["tenant_id"], detail=result["owner_email"])
        sent += 1
    return sent


def send_past_due_dunning(conn: sqlite3.Connection, settings: Settings, *,
                          limit: int = 200) -> int:
    """Card-failed outreach: every studio whose subscription is past_due gets one
    polite fix-your-card email per cooldown window, on the same audit-row pattern
    as launch nudges. past_due keeps full access (grace period) — this email is
    how the grace period ends with a fixed card instead of a churned studio.
    Returns the number sent."""
    rows = conn.execute(
        """
        SELECT s.tenant_id, t.name FROM subscriptions s
          JOIN tenants t ON t.id = s.tenant_id
         WHERE s.status = 'past_due'
           AND t.plan IN ('studio', 'studio_pro')
         ORDER BY s.updated_at
         LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    sent = 0
    for row in rows:
        tenant_id = row["tenant_id"]
        cooling = conn.execute(
            "SELECT 1 FROM audit_log WHERE action = ? AND tenant_id = ? "
            "AND datetime(created_at) >= datetime('now', ?) LIMIT 1",
            (DUNNING_ACTION, tenant_id, f"-{int(DUNNING_COOLDOWN_DAYS)} days"),
        ).fetchone()
        if cooling:
            continue
        owner = conn.execute(
            "SELECT email FROM users WHERE tenant_id = ? AND role = 'owner' "
            "ORDER BY id LIMIT 1",
            (tenant_id,),
        ).fetchone()
        if not owner or not owner["email"]:
            continue
        billing_url = f"{settings.public_url.rstrip('/')}/settings/billing"
        status = notify(
            conn,
            settings,
            to=owner["email"],
            tenant_id=tenant_id,
            signed=False,
            subject="Your Hestia payment needs attention",
            body=(
                f"Hi {row['name']},\n\n"
                "The monthly payment for your Hestia studio didn't go through — "
                "usually an expired or replaced card.\n\n"
                "Nothing is lost: your studio, galleries, and client links are all "
                "still running. To keep it that way, update your card here:\n\n"
                f"  {billing_url}\n\n"
                "It takes about a minute. If something looks off on our side, just "
                "reply to this email.\n\n"
                "— Hestia"
            ),
        )
        if not status:
            continue
        audit(conn, actor="worker", action=DUNNING_ACTION, tenant_id=tenant_id,
              detail=owner["email"])
        sent += 1
    return sent


def build_beta_launch_digest(conn: sqlite3.Connection, settings: Settings) -> dict:
    kit = beta_launch_kit(conn, settings)
    pipeline = kit["revenue_pipeline"]
    paid = int(pipeline["paid"])
    mrr = price_label(settings, paid)
    stalled = int(kit["summary"]["stalled"])
    open_interest = int(kit["interest"]["open_total"])
    subject = (
        f"Hestia launch digest: {paid} paid, "
        f"{stalled} stalled, {open_interest} open interest"
    )
    base = settings.public_url.rstrip("/") or "http://127.0.0.1:8500"
    lines = [
        "Hestia hosted launch snapshot",
        "",
        f"Current flat-plan MRR: {mrr}",
        f"Pipeline bottleneck: {pipeline['bottleneck']['label']} "
        f"({pipeline['bottleneck']['dropoff']} drop-off)",
        "",
        "Revenue pipeline",
    ]
    lines.extend(
        f"- {stage['label']}: {stage['count']} "
        f"({stage['percent']}% from prior, -{stage['dropoff']})"
        for stage in pipeline["stages"]
    )
    lines.extend([
        "",
        "Beta interest",
        f"- New in last 7 days: {kit['interest']['last_7_days']}",
        f"- Open: {kit['interest']['open_total']}",
        f"- Invited: {kit['interest']['invited_total']}",
        f"- Converted: {kit['interest']['converted_total']}",
        "",
        "Founder checklist",
    ])
    lines.extend(
        f"{item['rank']}. {item['label']} - {item['detail']}"
        for item in kit["operating_checklist"]
    )
    lines.extend(["", "Follow up today"])
    if kit["followups"]:
        lines.extend(
            f"- {item['name']}: {item['prompt']} ({item['risk']})"
            for item in kit["followups"]
        )
    else:
        lines.append("- No urgent follow-ups.")
    lines.extend([
        "",
        f"Open launch kit: {base}/admin/launch",
        f"Trial cockpit: {base}/admin/trials",
    ])
    return {"subject": subject, "body": "\n".join(lines), "kit": kit}


def send_beta_launch_digest(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    force: bool = False,
    cooldown_days: int = LAUNCH_DIGEST_COOLDOWN_DAYS,
    actor: str = "system",
) -> dict:
    to = launch_digest_recipient(settings)
    if not to:
        return {"sent": False, "status": "missing", "to": ""}
    if not force and launch_digest_recent(conn, cooldown_days=cooldown_days):
        return {"sent": False, "status": "cooldown", "to": to}
    digest = build_beta_launch_digest(conn, settings)
    status = notify(
        conn,
        settings,
        to=to,
        subject=digest["subject"],
        body=digest["body"],
        signed=False,
    )
    audit(conn, actor=actor, action="launch.digest_sent", detail=f"{to}:{status or ''}")
    return {
        "sent": True,
        "status": status or "sent",
        "to": to,
        "subject": digest["subject"],
    }
