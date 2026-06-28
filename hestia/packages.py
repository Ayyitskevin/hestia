"""Service packages — a studio's reusable "service menu".

Photographers quote the same offerings over and over (a wedding collection, a
mini-session, an album add-on). A package is reference data priced once and pulled
into the invoice builder as a starting point — the invoice copies the amount, so a
package can be re-priced or archived later without touching past invoices. Money is
integer cents; everything is tenant-scoped. Packages are soft-archived (``active = 0``)
rather than deleted, so the catalog can be tidied without losing history.
"""

from __future__ import annotations

import sqlite3


def create_package(
    conn: sqlite3.Connection, *, tenant_id: str, name: str, description: str = "",
    price_cents: int = 0, deposit_cents: int = 0,
) -> dict | None:
    """Add a package to the studio's catalog. Returns None for a blank name."""
    name = (name or "").strip()
    if not name:
        return None
    row = conn.execute(
        "SELECT COALESCE(MAX(position), 0) AS m FROM service_packages WHERE tenant_id = ?",
        (tenant_id,),
    ).fetchone()
    pos = (row["m"] if row else 0) + 1
    cur = conn.execute(
        "INSERT INTO service_packages (tenant_id, name, description, price_cents, "
        "deposit_cents, position) VALUES (?, ?, ?, ?, ?, ?)",
        (tenant_id, name[:200], (description or "").strip()[:2000],
         max(0, int(price_cents)), max(0, int(deposit_cents)), pos),
    )
    return get_package(conn, tenant_id, cur.lastrowid)


def get_package(conn: sqlite3.Connection, tenant_id: str, package_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM service_packages WHERE id = ? AND tenant_id = ?", (package_id, tenant_id)
    ).fetchone()
    return dict(row) if row else None


def list_packages(
    conn: sqlite3.Connection, tenant_id: str, *, active_only: bool = False
) -> list[dict]:
    """The tenant's packages — active first, then by position. ``active_only`` skips
    archived ones (used for the invoice-builder picker)."""
    sql = "SELECT * FROM service_packages WHERE tenant_id = ?"
    if active_only:
        sql += " AND active = 1"
    sql += " ORDER BY active DESC, position, id"
    return [dict(r) for r in conn.execute(sql, (tenant_id,)).fetchall()]


def update_package(
    conn: sqlite3.Connection, tenant_id: str, package_id: int, *, name: str,
    description: str = "", price_cents: int = 0, deposit_cents: int = 0,
) -> bool:
    """Edit a package in place. Returns True if a row of this tenant's was updated.
    A blank name is rejected (returns False)."""
    name = (name or "").strip()
    if not name:
        return False
    cur = conn.execute(
        "UPDATE service_packages SET name = ?, description = ?, price_cents = ?, "
        "deposit_cents = ?, updated_at = datetime('now') WHERE id = ? AND tenant_id = ?",
        (name[:200], (description or "").strip()[:2000], max(0, int(price_cents)),
         max(0, int(deposit_cents)), package_id, tenant_id),
    )
    return cur.rowcount == 1


def set_package_active(
    conn: sqlite3.Connection, tenant_id: str, package_id: int, active: bool
) -> None:
    """Archive (active=False) or restore (active=True) a package — tenant-scoped."""
    conn.execute(
        "UPDATE service_packages SET active = ?, updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ?",
        (1 if active else 0, package_id, tenant_id),
    )
