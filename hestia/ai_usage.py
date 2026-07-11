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


def _live_vision_gallery_count(conn: sqlite3.Connection, tenant_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT gallery_id) AS n FROM ai_usage_events "
        "WHERE tenant_id = ? AND module = 'vision' AND gallery_id IS NOT NULL",
        (tenant_id,),
    ).fetchone()
    return int(row["n"] or 0)


def gallery_has_live_vision(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM ai_usage_events WHERE tenant_id = ? AND gallery_id = ? AND module = 'vision' LIMIT 1",
        (tenant_id, gallery_id),
    ).fetchone() is not None


def resolve_vision_provider(
    conn: sqlite3.Connection,
    settings,
    *,
    tenant_id: str,
    gallery_id: int,
    provider=None,
):
    """Pick the vision provider for a gallery process.

    Precedence (a live backend is configured at the app level):
    1. A studio's *own* xAI key — they pay xAI directly, so the founder-hosted
       subsidy cap and image cap do not apply. This conserves founder credits
       and is the post-subsidy path for studios that want unlimited live vision.
    2. The founder-hosted beta subsidy — one gallery per studio (capped), then a
       deterministic mock cull so the run still completes without a paid key.
    3. ``mock`` when no live backend is configured at all.
    """
    from .tenants import get_tenant_ai_key
    from .vision import MockVisionProvider, XaiVisionProvider, build_provider

    configured = provider or build_provider(settings)
    backend = getattr(configured, "backend", "mock")
    if backend == "mock":
        return configured, None

    # A tenant-owned key takes precedence over the founder subsidy: build a
    # provider scoped to that key (shared base_url + model) with no caps.
    own_key = get_tenant_ai_key(conn, tenant_id, session_secret=settings.session_secret)
    if own_key:
        import dataclasses

        scoped = dataclasses.replace(settings, xai_api_key=own_key)
        return XaiVisionProvider(scoped), "Using your own xAI key (no subsidy cap)."

    if not settings.ai_subsidy_enabled:
        return configured, None

    if gallery_has_live_vision(conn, tenant_id, gallery_id):
        return configured, None

    used = _live_vision_gallery_count(conn, tenant_id)
    if used >= settings.ai_subsidy_galleries_per_tenant:
        return MockVisionProvider(), (
            f"Live AI subsidy already used on {used} gallery"
            f"{'' if used == 1 else 'ies'} — this run uses the deterministic mock cull. "
            "Add your own xAI key in Site settings to keep live vision with no cap."
        )

    from .galleries import list_images

    image_count = len(list_images(conn, gallery_id, tenant_id=tenant_id))
    if image_count > settings.ai_subsidy_image_cap:
        return MockVisionProvider(), (
            f"Gallery has {image_count} images (subsidized cap is {settings.ai_subsidy_image_cap}) "
            "— using mock cull. Upload fewer frames, split the gallery, or add your own "
            "xAI key in Site settings to lift the cap."
        )

    return configured, None


def tenant_subsidy_status(conn: sqlite3.Connection, settings, tenant_id: str) -> dict:
    """Owner-facing summary of beta AI subsidy remaining + own-key state."""
    from .tenants import tenant_has_own_ai_key

    live_backend = settings.vision_backend != "mock"
    own_key = tenant_has_own_ai_key(conn, tenant_id)
    used = _live_vision_gallery_count(conn, tenant_id)
    allowed = settings.ai_subsidy_galleries_per_tenant
    if not live_backend:
        return {
            "active": False,
            "live_backend": live_backend,
            "own_key": own_key,
            "galleries_used": used,
            "galleries_allowed": allowed,
            "image_cap": settings.ai_subsidy_image_cap,
            "remaining_galleries": 0,
            "message": "" if own_key else "Live AI vision is off for this deployment.",
        }
    if own_key:
        return {
            "active": False,
            "live_backend": True,
            "own_key": True,
            "galleries_used": used,
            "galleries_allowed": allowed,
            "image_cap": settings.ai_subsidy_image_cap,
            "remaining_galleries": 0,
            "message": "Using your own xAI key — unlimited live vision, no subsidy cap.",
        }
    if not settings.ai_subsidy_enabled:
        return {
            "active": False,
            "live_backend": True,
            "own_key": False,
            "galleries_used": used,
            "galleries_allowed": allowed,
            "image_cap": settings.ai_subsidy_image_cap,
            "remaining_galleries": 0,
            "message": "",
        }
    remaining = max(0, allowed - used)
    if remaining == 0:
        message = (
            "Your included live AI gallery process is used — new galleries use the mock cull "
            "until you add your own xAI key in Site settings."
        )
    elif remaining == 1:
        message = (
            f"Your next gallery process (up to {settings.ai_subsidy_image_cap} images) "
            "uses live AI vision."
        )
    else:
        message = ""
    return {
        "active": True,
        "live_backend": True,
        "own_key": False,
        "galleries_used": used,
        "galleries_allowed": allowed,
        "image_cap": settings.ai_subsidy_image_cap,
        "remaining_galleries": remaining,
        "message": message,
    }

