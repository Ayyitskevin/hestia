"""Read-only migration-state evidence for an offline Hestia database.

This module deliberately does not import or call init_db. It opens only an
isolated, sidecar-free snapshot with SQLite's immutable read-only URI, inventories
the ledger, and classifies the known migration-0065 history split without changing
schema or data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import MIGRATIONS_DIR

EXIT_CURRENT = 0
EXIT_DECISION_REQUIRED = 1
EXIT_INCONSISTENT = 2
EXIT_INPUT_ERROR = 3
_SOURCE_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_CURRENT_0065_INDEX_SQL = (
    "create unique index idx_images_access_token on images(access_token) "
    "where access_token is not null"
)
_HISTORICAL_0065_INDEX_SQL = "create unique index idx_images_access_token on images(access_token)"
_HESTIA_BASELINE_COLUMNS = {
    "tenants": {"id", "slug", "name"},
    "galleries": {"id", "tenant_id", "project_id", "slug", "title"},
    "images": {"id", "gallery_id", "tenant_id", "filename", "storage_key"},
}


class MigrationAuditError(RuntimeError):
    """The requested database cannot be audited safely."""


def _version_key(value: str) -> tuple[int, int | str]:
    if value.isdigit():
        return (0, int(value))
    return (1, value)


def _source_inventory(migrations_dir: Path) -> dict[str, Any]:
    migrations_dir = migrations_dir.resolve()
    manifest_path = migrations_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationAuditError(f"cannot read migration manifest {manifest_path}: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format_version") != 1
        or not isinstance(manifest.get("files"), dict)
    ):
        raise MigrationAuditError(f"unsupported or malformed migration manifest: {manifest_path}")

    manifest_files: dict[str, str] = {}
    expected: dict[str, str] = {}
    for filename, digest in manifest["files"].items():
        if (
            not isinstance(filename, str)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise MigrationAuditError(f"invalid migration manifest entry: {filename!r}")
        match = _SOURCE_RE.fullmatch(filename)
        if not match or match.group(1) in expected:
            raise MigrationAuditError(f"invalid or duplicate migration filename: {filename}")
        manifest_files[filename] = digest
        expected[match.group(1)] = Path(filename).stem

    actual_paths = {path.name: path for path in migrations_dir.glob("*.sql")}
    actual_hashes = {
        filename: hashlib.sha256(path.read_bytes()).hexdigest()
        for filename, path in sorted(actual_paths.items())
    }
    missing = sorted(set(manifest_files) - set(actual_hashes))
    extra = sorted(set(actual_hashes) - set(manifest_files))
    changed = sorted(
        filename
        for filename in set(manifest_files) & set(actual_hashes)
        if manifest_files[filename] != actual_hashes[filename]
    )

    tree = hashlib.sha256()
    alter_table_files = 0
    for filename, digest in sorted(actual_hashes.items()):
        tree.update(filename.encode("utf-8"))
        tree.update(b"\0")
        tree.update(bytes.fromhex(digest))
        if b"ALTER TABLE" in actual_paths[filename].read_bytes().upper():
            alter_table_files += 1

    known_historical = manifest.get("known_historical")
    if not isinstance(known_historical, dict):
        raise MigrationAuditError("migration manifest has malformed historical evidence")
    historical = known_historical.get("0065_image_access_token.sql")
    if not isinstance(historical, dict):
        raise MigrationAuditError("migration manifest lacks historical 0065 evidence")
    original_hash = historical.get("sha256")
    original_commit = historical.get("commit")
    if not isinstance(original_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", original_hash):
        raise MigrationAuditError("migration manifest lacks the known original 0065 hash")
    if not isinstance(original_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", original_commit):
        raise MigrationAuditError("migration manifest lacks the full original 0065 commit")
    return {
        "count": len(actual_hashes),
        "manifest_count": len(manifest_files),
        "head_version": max(expected, key=_version_key) if expected else None,
        "tree_sha256": tree.hexdigest(),
        "repository_state": (
            "manifest_clean" if not (missing or extra or changed) else "repository_checksum_drift"
        ),
        "missing_files": missing,
        "extra_files": extra,
        "changed_files": changed,
        "alter_table_files": alter_table_files,
        "migration_0065_sha256": actual_hashes.get("0065_image_access_token.sql"),
        "manifest_0065_sha256": manifest_files.get("0065_image_access_token.sql"),
        "known_original_0065_sha256": original_hash,
        "known_original_0065_commit": original_commit,
        "_expected": expected,
    }


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    path = db_path.expanduser().resolve()
    if not path.is_file():
        raise MigrationAuditError(f"database does not exist or is not a file: {path}")
    sidecars = [Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal")]
    present = [
        f"{sidecar.name} ({sidecar.stat().st_size} bytes)"
        for sidecar in sidecars
        if sidecar.exists()
    ]
    if present:
        raise MigrationAuditError(
            "SQLite journal sidecar evidence present: "
            f"{', '.join(present)}; audit an isolated SQLite online-backup or restored copy"
        )
    # mode=ro can create -wal/-shm for a WAL-mode file. immutable prevents those
    # writes; refusing sidecars first avoids ignoring known uncheckpointed state.
    # The caller must provide an isolated SQLite online-backup or restored copy.
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _hestia_identity_report(
    conn: sqlite3.Connection,
    *,
    tables: set[str],
) -> dict[str, Any]:
    """Fingerprint the immutable 0001 data spine, not a generic table name."""
    markers: dict[str, dict[str, Any]] = {}
    for table, required_columns in _HESTIA_BASELINE_COLUMNS.items():
        actual_columns = (
            {
                str(row["name"])
                for row in conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
            }
            if table in tables
            else set()
        )
        missing = sorted(required_columns - actual_columns)
        markers[table] = {
            "recognized": not missing,
            "missing_columns": missing,
        }
    return {
        "recognized": all(marker["recognized"] for marker in markers.values()),
        "baseline_markers": markers,
    }


def _normalize_sql(value: str | None) -> str:
    return " ".join((value or "").lower().replace('"', "").split())


def _valid_applied_at(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}",
        value,
    ):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return True


def _ledger_schema_report(conn: sqlite3.Connection, *, exists: bool) -> dict[str, Any]:
    if not exists:
        return {"canonical": None, "columns": [], "primary_key_index": None}

    column_rows = conn.execute("PRAGMA table_xinfo(schema_migrations)").fetchall()
    columns = [
        {
            "name": str(row["name"]),
            "type": str(row["type"]).upper(),
            "not_null": bool(row["notnull"]),
            "default_sql": row["dflt_value"],
            "pk": int(row["pk"]),
            "hidden": int(row["hidden"]),
        }
        for row in column_rows
    ]
    expected_columns = [
        {
            "name": "version",
            "type": "TEXT",
            "not_null": False,
            "default_sql": None,
            "pk": 1,
            "hidden": 0,
        },
        {
            "name": "name",
            "type": "TEXT",
            "not_null": True,
            "default_sql": None,
            "pk": 0,
            "hidden": 0,
        },
        {
            "name": "applied_at",
            "type": "TEXT",
            "not_null": True,
            "default_sql": "datetime('now')",
            "pk": 0,
            "hidden": 0,
        },
    ]

    index_rows = conn.execute("PRAGMA index_list(schema_migrations)").fetchall()
    primary_indexes = [row for row in index_rows if str(row["origin"]) == "pk"]
    primary_report = None
    canonical_primary = False
    if len(primary_indexes) == 1:
        primary = primary_indexes[0]
        primary_name = str(primary["name"])
        xinfo = conn.execute(
            "SELECT * FROM pragma_index_xinfo(?)",
            (primary_name,),
        ).fetchall()
        terms = [
            {
                "cid": int(row["cid"]),
                "name": row["name"],
                "descending": bool(row["desc"]),
                "collation": row["coll"],
                "key": bool(row["key"]),
            }
            for row in xinfo
        ]
        primary_report = {
            "name": primary_name,
            "unique": bool(primary["unique"]),
            "partial": bool(primary["partial"]),
            "terms": terms,
        }
        canonical_primary = (
            bool(primary["unique"])
            and not bool(primary["partial"])
            and len(terms) == 2
            and terms[0]["name"] == "version"
            and terms[0]["key"] is True
            and terms[0]["descending"] is False
            and terms[0]["collation"] == "BINARY"
            and terms[1]["cid"] == -1
            and terms[1]["key"] is False
        )

    return {
        "canonical": columns == expected_columns and canonical_primary,
        "columns": columns,
        "primary_key_index": primary_report,
    }


def _ledger_report(
    conn: sqlite3.Connection,
    *,
    tables: set[str],
    expected: dict[str, str],
) -> tuple[dict[str, Any], set[str]]:
    exists = "schema_migrations" in tables
    schema = _ledger_schema_report(conn, exists=exists)
    applied: dict[str, str] = {}
    duplicate_versions: set[str] = set()
    malformed_rows = 0
    null_applied_at = 0
    invalid_applied_at = 0
    row_count = 0

    column_names = {column["name"] for column in schema["columns"]}
    if exists and {"version", "name"} <= column_names:
        selected = "version, name"
        if "applied_at" in column_names:
            selected += ", applied_at"
        rows = conn.execute(f"SELECT {selected} FROM schema_migrations").fetchall()
        row_count = len(rows)
        for row in rows:
            raw_version = row["version"]
            raw_name = row["name"]
            if raw_version is None or raw_name is None:
                malformed_rows += 1
                continue
            version = str(raw_version)
            name = str(raw_name)
            if not re.fullmatch(r"\d{4}", version) or not re.fullmatch(
                r"\d{4}_[a-z0-9_]+", name
            ):
                malformed_rows += 1
            if "applied_at" in column_names:
                applied_at = row["applied_at"]
                if applied_at is None:
                    null_applied_at += 1
                elif not _valid_applied_at(applied_at):
                    invalid_applied_at += 1
            if version in applied:
                duplicate_versions.add(version)
            else:
                applied[version] = name
    elif exists:
        row_count = int(conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0])

    expected_versions = set(expected)
    applied_versions = set(applied)
    pending = sorted(expected_versions - applied_versions, key=_version_key)
    unknown = sorted(applied_versions - expected_versions, key=_version_key)
    numeric_applied = [int(version) for version in applied_versions if version.isdigit()]
    applied_ceiling = max(numeric_applied) if numeric_applied else -1
    gaps = [
        version for version in pending if version.isdigit() and int(version) < applied_ceiling
    ]
    pending_suffix = [version for version in pending if version not in gaps]
    name_mismatches = [
        {
            "version": version,
            "ledger_name": applied[version],
            "source_name": expected[version],
        }
        for version in sorted(expected_versions & applied_versions, key=_version_key)
        if applied[version] != expected[version]
    ]
    ordered_applied = sorted(applied_versions, key=_version_key)
    return (
        {
            "exists": exists,
            "schema": schema,
            "row_count": row_count,
            "applied_count": len(applied),
            "head_version": ordered_applied[-1] if ordered_applied else None,
            "pending_versions": pending,
            "gaps": gaps,
            "pending_suffix": pending_suffix,
            "unknown_versions": unknown,
            "name_mismatches": name_mismatches,
            "duplicate_versions": sorted(duplicate_versions, key=_version_key),
            "malformed_row_count": malformed_rows,
            "null_applied_at_count": null_applied_at,
            "invalid_applied_at_count": invalid_applied_at,
            "checksum_verification": (
                "unavailable: schema_migrations stores no applied source hashes"
            ),
        },
        applied_versions,
    )


def _image_access_report(
    conn: sqlite3.Connection,
    *,
    tables: set[str],
    ledger_applied: bool,
) -> dict[str, Any]:
    if "images" not in tables:
        return {
            "shape": "images_table_missing",
            "ledger_applied": ledger_applied,
            "column": None,
            "index": None,
            "data": None,
        }

    columns = {str(row["name"]): row for row in conn.execute("PRAGMA table_xinfo(images)")}
    column = columns.get("access_token")
    index_rows = {
        str(row["name"]): row for row in conn.execute("PRAGMA index_list(images)")
    }
    index_terms_by_name: dict[str, list[dict[str, Any]]] = {}
    index_sql_by_name: dict[str, str | None] = {}
    for index_name in index_rows:
        xinfo = conn.execute(
            "SELECT * FROM pragma_index_xinfo(?)",
            (index_name,),
        ).fetchall()
        index_terms_by_name[index_name] = [
            {
                "seqno": int(row["seqno"]),
                "cid": int(row["cid"]),
                "name": row["name"],
                "descending": bool(row["desc"]),
                "collation": row["coll"],
                "key": bool(row["key"]),
            }
            for row in xinfo
        ]
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
        index_sql_by_name[index_name] = sql_row["sql"] if sql_row else None
    access_token_indexes = sorted(
        index_name
        for index_name, terms in index_terms_by_name.items()
        if (
            any(term["key"] and term["name"] == "access_token" for term in terms)
            or re.search(r"\baccess_token\b", _normalize_sql(index_sql_by_name[index_name]))
        )
    )
    unexpected_access_token_indexes = [
        name for name in access_token_indexes if name != "idx_images_access_token"
    ]
    index = index_rows.get("idx_images_access_token")

    if column is None:
        column_report = None
        column_shape = "missing"
    else:
        default = column["dflt_value"]
        declared_type = str(column["type"]).strip().upper()
        not_null = bool(column["notnull"])
        ordinary_text = (
            declared_type == "TEXT"
            and int(column["pk"]) == 0
            and int(column["hidden"]) == 0
        )
        column_report = {
            "declared_type": declared_type,
            "not_null": not_null,
            "default_sql": default,
            "primary_key_position": int(column["pk"]),
            "hidden": int(column["hidden"]),
        }
        if ordinary_text and not not_null and default is None:
            column_shape = "current_nullable"
        elif (
            ordinary_text
            and not_null
            and _normalize_sql(str(default)) in {"''", "('')"}
        ):
            column_shape = "historical_not_null_blank_default"
        else:
            column_shape = "unexpected"

    if index is None:
        index_report = None
        index_shape = "missing"
    else:
        index_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("idx_images_access_token",),
        ).fetchone()
        index_sql = index_sql_row["sql"] if index_sql_row else None
        terms = index_terms_by_name["idx_images_access_token"]
        key_terms = [term for term in terms if term["key"]]
        auxiliary_terms = [term for term in terms if not term["key"]]
        unique = bool(index["unique"])
        partial = bool(index["partial"])
        normalized = _normalize_sql(index_sql)
        canonical_terms = (
            len(key_terms) == 1
            and key_terms[0]["cid"] >= 0
            and key_terms[0]["name"] == "access_token"
            and key_terms[0]["descending"] is False
            and key_terms[0]["collation"] == "BINARY"
            and len(auxiliary_terms) == 1
            and auxiliary_terms[0]["cid"] == -1
            and auxiliary_terms[0]["name"] is None
            and auxiliary_terms[0]["descending"] is False
            and auxiliary_terms[0]["collation"] == "BINARY"
        )
        index_report = {
            "unique": unique,
            "partial": partial,
            "origin": str(index["origin"]),
            "terms": terms,
            "sql": index_sql,
            "other_access_token_indexes": unexpected_access_token_indexes,
        }
        if (
            unique
            and partial
            and str(index["origin"]) == "c"
            and canonical_terms
            and not unexpected_access_token_indexes
            and normalized == _CURRENT_0065_INDEX_SQL
        ):
            index_shape = "current_partial_unique"
        elif (
            unique
            and not partial
            and str(index["origin"]) == "c"
            and canonical_terms
            and not unexpected_access_token_indexes
            and normalized == _HISTORICAL_0065_INDEX_SQL
        ):
            index_shape = "historical_full_unique"
        else:
            index_shape = "unexpected"

    if column is None:
        data_report = None
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS images, "
            "SUM(CASE WHEN access_token IS NULL THEN 1 ELSE 0 END) AS null_tokens, "
            "SUM(CASE WHEN access_token = '' THEN 1 ELSE 0 END) AS blank_tokens "
            "FROM images"
        ).fetchone()
        duplicate_groups = conn.execute(
            "SELECT COUNT(*) AS n FROM ("
            "SELECT access_token FROM images WHERE access_token IS NOT NULL "
            "GROUP BY access_token HAVING COUNT(*) > 1"
            ")"
        ).fetchone()["n"]
        data_report = {
            "images": int(row["images"]),
            "null_tokens": int(row["null_tokens"] or 0),
            "blank_tokens": int(row["blank_tokens"] or 0),
            "duplicate_nonnull_token_groups": int(duplicate_groups),
        }

    if ledger_applied:
        if column_shape == "current_nullable" and index_shape == "current_partial_unique":
            shape = "current"
        elif (
            column_shape == "historical_not_null_blank_default"
            and index_shape == "historical_full_unique"
        ):
            shape = "historical_original"
        elif column_shape == "missing" or index_shape == "missing":
            shape = "ledger_schema_mismatch"
        else:
            shape = "schema_drift"
    elif column_shape == "missing" and index_shape == "missing":
        shape = "pre_0065"
    else:
        shape = "partial_application"

    return {
        "shape": shape,
        "ledger_applied": ledger_applied,
        "column": column_report,
        "index": index_report,
        "data": data_report,
    }


def _database_fingerprint(path: Path) -> dict[str, Any]:
    sidecars = {
        suffix: {
            "size_bytes": Path(f"{path}{suffix}").stat().st_size,
            "mtime_ns": Path(f"{path}{suffix}").stat().st_mtime_ns,
        }
        for suffix in ("-wal", "-shm", "-journal")
        if Path(f"{path}{suffix}").exists()
    }
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise MigrationAuditError(f"database changed while fingerprinting: {path}")
    return {
        "device": before.st_dev,
        "inode": before.st_ino,
        "size_bytes": before.st_size,
        "mtime_ns": before.st_mtime_ns,
        "sha256": digest.hexdigest(),
        "journal_sidecars": sidecars,
    }



def audit_migration_state(
    db_path: str | Path,
    *,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> dict[str, Any]:
    """Inspect one isolated, sidecar-free database snapshot without mutating it."""
    path = Path(db_path).expanduser().resolve()
    source = _source_inventory(migrations_dir)
    expected = source.pop("_expected")
    if not path.is_file():
        raise MigrationAuditError(f"database does not exist or is not a file: {path}")
    before_fingerprint = _database_fingerprint(path)
    conn = _open_readonly(path)
    try:
        conn.execute("BEGIN")
        tables = _tables(conn)
        identity = _hestia_identity_report(conn, tables=tables)
        if not identity["recognized"]:
            raise MigrationAuditError(f"not a recognizable Hestia database: {path}")
        ledger, applied_versions = _ledger_report(conn, tables=tables, expected=expected)
        image_access = _image_access_report(
            conn,
            tables=tables,
            ledger_applied="0065" in applied_versions,
        )
    finally:
        conn.close()
    after_fingerprint = _database_fingerprint(path)
    if after_fingerprint != before_fingerprint:
        raise MigrationAuditError(f"database or journal sidecars changed during audit: {path}")

    findings: list[dict[str, str]] = [
        {
            "level": "info",
            "code": "ledger_checksums_unavailable",
            "detail": ledger["checksum_verification"],
        }
    ]
    shape = image_access["shape"]
    if shape == "current":
        exit_code = EXIT_CURRENT
        findings.append(
            {
                "level": "info",
                "code": "migration_0065_current_shape",
                "detail": "0065 ledger, nullable column, and partial unique index agree",
            }
        )
    elif shape == "historical_original":
        exit_code = EXIT_DECISION_REQUIRED
        findings.append(
            {
                "level": "warning",
                "code": "migration_0065_historical_shape",
                "detail": "known original NOT NULL/default-blank and full-index shape needs policy",
            }
        )
    elif shape == "pre_0065":
        exit_code = EXIT_DECISION_REQUIRED
        findings.append(
            {
                "level": "warning",
                "code": "migration_0065_pending",
                "detail": "0065 has neither a ledger row nor applied schema",
            }
        )
    else:
        exit_code = EXIT_INCONSISTENT
        findings.append(
            {
                "level": "error",
                "code": f"migration_0065_{shape}",
                "detail": "0065 ledger/schema evidence is incomplete or unrecognized",
            }
        )

    if ledger["exists"] and ledger["schema"]["canonical"] is not True:
        exit_code = EXIT_INCONSISTENT
        findings.append(
            {
                "level": "error",
                "code": "migration_ledger_schema_drift",
                "detail": "schema_migrations columns or primary-key index are noncanonical",
            }
        )
    if ledger["duplicate_versions"]:
        exit_code = EXIT_INCONSISTENT
        findings.append(
            {
                "level": "error",
                "code": "duplicate_migration_versions",
                "detail": f"duplicate ledger versions: {', '.join(ledger['duplicate_versions'])}",
            }
        )
    if ledger["malformed_row_count"]:
        exit_code = EXIT_INCONSISTENT
        findings.append(
            {
                "level": "error",
                "code": "malformed_migration_rows",
                "detail": f"malformed ledger rows: {ledger['malformed_row_count']}",
            }
        )
    if ledger["null_applied_at_count"]:
        exit_code = EXIT_INCONSISTENT
        findings.append(
            {
                "level": "error",
                "code": "null_migration_timestamps",
                "detail": f"ledger rows without applied_at: {ledger['null_applied_at_count']}",
            }
        )
    if ledger["invalid_applied_at_count"]:
        exit_code = EXIT_INCONSISTENT
        findings.append(
            {
                "level": "error",
                "code": "invalid_migration_timestamps",
                "detail": (
                    "ledger rows with invalid applied_at: "
                    f"{ledger['invalid_applied_at_count']}"
                ),
            }
        )

    if ledger["unknown_versions"]:
        exit_code = EXIT_INCONSISTENT
        findings.append(
            {
                "level": "error",
                "code": "unknown_applied_versions",
                "detail": f"ledger-only versions: {', '.join(ledger['unknown_versions'])}",
            }
        )
    if ledger["name_mismatches"]:
        exit_code = EXIT_INCONSISTENT
        findings.append(
            {
                "level": "error",
                "code": "migration_name_mismatch",
                "detail": "one or more applied version names disagree with packaged sources",
            }
        )
    if ledger["gaps"]:
        exit_code = EXIT_INCONSISTENT
        findings.append(
            {
                "level": "error",
                "code": "migration_ledger_gap",
                "detail": f"missing below a later applied version: {', '.join(ledger['gaps'])}",
            }
        )
    elif ledger["pending_versions"]:
        if exit_code == EXIT_CURRENT:
            exit_code = EXIT_DECISION_REQUIRED
        findings.append(
            {
                "level": "warning",
                "code": "pending_migrations",
                "detail": f"source-only versions: {', '.join(ledger['pending_versions'])}",
            }
        )

    if source["repository_state"] != "manifest_clean":
        exit_code = EXIT_INCONSISTENT
        findings.append(
            {
                "level": "error",
                "code": "repository_checksum_drift",
                "detail": (
                    f"missing={source['missing_files']}, extra={source['extra_files']}, "
                    f"changed={source['changed_files']}"
                ),
            }
        )

    data_report = image_access["data"] or {}
    if data_report.get("duplicate_nonnull_token_groups", 0):
        exit_code = EXIT_INCONSISTENT
        findings.append(
            {
                "level": "error",
                "code": "duplicate_image_access_tokens",
                "detail": "duplicate non-null access-token groups exist",
            }
        )

    classification = {
        EXIT_CURRENT: "observed_current",
        EXIT_DECISION_REQUIRED: "decision_required",
        EXIT_INCONSISTENT: "inconsistent",
    }[exit_code]
    return {
        "format_version": 1,
        "database": str(path),
        "read_only": True,
        "identity": identity,
        "snapshot_evidence": {
            "database_sha256": before_fingerprint["sha256"],
            "size_bytes": before_fingerprint["size_bytes"],
            "journal_sidecars": [],
            "unchanged_during_audit": True,
        },

        "classification": classification,
        "exit_code": exit_code,
        "source": source,
        "ledger": ledger,
        "migration_0065": image_access,
        "checksum_evidence": {
            "repository_current_sha256": source["migration_0065_sha256"],
            "manifest_current_sha256": source["manifest_0065_sha256"],
            "known_original_sha256": source["known_original_0065_sha256"],
            "known_original_commit": source["known_original_0065_commit"],
            "database_applied_sha256": None,
            "database_checksum_attestation": "unavailable",
            "schema_signature_match": image_access["shape"],
        },
        "findings": findings,
    }


def print_report(report: dict[str, Any]) -> None:
    print("== Hestia migration audit (read-only) ==")
    print(f"database: {report['database']}")
    print(f"classification: {report['classification']}")
    print(
        "source: "
        f"{report['source']['count']} migrations, head={report['source']['head_version']}, "
        f"repository={report['source']['repository_state']}, "
        f"tree_sha256={report['source']['tree_sha256']}"
    )
    print(
        "ledger: "
        f"exists={report['ledger']['exists']}, "
        f"applied={report['ledger']['applied_count']}, "
        f"head={report['ledger']['head_version']}"
    )
    print(f"migration 0065 shape: {report['migration_0065']['shape']}")
    for finding in report["findings"]:
        print(f"{finding['level'].upper():7} {finding['code']}: {finding['detail']}")
    print("No schema or data changes were attempted.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect an isolated Hestia SQLite snapshot without changing it."
    )
    parser.add_argument("database", help="Path to a sidecar-free online-backup or restored copy.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)
    try:
        report = audit_migration_state(args.database)
    except (MigrationAuditError, OSError, sqlite3.Error) as exc:
        print(f"migration audit error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_report(report)
    return int(report["exit_code"])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
