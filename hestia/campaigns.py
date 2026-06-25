"""Sales campaigns — a time-limited, urgency-gated sale on a gallery's offer.

A campaign carries a headline, a discount, and a deadline. While it's active the
public offer applies the discount *live at render* (the idempotent offer token is
never touched) and shows the deadline for urgency. One active campaign per
gallery — launching a new one ends the prior. Tenant-scoped throughout.
"""

from __future__ import annotations

import sqlite3

from .db import audit

MAX_DISCOUNT_PCT = 90


def create_campaign(
    conn: sqlite3.Connection, *, tenant_id: str, gallery_id: int,
    headline: str, discount_pct: int, days: int,
) -> dict:
    """Launch a sale, ending any campaign already active on this gallery."""
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


def get_active_campaign(conn: sqlite3.Connection, gallery_id: int) -> dict | None:
    """The live campaign for a gallery (active and not past its deadline), if any."""
    row = conn.execute(
        "SELECT * FROM sales_campaigns WHERE gallery_id = ? AND status = 'active' "
        "AND ends_at > datetime('now') ORDER BY id DESC LIMIT 1",
        (gallery_id,),
    ).fetchone()
    return dict(row) if row else None


def end_campaign(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> None:
    conn.execute(
        "UPDATE sales_campaigns SET status = 'ended' "
        "WHERE gallery_id = ? AND tenant_id = ? AND status = 'active'",
        (gallery_id, tenant_id),
    )


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
