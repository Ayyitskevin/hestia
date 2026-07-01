"""Sales campaigns — a time-limited, urgency-gated sale on a gallery's offer.

A campaign carries a headline, a discount, and a deadline. While it's active the
public offer applies the discount *live at render* (the idempotent offer token is
never touched) and shows the deadline for urgency. One active campaign per
gallery — launching a new one ends the prior. Tenant-scoped throughout.
"""

from __future__ import annotations

import sqlite3

from . import messaging
from .config import Settings
from .db import audit
from .email import notify
from .ownership import owned_gallery_id
from .sales import get_offer_for_gallery, offer_public_url

MAX_DISCOUNT_PCT = 90
DEFAULT_CAMPAIGN_DISCOUNT = 15
DEFAULT_CAMPAIGN_DAYS = 7
DEFAULT_CAMPAIGN_HEADLINE = "Your gallery print sale"
EMAIL_SENT_ACTION = "campaign.email_sent"
EMAIL_SKIPPED_ACTION = "campaign.email_skipped"


def create_campaign(
    conn: sqlite3.Connection, *, tenant_id: str, gallery_id: int,
    headline: str, discount_pct: int, days: int,
) -> dict | None:
    """Launch a sale, ending any campaign already active on this gallery."""
    if owned_gallery_id(conn, tenant_id, gallery_id) is None:
        return None
    conn.execute(
        "UPDATE sales_campaigns SET status = 'ended' "
        "WHERE gallery_id = ? AND tenant_id = ? AND status = 'active'",
        (gallery_id, tenant_id),
    )
    pct = max(0, min(MAX_DISCOUNT_PCT, int(discount_pct)))
    span = max(1, int(days))
    cur = conn.execute(
        "INSERT INTO sales_campaigns (tenant_id, gallery_id, headline, discount_pct, ends_at) "
        "VALUES (?, ?, ?, ?, datetime('now', ?))",
        (tenant_id, gallery_id, headline.strip(), pct, f"+{span} days"),
    )
    audit(conn, actor="owner", action="campaign.launched", tenant_id=tenant_id,
          detail=f"gallery #{gallery_id} · {pct}% off · {span}d")
    return get_campaign(conn, tenant_id, cur.lastrowid)


def get_campaign(conn: sqlite3.Connection, tenant_id: str, campaign_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM sales_campaigns WHERE id = ? AND tenant_id = ?", (campaign_id, tenant_id)
    ).fetchone()
    return dict(row) if row else None


def get_active_campaign(
    conn: sqlite3.Connection,
    gallery_id: int,
    *,
    tenant_id: str | None = None,
) -> dict | None:
    """The live campaign for a gallery (active and not past its deadline), if any."""
    params: list = [gallery_id]
    tenant_filter = ""
    if tenant_id is not None:
        tenant_filter = "AND c.tenant_id = ? "
        params.append(tenant_id)
    row = conn.execute(
        "SELECT c.* FROM sales_campaigns c "
        "JOIN galleries g ON g.id = c.gallery_id AND g.tenant_id = c.tenant_id "
        "WHERE c.gallery_id = ? AND c.status = 'active' "
        f"{tenant_filter}"
        "AND c.ends_at > datetime('now') ORDER BY c.id DESC LIMIT 1",
        params,
    ).fetchone()
    return dict(row) if row else None


def end_campaign(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> None:
    conn.execute(
        "UPDATE sales_campaigns SET status = 'ended' "
        "WHERE gallery_id = ? AND tenant_id = ? AND status = 'active'",
        (gallery_id, tenant_id),
    )


def _recent_campaign_email(
    conn: sqlite3.Connection,
    tenant_id: str,
    gallery_id: int,
    *,
    cooldown_days: int,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM audit_log WHERE tenant_id = ? AND action = ? "
        "AND detail LIKE ? AND created_at > datetime('now', ?) LIMIT 1",
        (tenant_id, EMAIL_SENT_ACTION, f"gallery #{gallery_id} %", f"-{int(cooldown_days)} days"),
    ).fetchone()
    return row is not None


def _client_for_gallery(conn: sqlite3.Connection, tenant_id: str, gallery: dict) -> dict | None:
    if not gallery.get("project_id"):
        return None
    row = conn.execute(
        "SELECT c.* FROM projects p "
        "JOIN clients c ON c.id = p.client_id AND c.tenant_id = p.tenant_id "
        "WHERE p.id = ? AND p.tenant_id = ?",
        (gallery["project_id"], tenant_id),
    ).fetchone()
    return dict(row) if row else None


def _raw_opportunities(conn: sqlite3.Connection, tenant_id: str, *, gallery_id: int | None = None) -> list[dict]:
    sql = (
        "SELECT g.*, o.id AS offer_id, o.token AS offer_token, "
        "       c.id AS client_id, c.name AS client_name, c.email AS client_email, "
        "       COALESCE((SELECT COUNT(*) FROM image_favorites f "
        "                  WHERE f.tenant_id = g.tenant_id AND f.gallery_id = g.id), 0) AS favorite_count, "
        "       COALESCE((SELECT COUNT(*) FROM orders ord "
        "                  WHERE ord.tenant_id = g.tenant_id AND ord.gallery_id = g.id "
        "                    AND ord.status IN ('pending', 'paid')), 0) AS order_count "
        "FROM galleries g "
        "LEFT JOIN offers o ON o.gallery_id = g.id AND o.tenant_id = g.tenant_id AND o.status = 'active' "
        "LEFT JOIN projects p ON p.id = g.project_id AND p.tenant_id = g.tenant_id "
        "LEFT JOIN clients c ON c.id = p.client_id AND c.tenant_id = p.tenant_id "
        "WHERE g.tenant_id = ? AND g.status = 'published'"
    )
    params: list = [tenant_id]
    if gallery_id is not None:
        sql += " AND g.id = ?"
        params.append(gallery_id)
    sql += " ORDER BY g.published_at DESC, g.id DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _opportunity_state(
    conn: sqlite3.Connection,
    row: dict,
    *,
    cooldown_days: int,
) -> dict:
    campaign = get_active_campaign(conn, row["id"], tenant_id=row["tenant_id"])
    recent = _recent_campaign_email(conn, row["tenant_id"], row["id"], cooldown_days=cooldown_days)
    reasons = []
    score = 20

    if row.get("delivery_token"):
        score += 20
        reasons.append("Delivered")
    if int(row.get("view_count") or 0):
        score += 10
        reasons.append(f"{row['view_count']} view{'' if row['view_count'] == 1 else 's'}")
    if int(row.get("download_count") or 0):
        score += 15
        reasons.append(f"{row['download_count']} download{'' if row['download_count'] == 1 else 's'}")
    if int(row.get("favorite_count") or 0):
        score += 20
        reasons.append(f"{row['favorite_count']} favorite{'' if row['favorite_count'] == 1 else 's'}")
    if row.get("selections_submitted_at"):
        score += 15
        reasons.append("Selections submitted")
    if row.get("offer_token"):
        score += 10
        reasons.append("Offer ready")

    if int(row.get("order_count") or 0):
        status = "sold"
        status_label = "Already ordered"
        next_action = "Review order"
    elif campaign:
        status = "active"
        status_label = "Sale active"
        next_action = "Watch campaign"
    elif not row.get("offer_token"):
        status = "process"
        status_label = "Needs offer"
        next_action = "Process gallery"
    elif not row.get("delivery_token"):
        status = "deliver"
        status_label = "Needs delivery"
        next_action = "Enable delivery"
    elif not (row.get("client_email") or "").strip():
        status = "email"
        status_label = "Needs client email"
        next_action = "Add client email"
    elif recent:
        status = "cooldown"
        status_label = "Recently emailed"
        next_action = "Wait for cooldown"
    else:
        status = "ready"
        status_label = "Ready to sell"
        next_action = "Launch sale"

    out = dict(row)
    out.update({
        "score": min(100, score),
        "status": status,
        "status_label": status_label,
        "next_action": next_action,
        "reason_line": " · ".join(reasons[:4]) or "Published gallery",
        "href": f"/galleries/{row['id']}",
        "campaign": campaign,
        "recently_emailed": recent,
    })
    return out


def gallery_sales_opportunity(
    conn: sqlite3.Connection,
    tenant_id: str,
    gallery_id: int,
    *,
    cooldown_days: int = 14,
) -> dict | None:
    rows = _raw_opportunities(conn, tenant_id, gallery_id=gallery_id)
    if not rows:
        return None
    return _opportunity_state(conn, rows[0], cooldown_days=cooldown_days)


def gallery_sales_opportunities(
    conn: sqlite3.Connection,
    tenant_id: str,
    *,
    limit: int = 5,
    ready_only: bool = False,
    cooldown_days: int = 14,
) -> list[dict]:
    rows = [
        _opportunity_state(conn, row, cooldown_days=cooldown_days)
        for row in _raw_opportunities(conn, tenant_id)
    ]
    if ready_only:
        rows = [row for row in rows if row["status"] == "ready"]
    rows.sort(key=lambda row: (row["status"] == "ready", row["score"], row.get("published_at") or ""),
              reverse=True)
    return rows[: max(0, int(limit))]


def launch_gallery_sales_campaign(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    tenant: dict,
    gallery_id: int,
    headline: str = "",
    discount_pct: int = DEFAULT_CAMPAIGN_DISCOUNT,
    days: int = DEFAULT_CAMPAIGN_DAYS,
    source: str = "manual",
    cooldown_days: int = 14,
    require_ready: bool = False,
) -> dict:
    gallery_row = conn.execute(
        "SELECT * FROM galleries WHERE id = ? AND tenant_id = ?", (gallery_id, tenant["id"])
    ).fetchone()
    if not gallery_row:
        return {"sent": False, "status": "missing_gallery"}
    gallery = dict(gallery_row)
    opportunity = gallery_sales_opportunity(conn, tenant["id"], gallery_id, cooldown_days=cooldown_days)
    if require_ready and (not opportunity or opportunity["status"] != "ready"):
        return {"sent": False, "status": (opportunity or {}).get("status") or "not_ready"}
    if opportunity and opportunity["status"] in ("sold", "active"):
        return {"sent": False, "status": opportunity["status"], "opportunity": opportunity}

    offer = get_offer_for_gallery(conn, tenant["id"], gallery_id)
    client = _client_for_gallery(conn, tenant["id"], gallery)
    if not offer:
        return {"sent": False, "status": "missing_offer", "opportunity": opportunity}
    if not client or not (client.get("email") or "").strip():
        return {"sent": False, "status": "missing_client_email", "opportunity": opportunity}

    pct = max(0, min(MAX_DISCOUNT_PCT, int(discount_pct)))
    span = max(1, int(days))
    title = (headline or "").strip() or DEFAULT_CAMPAIGN_HEADLINE
    campaign = create_campaign(
        conn,
        tenant_id=tenant["id"],
        gallery_id=gallery_id,
        headline=title,
        discount_pct=pct,
        days=span,
    )

    if _recent_campaign_email(conn, tenant["id"], gallery_id, cooldown_days=cooldown_days):
        audit(conn, actor="system", action=EMAIL_SKIPPED_ACTION, tenant_id=tenant["id"],
              detail=f"gallery #{gallery_id} · cooldown · {source}")
        return {"sent": False, "status": "cooldown", "campaign": campaign, "opportunity": opportunity}

    url = offer_public_url(settings, tenant["slug"], offer["token"])
    ctx = {
        "client": client["name"],
        "studio": tenant.get("name", "your photographer"),
        "discount": pct,
        "headline": title,
        "offer_url": url,
    }
    msg = messaging.render(conn, tenant["id"], "print_offer", ctx)
    email_status = notify(conn, settings, to=client["email"], tenant_id=tenant["id"],
                          subject=msg["subject"], body=msg["body"])
    audit(conn, actor="system", action=EMAIL_SENT_ACTION, tenant_id=tenant["id"],
          detail=f"gallery #{gallery_id} · {client['email']} · {source}")
    return {
        "sent": True,
        "status": "sent",
        "campaign": campaign,
        "opportunity": opportunity,
        "email_status": email_status,
        "offer_url": url,
    }


def send_gallery_sales_campaigns(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    limit: int = 25,
    cooldown_days: int = 14,
) -> int:
    sent = 0
    tenants = conn.execute("SELECT * FROM tenants ORDER BY id").fetchall()
    for tenant_row in tenants:
        if sent >= limit:
            break
        tenant = dict(tenant_row)
        opportunities = gallery_sales_opportunities(
            conn,
            tenant["id"],
            limit=limit - sent,
            ready_only=True,
            cooldown_days=cooldown_days,
        )
        for opp in opportunities:
            result = launch_gallery_sales_campaign(
                conn,
                settings,
                tenant=tenant,
                gallery_id=opp["id"],
                headline=DEFAULT_CAMPAIGN_HEADLINE,
                discount_pct=DEFAULT_CAMPAIGN_DISCOUNT,
                days=DEFAULT_CAMPAIGN_DAYS,
                source="auto",
                cooldown_days=cooldown_days,
                require_ready=True,
            )
            sent += 1 if result.get("sent") else 0
            if sent >= limit:
                break
    return sent


def apply_discount(price_cents: int, pct: int) -> int:
    return round(price_cents * (100 - max(0, min(MAX_DISCOUNT_PCT, pct))) / 100)


def discount_bundle(bundle: dict, pct: int) -> dict:
    """Return a copy of a bundle with the sale price applied, keeping the original
    for strike-through display."""
    if not pct:
        return bundle
    discounted = apply_discount(bundle["price_cents"], pct)
    out = dict(bundle)
    out["orig_price"] = bundle["price"]
    out["price_cents"] = discounted
    out["price"] = f"${discounted / 100:,.0f}"
    return out
