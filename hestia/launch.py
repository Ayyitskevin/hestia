"""Operator launch kit for the hosted $40/month beta."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from .config import Settings
from .db import audit
from .email import notify
from .founder_demo import founder_demo_summary
from .interest import beta_interest_summary
from .tenants import get_tenant
from .trial_conversion import trial_conversion_cockpit, trial_conversion_for_tenant

BETA_TARGET_STUDIOS = 5
LAUNCH_NUDGE_COOLDOWN_DAYS = 3
LAUNCH_DIGEST_COOLDOWN_DAYS = 7


def beta_launch_kit(conn: sqlite3.Connection, settings: Settings) -> dict:
    cockpit = trial_conversion_cockpit(conn, settings)
    studios = cockpit["studios"]
    nudge_activity = _launch_nudge_activity(conn)
    interest = beta_interest_summary(conn)
    verified = sum(1 for studio in studios if studio["owner_verified"])
    presets_started = sum(1 for studio in studios if studio["activation_done"] > 0)
    sourced = sum(1 for studio in studios if studio["signup_source"] in ("pricing", "demo"))
    trialing_or_active = sum(
        1 for studio in studios if studio["trial_state"] in ("trialing", "active")
    )
    return {
        "target": BETA_TARGET_STUDIOS,
        "invite_links": _invite_links(settings),
        "operations": _launch_operations(settings),
        "founder_demo": founder_demo_summary(conn, settings),
        "followups": _followups(studios, nudge_activity=nudge_activity),
        "cohort": _cohort_summary(studios, nudge_activity=nudge_activity),
        "interest": interest,
        "revenue_pipeline": _revenue_pipeline(conn, settings, studios, interest=interest),
        "operating_checklist": _operating_checklist(
            studios,
            nudge_activity=nudge_activity,
            interest=interest,
        ),
        "milestones": [
            _milestone("Invite 5 studios", len(studios), BETA_TARGET_STUDIOS),
            _milestone("Verify 3 owners", verified, 3),
            _milestone("Start 3 niche presets", presets_started, 3),
            _milestone("Source 2 from pricing/demo", sourced, 2),
            _milestone("Start first hosted trial", trialing_or_active, 1),
        ],
        "summary": {
            "studios": len(studios),
            "verified": verified,
            "presets_started": presets_started,
            "sourced": sourced,
            "trialing_or_active": trialing_or_active,
            "stalled": cockpit["summary"]["stalled"],
        },
    }


def beta_launch_export_rows(conn: sqlite3.Connection, settings: Settings) -> list[dict]:
    cockpit = trial_conversion_cockpit(conn, settings)
    nudge_activity = _launch_nudge_activity(conn)
    followups = {
        f["tenant_id"]: f
        for f in _followups(cockpit["studios"], limit=999, nudge_activity=nudge_activity)
    }
    rows = []
    for studio in cockpit["studios"]:
        followup = followups.get(studio["tenant_id"]) or _followup(
            studio,
            nudge_activity=nudge_activity,
        )
        rows.append({
            "studio": studio["name"],
            "slug": studio["slug"],
            "owner_email": studio["owner_email"],
            "owner_verified": "yes" if studio["owner_verified"] else "no",
            "source": studio["signup_source_label"],
            "landing_path": studio["signup_landing_path"] or "",
            "trial_state": studio["trial_state"],
            "trial_label": studio["trial_label"],
            "risk": studio["risk"],
            "risk_reason": studio["risk_reason"],
            "activation": f"{studio['activation_done']}/{studio['activation_total']}",
            "activation_percent": studio["activation_percent"],
            "next_action": studio["next_action"],
            "owner_path": studio["next_href"],
            "followup_prompt": followup["prompt"],
            "mailto": followup["mailto"],
            "last_nudged_at": followup["last_nudged_at"],
            "nudge_status": followup["nudge_status"],
        })
    return rows


def send_beta_launch_nudge(
    conn: sqlite3.Connection,
    settings: Settings,
    tenant_id: str,
) -> dict | None:
    tenant = get_tenant(conn, tenant_id)
    if not tenant:
        return None
    studio = trial_conversion_for_tenant(conn, tenant, settings)
    activity = _launch_nudge_activity(conn)
    followup = _followup(studio, nudge_activity=activity)
    if not followup["owner_email"]:
        return None
    if followup["nudge_cooling_down"]:
        return {**followup, "email_status": "cooldown", "skipped": True}
    status = notify(
        conn,
        settings,
        to=followup["owner_email"],
        tenant_id=tenant_id,
        signed=False,
        subject=followup["email_subject"],
        body=followup["email_body"],
    )
    return {**followup, "email_status": status}


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
        # Same ledger row the admin route writes — it IS the cooldown.
        audit(conn, actor="worker", action="launch.nudge_sent",
              tenant_id=studio["tenant_id"], detail=result["owner_email"])
        sent += 1
    return sent


DUNNING_COOLDOWN_DAYS = 4
DUNNING_ACTION = "billing.dunning_sent"


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
    mrr = _price_label(settings, paid)
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
    to = _launch_digest_recipient(settings)
    if not to:
        return {"sent": False, "status": "missing", "to": ""}
    if not force and _launch_digest_recent(conn, cooldown_days=cooldown_days):
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


def _launch_nudge_activity(
    conn: sqlite3.Connection,
    *,
    cooldown_days: int = LAUNCH_NUDGE_COOLDOWN_DAYS,
) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT tenant_id, detail, created_at,
               datetime(created_at) >= datetime('now', ?) AS cooling_down
          FROM audit_log
         WHERE action = 'launch.nudge_sent'
           AND tenant_id IS NOT NULL
         ORDER BY id DESC
        """,
        (f"-{int(cooldown_days)} days",),
    ).fetchall()
    activity: dict[str, dict] = {}
    for row in rows:
        if row["tenant_id"] in activity:
            continue
        activity[row["tenant_id"]] = {
            "last_nudged_at": row["created_at"],
            "last_nudged_to": row["detail"] or "",
            "nudge_cooling_down": bool(row["cooling_down"]),
        }
    return activity


def _launch_digest_recipient(settings: Settings) -> str:
    return (settings.smtp_from or settings.smtp_user or "").strip()


def _launch_digest_recent(conn: sqlite3.Connection, *, cooldown_days: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
          FROM audit_log
         WHERE action = 'launch.digest_sent'
           AND datetime(created_at) >= datetime('now', ?)
         LIMIT 1
        """,
        (f"-{max(0, int(cooldown_days))} days",),
    ).fetchone()
    return bool(row)


def _operating_checklist(
    studios: list[dict],
    *,
    nudge_activity: dict[str, dict],
    interest: dict | None = None,
) -> list[dict]:
    total = len(studios)
    verified = sum(1 for studio in studios if studio["owner_verified"])
    presets_started = sum(1 for studio in studios if studio["activation_done"] > 0)
    sourced = sum(1 for studio in studios if studio["signup_source"] in ("pricing", "demo"))
    trialing_or_active = sum(
        1 for studio in studios if studio["trial_state"] in ("trialing", "active")
    )
    at_risk = [s for s in studios if s["risk"] in ("high", "medium")]
    ready_to_contact = [
        s for s in studios
        if s["owner_email"] and _contact_bucket(s, nudge_activity) in (
            "Never nudged",
            "Ready after cooldown",
        )
    ]
    activated_not_trialing = [
        s for s in studios
        if s["trial_state"] == "ready" and s["activation_percent"] >= 50
    ]
    interest_total = int((interest or {}).get("open_total") or 0)

    candidates: list[dict] = []
    if interest_total:
        candidates.append(_operator_task(
            96 + min(interest_total, 10),
            "Review beta interest leads",
            f"{interest_total} interested photographer{'s' if interest_total != 1 else ''} ready to invite.",
            "/admin/launch",
            "acquisition",
        ))
    if at_risk and ready_to_contact:
        overlap = [s for s in at_risk if s in ready_to_contact]
        count = len(overlap) or min(len(at_risk), len(ready_to_contact))
        candidates.append(_operator_task(
            100 + count,
            "Nudge at-risk studios",
            f"{count} studio{'s' if count != 1 else ''} can be contacted now.",
            "/admin/launch",
            "outreach",
        ))
    if verified < 3:
        gap = 3 - verified
        candidates.append(_operator_task(
            90 + gap,
            "Verify owner emails",
            f"{gap} more verified owner{'s' if gap != 1 else ''} gets the beta to signal.",
            "/admin/trials",
            "activation",
        ))
    if trialing_or_active == 0 and activated_not_trialing:
        candidates.append(_operator_task(
            86 + len(activated_not_trialing),
            "Start the first hosted trial",
            "At least one studio has enough setup progress to start the 14-day trial.",
            "/admin/trials",
            "billing",
        ))
    if sourced < 2:
        gap = 2 - sourced
        candidates.append(_operator_task(
            80 + gap,
            "Push pricing and demo links",
            f"Need {gap} more pricing/demo-sourced signup{'s' if gap != 1 else ''}.",
            "/admin/launch",
            "acquisition",
        ))
    if presets_started < 3:
        gap = 3 - presets_started
        candidates.append(_operator_task(
            70 + gap,
            "Get niche presets installed",
            f"{gap} more studio{'s' if gap != 1 else ''} should start wedding, food, or real-estate setup.",
            "/admin/trials",
            "activation",
        ))
    if total < BETA_TARGET_STUDIOS:
        gap = BETA_TARGET_STUDIOS - total
        candidates.append(_operator_task(
            60 + gap,
            "Invite more beta studios",
            f"{gap} invite{'s' if gap != 1 else ''} left to fill the first cohort.",
            "/admin/launch",
            "acquisition",
        ))
    if not candidates:
        candidates.extend([
            _operator_task(
                30,
                "Review trial cockpit",
                "Scan conversion risk and keep activated studios moving toward paid.",
                "/admin/trials",
                "operator",
            ),
            _operator_task(
                20,
                "Refresh invite links",
                "Share the strongest pricing or niche demo link with the next qualified photographer.",
                "/admin/launch",
                "acquisition",
            ),
            _operator_task(
                10,
                "Keep the cohort warm",
                "Check contact freshness before sending another founder nudge.",
                "/admin/launch",
                "retention",
            ),
        ])
    candidates.sort(key=lambda item: (-item["score"], item["label"]))
    for index, item in enumerate(candidates[:3], start=1):
        item["rank"] = index
    return candidates[:3]


def _operator_task(score: int, label: str, detail: str, href: str, theme: str) -> dict:
    return {
        "score": score,
        "label": label,
        "detail": detail,
        "href": href,
        "theme": theme,
    }


def _cohort_summary(
    studios: list[dict],
    *,
    nudge_activity: dict[str, dict],
) -> dict:
    total = len(studios)
    return {
        "window": "Last 7 days",
        "pulse": [
            _metric("New signups", sum(1 for s in studios if _is_recent(s["created_at"]))),
            _metric(
                "Pricing/demo signups",
                sum(
                    1 for s in studios
                    if s["signup_source"] in ("pricing", "demo") and _is_recent(s["created_at"])
                ),
            ),
            _metric(
                "Nudged",
                sum(1 for a in nudge_activity.values() if _is_recent(a["last_nudged_at"])),
            ),
            _metric(
                "Trialing or active",
                sum(1 for s in studios if s["trial_state"] in ("trialing", "active")),
            ),
        ],
        "sources": _group_counts(
            studios,
            lambda s: s["signup_source_label"],
            total=total,
        ),
        "risks": _group_counts(
            studios,
            lambda s: s["risk"].title(),
            total=total,
            order=["High", "Medium", "Watch", "Low"],
        ),
        "trial_states": _group_counts(
            studios,
            lambda s: _trial_bucket(s["trial_state"]),
            total=total,
            order=["Trial ready", "Trialing", "Paid active", "Past due", "Canceled"],
        ),
        "contact": _group_counts(
            studios,
            lambda s: _contact_bucket(s, nudge_activity),
            total=total,
            order=["Never nudged", "Cooling down", "Ready after cooldown", "No owner email"],
        ),
    }


def _revenue_pipeline(
    conn: sqlite3.Connection,
    settings: Settings,
    studios: list[dict],
    *,
    interest: dict,
) -> dict:
    rows = conn.execute(
        """
        SELECT status, tenant_id, invited_at
          FROM beta_interests
        """
    ).fetchall()
    converted_tenant_ids = {
        row["tenant_id"]
        for row in rows
        if (row["tenant_id"] or "").strip()
    }
    direct_studios = [
        studio for studio in studios
        if studio["tenant_id"] not in converted_tenant_ids
    ]
    direct_count = len(direct_studios)
    prospect_count = int(interest["total"]) + direct_count
    uninvited_count = sum(
        1 for row in rows
        if (row["status"] or "").strip().lower() != "converted"
        and not (row["invited_at"] or "").strip()
    )
    invited_count = (
        sum(
            1 for row in rows
            if (row["invited_at"] or "").strip()
            or (row["status"] or "").strip().lower() == "converted"
        )
        + direct_count
    )
    created_count = len(studios)
    verified_count = sum(1 for studio in studios if studio["owner_verified"])
    preset_count = sum(1 for studio in studios if studio["activation_done"] > 0)
    trialing_count = sum(
        1 for studio in studios if studio["trial_state"] in ("trialing", "active")
    )
    paid_count = sum(1 for studio in studios if studio["trial_state"] == "active")

    stage_specs = [
        ("Interest", prospect_count, "Prospects captured from beta interest plus direct signups.",
         _pipeline_action("Share beta access links", "/admin/launch")),
        ("Invited", invited_count, _invite_detail(interest, direct_count, uninvited_count),
         _pipeline_action(_invite_action(uninvited_count), "/admin/launch")),
        ("Studio created", created_count, _created_detail(invited_count, created_count),
         _pipeline_action("Follow up invited leads", "/admin/launch")),
        ("Verified", verified_count, _stage_gap_detail(created_count, verified_count, "owner verification"),
         _pipeline_action("Verify owner emails", "/admin/trials")),
        ("Preset started", preset_count, _stage_gap_detail(verified_count, preset_count, "niche preset"),
         _pipeline_action("Install niche presets", "/admin/trials")),
        ("Trialing", trialing_count, _stage_gap_detail(preset_count, trialing_count, "trial checkout"),
         _pipeline_action("Move ready studios into trial", "/admin/trials")),
        ("Paid", paid_count, f"{_price_label(settings, paid_count)} in current flat-plan MRR.",
         _pipeline_action("Protect paid accounts", "/admin/trials")),
    ]
    stages = []
    previous = None
    for label, count, detail, action in stage_specs:
        stages.append(_pipeline_stage(label, count, previous, detail, action))
        previous = count
    bottleneck = next(
        (stage for stage in stages[1:] if stage["dropoff"] > 0),
        stages[-1],
    )
    return {
        "stages": stages,
        "bottleneck": bottleneck,
        "mrr_cents": paid_count * int(settings.flat_price_cents),
        "direct_studios": direct_count,
        "uninvited": uninvited_count,
        "paid": paid_count,
    }


def _pipeline_stage(
    label: str,
    count: int,
    previous: int | None,
    detail: str,
    action: dict,
) -> dict:
    if previous is None:
        dropoff = 0
        percent = 100
    else:
        dropoff = max(0, previous - count)
        percent = round(100 * count / max(1, previous))
    return {
        "label": label,
        "count": count,
        "dropoff": dropoff,
        "percent": percent,
        "detail": detail,
        "action": action["label"],
        "href": action["href"],
    }


def _pipeline_action(label: str, href: str) -> dict:
    return {"label": label, "href": href}


def _invite_detail(interest: dict, direct_count: int, uninvited_count: int) -> str:
    invited_total = int(interest["invited_total"])
    converted_total = int(interest["converted_total"])
    if uninvited_count:
        return (
            f"{uninvited_count} interest lead{'s' if uninvited_count != 1 else ''} "
            "still need a private invite."
        )
    return (
        f"{invited_total} invited, {converted_total} converted, "
        f"{direct_count} direct signup{'s' if direct_count != 1 else ''} bypassed invite."
    )


def _invite_action(uninvited_count: int) -> str:
    if uninvited_count:
        return f"Send {uninvited_count} private invite{'s' if uninvited_count != 1 else ''}"
    return "Follow up invited leads"


def _created_detail(invited_count: int, created_count: int) -> str:
    gap = max(0, invited_count - created_count)
    if gap:
        noun = "leads" if gap != 1 else "lead"
        verb = "have" if gap != 1 else "has"
        return f"{gap} invited {noun} {verb} not created a studio yet."
    return "Every invited or direct prospect has reached studio creation."


def _stage_gap_detail(previous: int, count: int, label: str) -> str:
    gap = max(0, previous - count)
    if gap:
        return f"{gap} studio{'s' if gap != 1 else ''} still need {label}."
    return f"No drop-off at {label}."


def _price_label(settings: Settings, paid_count: int) -> str:
    cents = paid_count * int(settings.flat_price_cents)
    if settings.currency.lower() == "usd" and cents % 100 == 0:
        return f"${cents // 100}/month"
    return f"{cents / 100:.2f} {settings.currency.upper()}/month"


def _metric(label: str, count: int) -> dict:
    return {"label": label, "count": count}


def _group_counts(
    studios: list[dict],
    label_for,
    *,
    total: int,
    order: list[str] | None = None,
) -> list[dict]:
    counts: dict[str, int] = {}
    for studio in studios:
        label = label_for(studio)
        counts[label] = counts.get(label, 0) + 1
    labels = [label for label in (order or []) if label in counts]
    labels.extend(sorted(label for label in counts if label not in labels))
    return [
        {
            "label": label,
            "count": counts[label],
            "percent": round(100 * counts[label] / max(1, total)),
        }
        for label in labels
    ]


def _trial_bucket(state: str) -> str:
    labels = {
        "ready": "Trial ready",
        "trialing": "Trialing",
        "active": "Paid active",
        "past_due": "Past due",
        "canceled": "Canceled",
    }
    return labels.get(state, state.replace("_", " ").title())


def _contact_bucket(studio: dict, nudge_activity: dict[str, dict]) -> str:
    if not studio["owner_email"]:
        return "No owner email"
    activity = nudge_activity.get(studio["tenant_id"])
    if not activity:
        return "Never nudged"
    if activity["nudge_cooling_down"]:
        return "Cooling down"
    return "Ready after cooldown"


def _is_recent(value: str | None, *, days: int = 7) -> bool:
    ts = _parse_time(value)
    if not ts:
        return False
    return ts >= datetime.now(UTC) - timedelta(days=max(0, int(days)))


def _parse_time(value: str | None) -> datetime | None:
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


def _invite_links(settings: Settings) -> list[dict]:
    base = settings.public_url.rstrip("/") or "http://127.0.0.1:8500"
    links = [
        ("Beta landing", "beta", "/beta", "/beta"),
        ("Pricing page", "pricing", "/pricing", "/signup"),
        ("Wedding demo", "demo", "/demo/wedding", "/signup"),
        ("Food & beverage demo", "demo", "/demo/food", "/signup"),
        ("Real-estate demo", "demo", "/demo/real-estate", "/signup"),
        ("Direct landing", "landing", "/", "/signup"),
    ]
    return [
        {
            "label": label,
            "source": source,
            "path": path,
            "url": f"{base}{target}?source={source}&path={path}",
        }
        for label, source, path, target in links
    ]


def _launch_operations(settings: Settings) -> dict:
    base = settings.public_url.rstrip("/") or "http://127.0.0.1:8500"
    hosted_domain = (settings.hosted_domain or "").strip()
    price = _single_price_label(settings)
    return {
        "base_url": base,
        "hosted_domain": hosted_domain,
        "readiness": [
            _readiness(
                "Public URL",
                base,
                base.startswith("https://"),
                "Use HTTPS before launch so checkout, email links, and share previews match production.",
            ),
            _readiness(
                "Wildcard domain",
                hosted_domain or "Set HESTIA_DOMAIN",
                bool(hosted_domain),
                "Point apex and wildcard DNS at the Caddy host.",
            ),
            _readiness(
                "Billing",
                settings.subscription_backend,
                settings.subscription_backend == "stripe",
                "Use Stripe subscriptions for the hosted $40/month plan.",
            ),
            _readiness(
                "Email",
                settings.email_backend,
                settings.email_backend == "smtp",
                "Use SMTP so verification, invites, nudges, and digests leave the mock outbox.",
            ),
            _readiness(
                "Plan contract",
                f"{price} after {settings.trial_days} days",
                int(settings.flat_price_cents) == 4000 and int(settings.trial_days) == 14,
                "The public offer stays exactly $40/month after a 14-day trial.",
            ),
            _readiness(
                "Storage",
                settings.storage_backend,
                settings.storage_backend in ("local", "s3"),
                "Use a persistent volume or S3/R2-backed media storage.",
            ),
        ],
        "commands": [
            _runbook_command(
                "Boot hosted stack",
                "docker compose up --build -d",
                "Build the FastAPI app behind Caddy with the mounted SQLite/media volumes.",
            ),
            _runbook_command(
                "Run hosted preflight",
                f"bash scripts/hosted-preflight.sh --url {base}",
                "Validate HTTPS URL, wildcard domain, secrets, Stripe, SMTP, volumes, Docker/Caddy, and probes.",
            ),
            _runbook_command(
                "Probe runtime",
                f"curl -fsS {base}/healthz && curl -fsS {base}/readyz",
                "Confirm the deployed service and database are reachable after boot.",
            ),
            _runbook_command(
                "Run full smoke",
                "bash scripts/ci-smoke.sh",
                "Run Ruff, pytest, and local healthz boot before announcing a launch build.",
            ),
            _runbook_command(
                "Dogfood magic moment",
                "bash scripts/dogfood-hestia.sh",
                "Create a studio, upload frames, generate an offer, and verify idempotent re-processing.",
            ),
        ],
        "links": [
            _operator_link("Public landing", f"{base}/"),
            _operator_link("Beta page", f"{base}/beta"),
            _operator_link("Pricing page", f"{base}/pricing"),
            _operator_link("Wedding demo", f"{base}/demo/wedding"),
            _operator_link("Food & beverage demo", f"{base}/demo/food"),
            _operator_link("Real-estate demo", f"{base}/demo/real-estate"),
            _operator_link("Admin launch kit", f"{base}/admin/launch"),
            _operator_link("Trial cockpit", f"{base}/admin/trials"),
        ],
    }


def _readiness(label: str, value: str, ok: bool, detail: str) -> dict:
    return {"label": label, "value": value, "ok": ok, "detail": detail}


def _runbook_command(label: str, command: str, detail: str) -> dict:
    return {"label": label, "command": command, "detail": detail}


def _operator_link(label: str, url: str) -> dict:
    return {"label": label, "url": url}


def _single_price_label(settings: Settings) -> str:
    cents = int(settings.flat_price_cents)
    if settings.currency.lower() == "usd" and cents % 100 == 0:
        return f"${cents // 100}/month"
    return f"{cents / 100:.2f} {settings.currency.upper()}/month"


def _milestone(label: str, done: int, target: int) -> dict:
    capped = min(done, target)
    return {
        "label": label,
        "done": done,
        "target": target,
        "complete": done >= target,
        "percent": round(100 * capped / max(1, target)),
    }


def _followups(
    studios: list[dict],
    *,
    limit: int = 5,
    nudge_activity: dict[str, dict] | None = None,
) -> list[dict]:
    candidates = [
        studio for studio in studios
        if studio["risk"] in ("high", "medium")
        or studio["trial_state"] == "trialing"
        or not studio["setup_complete"]
    ]
    candidates.sort(key=lambda s: (
        s["risk_rank"],
        99 if s["trial_days_left"] is None else s["trial_days_left"],
        -s["activation_percent"],
        s["created_at"],
    ))
    return [
        _followup(studio, nudge_activity=nudge_activity)
        for studio in candidates[:limit]
    ]


def _followup(
    studio: dict,
    *,
    nudge_activity: dict[str, dict] | None = None,
) -> dict:
    prompt = _prompt(studio)
    subject = f"Next Hestia step for {studio['name']}"
    body = (
        f"Hi,\n\nI noticed {studio['name']} is at "
        f"{studio['activation_done']}/{studio['activation_total']} launch steps in Hestia. "
        f"{prompt}\n\nKevin"
    )
    mailto = ""
    if studio["owner_email"]:
        subject_q = quote(subject, safe="")
        body_q = quote(body, safe="")
        mailto = f"mailto:{studio['owner_email']}?subject={subject_q}&body={body_q}"
    activity = (nudge_activity or {}).get(studio["tenant_id"], {})
    last_nudged_at = activity.get("last_nudged_at", "")
    nudge_cooling_down = bool(activity.get("nudge_cooling_down"))
    nudge_available = bool(studio["owner_email"]) and not nudge_cooling_down
    if not studio["owner_email"]:
        nudge_status = "No owner email"
    elif nudge_cooling_down:
        nudge_status = f"Cooling down {LAUNCH_NUDGE_COOLDOWN_DAYS} days"
    else:
        nudge_status = "Ready to nudge"
    return {
        "tenant_id": studio["tenant_id"],
        "name": studio["name"],
        "owner_email": studio["owner_email"],
        "risk": studio["risk"],
        "risk_reason": studio["risk_reason"],
        "activation": f"{studio['activation_done']}/{studio['activation_total']}",
        "source": studio["signup_source_label"],
        "next_action": studio["next_action"],
        "owner_path": studio["next_href"],
        "prompt": prompt,
        "mailto": mailto,
        "email_subject": subject,
        "email_body": body,
        "last_nudged_at": last_nudged_at,
        "last_nudged_to": activity.get("last_nudged_to", ""),
        "nudge_available": nudge_available,
        "nudge_cooling_down": nudge_cooling_down,
        "nudge_status": nudge_status,
    }


def _prompt(studio: dict) -> str:
    if not studio["owner_verified"]:
        return "Can I help you verify your owner email and unlock onboarding?"
    if studio["trial_state"] == "trialing" and (studio["trial_days_left"] or 0) <= 3:
        return "Your trial is close to ending; want help finishing the launch path?"
    if studio["trial_state"] == "ready" and studio["activation_percent"] >= 50:
        return "You are close to launch; want to start the 14-day trial together?"
    if studio.get("setup_next"):
        return f"The next useful step is: {studio['setup_next']['label']}."
    return f"The next useful step is: {studio['next_action']}."
