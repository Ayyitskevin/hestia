"""CRM module — clients and projects (the studio-OS backbone, essence of Mise).

This is the connective tissue of the studio OS: galleries (and invoices, albums,
and campaigns) hang off a project, and projects belong to a client. Pure data
access, tenant-scoped throughout.

    client → project (shoot_type, status, event_date) → gallery → offer
"""

from __future__ import annotations

import sqlite3

from .automations import emit_event
from .features import normalize_shoot_type
from .invoices import money

PROJECT_STATUSES = ("lead", "booked", "shooting", "delivered", "archived")


# ── Clients ─────────────────────────────────────────────────────────────────


def create_client(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    name: str,
    email: str = "",
    phone: str = "",
    notes: str = "",
) -> dict:
    cur = conn.execute(
        "INSERT INTO clients (tenant_id, name, email, phone, notes) VALUES (?, ?, ?, ?, ?)",
        (tenant_id, name.strip(), email.strip(), phone.strip(), notes.strip()),
    )
    return get_client(conn, tenant_id, cur.lastrowid)


def get_client(conn: sqlite3.Connection, tenant_id: str, client_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM clients WHERE id = ? AND tenant_id = ?", (client_id, tenant_id)
    ).fetchone()
    return dict(row) if row else None


def list_clients(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT c.*, COUNT(p.id) AS project_count
          FROM clients c
          LEFT JOIN projects p ON p.client_id = c.id AND p.tenant_id = c.tenant_id
         WHERE c.tenant_id = ?
         GROUP BY c.id
         ORDER BY c.created_at DESC
        """,
        (tenant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Projects ────────────────────────────────────────────────────────────────


def create_project(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    name: str,
    client_id: int | None = None,
    shoot_type: str = "other",
    status: str = "lead",
    event_date: str = "",
    notes: str = "",
) -> dict:
    if status not in PROJECT_STATUSES:
        status = "lead"
    cur = conn.execute(
        """
        INSERT INTO projects (tenant_id, client_id, name, shoot_type, status, event_date, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (tenant_id, client_id, name.strip(), normalize_shoot_type(shoot_type), status,
         event_date.strip() or None, notes.strip()),
    )
    return get_project(conn, tenant_id, cur.lastrowid)


def get_project(conn: sqlite3.Connection, tenant_id: str, project_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT p.*, c.name AS client_name
          FROM projects p
          LEFT JOIN clients c ON c.id = p.client_id AND c.tenant_id = p.tenant_id
         WHERE p.id = ? AND p.tenant_id = ?
        """,
        (project_id, tenant_id),
    ).fetchone()
    return dict(row) if row else None


def list_projects(conn: sqlite3.Connection, tenant_id: str, *, client_id: int | None = None) -> list[dict]:
    sql = (
        "SELECT p.*, c.name AS client_name, "
        "       (SELECT COUNT(*) FROM galleries g WHERE g.project_id = p.id) AS gallery_count "
        "  FROM projects p LEFT JOIN clients c ON c.id = p.client_id AND c.tenant_id = p.tenant_id "
        " WHERE p.tenant_id = ?"
    )
    params: list = [tenant_id]
    if client_id is not None:
        sql += " AND p.client_id = ?"
        params.append(client_id)
    sql += " ORDER BY p.created_at DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def set_project_status(conn: sqlite3.Connection, tenant_id: str, project_id: int, status: str) -> None:
    if status not in PROJECT_STATUSES:
        return
    conn.execute(
        "UPDATE projects SET status = ? WHERE id = ? AND tenant_id = ?",
        (status, project_id, tenant_id),
    )
    if status == "booked":
        emit_event(conn, tenant_id=tenant_id, event="project.booked",
                   context={"project_id": project_id})
        # A referred lead that books pays its referrer a credit (idempotent). Lazy
        # import keeps crm ⇄ referral_rewards from forming an import cycle.
        from .referral_rewards import award_referral_credit
        award_referral_credit(conn, tenant_id, project_id)


# ── Gallery ↔ project linkage ───────────────────────────────────────────────


def assign_gallery_to_project(
    conn: sqlite3.Connection, tenant_id: str, gallery_id: int, project_id: int | None
) -> None:
    # Validate the project belongs to the tenant (or clear the link).
    if project_id is not None and not get_project(conn, tenant_id, project_id):
        return
    conn.execute(
        "UPDATE galleries SET project_id = ? WHERE id = ? AND tenant_id = ?",
        (project_id, gallery_id, tenant_id),
    )


def galleries_for_project(conn: sqlite3.Connection, tenant_id: str, project_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM galleries WHERE tenant_id = ? AND project_id = ? ORDER BY created_at DESC",
        (tenant_id, project_id),
    ).fetchall()
    return [dict(r) for r in rows]


def client_timeline(conn: sqlite3.Connection, tenant_id: str, client_id: int, *,
                    limit: int = 60) -> list[dict]:
    """A chronological feed of everything that's happened with a client across the
    whole loop — added → projects → contracts → questionnaires → sessions → invoices
    → payment plans → delivered galleries — newest first. Pure read-side aggregation
    for the client detail page; each event is tenant-scoped."""
    events: list[dict] = []

    def add(date, icon, label, url=None):
        if date:                                    # skip events with no usable date
            events.append({"date": date, "icon": icon, "label": label, "url": url})

    client = conn.execute("SELECT created_at FROM clients WHERE id = ? AND tenant_id = ?",
                          (client_id, tenant_id)).fetchone()
    if not client:
        return []
    add(client["created_at"], "👤", "Added as a client")

    for r in conn.execute("SELECT id, name, created_at FROM projects "
                          "WHERE tenant_id = ? AND client_id = ?", (tenant_id, client_id)):
        add(r["created_at"], "📁", f"Project created — {r['name']}", f"/projects/{r['id']}")

    for r in conn.execute("SELECT id, title, status, created_at, signed_at FROM contracts "
                          "WHERE tenant_id = ? AND client_id = ? AND status != 'void'",
                          (tenant_id, client_id)):
        if r["status"] == "signed":
            add(r["signed_at"] or r["created_at"], "✍️", f"Signed contract — {r['title']}",
                f"/contracts/{r['id']}")
        else:
            verb = "Sent" if r["status"] == "sent" else "Drafted"
            add(r["created_at"], "📄", f"{verb} contract — {r['title']}", f"/contracts/{r['id']}")

    for r in conn.execute("SELECT id, title, status, created_at FROM questionnaires "
                          "WHERE tenant_id = ? AND client_id = ? AND status IN ('sent', 'completed')",
                          (tenant_id, client_id)):
        verb = "Completed" if r["status"] == "completed" else "Sent"
        icon = "✅" if r["status"] == "completed" else "📋"
        add(r["created_at"], icon, f"{verb} questionnaire — {r['title']}")

    for r in conn.execute("SELECT id, title, status, starts_at, created_at FROM appointments "
                          "WHERE tenant_id = ? AND client_id = ? AND status != 'canceled'",
                          (tenant_id, client_id)):
        if r["status"] == "confirmed":
            add(r["starts_at"] or r["created_at"], "📅", f"Session booked — {r['title']}",
                f"/schedule/{r['id']}")
        else:
            add(r["created_at"], "📅", f"Session proposed — {r['title']}", f"/schedule/{r['id']}")

    for r in conn.execute("SELECT id, title, amount_cents, currency, status, created_at, paid_at "
                          "FROM invoices WHERE tenant_id = ? AND client_id = ? "
                          "AND status != 'void' AND plan_id IS NULL", (tenant_id, client_id)):
        amt = money(r["amount_cents"], r["currency"] or "usd")
        if r["status"] == "paid":
            add(r["paid_at"] or r["created_at"], "💵", f"Paid invoice — {r['title']} ({amt})",
                f"/invoices/{r['id']}")
        else:
            verb = "Sent" if r["status"] == "sent" else "Drafted"
            add(r["created_at"], "🧾", f"{verb} invoice — {r['title']} ({amt})", f"/invoices/{r['id']}")

    for r in conn.execute("SELECT id, title, created_at FROM payment_plans "
                          "WHERE tenant_id = ? AND client_id = ? AND status != 'void'",
                          (tenant_id, client_id)):
        add(r["created_at"], "📆", f"Payment plan — {r['title']}", f"/payment-plans/{r['id']}")

    for r in conn.execute(
            "SELECT g.id, g.title, g.published_at, g.created_at FROM galleries g "
            "JOIN projects p ON p.id = g.project_id AND p.tenant_id = g.tenant_id "
            "WHERE g.tenant_id = ? AND p.client_id = ? AND g.status = 'published'",
            (tenant_id, client_id)):
        add(r["published_at"] or r["created_at"], "🖼️", f"Gallery delivered — {r['title']}",
            f"/galleries/{r['id']}")

    events.sort(key=lambda e: e["date"], reverse=True)
    return events[:limit]


def project_pipeline(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    """Projects grouped by stage (lead → booked → shooting → delivered → archived)
    for the portfolio funnel — each stage with its projects, a count, and the revenue
    collected on them so far. Read-side aggregation, tenant-scoped."""
    rows = conn.execute(
        "SELECT p.id, p.name, p.status, p.shoot_type, p.event_date, c.name AS client_name, "
        "  COALESCE((SELECT SUM(amount_cents) FROM invoices i WHERE i.project_id = p.id "
        "            AND i.tenant_id = p.tenant_id AND i.status = 'paid'), 0) AS collected_cents "
        "FROM projects p LEFT JOIN clients c ON c.id = p.client_id AND c.tenant_id = p.tenant_id "
        "WHERE p.tenant_id = ? ORDER BY p.created_at DESC",
        (tenant_id,),
    ).fetchall()
    stages = {s: {"stage": s, "projects": [], "count": 0, "collected_cents": 0}
              for s in PROJECT_STATUSES}
    for r in rows:
        st = r["status"] if r["status"] in stages else "lead"
        d = dict(r)
        d["collected_display"] = money(d["collected_cents"])
        g = stages[st]
        g["projects"].append(d)
        g["count"] += 1
        g["collected_cents"] += int(r["collected_cents"])
    out = []
    for s in PROJECT_STATUSES:
        g = stages[s]
        g["collected_display"] = money(g["collected_cents"])
        out.append(g)
    return out


def galleries_for_client(conn: sqlite3.Connection, tenant_id: str, client_id: int) -> list[dict]:
    """Every gallery whose project belongs to this client (for the client portal)."""
    rows = conn.execute(
        "SELECT g.* FROM galleries g JOIN projects p ON p.id = g.project_id AND p.tenant_id = g.tenant_id "
        "WHERE g.tenant_id = ? AND p.client_id = ? ORDER BY g.created_at DESC",
        (tenant_id, client_id),
    ).fetchall()
    return [dict(r) for r in rows]
