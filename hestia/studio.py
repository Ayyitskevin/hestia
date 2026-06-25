"""Public studio site + inquiry intake (essence of Mise's marketing site).

The closer of the behemoth: a public per-studio page with a booking/inquiry form
that drops straight into the CRM as a lead. A stranger on the internet becomes
``client → project (lead)`` in the same app that will later deliver their gallery,
sell them prints, and invoice them. Public → pipeline, end to end.
"""

from __future__ import annotations

import sqlite3

from .crm import create_client, create_project
from .db import audit
from .features import normalize_shoot_type


def get_profile(conn: sqlite3.Connection, tenant_id: str) -> dict:
    row = conn.execute("SELECT * FROM studio_profiles WHERE tenant_id = ?", (tenant_id,)).fetchone()
    if row:
        return dict(row)
    return {"tenant_id": tenant_id, "headline": "", "about": "", "contact_email": "",
            "published": 0, "updated_at": None}


def upsert_profile(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    headline: str,
    about: str,
    contact_email: str,
    published: bool,
) -> dict:
    conn.execute(
        """
        INSERT INTO studio_profiles (tenant_id, headline, about, contact_email, published, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT (tenant_id) DO UPDATE SET
            headline = excluded.headline, about = excluded.about,
            contact_email = excluded.contact_email, published = excluded.published,
            updated_at = datetime('now')
        """,
        (tenant_id, headline.strip(), about.strip(), contact_email.strip(), 1 if published else 0),
    )
    conn.commit()
    return get_profile(conn, tenant_id)


def create_inquiry(
    conn: sqlite3.Connection,
    *,
    tenant: dict,
    name: str,
    email: str = "",
    message: str = "",
    shoot_type: str = "other",
    event_date: str = "",
) -> dict:
    """Turn a public inquiry into a CRM client + project lead. Returns the project."""
    st = normalize_shoot_type(shoot_type)
    client = create_client(conn, tenant_id=tenant["id"], name=name.strip() or "Website inquiry",
                           email=email)
    project = create_project(
        conn, tenant_id=tenant["id"],
        name=f"{st.title()} inquiry — {name.strip() or email or 'website'}",
        client_id=client["id"], shoot_type=st, status="lead",
        event_date=event_date, notes=message,
    )
    audit(conn, actor="public", action="studio.inquiry", tenant_id=tenant["id"],
          detail=f"lead from {email or name}")
    conn.commit()
    return project
