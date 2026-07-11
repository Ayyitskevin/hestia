"""AI usage ledger — track live provider calls per studio for cost control."""

from __future__ import annotations

import sqlite3

LIVE_BACKENDS = frozenset({"xai", "anthropic", "zai"})


def record_usage(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    module: str,
    backend: str,
    units: int = 1,
    gallery_id: int | None = None,
) -> None:
    """Persist a usage event when a real (non-mock) AI backend ran."""
    if backend not in LIVE_BACKENDS or units <= 0:
        return
    conn.execute(
        "INSERT INTO ai_usage_events (tenant_id, gallery_id, module, backend, units) "
        "VALUES (?, ?, ?, ?, ?)",
        (tenant_id, gallery_id, module, backend, int(units)),
    )


def tenant_usage_summary(conn: sqlite3.Connection, tenant_id: str) -> dict:
    """Totals for one studio — all time and last 30 days."""
    rows = conn.execute(
        "SELECT module, backend, SUM(units) AS units FROM ai_usage_events "
        "WHERE tenant_id = ? GROUP BY module, backend ORDER BY module, backend",
        (tenant_id,),
    ).fetchall()
    recent = conn.execute(
        "SELECT COALESCE(SUM(units), 0) AS n FROM ai_usage_events "
        "WHERE tenant_id = ? AND created_at >= datetime('now', '-30 days')",
        (tenant_id,),
    ).fetchone()["n"]
    total = sum(r["units"] for r in rows)
    return {
        "total_units": total,
        "recent_30d_units": recent,
        "by_module": [dict(r) for r in rows],
    }


def gallery_usage_summary(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> dict:
    """Usage attributed to one gallery (vision runs, album drafts, …)."""
    rows = conn.execute(
        "SELECT module, backend, SUM(units) AS units FROM ai_usage_events "
        "WHERE tenant_id = ? AND gallery_id = ? GROUP BY module, backend",
        (tenant_id, gallery_id),
    ).fetchall()
    return {
        "total_units": sum(r["units"] for r in rows),
        "by_module": [dict(r) for r in rows],
    }


def operator_usage_summary(conn: sqlite3.Connection, *, limit: int = 12) -> dict:
    """Cross-tenant totals for the founder admin view."""
    total = conn.execute("SELECT COALESCE(SUM(units), 0) AS n FROM ai_usage_events").fetchone()["n"]
    recent = conn.execute(
        "SELECT COALESCE(SUM(units), 0) AS n FROM ai_usage_events "
        "WHERE created_at >= datetime('now', '-30 days')"
    ).fetchone()["n"]
    by_module = conn.execute(
        "SELECT module, backend, SUM(units) AS units FROM ai_usage_events "
        "GROUP BY module, backend ORDER BY units DESC"
    ).fetchall()
    top_tenants = conn.execute(
        "SELECT e.tenant_id, t.name, t.slug, SUM(e.units) AS units "
        "FROM ai_usage_events e JOIN tenants t ON t.id = e.tenant_id "
        "GROUP BY e.tenant_id ORDER BY units DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return {
        "total_units": total,
        "recent_30d_units": recent,
        "by_module": [dict(r) for r in by_module],
        "top_tenants": [dict(r) for r in top_tenants],
    }
