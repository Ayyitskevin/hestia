"""Project file attachments — the studio's per-project document workspace.

The owner attaches reference files to a project (a signed PDF, a shot list, a mood board,
a vendor COI). Blobs live in :mod:`hestia.storage`; rows are tenant-scoped. Owner-only —
no public surface (downloads go through an authenticated, tenant-scoped route).
"""

from __future__ import annotations

import io
import sqlite3

from .storage import Storage

_MAX_FILE_BYTES = 25_000_000   # 25 MB/file — generous for PDFs/docs, but bounded


def add_project_file(
    conn: sqlite3.Connection, storage: Storage, *, tenant_id: str, project_id: int,
    filename: str, fileobj, content_type: str = "application/octet-stream",
) -> dict | None:
    """Attach a file to a project this studio owns. Returns None if the project isn't the
    tenant's, or the upload is empty / over the size cap. Inserts the row first to get an id,
    then writes the blob under a tenant/project-scoped key (mirrors gallery image upload)."""
    if not conn.execute(
        "SELECT 1 FROM projects WHERE id = ? AND tenant_id = ?", (project_id, tenant_id)
    ).fetchone():
        return None                                   # not this studio's project
    data = fileobj.read()
    if not data or len(data) > _MAX_FILE_BYTES:
        return None
    name = (filename or "").strip()[:255] or "file"
    ext = name.rsplit(".", 1)[-1] if "." in name else "bin"
    cur = conn.execute(
        "INSERT INTO project_files (tenant_id, project_id, filename, storage_key, "
        "content_type, bytes) VALUES (?, ?, ?, '', ?, ?)",
        (tenant_id, project_id, name, content_type, len(data)),
    )
    file_id = cur.lastrowid
    key = f"{tenant_id}/project-files/{project_id}/{file_id}.{ext}"
    storage.put(key, io.BytesIO(data), content_type)
    conn.execute("UPDATE project_files SET storage_key = ? WHERE id = ?", (key, file_id))
    return get_project_file(conn, tenant_id, file_id)


def get_project_file(conn: sqlite3.Connection, tenant_id: str, file_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM project_files WHERE id = ? AND tenant_id = ?", (file_id, tenant_id)
    ).fetchone()
    return dict(row) if row else None


def list_project_files(conn: sqlite3.Connection, tenant_id: str, project_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM project_files WHERE tenant_id = ? AND project_id = ? ORDER BY id DESC",
        (tenant_id, project_id),
    ).fetchall()
    return [dict(r) for r in rows]


def list_client_files(conn: sqlite3.Connection, tenant_id: str, client_id: int) -> list[dict]:
    """Every file across the client's projects — for the client portal. Joins through
    projects so only files on a project belonging to this client are returned."""
    rows = conn.execute(
        "SELECT pf.* FROM project_files pf "
        "JOIN projects p ON p.id = pf.project_id AND p.tenant_id = pf.tenant_id "
        "WHERE pf.tenant_id = ? AND p.client_id = ? ORDER BY pf.id DESC",
        (tenant_id, client_id),
    ).fetchall()
    return [dict(r) for r in rows]


def get_client_file(conn: sqlite3.Connection, tenant_id: str, client_id: int,
                    file_id: int) -> dict | None:
    """A single file ONLY IF it belongs to a project of this client — the portal download
    gate. A client's token therefore can't reach another client's (or tenant's) files."""
    row = conn.execute(
        "SELECT pf.* FROM project_files pf "
        "JOIN projects p ON p.id = pf.project_id AND p.tenant_id = pf.tenant_id "
        "WHERE pf.id = ? AND pf.tenant_id = ? AND p.client_id = ?",
        (file_id, tenant_id, client_id),
    ).fetchone()
    return dict(row) if row else None


def delete_project_file(conn: sqlite3.Connection, storage: Storage, tenant_id: str,
                        file_id: int, *, project_id: int | None = None) -> bool:
    """Remove a file (tenant/project-scoped). Drops the row, then the blob best-effort."""
    f = get_project_file(conn, tenant_id, file_id)
    if not f:
        return False
    if project_id is not None and f["project_id"] != project_id:
        return False
    sql = "DELETE FROM project_files WHERE id = ? AND tenant_id = ?"
    params: list = [file_id, tenant_id]
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    cur = conn.execute(sql, params)
    if cur.rowcount <= 0:
        return False
    if f.get("storage_key"):
        try:
            storage.delete(f["storage_key"])
        except Exception:  # noqa: BLE001 - blob cleanup is best-effort; the row is gone
            pass
    return True
