"""Founder demo studios for launch screenshots and sales walkthroughs."""

from __future__ import annotations

import sqlite3

from .config import Settings
from .crypto import new_session_token
from .db import audit
from .hosted import tenant_public_url
from .presets import apply_preset, preset_applied
from .studio import get_profile, upsert_profile
from .tenants import create_tenant, create_user, get_tenant_by_slug

FOUNDER_DEMO_STUDIOS = (
    {
        "key": "wedding",
        "preset": "wedding",
        "label": "Wedding",
        "name": "Hestia Wedding Demo Studio",
        "slug": "hestia-wedding-demo",
        "owner_email": "founder-demo+wedding@hestia.local",
        "landing_path": "/demo/wedding",
    },
    {
        "key": "food",
        "preset": "food",
        "label": "Food & Beverage",
        "name": "Hestia Food & Beverage Demo Studio",
        "slug": "hestia-food-demo",
        "owner_email": "founder-demo+food@hestia.local",
        "landing_path": "/demo/food",
    },
    {
        "key": "real_estate",
        "preset": "real_estate",
        "label": "Real Estate",
        "name": "Hestia Real Estate Demo Studio",
        "slug": "hestia-real-estate-demo",
        "owner_email": "founder-demo+real-estate@hestia.local",
        "landing_path": "/demo/real-estate",
    },
)


def founder_demo_summary(conn: sqlite3.Connection, settings: Settings) -> dict:
    studios = [_demo_row(conn, settings, spec) for spec in FOUNDER_DEMO_STUDIOS]
    ready = sum(1 for studio in studios if studio["ready"])
    return {
        "target": len(FOUNDER_DEMO_STUDIOS),
        "ready": ready,
        "complete": ready == len(FOUNDER_DEMO_STUDIOS),
        "studios": studios,
    }


def seed_founder_demo_studios(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    actor: str = "admin",
) -> dict:
    results = []
    for spec in FOUNDER_DEMO_STUDIOS:
        tenant = get_tenant_by_slug(conn, spec["slug"])
        created = False
        if not tenant:
            tenant = create_tenant(
                conn,
                name=spec["name"],
                shoot_type="other",
                slug=spec["slug"],
                signup_source="demo",
                signup_landing_path=spec["landing_path"],
            )
            created = True
        _ensure_owner(conn, tenant["id"], spec["owner_email"])
        preset_summary = apply_preset(
            conn,
            tenant["id"],
            spec["preset"],
            include_demo=True,
            actor=actor,
        )
        _publish_demo_profile(conn, tenant["id"], spec["owner_email"])
        audit(
            conn,
            actor=actor,
            action="founder_demo.seeded",
            tenant_id=tenant["id"],
            detail=spec["key"],
        )
        results.append({
            "key": spec["key"],
            "tenant_id": tenant["id"],
            "created": created,
            "preset": preset_summary,
        })
    summary = founder_demo_summary(conn, settings)
    return {
        "created": sum(1 for result in results if result["created"]),
        "updated": len(results),
        "results": results,
        **summary,
    }


def _ensure_owner(conn: sqlite3.Connection, tenant_id: str, email: str) -> None:
    existing = conn.execute(
        "SELECT 1 FROM users WHERE tenant_id = ? AND lower(email) = lower(?) LIMIT 1",
        (tenant_id, email),
    ).fetchone()
    if existing:
        return
    create_user(
        conn,
        tenant_id=tenant_id,
        email=email,
        password=new_session_token(),
        role="owner",
        verified=1,
    )


def _publish_demo_profile(conn: sqlite3.Connection, tenant_id: str, owner_email: str) -> None:
    profile = get_profile(conn, tenant_id)
    if not (profile["headline"] or profile["about"]):
        return
    upsert_profile(
        conn,
        tenant_id=tenant_id,
        headline=profile["headline"],
        about=profile["about"],
        contact_email=profile["contact_email"] or owner_email,
        published=True,
    )


def _demo_row(conn: sqlite3.Connection, settings: Settings, spec: dict) -> dict:
    tenant = get_tenant_by_slug(conn, spec["slug"])
    if not tenant:
        return {
            **spec,
            "tenant_id": "",
            "found": False,
            "setup": False,
            "published": False,
            "ready": False,
            "admin_url": "",
            "public_url": "",
            "status": "Missing",
        }
    profile = get_profile(conn, tenant["id"])
    setup = preset_applied(conn, tenant["id"])
    published = bool(profile.get("published"))
    ready = setup and published
    return {
        **spec,
        "tenant_id": tenant["id"],
        "found": True,
        "setup": setup,
        "published": published,
        "ready": ready,
        "admin_url": f"/admin/tenants/{tenant['id']}",
        "public_url": tenant_public_url(settings, tenant["slug"]),
        "status": "Ready" if ready else "Needs setup",
    }
