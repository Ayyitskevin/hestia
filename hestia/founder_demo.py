"""Founder demo studios for launch screenshots and sales walkthroughs."""

from __future__ import annotations

import sqlite3
import struct
import zlib

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
    storage,
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
        showcase = _seed_showcase_gallery(conn, storage, settings, tenant["id"])
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
            "showcase": showcase,
        })
    summary = founder_demo_summary(conn, settings)
    return {
        "created": sum(1 for result in results if result["created"]),
        "updated": len(results),
        "results": results,
        **summary,
    }


SHOWCASE_TITLE = "Showcase Gallery"

# Deterministic demo frames. The mock vision provider keys on FILENAME (frame-03
# hashes past the blink threshold) and duplicate detection keys on CONTENT (frame-04
# reuses frame-02's exact bytes) — so every seeded showcase demonstrably has one
# likely blink and one near-duplicate for the AI cull to catch, on any machine.
_SHOWCASE_FRAMES = (
    ("frame-01.jpg", (196, 106, 74)),
    ("frame-02.jpg", (222, 168, 62)),
    ("frame-03.jpg", (146, 172, 132)),   # mock provider scores this a likely blink
    ("frame-04.jpg", (222, 168, 62)),    # same bytes as frame-02 → guaranteed duplicate
    ("frame-05.jpg", (94, 122, 158)),
    ("frame-07.jpg", (182, 128, 164)),
    ("frame-08.jpg", (120, 96, 84)),
    ("frame-09.jpg", (238, 214, 178)),
)


def _demo_png(rgb: tuple[int, int, int], *, width: int = 320, height: int = 220) -> bytes:
    """A tiny valid solid-color PNG, stdlib-only — so demo galleries render real
    thumbnails in the browser without adding an imaging dependency."""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    row = b"\x00" + bytes(rgb) * width          # filter 0 + RGB pixels
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b""))


def _seed_showcase_gallery(conn: sqlite3.Connection, storage, settings: Settings,
                           tenant_id: str) -> dict:
    """Give a demo studio the thing competitors can't show: a processed gallery.
    Uploads deterministic frames, runs the vision pass, applies the AI cull (the
    blink + duplicate are hidden), publishes + enables delivery, and drafts an
    album shared for client review. Idempotent — an existing showcase is left as
    the founder staged it."""
    import io

    from .albums import enable_album_review, generate_album
    from .delivery import enable_delivery
    from .galleries import add_image, apply_cull, create_gallery, publish_gallery
    from .tenants import get_tenant
    from .vision import analyze_gallery

    existing = conn.execute(
        "SELECT id FROM galleries WHERE tenant_id = ? AND title = ? LIMIT 1",
        (tenant_id, SHOWCASE_TITLE),
    ).fetchone()
    if existing:
        return {"gallery_id": existing["id"], "created": False}

    gallery = create_gallery(conn, tenant_id=tenant_id, title=SHOWCASE_TITLE)
    for filename, rgb in _SHOWCASE_FRAMES:
        add_image(conn, storage, tenant_id=tenant_id, gallery_id=gallery["id"],
                  filename=filename, fileobj=io.BytesIO(_demo_png(rgb)),
                  content_type="image/png")
    summary = analyze_gallery(conn, storage, settings, tenant_id=tenant_id,
                              gallery_id=gallery["id"])
    hidden = apply_cull(conn, tenant_id, gallery["id"])
    publish_gallery(conn, tenant_id, gallery["id"])
    enable_delivery(conn, tenant_id, gallery["id"])
    album = generate_album(conn, settings, tenant=get_tenant(conn, tenant_id),
                           gallery=gallery)
    enable_album_review(conn, tenant_id, album["id"])
    return {"gallery_id": gallery["id"], "created": True,
            "analyzed": summary["analyzed"], "culled": hidden, "album_id": album["id"]}


def _has_showcase(conn: sqlite3.Connection, tenant_id: str) -> bool:
    """A published gallery with persisted vision analyses — the demo can show the moat."""
    return conn.execute(
        "SELECT 1 FROM galleries g WHERE g.tenant_id = ? AND g.status = 'published' "
        "AND EXISTS (SELECT 1 FROM image_analyses a WHERE a.gallery_id = g.id "
        "            AND a.tenant_id = g.tenant_id) LIMIT 1",
        (tenant_id,),
    ).fetchone() is not None


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
    showcase = _has_showcase(conn, tenant["id"])
    ready = setup and published
    return {
        **spec,
        "tenant_id": tenant["id"],
        "found": True,
        "setup": setup,
        "published": published,
        "showcase": showcase,
        "ready": ready,
        "admin_url": f"/admin/tenants/{tenant['id']}",
        "public_url": tenant_public_url(settings, tenant["slug"]),
        "status": "Ready" if ready else "Needs setup",
    }
