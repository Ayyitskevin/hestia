"""Operator launch kit for the hosted $40/month beta."""

from __future__ import annotations

import sqlite3
from urllib.parse import quote

from .config import Settings
from .email import notify
from .tenants import get_tenant
from .trial_conversion import trial_conversion_cockpit, trial_conversion_for_tenant

BETA_TARGET_STUDIOS = 5


def beta_launch_kit(conn: sqlite3.Connection, settings: Settings) -> dict:
    cockpit = trial_conversion_cockpit(conn, settings)
    studios = cockpit["studios"]
    verified = sum(1 for studio in studios if studio["owner_verified"])
    presets_started = sum(1 for studio in studios if studio["activation_done"] > 0)
    sourced = sum(1 for studio in studios if studio["signup_source"] in ("pricing", "demo"))
    trialing_or_active = sum(
        1 for studio in studios if studio["trial_state"] in ("trialing", "active")
    )
    return {
        "target": BETA_TARGET_STUDIOS,
        "invite_links": _invite_links(settings),
        "followups": _followups(studios),
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
    followups = {f["tenant_id"]: f for f in _followups(cockpit["studios"], limit=999)}
    rows = []
    for studio in cockpit["studios"]:
        followup = followups.get(studio["tenant_id"]) or _followup(studio)
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
    followup = _followup(studio)
    if not followup["owner_email"]:
        return None
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


def _followups(studios: list[dict], *, limit: int = 5) -> list[dict]:
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
    return [_followup(studio) for studio in candidates[:limit]]


def _followup(studio: dict) -> dict:
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
