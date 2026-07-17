"""Tenant-scoped storage metadata rollups.

This module measures only byte metadata Hestia already persists for gallery originals
and project attachments. It deliberately does not enumerate a local/S3 backend or claim
physical/billable storage: thumbnails, generated product renders, orphaned objects,
provider overhead/versioning/replication, requests, and transfer have no trustworthy
per-object byte ledger yet.
"""

from __future__ import annotations

import sqlite3

from .galleries import _MAX_IMAGE_BYTES
from .project_files import _MAX_FILE_BYTES

_GIB = 1024**3
_TOP_TENANTS = 25

_TENANT_OBJECT_ROWS = """
SELECT i.tenant_id, 'gallery_original' AS kind, i.bytes, i.storage_key, i.thumb_key
FROM galleries AS g
CROSS JOIN images AS i
WHERE g.tenant_id = ? AND i.gallery_id = g.id AND i.tenant_id = g.tenant_id
UNION ALL
SELECT pf.tenant_id, 'project_file' AS kind, pf.bytes, pf.storage_key, NULL AS thumb_key
FROM projects AS p
CROSS JOIN project_files AS pf
WHERE p.tenant_id = ? AND pf.project_id = p.id AND pf.tenant_id = p.tenant_id
"""

_ALL_OBJECT_ROWS = """
SELECT i.tenant_id, 'gallery_original' AS kind, i.bytes, i.storage_key, i.thumb_key
FROM images AS i
JOIN galleries AS g ON g.id = i.gallery_id AND g.tenant_id = i.tenant_id
UNION ALL
SELECT pf.tenant_id, 'project_file' AS kind, pf.bytes, pf.storage_key, NULL AS thumb_key
FROM project_files AS pf
JOIN projects AS p ON p.id = pf.project_id AND p.tenant_id = pf.tenant_id
"""


def format_storage_bytes(value) -> str:
    """Format a nonnegative byte count with binary units; malformed values fail to zero."""
    size = value if type(value) is int and value >= 0 else 0
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    if size < 1024:
        return f"{size} B"
    amount = float(size)
    unit = units[0]
    for unit in units[1:]:
        amount /= 1024
        if amount < 1024 or unit == units[-1]:
            break
    return f"{amount:.1f} {unit}"


def _new_usage(
    tenant_id: str = "",
    *,
    name: str = "",
    slug: str = "",
) -> dict:
    return {
        "tenant_id": tenant_id,
        "name": name,
        "slug": slug,
        "gallery_originals": {
            "objects": 0,
            "tracked_objects": 0,
            "bytes": 0,
        },
        "project_files": {
            "objects": 0,
            "tracked_objects": 0,
            "bytes": 0,
        },
        "unmetered_thumbnail_objects": 0,
    }


def _valid_bytes(kind: str, value, storage_key) -> int | None:
    # Reuse each ingestion path's source-of-truth ceiling. Values above it could
    # not have been written by that path and are corrupt metadata, not usage.
    maximum = _MAX_IMAGE_BYTES if kind == "gallery_original" else _MAX_FILE_BYTES
    if (
        type(value) is not int
        or value < 0
        or value > maximum
        or not isinstance(storage_key, str)
        or not storage_key.strip()
    ):
        return None
    return value


def _accumulate(usage: dict, row: sqlite3.Row) -> None:
    category_name = (
        "gallery_originals" if row["kind"] == "gallery_original" else "project_files"
    )
    category = usage[category_name]
    category["objects"] += 1
    byte_count = _valid_bytes(row["kind"], row["bytes"], row["storage_key"])
    if byte_count is not None:
        category["tracked_objects"] += 1
        category["bytes"] += byte_count
    if (
        row["kind"] == "gallery_original"
        and isinstance(row["thumb_key"], str)
        and row["thumb_key"].strip()
    ):
        usage["unmetered_thumbnail_objects"] += 1


def _finalize(usage: dict) -> dict:
    for name in ("gallery_originals", "project_files"):
        category = usage[name]
        category["unknown_objects"] = (
            category["objects"] - category["tracked_objects"]
        )
        category["display"] = format_storage_bytes(category["bytes"])
    usage["tracked_bytes"] = (
        usage["gallery_originals"]["bytes"] + usage["project_files"]["bytes"]
    )
    usage["tracked_display"] = format_storage_bytes(usage["tracked_bytes"])
    usage["tracked_gib_display"] = f"{usage['tracked_bytes'] / _GIB:.6f} GiB"
    usage["object_rows"] = (
        usage["gallery_originals"]["objects"] + usage["project_files"]["objects"]
    )
    usage["tracked_object_rows"] = (
        usage["gallery_originals"]["tracked_objects"]
        + usage["project_files"]["tracked_objects"]
    )
    usage["unknown_object_rows"] = (
        usage["gallery_originals"]["unknown_objects"]
        + usage["project_files"]["unknown_objects"]
    )
    return usage


def tenant_storage_usage(
    conn: sqlite3.Connection,
    tenant_id: str,
) -> dict:
    """Return one studio's tracked upload metadata without touching object storage."""
    usage = _new_usage(tenant_id)
    for row in conn.execute(_TENANT_OBJECT_ROWS, (tenant_id, tenant_id)):
        _accumulate(usage, row)
    return _finalize(usage)


def _merge_usage(target: dict, source: dict) -> None:
    for name in ("gallery_originals", "project_files"):
        for field in ("objects", "tracked_objects", "bytes"):
            target[name][field] += source[name][field]
    target["unmetered_thumbnail_objects"] += source[
        "unmetered_thumbnail_objects"
    ]


def operator_storage_summary(
    conn: sqlite3.Connection,
    *,
    limit: int = _TOP_TENANTS,
) -> dict:
    """Return one set-based metadata rollup for the master-admin system surface."""
    by_tenant: dict[str, dict] = {}
    for row in conn.execute(_ALL_OBJECT_ROWS):
        usage = by_tenant.setdefault(row["tenant_id"], _new_usage(row["tenant_id"]))
        _accumulate(usage, row)

    tenants: list[dict] = []
    total = _new_usage()
    for tenant in conn.execute(
        "SELECT id, name, slug FROM tenants ORDER BY name COLLATE NOCASE, id"
    ):
        usage = by_tenant.get(tenant["id"], _new_usage(tenant["id"]))
        usage["name"] = tenant["name"]
        usage["slug"] = tenant["slug"]
        _merge_usage(total, usage)
        tenants.append(_finalize(usage))
    tenants.sort(
        key=lambda row: (
            -row["tracked_bytes"],
            row["name"].casefold(),
            row["tenant_id"],
        )
    )
    summary = _finalize(total)
    summary["tenant_count"] = len(tenants)
    safe_limit = min(100, max(1, limit)) if type(limit) is int else _TOP_TENANTS
    summary["tenants"] = tenants[:safe_limit]
    return summary
