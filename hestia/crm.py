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


def import_clients(conn: sqlite3.Connection, *, tenant_id: str, rows: list[dict]) -> dict:
    """Bulk-create clients from parsed rows (each a dict with name/email/phone/notes and
    an optional ``tags`` list). A blank-name row is skipped; a row whose email already
    belongs to this tenant — or repeats earlier in the same batch — is skipped as a
    duplicate, so re-importing the same file is idempotent (matched case-insensitively
    on email). Rows without an email can't be deduped and are always imported. Tags are
    applied. Everything is tenant-scoped. Returns a summary of counts."""
    existing = {
        (r["email"] or "").strip().lower()
        for r in conn.execute("SELECT email FROM clients WHERE tenant_id = ?", (tenant_id,))
        if (r["email"] or "").strip()
    }
    seen: set[str] = set()
    imported = skipped_duplicate = skipped_blank = 0
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            skipped_blank += 1
            continue
        email = (row.get("email") or "").strip()
        key = email.lower()
        if email and (key in existing or key in seen):
            skipped_duplicate += 1
            continue
        client = create_client(conn, tenant_id=tenant_id, name=name, email=email,
                               phone=(row.get("phone") or "").strip(),
                               notes=(row.get("notes") or "").strip())
        for tag in row.get("tags") or []:
            add_client_tag(conn, tenant_id, client["id"], tag)
        if email:
            seen.add(key)
        imported += 1
    return {"imported": imported, "skipped_duplicate": skipped_duplicate,
            "skipped_blank": skipped_blank}


def _norm_tag(tag: str) -> str:
    """Normalize a tag: trimmed, lower-cased, single-spaced, capped — so 'VIP ' and
    'vip' are the same tag."""
    return " ".join((tag or "").strip().lower().split())[:40]


def _like_escape(s: str) -> str:
    """Escape LIKE wildcards so a literal % or _ in the query isn't treated as a pattern."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_crm(conn: sqlite3.Connection, tenant_id: str, query: str, *, limit: int = 20) -> dict:
    """Free-text search across the studio's clients (name/email) and projects (name).
    Tenant-scoped; case-insensitive substring; empty query returns nothing."""
    q = (query or "").strip()
    if not q:
        return {"clients": [], "projects": []}
    like = f"%{_like_escape(q)}%"
    clients = [dict(r) for r in conn.execute(
        "SELECT id, name, email FROM clients WHERE tenant_id = ? "
        "AND (name LIKE ? ESCAPE '\\' OR email LIKE ? ESCAPE '\\') ORDER BY name LIMIT ?",
        (tenant_id, like, like, limit),
    )]
    projects = [dict(r) for r in conn.execute(
        "SELECT p.id, p.name, p.status, c.name AS client_name FROM projects p "
        "LEFT JOIN clients c ON c.id = p.client_id AND c.tenant_id = p.tenant_id "
        "WHERE p.tenant_id = ? AND p.name LIKE ? ESCAPE '\\' ORDER BY p.created_at DESC LIMIT ?",
        (tenant_id, like, limit),
    )]
    return {"clients": clients, "projects": projects}


def list_clients(conn: sqlite3.Connection, tenant_id: str, *, tag: str | None = None) -> list[dict]:
    sql = (
        "SELECT c.*, COUNT(DISTINCT p.id) AS project_count, "
        "       COALESCE((SELECT SUM(i.amount_cents) FROM invoices i "
        "                 WHERE i.client_id = c.id AND i.tenant_id = c.tenant_id "
        "                   AND i.status = 'paid'), 0) AS lifetime_cents, "
        "       (SELECT GROUP_CONCAT(ct.tag, ',') FROM client_tags ct "
        "        WHERE ct.client_id = c.id AND ct.tenant_id = c.tenant_id) AS tags_csv "
        "  FROM clients c "
        "  LEFT JOIN projects p ON p.client_id = c.id AND p.tenant_id = c.tenant_id "
        " WHERE c.tenant_id = ? "
    )
    params: list = [tenant_id]
    if tag:
        sql += ("AND c.id IN (SELECT client_id FROM client_tags "
                "WHERE tenant_id = c.tenant_id AND tag = ?) ")
        params.append(_norm_tag(tag))
    sql += "GROUP BY c.id ORDER BY lifetime_cents DESC, c.created_at DESC"
    out = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        d["lifetime_display"] = money(int(d["lifetime_cents"]))     # collected revenue, this client
        d["tags"] = [t for t in (d.get("tags_csv") or "").split(",") if t]
        out.append(d)
    return out


def add_client_tag(conn: sqlite3.Connection, tenant_id: str, client_id: int, tag: str) -> str | None:
    """Tag a client (idempotent). Only tags a client this studio owns; returns the
    normalized tag, or None if the tag was empty or the client isn't theirs."""
    t = _norm_tag(tag)
    if not t or not get_client(conn, tenant_id, client_id):
        return None
    conn.execute("INSERT OR IGNORE INTO client_tags (tenant_id, client_id, tag) VALUES (?, ?, ?)",
                 (tenant_id, client_id, t))
    return t


def remove_client_tag(conn: sqlite3.Connection, tenant_id: str, client_id: int, tag: str) -> bool:
    cur = conn.execute("DELETE FROM client_tags WHERE tenant_id = ? AND client_id = ? AND tag = ?",
                       (tenant_id, client_id, _norm_tag(tag)))
    return cur.rowcount > 0


def tags_for_client(conn: sqlite3.Connection, tenant_id: str, client_id: int) -> list[str]:
    return [r["tag"] for r in conn.execute(
        "SELECT tag FROM client_tags WHERE tenant_id = ? AND client_id = ? ORDER BY tag",
        (tenant_id, client_id)).fetchall()]


def all_tags(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    """Every tag in use for this studio, with how many clients carry it."""
    return [{"tag": r["tag"], "count": int(r["n"])} for r in conn.execute(
        "SELECT tag, COUNT(*) AS n FROM client_tags WHERE tenant_id = ? "
        "GROUP BY tag ORDER BY tag", (tenant_id,)).fetchall()]


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
    lead_source: str = "",
) -> dict:
    if status not in PROJECT_STATUSES:
        status = "lead"
    cur = conn.execute(
        """
        INSERT INTO projects (tenant_id, client_id, name, shoot_type, status, event_date,
                              notes, lead_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (tenant_id, client_id, name.strip(), normalize_shoot_type(shoot_type), status,
         event_date.strip() or None, notes.strip(), (lead_source or "").strip()[:50]),
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
        # Lay down the studio's checklist for this shoot type so no deliverable is
        # forgotten. Idempotent (skips tasks already present), so re-booking is safe.
        from .checklists import apply_checklist
        apply_checklist(conn, tenant_id, project_id)


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
