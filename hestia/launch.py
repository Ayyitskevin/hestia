"""Operator launch kit for the hosted $40/month beta."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from .config import Settings
from .email import notify
from .interest import beta_interest_summary
from .tenants import get_tenant
from .trial_conversion import trial_conversion_cockpit, trial_conversion_for_tenant

BETA_TARGET_STUDIOS = 5
LAUNCH_NUDGE_COOLDOWN_DAYS = 3


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
        "followups": _followups(studios, nudge_activity=nudge_activity),
        "cohort": _cohort_summary(studios, nudge_activity=nudge_activity),
        "interest": interest,
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
        ("Pricing page", "pricing", "/pricing"),
        ("Wedding demo", "demo", "/demo/wedding"),
        ("Food & beverage demo", "demo", "/demo/food"),
        ("Real-estate demo", "demo", "/demo/real-estate"),
        ("Direct landing", "landing", "/"),
    ]
    return [
        {
            "label": label,
            "source": source,
            "path": path,
            "url": f"{base}/signup?source={source}&path={path}",
        }
        for label, source, path in links
    ]


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
