"""Operator-facing trial conversion read model.

This is deliberately derived from existing tenant data, not a separate state
machine. The owner dashboard already knows the activation path; this module gives
the operator a cross-studio view of that same path plus billing/trial state.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from .config import Settings
from .dashboard import setup_checklist
from .presets import preset_applied
from .studio import get_profile
from .subscriptions import get_subscription
from .tenants import list_tenants


def trial_conversion_cockpit(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    limit: int = 100,
) -> dict:
    studios = [
        trial_conversion_for_tenant(conn, tenant, settings)
        for tenant in list_tenants(conn)[: max(1, int(limit))]
    ]
    studios.sort(key=lambda s: (s["risk_rank"], s["activation_percent"], s["created_at"]))
    source_counts = _source_counts(studios)
    return {
        "studios": studios,
        "summary": {
            "total": len(studios),
            "trial_ready": sum(1 for s in studios if s["trial_state"] == "ready"),
            "trialing": sum(1 for s in studios if s["trial_state"] == "trialing"),
            "active": sum(1 for s in studios if s["trial_state"] == "active"),
            "stalled": sum(1 for s in studios if s["risk"] in ("high", "medium")),
            "pricing_signups": sum(1 for s in studios if s["signup_source"] == "pricing"),
            "demo_signups": sum(1 for s in studios if s["signup_source"] == "demo"),
            "source_breakdown": source_counts,
        },
    }


def trial_conversion_for_tenant(
    conn: sqlite3.Connection,
    tenant: dict,
    settings: Settings,
) -> dict:
    tenant_id = tenant["id"]
    owner = _owner(conn, tenant_id)
    subscription = get_subscription(conn, tenant_id)
    profile = get_profile(conn, tenant_id)
    setup = setup_checklist(conn, tenant_id, published=bool(profile.get("published")))
    signals = _signals(conn, tenant_id)
    trial_state = _trial_state(tenant, subscription)
    days_left = _trial_days_left(subscription, settings) if trial_state == "trialing" else None
    verified = bool(owner.get("verified"))
    has_preset = preset_applied(conn, tenant_id)
    action = _operator_action(
        verified=verified,
        has_preset=has_preset,
        trial_state=trial_state,
        setup=setup,
    )
    action["href"] = action["href"].format(tenant_id=tenant_id)
    risk = _risk(
        verified=verified,
        trial_state=trial_state,
        days_left=days_left,
        days_since_signup=_days_since(tenant.get("created_at")),
        setup=setup,
    )
    total = max(1, int(setup["total"]))
    done = int(setup["done"])
    return {
        "tenant_id": tenant_id,
        "name": tenant["name"],
        "slug": tenant["slug"],
        "shoot_type": tenant.get("shoot_type") or "other",
        "plan": tenant.get("plan") or "beta",
        "created_at": tenant.get("created_at") or "",
        "signup_source": tenant.get("signup_source") or "",
        "signup_source_label": _source_label(tenant.get("signup_source")),
        "signup_landing_path": tenant.get("signup_landing_path") or "",
        "owner_email": owner.get("email") or "",
        "owner_verified": verified,
        "trial_state": trial_state,
        "trial_label": _trial_label(trial_state, days_left),
        "trial_days_left": days_left,
        "activation_done": done,
        "activation_total": total,
        "activation_percent": round(100 * done / total),
        "next_action": action["label"],
        "next_href": action["href"],
        "risk": risk["label"],
        "risk_rank": risk["rank"],
        "risk_reason": risk["reason"],
        "setup_complete": bool(setup["complete"]),
        "setup_next": setup.get("next"),
        **signals,
    }


def beta_conversion_timeline(
    conn: sqlite3.Connection,
    tenant: dict,
    settings: Settings,
    *,
    conversion: dict | None = None,
    nudge_limit: int = 5,
) -> dict:
    """Operator timeline for one hosted beta studio.

    Derived from existing state only: tenant signup metadata, owner verification,
    setup checklist, subscription state, and launch-nudge audit rows.
    """
    summary = conversion or trial_conversion_for_tenant(conn, tenant, settings)
    tenant_id = tenant["id"]
    has_preset = preset_applied(conn, tenant_id)
    source_detail = summary["signup_source_label"]
    if summary.get("signup_landing_path"):
        source_detail = f"{source_detail} from {summary['signup_landing_path']}"
    else:
        source_detail = f"{source_detail} signup"

    items = [
        _timeline_item(
            "done",
            "Signup captured",
            source_detail,
            at=summary.get("created_at") or "",
        )
    ]
    if summary["owner_verified"]:
        items.append(_timeline_item(
            "done",
            "Owner email verified",
            summary["owner_email"] or "Owner account is verified.",
        ))
    else:
        items.append(_timeline_item(
            "error",
            "Owner email not verified",
            summary["owner_email"] or "No owner email is available.",
            href="/admin/launch",
        ))

    if has_preset:
        items.append(_timeline_item(
            "done",
            "Niche preset installed",
            f"{summary['activation_done']}/{summary['activation_total']} activation steps complete.",
        ))
    else:
        items.append(_timeline_item(
            "skipped",
            "Niche preset not started",
            "Send the owner to onboarding before asking them to start the trial.",
            href="/onboarding",
        ))

    setup_detail = (
        f"{summary['activation_done']}/{summary['activation_total']} steps complete"
        f" ({summary['activation_percent']}%)."
    )
    if summary.get("setup_next"):
        setup_detail = f"{setup_detail} Next: {summary['setup_next']['label']}."
    items.append(_timeline_item(
        "done" if summary["setup_complete"] else "running",
        "Activation path complete" if summary["setup_complete"] else "Activation in progress",
        setup_detail,
        href=summary.get("next_href") or "",
    ))

    items.append(_timeline_item(
        _billing_status(summary["trial_state"]),
        _billing_title(summary["trial_state"]),
        f"{summary['trial_label']} on the flat {_price_label(settings)} plan.",
        href="/settings/billing",
    ))

    for row in _launch_nudge_rows(conn, tenant_id, limit=nudge_limit):
        sent = row["action"] == "launch.nudge_sent"
        items.append(_timeline_item(
            "done" if sent else "skipped",
            "Launch nudge sent" if sent else "Launch nudge skipped",
            _nudge_detail(row),
            at=row["created_at"],
        ))

    items.append(_timeline_item(
        "running",
        "Next operator action",
        summary["next_action"],
        href=summary["next_href"],
    ))
    return {
        "steps": items,
        "nudge_count": sum(1 for item in items if item["title"].startswith("Launch nudge")),
        "next_action": summary["next_action"],
    }


def _source_counts(studios: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for studio in studios:
        label = studio["signup_source_label"]
        counts[label] = counts.get(label, 0) + 1
    return [
        {"label": label, "count": count}
        for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _source_label(source: str | None) -> str:
    labels = {
        "landing": "Landing",
        "pricing": "Pricing",
        "demo": "Demo",
        "beta": "Beta page",
        "interest": "Beta interest",
    }
    return labels.get((source or "").strip().lower(), "Direct / unknown")


def _price_label(settings: Settings) -> str:
    cents = int(settings.flat_price_cents)
    if settings.currency.lower() == "usd" and cents % 100 == 0:
        return f"${cents // 100}/month"
    return f"{cents / 100:.2f} {settings.currency.upper()}/month"


def _billing_status(trial_state: str) -> str:
    if trial_state == "active":
        return "done"
    if trial_state in ("canceled", "past_due"):
        return "error"
    return "running"


def _billing_title(trial_state: str) -> str:
    if trial_state == "ready":
        return "Trial ready"
    if trial_state == "trialing":
        return "Trial active"
    if trial_state == "past_due":
        return "Billing past due"
    if trial_state == "canceled":
        return "Billing canceled"
    return "Paid plan active"


def _timeline_item(
    status: str,
    title: str,
    detail: str,
    *,
    at: str = "",
    href: str = "",
) -> dict:
    labels = {
        "done": "Done",
        "running": "Active",
        "error": "Needs attention",
        "skipped": "Not started",
    }
    return {
        "status": status,
        "status_label": labels.get(status, status.title()),
        "title": title,
        "detail": detail,
        "at": at,
        "href": href,
    }


def _launch_nudge_rows(
    conn: sqlite3.Connection,
    tenant_id: str,
    *,
    limit: int,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT action, detail, created_at
          FROM audit_log
         WHERE tenant_id = ?
           AND action IN ('launch.nudge_sent', 'launch.nudge_skipped')
         ORDER BY id DESC
         LIMIT ?
        """,
        (tenant_id, max(1, int(limit))),
    ).fetchall()
    return [dict(row) for row in rows]


def _nudge_detail(row: dict) -> str:
    detail = (row.get("detail") or "").strip()
    if detail.startswith("cooldown:"):
        return f"Cooldown protected {detail.removeprefix('cooldown:')}."
    if detail:
        return f"Sent to {detail}."
    return "Recorded through the Hestia email seam."


def _owner(conn: sqlite3.Connection, tenant_id: str) -> dict:
    row = conn.execute(
        "SELECT email, verified FROM users WHERE tenant_id = ? AND role = 'owner' "
        "ORDER BY id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    if not row:
        return {"email": "", "verified": 0}
    return dict(row)


def _signals(conn: sqlite3.Connection, tenant_id: str) -> dict:
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM proposals
              WHERE tenant_id = ? AND status IN ('sent', 'accepted')) AS proposals_sent,
            (SELECT COUNT(*) FROM proposals
              WHERE tenant_id = ? AND status = 'accepted') AS proposals_accepted,
            (SELECT COALESCE(SUM(view_count), 0) FROM proposals
              WHERE tenant_id = ?) AS proposal_views,
            (SELECT COUNT(*) FROM galleries
              WHERE tenant_id = ? AND status = 'published') AS published_galleries,
            (SELECT COUNT(*) FROM offers
              WHERE tenant_id = ? AND status = 'active') AS active_offers,
            (SELECT COUNT(*) FROM invoices
              WHERE tenant_id = ? AND status IN ('sent', 'paid')) AS money_links,
            (SELECT COUNT(*) FROM booking_types
              WHERE tenant_id = ? AND active = 1) AS active_booking_types,
            (SELECT COUNT(*) FROM service_packages
              WHERE tenant_id = ? AND active = 1) AS active_packages
        """,
        (tenant_id, tenant_id, tenant_id, tenant_id, tenant_id, tenant_id, tenant_id, tenant_id),
    ).fetchone()
    return {k: int(row[k] or 0) for k in row.keys()}


def _trial_state(tenant: dict, subscription: dict | None) -> str:
    status = ((subscription or {}).get("status") or "").strip().lower()
    plan = tenant.get("plan") or "beta"
    if plan == "studio_pro":
        plan = "studio"
    if plan != "studio":
        return "ready"
    if status == "trialing":
        return "trialing"
    if status in ("canceled", "past_due"):
        return status
    return "active"


def _trial_label(state: str, days_left: int | None) -> str:
    if state == "ready":
        return "Trial ready"
    if state == "trialing":
        return f"Trial active · {days_left}d left" if days_left is not None else "Trial active"
    if state == "past_due":
        return "Past due"
    if state == "canceled":
        return "Canceled"
    return "Paid active"


def _operator_action(
    *,
    verified: bool,
    has_preset: bool,
    trial_state: str,
    setup: dict,
) -> dict:
    if not verified:
        return {"label": "Verify owner email", "href": "/admin/tenants/{tenant_id}"}
    if not has_preset:
        return {"label": "Install niche preset", "href": "/onboarding"}
    if trial_state == "ready":
        return {"label": "Start trial checkout", "href": "/settings/billing"}
    if not setup["complete"]:
        nxt = setup.get("next") or {}
        return {
            "label": f"Next: {nxt.get('label', 'finish activation')}",
            "href": nxt.get("href") or "/dashboard",
        }
    if trial_state == "trialing":
        return {"label": "Convert trial", "href": "/settings/billing"}
    if trial_state in ("canceled", "past_due"):
        return {"label": "Recover billing", "href": "/settings/billing"}
    return {"label": "Active account", "href": "/dashboard"}


def _risk(
    *,
    verified: bool,
    trial_state: str,
    days_left: int | None,
    days_since_signup: int,
    setup: dict,
) -> dict:
    if not verified:
        return {"label": "high", "rank": 0, "reason": "owner email is unverified"}
    if int(setup["done"]) == 0 and days_since_signup >= 3:
        return {"label": "high", "rank": 0, "reason": "no activation progress after 3 days"}
    if trial_state == "trialing" and (days_left or 0) <= 3 and not setup["complete"]:
        return {"label": "medium", "rank": 1, "reason": "trial ending before setup is complete"}
    if days_since_signup >= 7 and not setup["complete"]:
        return {"label": "medium", "rank": 1, "reason": "setup is still incomplete after a week"}
    if setup["complete"]:
        return {"label": "low", "rank": 3, "reason": "activation path is complete"}
    return {"label": "watch", "rank": 2, "reason": "activation is in progress"}


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


def _days_since(value: str | None) -> int:
    started = _parse_time(value)
    if not started:
        return 0
    delta = datetime.now(UTC) - started
    return max(0, delta.days)


def _trial_days_left(subscription: dict | None, settings: Settings) -> int:
    started = _parse_time((subscription or {}).get("created_at"))
    trial_days = max(0, int(settings.trial_days))
    if not started:
        return trial_days
    remaining = (started + timedelta(days=trial_days)) - datetime.now(UTC)
    if remaining.total_seconds() <= 0:
        return 0
    return max(1, remaining.days + (1 if remaining.seconds or remaining.microseconds else 0))
