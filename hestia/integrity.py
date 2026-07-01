"""Tenant data-integrity audit and safe relationship repair.

The app's read paths now tenant-match parent joins and hide malformed legacy links.
This module gives owners a positive report of those hidden links and an idempotent
repair that clears optional bad references. Required parent rows are reported for
manual review instead of being deleted automatically.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .db import audit


@dataclass(frozen=True)
class IntegrityRule:
    code: str
    label: str
    detail: str
    action: str
    select_sql: str
    repair_sql: str | None = None

    @property
    def repairable(self) -> bool:
        return self.repair_sql is not None


def _client_only_rule(
    *,
    table: str,
    column: str,
    code: str,
    label: str,
    detail: str,
    item_label: str,
) -> IntegrityRule:
    return IntegrityRule(
        code=code,
        label=label,
        detail=detail,
        action=f"clear {column}",
        select_sql=f"""
            SELECT t.id, {item_label} AS item, t.{column} AS bad_value,
                   'client link points outside this studio' AS reason
              FROM {table} t
              LEFT JOIN clients c ON c.id = t.{column} AND c.tenant_id = t.tenant_id
             WHERE t.tenant_id = ? AND t.{column} IS NOT NULL AND c.id IS NULL
             ORDER BY t.id
        """,
        repair_sql=f"""
            UPDATE {table}
               SET {column} = NULL
             WHERE tenant_id = ? AND {column} IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM clients c
                    WHERE c.id = {table}.{column}
                      AND c.tenant_id = {table}.tenant_id
               )
        """,
    )


def _project_only_rule(
    *,
    table: str,
    column: str = "project_id",
    code: str,
    label: str,
    detail: str,
    item_label: str,
) -> IntegrityRule:
    return IntegrityRule(
        code=code,
        label=label,
        detail=detail,
        action=f"clear {column}",
        select_sql=f"""
            SELECT t.id, {item_label} AS item, t.{column} AS bad_value,
                   'project link points outside this studio' AS reason
              FROM {table} t
              LEFT JOIN projects p ON p.id = t.{column} AND p.tenant_id = t.tenant_id
             WHERE t.tenant_id = ? AND t.{column} IS NOT NULL AND p.id IS NULL
             ORDER BY t.id
        """,
        repair_sql=f"""
            UPDATE {table}
               SET {column} = NULL
             WHERE tenant_id = ? AND {column} IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM projects p
                    WHERE p.id = {table}.{column}
                      AND p.tenant_id = {table}.tenant_id
               )
        """,
    )


def _client_project_rules(
    *,
    table: str,
    code_prefix: str,
    noun: str,
    item_label: str,
) -> list[IntegrityRule]:
    return [
        _client_only_rule(
            table=table,
            column="client_id",
            code=f"{code_prefix}.invalid_client",
            label=f"{noun}: invalid client links",
            detail=f"{noun} whose client link no longer belongs to this studio.",
            item_label=item_label,
        ),
        _project_only_rule(
            table=table,
            code=f"{code_prefix}.invalid_project",
            label=f"{noun}: invalid project links",
            detail=f"{noun} whose project link no longer belongs to this studio.",
            item_label=item_label,
        ),
        IntegrityRule(
            code=f"{code_prefix}.mismatched_project",
            label=f"{noun}: client/project mismatches",
            detail=f"{noun} linked to a valid client and a valid project for a different client.",
            action="clear project_id",
            select_sql=f"""
                SELECT t.id, {item_label} AS item, t.project_id AS bad_value,
                       'project belongs to a different client' AS reason
                  FROM {table} t
                  JOIN clients c ON c.id = t.client_id AND c.tenant_id = t.tenant_id
                  JOIN projects p ON p.id = t.project_id AND p.tenant_id = t.tenant_id
                 WHERE t.tenant_id = ?
                   AND t.client_id IS NOT NULL
                   AND t.project_id IS NOT NULL
                   AND NOT (p.client_id IS t.client_id)
                 ORDER BY t.id
            """,
            repair_sql=f"""
                UPDATE {table}
                   SET project_id = NULL
                 WHERE tenant_id = ?
                   AND client_id IS NOT NULL
                   AND project_id IS NOT NULL
                   AND EXISTS (
                       SELECT 1 FROM clients c
                        WHERE c.id = {table}.client_id
                          AND c.tenant_id = {table}.tenant_id
                   )
                   AND EXISTS (
                       SELECT 1 FROM projects p
                        WHERE p.id = {table}.project_id
                          AND p.tenant_id = {table}.tenant_id
                          AND NOT (p.client_id IS {table}.client_id)
                   )
            """,
        ),
    ]


def _manual_parent_rule(
    *,
    table: str,
    parent_table: str,
    parent_column: str,
    code: str,
    label: str,
    detail: str,
    item_label: str,
) -> IntegrityRule:
    return IntegrityRule(
        code=code,
        label=label,
        detail=detail,
        action="manual review",
        select_sql=f"""
            SELECT t.id, {item_label} AS item, t.{parent_column} AS bad_value,
                   '{parent_column} points outside this studio' AS reason
              FROM {table} t
              LEFT JOIN {parent_table} p
                ON p.id = t.{parent_column} AND p.tenant_id = t.tenant_id
             WHERE t.tenant_id = ? AND p.id IS NULL
             ORDER BY t.id
        """,
    )


_TITLE = "COALESCE(NULLIF(t.title, ''), '#' || t.id)"
_NAME = "COALESCE(NULLIF(t.name, ''), '#' || t.id)"

RULES: tuple[IntegrityRule, ...] = (
    _client_only_rule(
        table="projects",
        column="client_id",
        code="projects.invalid_client",
        label="Projects: invalid client links",
        detail="Projects whose client no longer belongs to this studio.",
        item_label=_NAME,
    ),
    _client_only_rule(
        table="projects",
        column="referred_by_client_id",
        code="projects.invalid_referrer",
        label="Projects: invalid referral links",
        detail="Projects credited to a referrer outside this studio.",
        item_label=_NAME,
    ),
    *_client_project_rules(
        table="invoices",
        code_prefix="invoices",
        noun="Invoices",
        item_label=_TITLE,
    ),
    *_client_project_rules(
        table="payment_plans",
        code_prefix="payment_plans",
        noun="Payment plans",
        item_label=_TITLE,
    ),
    *_client_project_rules(
        table="recurring_invoices",
        code_prefix="recurring_invoices",
        noun="Recurring invoice profiles",
        item_label=_TITLE,
    ),
    *_client_project_rules(
        table="contracts",
        code_prefix="contracts",
        noun="Contracts",
        item_label=_TITLE,
    ),
    *_client_project_rules(
        table="questionnaires",
        code_prefix="questionnaires",
        noun="Questionnaires",
        item_label=_TITLE,
    ),
    *_client_project_rules(
        table="appointments",
        code_prefix="appointments",
        noun="Appointments",
        item_label=_TITLE,
    ),
    _client_only_rule(
        table="testimonials",
        column="client_id",
        code="testimonials.invalid_client",
        label="Testimonials: invalid client links",
        detail="Testimonials whose requested client no longer belongs to this studio.",
        item_label="COALESCE(NULLIF(t.author_name, ''), '#' || t.id)",
    ),
    _project_only_rule(
        table="galleries",
        code="galleries.invalid_project",
        label="Galleries: invalid project links",
        detail="Galleries attached to a project outside this studio.",
        item_label=_TITLE,
    ),
    _project_only_rule(
        table="content_packs",
        code="content_packs.invalid_project",
        label="Content packs: invalid project links",
        detail="Content packs attached to a project outside this studio.",
        item_label=_TITLE,
    ),
    _project_only_rule(
        table="expenses",
        code="expenses.invalid_project",
        label="Expenses: invalid project links",
        detail="Expenses attached to a project outside this studio.",
        item_label="COALESCE(NULLIF(t.description, ''), '#' || t.id)",
    ),
    IntegrityRule(
        code="galleries.invalid_cover",
        label="Galleries: invalid cover images",
        detail="Gallery covers that no longer point to an image in that gallery.",
        action="clear cover_image_id",
        select_sql="""
            SELECT g.id, COALESCE(NULLIF(g.title, ''), '#' || g.id) AS item,
                   g.cover_image_id AS bad_value,
                   'cover image is not in this gallery' AS reason
              FROM galleries g
              LEFT JOIN images i
                ON i.id = g.cover_image_id
               AND i.gallery_id = g.id
               AND i.tenant_id = g.tenant_id
             WHERE g.tenant_id = ?
               AND g.cover_image_id IS NOT NULL
               AND i.id IS NULL
             ORDER BY g.id
        """,
        repair_sql="""
            UPDATE galleries
               SET cover_image_id = NULL
             WHERE tenant_id = ?
               AND cover_image_id IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM images i
                    WHERE i.id = galleries.cover_image_id
                      AND i.gallery_id = galleries.id
                      AND i.tenant_id = galleries.tenant_id
               )
        """,
    ),
    IntegrityRule(
        code="image_favorites.invalid_target",
        label="Proofing favorites: invalid targets",
        detail="Favorites whose gallery or image target is outside this studio.",
        action="delete invalid favorite rows",
        select_sql="""
            SELECT f.id, 'Favorite #' || f.id AS item, f.gallery_id AS bad_value,
                   'favorite does not match a tenant-owned gallery image' AS reason
              FROM image_favorites f
              LEFT JOIN galleries g ON g.id = f.gallery_id AND g.tenant_id = f.tenant_id
              LEFT JOIN images i
                ON i.id = f.image_id
               AND i.gallery_id = f.gallery_id
               AND i.tenant_id = f.tenant_id
             WHERE f.tenant_id = ? AND (g.id IS NULL OR i.id IS NULL)
             ORDER BY f.id
        """,
        repair_sql="""
            DELETE FROM image_favorites
             WHERE tenant_id = ?
               AND (
                   NOT EXISTS (
                       SELECT 1 FROM galleries g
                        WHERE g.id = image_favorites.gallery_id
                          AND g.tenant_id = image_favorites.tenant_id
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM images i
                        WHERE i.id = image_favorites.image_id
                          AND i.gallery_id = image_favorites.gallery_id
                          AND i.tenant_id = image_favorites.tenant_id
                   )
               )
        """,
    ),
    IntegrityRule(
        code="image_comments.invalid_target",
        label="Proofing comments: invalid targets",
        detail="Comments whose gallery or image target is outside this studio.",
        action="delete invalid comment rows",
        select_sql="""
            SELECT c.id, 'Comment #' || c.id AS item, c.gallery_id AS bad_value,
                   'comment does not match a tenant-owned gallery image' AS reason
              FROM image_comments c
              LEFT JOIN galleries g ON g.id = c.gallery_id AND g.tenant_id = c.tenant_id
              LEFT JOIN images i
                ON i.id = c.image_id
               AND i.gallery_id = c.gallery_id
               AND i.tenant_id = c.tenant_id
             WHERE c.tenant_id = ? AND (g.id IS NULL OR i.id IS NULL)
             ORDER BY c.id
        """,
        repair_sql="""
            DELETE FROM image_comments
             WHERE tenant_id = ?
               AND (
                   NOT EXISTS (
                       SELECT 1 FROM galleries g
                        WHERE g.id = image_comments.gallery_id
                          AND g.tenant_id = image_comments.tenant_id
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM images i
                        WHERE i.id = image_comments.image_id
                          AND i.gallery_id = image_comments.gallery_id
                          AND i.tenant_id = image_comments.tenant_id
                   )
               )
        """,
    ),
    _manual_parent_rule(
        table="images",
        parent_table="galleries",
        parent_column="gallery_id",
        code="images.invalid_gallery",
        label="Images: invalid gallery links",
        detail="Images whose gallery belongs to another studio or is missing.",
        item_label="COALESCE(NULLIF(t.filename, ''), '#' || t.id)",
    ),
    _manual_parent_rule(
        table="offers",
        parent_table="galleries",
        parent_column="gallery_id",
        code="offers.invalid_gallery",
        label="Offers: invalid gallery links",
        detail="Offers whose required gallery belongs to another studio or is missing.",
        item_label=_TITLE,
    ),
    _manual_parent_rule(
        table="albums",
        parent_table="galleries",
        parent_column="gallery_id",
        code="albums.invalid_gallery",
        label="Albums: invalid gallery links",
        detail="Albums whose required gallery belongs to another studio or is missing.",
        item_label=_TITLE,
    ),
    _manual_parent_rule(
        table="product_sets",
        parent_table="galleries",
        parent_column="gallery_id",
        code="product_sets.invalid_gallery",
        label="Product sets: invalid gallery links",
        detail="Product sets whose required gallery belongs to another studio or is missing.",
        item_label="'Product set #' || t.id",
    ),
    _manual_parent_rule(
        table="sales_campaigns",
        parent_table="galleries",
        parent_column="gallery_id",
        code="sales_campaigns.invalid_gallery",
        label="Sales campaigns: invalid gallery links",
        detail="Sales campaigns whose required gallery belongs to another studio or is missing.",
        item_label="COALESCE(NULLIF(t.headline, ''), '#' || t.id)",
    ),
    _manual_parent_rule(
        table="project_tasks",
        parent_table="projects",
        parent_column="project_id",
        code="project_tasks.invalid_project",
        label="Project tasks: invalid project links",
        detail="Tasks whose required project belongs to another studio or is missing.",
        item_label="COALESCE(NULLIF(t.label, ''), '#' || t.id)",
    ),
    _manual_parent_rule(
        table="project_files",
        parent_table="projects",
        parent_column="project_id",
        code="project_files.invalid_project",
        label="Project files: invalid project links",
        detail="Files whose required project belongs to another studio or is missing.",
        item_label="COALESCE(NULLIF(t.filename, ''), '#' || t.id)",
    ),
)


def integrity_report(
    conn: sqlite3.Connection,
    tenant_id: str,
    *,
    sample_limit: int = 25,
) -> dict:
    rules = []
    total = repairable_total = manual_total = 0
    for rule in RULES:
        rows = [dict(r) for r in conn.execute(rule.select_sql, (tenant_id,)).fetchall()]
        count = len(rows)
        total += count
        if rule.repairable:
            repairable_total += count
        else:
            manual_total += count
        rules.append({
            "code": rule.code,
            "label": rule.label,
            "detail": rule.detail,
            "action": rule.action,
            "repairable": rule.repairable,
            "count": count,
            "rows": rows[:sample_limit],
            "more": max(0, count - sample_limit),
        })
    return {
        "total": total,
        "repairable_total": repairable_total,
        "manual_total": manual_total,
        "clean": total == 0,
        "rules": rules,
        "active_rules": [r for r in rules if r["count"]],
    }


def repair_integrity(conn: sqlite3.Connection, tenant_id: str) -> dict:
    fixes = []
    fixed_total = 0
    for rule in RULES:
        if not rule.repair_sql:
            continue
        cur = conn.execute(rule.repair_sql, (tenant_id,))
        fixed = max(0, cur.rowcount)
        if fixed:
            fixed_total += fixed
            fixes.append({"code": rule.code, "label": rule.label, "fixed": fixed})
    if fixed_total:
        audit(conn, actor="owner", action="integrity.repaired", tenant_id=tenant_id,
              detail=f"{fixed_total} hidden relationship issue(s) repaired")
    return {
        "fixed_total": fixed_total,
        "fixes": fixes,
        "report": integrity_report(conn, tenant_id),
    }


def tenant_integrity_overview(conn: sqlite3.Connection) -> dict:
    """Cross-tenant operator summary. Read-only: repair remains tenant-scoped."""
    rows = conn.execute(
        "SELECT id, name, slug FROM tenants ORDER BY created_at DESC, name"
    ).fetchall()
    tenants = []
    total = repairable_total = manual_total = dirty = 0
    for tenant in rows:
        report = integrity_report(conn, tenant["id"], sample_limit=0)
        active = report["active_rules"]
        manual_rules = [
            {"label": r["label"], "count": r["count"]}
            for r in active if not r["repairable"]
        ]
        repairable_rules = [
            {"label": r["label"], "count": r["count"]}
            for r in active if r["repairable"]
        ]
        if report["total"]:
            dirty += 1
        total += report["total"]
        repairable_total += report["repairable_total"]
        manual_total += report["manual_total"]
        tenants.append({
            "id": tenant["id"],
            "name": tenant["name"],
            "slug": tenant["slug"],
            "total": report["total"],
            "repairable_total": report["repairable_total"],
            "manual_total": report["manual_total"],
            "clean": report["clean"],
            "manual_rules": manual_rules,
            "repairable_rules": repairable_rules,
        })
    tenants.sort(key=lambda t: (t["total"] == 0, -t["manual_total"], -t["repairable_total"], t["name"].lower()))
    return {
        "tenants": tenants,
        "tenant_count": len(tenants),
        "dirty_tenants": dirty,
        "clean_tenants": len(tenants) - dirty,
        "total": total,
        "repairable_total": repairable_total,
        "manual_total": manual_total,
    }
