"""First-run studio presets.

These presets turn a blank tenant into a working photography studio by seeding
the existing Hestia surfaces: public site draft copy, self-serve booking types,
service packages, and one intake questionnaire. There is no separate onboarding
state to drift from reality; completion is derived from tenant-owned rows.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .booking import create_booking_type, list_booking_types
from .crm import create_client, create_project
from .db import audit
from .packages import create_package, list_packages
from .questionnaires import create_questionnaire, list_questionnaires
from .studio import get_profile, upsert_profile
from .tenants import get_tenant, set_shoot_type

Preset = dict[str, Any]

PRESETS: dict[str, Preset] = {
    "wedding": {
        "label": "Wedding",
        "shoot_type": "wedding",
        "blurb": "A high-touch wedding studio setup with retainers, timelines, and client details ready.",
        "headline": "Wedding photography for honest, beautiful celebrations",
        "about": (
            "Thoughtful wedding coverage with a calm planning process, polished galleries, "
            "and artwork your family can keep."
        ),
        "booking_types": [
            {
                "title": "Wedding consultation",
                "description": "A no-pressure planning call to talk date, venue, timeline, and coverage.",
                "kind": "consultation",
                "duration_minutes": 45,
                "price_cents": 0,
                "deposit_cents": 0,
            },
            {
                "title": "Engagement session",
                "description": "A relaxed portrait session before the wedding with an online gallery.",
                "kind": "shoot",
                "duration_minutes": 90,
                "price_cents": 65000,
                "deposit_cents": 15000,
            },
        ],
        "packages": [
            {
                "name": "Wedding Essentials",
                "description": "6 hours of coverage, edited gallery, print release, and planning support.",
                "price_cents": 320000,
                "deposit_cents": 80000,
            },
            {
                "name": "Full Wedding Story",
                "description": "8 hours of coverage, engagement session, edited gallery, album credit, and timeline help.",
                "price_cents": 480000,
                "deposit_cents": 120000,
            },
            {
                "name": "Heirloom Album Add-On",
                "description": "Custom designed wedding album with one revision round.",
                "price_cents": 85000,
                "deposit_cents": 0,
            },
        ],
        "questionnaire_title": "Wedding intake",
        "questionnaire_prompts": [
            "What is your wedding date and venue?",
            "What time should coverage begin and end?",
            "Who are the must-have family groupings?",
            "Are there sensitive family dynamics we should know about?",
            "What three moments matter most to you?",
            "Who is your planner or day-of contact?",
        ],
        "demo_client": "Avery & Jordan Demo",
        "demo_project": "Avery & Jordan Wedding Demo",
    },
    "portrait": {
        "label": "Portrait & Family",
        "shoot_type": "portrait",
        "blurb": "A portrait studio setup with mini and full sessions, print-ready packages, and simple intake.",
        "headline": "Warm, unhurried portraits for people who hate stiff photos",
        "about": (
            "Relaxed portrait and family sessions with friendly direction, a quick online "
            "gallery, and artwork options worth hanging."
        ),
        "booking_types": [
            {
                "title": "Mini session",
                "description": "Thirty focused minutes, one look and location, five edited images.",
                "kind": "shoot",
                "duration_minutes": 30,
                "price_cents": 17500,
                "deposit_cents": 5000,
            },
            {
                "title": "Full portrait session",
                "description": "Ninety minutes, multiple looks, and a gallery of twenty edited images.",
                "kind": "shoot",
                "duration_minutes": 90,
                "price_cents": 42500,
                "deposit_cents": 10000,
            },
        ],
        "packages": [
            {
                "name": "Portrait Session",
                "description": "Full session, online gallery, twenty edited images, and a print release.",
                "price_cents": 42500,
                "deposit_cents": 10000,
            },
            {
                "name": "Family Story",
                "description": "Extended family session, thirty edited images, and two fine-art prints.",
                "price_cents": 65000,
                "deposit_cents": 15000,
            },
            {
                "name": "Wall Art Add-On",
                "description": "A framed 16x20 fine-art print of your favorite frame.",
                "price_cents": 22000,
                "deposit_cents": 0,
            },
        ],
        "questionnaire_title": "Portrait intake",
        "questionnaire_prompts": [
            "Who is being photographed, and how do they feel about being in front of a camera?",
            "Where will we shoot — studio, home, or outdoors?",
            "What will the photos be used for (walls, cards, profiles)?",
            "Any dates or times that work best?",
            "Anything else that would make the session feel easy?",
        ],
        "demo_client": "The Rivera Family Demo",
        "demo_project": "Rivera Family Portraits Demo",
    },
    "food": {
        "label": "Food & Beverage",
        "shoot_type": "food",
        "blurb": "A commercial food workflow for restaurants, menus, campaigns, and repeat clients.",
        "headline": "Food and beverage photography built for brands that sell",
        "about": (
            "Editorial food, drink, and restaurant imagery with streamlined planning, fast delivery, "
            "and usage-ready galleries."
        ),
        "booking_types": [
            {
                "title": "Creative direction call",
                "description": "A short call to align shot list, usage, surfaces, props, and delivery needs.",
                "kind": "consultation",
                "duration_minutes": 30,
                "price_cents": 0,
                "deposit_cents": 0,
            },
            {
                "title": "Half-day restaurant shoot",
                "description": "Menu, staff, space, and lifestyle coverage for restaurants and hospitality brands.",
                "kind": "shoot",
                "duration_minutes": 240,
                "price_cents": 180000,
                "deposit_cents": 50000,
            },
        ],
        "packages": [
            {
                "name": "Menu Refresh",
                "description": "Up to 12 hero dishes, basic styling direction, edited gallery, and web usage.",
                "price_cents": 120000,
                "deposit_cents": 35000,
            },
            {
                "name": "Campaign Day",
                "description": "Half-day shoot for food, beverage, interiors, and social launch assets.",
                "price_cents": 220000,
                "deposit_cents": 70000,
            },
            {
                "name": "Monthly Content Retainer",
                "description": "One recurring content session per month for seasonal menus and social campaigns.",
                "price_cents": 150000,
                "deposit_cents": 0,
            },
        ],
        "questionnaire_title": "Food & beverage intake",
        "questionnaire_prompts": [
            "What product, menu, or campaign are we shooting?",
            "Where will the images be used?",
            "What are the must-have shots?",
            "Who is responsible for food styling and props?",
            "What brand references or mood boards should guide the shoot?",
            "What delivery deadline should we plan around?",
        ],
        "demo_client": "North Table Demo",
        "demo_project": "Seasonal Menu Refresh Demo",
    },
    "real_estate": {
        "label": "Real Estate",
        "shoot_type": "commercial",
        "blurb": "A listing-focused setup for agents, builders, short-term rentals, and fast delivery.",
        "headline": "Real estate photography that helps listings move",
        "about": (
            "Clean interior, exterior, and listing media workflows for agents and property teams "
            "who need reliable scheduling and quick delivery."
        ),
        "booking_types": [
            {
                "title": "Listing shoot",
                "description": "Interior and exterior listing coverage for MLS, social, and property marketing.",
                "kind": "shoot",
                "duration_minutes": 90,
                "price_cents": 35000,
                "deposit_cents": 0,
            },
            {
                "title": "Luxury listing package",
                "description": "Expanded listing coverage with detail images and premium marketing assets.",
                "kind": "shoot",
                "duration_minutes": 180,
                "price_cents": 75000,
                "deposit_cents": 0,
            },
        ],
        "packages": [
            {
                "name": "MLS Essentials",
                "description": "Interior and exterior listing images with web-ready delivery.",
                "price_cents": 35000,
                "deposit_cents": 0,
            },
            {
                "name": "Agent Marketing Bundle",
                "description": "Listing gallery, social selects, twilight exterior, and detail coverage.",
                "price_cents": 85000,
                "deposit_cents": 0,
            },
            {
                "name": "Short-Term Rental Launch",
                "description": "Full property story with amenity, lifestyle, and room-by-room coverage.",
                "price_cents": 120000,
                "deposit_cents": 0,
            },
        ],
        "questionnaire_title": "Real estate intake",
        "questionnaire_prompts": [
            "What is the property address?",
            "What spaces or amenities need special attention?",
            "Is the property occupied, vacant, or staged?",
            "Are there access instructions, lockbox codes, or parking notes?",
            "What MLS, social, or marketing deliverables do you need?",
            "When does the listing go live?",
        ],
        "demo_client": "Harbor Homes Demo",
        "demo_project": "Oak Street Listing Demo",
    },
}


def get_preset(key: str) -> Preset | None:
    return PRESETS.get((key or "").strip())


def preset_applied(conn: sqlite3.Connection, tenant_id: str) -> bool:
    """Whether the studio has any of the setup surfaces a preset would create."""
    return bool(
        list_booking_types(conn, tenant_id, active_only=True)
        or list_packages(conn, tenant_id, active_only=True)
        or list_questionnaires(conn, tenant_id)
    )


def _profile_is_blank(profile: dict) -> bool:
    return not (profile.get("headline") or "").strip() and not (profile.get("about") or "").strip()


def _seed_profile(conn: sqlite3.Connection, tenant_id: str, preset: Preset) -> bool:
    profile = get_profile(conn, tenant_id)
    if not _profile_is_blank(profile):
        return False
    owner = conn.execute(
        "SELECT email FROM users WHERE tenant_id = ? AND role = 'owner' ORDER BY id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    upsert_profile(
        conn,
        tenant_id=tenant_id,
        headline=preset["headline"],
        about=preset["about"],
        contact_email=owner["email"] if owner else "",
        published=False,
    )
    return True


def _seed_demo(conn: sqlite3.Connection, tenant_id: str, preset_key: str, preset: Preset) -> dict | None:
    email = f"demo+{preset_key}@hestia.local"
    existing = conn.execute(
        "SELECT id FROM clients WHERE tenant_id = ? AND email = ? LIMIT 1",
        (tenant_id, email),
    ).fetchone()
    if existing:
        return None
    client = create_client(
        conn,
        tenant_id=tenant_id,
        name=preset["demo_client"],
        email=email,
        notes="Hestia demo client. Replace or delete after exploring the workflow.",
    )
    project = create_project(
        conn,
        tenant_id=tenant_id,
        name=preset["demo_project"],
        client_id=client["id"],
        shoot_type=preset["shoot_type"],
        status="lead",
        notes="Demo project seeded by the onboarding preset.",
        lead_source="Hestia demo",
    )
    return {"client_id": client["id"], "project_id": project["id"]}


def apply_preset(
    conn: sqlite3.Connection,
    tenant_id: str,
    preset_key: str,
    *,
    include_demo: bool = True,
    actor: str = "owner",
) -> dict | None:
    """Apply a preset to empty setup surfaces and return a summary.

    Existing booking types, packages, questionnaires, and profile copy are left
    untouched so owners can safely revisit onboarding without duplicate starter
    rows or accidental overwrites.
    """
    preset_key = (preset_key or "").strip()
    preset = get_preset(preset_key)
    if preset is None or get_tenant(conn, tenant_id) is None:
        return None

    set_shoot_type(conn, tenant_id, preset["shoot_type"])
    summary = {
        "preset": preset_key,
        "label": preset["label"],
        "profile": _seed_profile(conn, tenant_id, preset),
        "booking_types": 0,
        "packages": 0,
        "questionnaires": 0,
        "demo": None,
    }

    if not list_booking_types(conn, tenant_id, active_only=True):
        for item in preset["booking_types"]:
            if create_booking_type(conn, tenant_id=tenant_id, **item):
                summary["booking_types"] += 1

    if not list_packages(conn, tenant_id, active_only=True):
        for item in preset["packages"]:
            if create_package(conn, tenant_id=tenant_id, **item):
                summary["packages"] += 1

    if not list_questionnaires(conn, tenant_id):
        q = create_questionnaire(
            conn,
            tenant_id=tenant_id,
            title=preset["questionnaire_title"],
            prompts=preset["questionnaire_prompts"],
        )
        summary["questionnaires"] = 1 if q else 0

    if include_demo:
        summary["demo"] = _seed_demo(conn, tenant_id, preset_key, preset)

    audit(
        conn,
        actor=actor,
        action="onboarding.preset_applied",
        tenant_id=tenant_id,
        detail=preset["label"],
    )
    conn.commit()
    return summary
