"""Disaster-recovery verification: post-restore integrity, media consistency, safety rails.

Pure helpers that shell drills and pytest can drive against scratch trees. Nothing here
touches a live production volume unless an operator deliberately points paths at one —
and even then :func:`assert_safe_restore_target` refuses known production locations
without an explicit override flag.

Diagnostics are privacy-safe: no client tokens, no email bodies, no secrets — only
counts, checksums, statuses, timings, and correlation identifiers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .obs import new_request_id

log = logging.getLogger("hestia.recovery")

# Absolute paths that always look like live production data directories.
_PRODUCTION_ABS_MARKERS = (
    Path("/data"),
    Path("/srv/hestia/data"),
    Path("/var/lib/hestia/data"),
)


def _cwd_default_data_paths() -> tuple[Path, ...]:
    """Resolve the repo-default ``./data`` against the *current* working directory.

    Evaluated at call time so import order / chdir cannot pin a stale absolute path.
    """
    out: list[Path] = []
    for rel in ("./data", "data"):
        try:
            out.append(Path(rel).resolve())
        except OSError:
            continue
    return tuple(out)


class RecoveryError(RuntimeError):
    """A restore or verification step refused to proceed or found fatal damage."""


@dataclass
class MediaBlob:
    """One on-disk media object relative to the media root."""

    relative_path: str
    size_bytes: int
    sha256: str


@dataclass
class DbMediaRef:
    """One images-row storage key (and optional thumb) the DB expects on disk."""

    image_id: int
    tenant_id: str
    gallery_id: int
    storage_key: str
    thumb_key: str | None
    bytes: int | None


@dataclass
class ConsistencyReport:
    """DB ↔ media consistency after a restore or sync."""

    image_rows: int = 0
    media_files: int = 0
    missing_blobs: list[str] = field(default_factory=list)
    missing_thumbs: list[str] = field(default_factory=list)
    orphan_blobs: list[str] = field(default_factory=list)
    size_mismatches: list[dict[str, Any]] = field(default_factory=list)
    checksum_mismatches: list[dict[str, Any]] = field(default_factory=list)
    # Expected-table query failures — never treat as an empty clean studio.
    query_errors: list[str] = field(default_factory=list)
    tenant_ids: list[str] = field(default_factory=list)
    gallery_count: int = 0
    published_gallery_count: int = 0

    @property
    def ok(self) -> bool:
        return (
            not self.missing_blobs
            and not self.size_mismatches
            and not self.checksum_mismatches
            and not self.query_errors
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ok"] = self.ok
        return d


# Backup-set manifest format (privacy-safe; no client PII or secrets).
MANIFEST_FORMAT_VERSION = 1
MANIFEST_FILENAME_SUFFIX = ".manifest.json"


@dataclass
class RestoreVerification:
    """Full post-restore verification payload (structured, privacy-safe)."""

    correlation_id: str
    db_path: str
    media_dir: str | None
    integrity_check: str
    schema_version: str | None
    tenant_count: int
    tenant_ids: list[str]
    gallery_count: int
    published_gallery_count: int
    image_count: int
    representative_gallery: dict[str, Any] | None
    consistency: ConsistencyReport | None
    backup_mtime_iso: str | None
    elapsed_ms: int
    rpo_seconds: float | None
    ok: bool
    failures: list[str] = field(default_factory=list)
    # How to read elapsed_ms / rpo_seconds — never treat CI drill numbers as incident RTO.
    measurement_kind: str = "operator_verify"
    timing_disclaimer: str = (
        "elapsed_ms and rpo_seconds are local wall-clock measurements for this run only; "
        "synthetic scratch drills are not real-incident RTO/RPO. Real recovery adds "
        "operator decision time, off-site media pull, DNS/TLS, and client verification."
    )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.consistency is not None:
            d["consistency"] = self.consistency.to_dict()
        return d


def new_correlation_id() -> str:
    """Short correlation id for a recovery/verification operation (privacy-safe)."""
    return new_request_id()


def structured_diag(
    action: str,
    *,
    correlation_id: str | None = None,
    level: int = logging.INFO,
    **fields: Any,
) -> dict[str, Any]:
    """Emit and return a privacy-safe structured diagnostic line.

    Never include tokens, secrets, email bodies, or client PII. Prefer counts,
    statuses, paths that are already operator-owned, and checksums.
    """
    cid = correlation_id or new_correlation_id()
    payload = {
        "action": action,
        "correlation_id": cid,
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **fields,
    }
    # Drop anything that looks credential-ish if a caller slipped.
    for banned in ("token", "password", "secret", "authorization", "cookie", "api_key"):
        payload.pop(banned, None)
    log.log(
        level,
        action,
        extra={
            "action": action,
            "request_id": cid,
            **{k: v for k, v in fields.items() if k not in ("token", "password", "secret")},
        },
    )
    return payload


def normalize_data_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def is_production_data_path(path: str | Path) -> bool:
    """True when *path* matches a known live/production data directory pattern.

    Resolution uses :meth:`Path.resolve` so a symlink whose *final* target is a
    production path is refused even when the operator-facing path looks like
    scratch (``/tmp/link`` → ``/srv/hestia/data``).
    """
    resolved = normalize_data_path(path)
    markers = list(_PRODUCTION_ABS_MARKERS) + list(_cwd_default_data_paths())
    for marker in markers:
        try:
            if resolved == marker.resolve():
                return True
        except OSError:
            continue
    # Absolute deploy-style paths commonly used in bare-metal / compose installs.
    text = str(resolved)
    if text == "/data" or text.startswith("/data/"):
        return True
    if "/srv/hestia" in text or "/var/lib/hestia" in text:
        return True
    # Layout ``…/hestia/data`` (e.g. /opt/hestia/data) even when cwd is elsewhere.
    parts = resolved.parts
    if len(parts) >= 2 and parts[-1] == "data" and parts[-2] == "hestia":
        return True
    env_dir = os.environ.get("HESTIA_PRODUCTION_DATA_DIR", "").strip()
    if env_dir:
        try:
            if resolved == Path(env_dir).expanduser().resolve():
                return True
        except OSError:
            pass
    return False


def assert_safe_restore_target(
    data_dir: str | Path,
    *,
    allow_production: bool = False,
    correlation_id: str | None = None,
) -> Path:
    """Refuse restores that would target a production data path by accident.

    Pass ``allow_production=True`` (or env ``HESTIA_ALLOW_PRODUCTION_RESTORE=1``)
    only for a deliberate, documented operator restore of a live volume.
    """
    cid = correlation_id or new_correlation_id()
    target = normalize_data_path(data_dir)
    env_allow = os.environ.get("HESTIA_ALLOW_PRODUCTION_RESTORE", "").strip() in (
        "1",
        "true",
        "yes",
        "YES",
        "TRUE",
    )
    if is_production_data_path(target) and not (allow_production or env_allow):
        structured_diag(
            "recovery.restore.refused_production",
            correlation_id=cid,
            level=logging.ERROR,
            data_dir=str(target),
            reason="production_path_without_override",
        )
        raise RecoveryError(
            f"refusing restore into production-like data dir {target}: "
            "pass --allow-production or set HESTIA_ALLOW_PRODUCTION_RESTORE=1 "
            "only for a deliberate live restore (see docs/backup-restore.md)"
        )
    return target


def free_space_bytes(path: str | Path) -> int:
    """Bytes free on the filesystem that holds *path*.

    Does **not** create directories: a missing path walks up to an existing ancestor
    so a disk probe cannot mkdir a production data dir as a side effect.
    """
    p = Path(path).expanduser()
    probe = p
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    if not probe.exists():
        probe = Path.cwd()
    usage = shutil.disk_usage(probe)
    return int(usage.free)


def same_filesystem(a: str | Path, b: str | Path) -> bool:
    """True when both paths share a device id (after resolving existing ancestors)."""

    def _stat_path(path: Path) -> os.stat_result:
        p = path.expanduser()
        while not p.exists():
            parent = p.parent
            if parent == p:
                p = Path.cwd()
                break
            p = parent
        return p.stat()

    return _stat_path(Path(a)).st_dev == _stat_path(Path(b)).st_dev


def assert_sufficient_disk(
    path: str | Path,
    need_bytes: int,
    *,
    correlation_id: str | None = None,
    free_bytes: int | None = None,
) -> int:
    """Refuse when free space is below *need_bytes*. Returns observed free bytes.

    ``free_bytes`` is injectable so tests can force the failure without filling a disk.
    """
    cid = correlation_id or new_correlation_id()
    free = free_space_bytes(path) if free_bytes is None else int(free_bytes)
    if free < need_bytes:
        structured_diag(
            "recovery.disk.insufficient",
            correlation_id=cid,
            level=logging.ERROR,
            path=str(path),
            free_bytes=free,
            need_bytes=need_bytes,
        )
        raise RecoveryError(f"insufficient disk space at {path}: free={free} need={need_bytes}")
    return free


def file_sha256(path: Path, *, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def media_inventory(media_dir: str | Path, *, max_files: int = 500_000) -> list[MediaBlob]:
    """Walk *media_dir* and return relative paths with size + sha256."""
    root = Path(media_dir)
    if not root.is_dir():
        return []
    out: list[MediaBlob] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        out.append(
            MediaBlob(
                relative_path=rel,
                size_bytes=path.stat().st_size,
                sha256=file_sha256(path),
            )
        )
        if len(out) >= max_files:
            break
    return out


def db_media_refs(conn: sqlite3.Connection) -> list[DbMediaRef]:
    """All image storage keys the database expects to find in media storage.

    Fail-closed: a missing/malformed ``images`` table raises :class:`RecoveryError`
    with reason code ``schema_query:images`` — never an empty list that looks clean.
    """
    try:
        rows = conn.execute(
            "SELECT id, tenant_id, gallery_id, storage_key, thumb_key, bytes "
            "FROM images ORDER BY id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise RecoveryError(f"schema_query:images:{type(exc).__name__}") from exc
    refs: list[DbMediaRef] = []
    for r in rows:
        d = dict(r)
        key = (d.get("storage_key") or "").strip()
        if not key:
            continue
        thumb = d.get("thumb_key")
        refs.append(
            DbMediaRef(
                image_id=int(d["id"]),
                tenant_id=str(d["tenant_id"]),
                gallery_id=int(d["gallery_id"]),
                storage_key=key,
                thumb_key=(str(thumb) if thumb else None),
                bytes=(int(d["bytes"]) if d.get("bytes") is not None else None),
            )
        )
    return refs


def media_checksum_map(media_dir: str | Path, *, max_files: int = 500_000) -> dict[str, str]:
    """Relative path → sha256 for every file under *media_dir* (empty if missing)."""
    return {
        blob.relative_path: blob.sha256 for blob in media_inventory(media_dir, max_files=max_files)
    }


def check_db_media_consistency(
    conn: sqlite3.Connection,
    media_dir: str | Path,
    *,
    checksum: bool = False,
    expected_checksums: dict[str, str] | None = None,
) -> ConsistencyReport:
    """Compare image rows to on-disk blobs under *media_dir* (local storage only).

    Missing blobs, size mismatches, and (when requested) checksum mismatches are
    fatal for a recovery claim. Orphan blobs (on disk, not in DB) are reported but
    do not flip ``ok`` — they are recoverable waste, not lost client assets.
    Missing thumbs are reported separately.

    When ``checksum=True``, *expected_checksums* is required: a map of relative
    path → sha256 (typically from :func:`media_checksum_map` of the source tree
    before restore). Each present referenced blob is hashed and compared.
    """
    if checksum and expected_checksums is None:
        raise ValueError("checksum=True requires expected_checksums (use media_checksum_map)")

    root = Path(media_dir)
    report = ConsistencyReport()
    try:
        refs = db_media_refs(conn)
    except RecoveryError as exc:
        report.query_errors.append(str(exc))
        refs = []
    report.image_rows = len(refs)

    try:
        tenants = [r[0] for r in conn.execute("SELECT id FROM tenants ORDER BY id")]
        report.tenant_ids = [str(t) for t in tenants]
        report.gallery_count = int(conn.execute("SELECT COUNT(*) FROM galleries").fetchone()[0])
        report.published_gallery_count = int(
            conn.execute("SELECT COUNT(*) FROM galleries WHERE status = 'published'").fetchone()[0]
        )
    except sqlite3.Error as exc:
        # Expected tables failed — not a silent empty studio.
        report.query_errors.append(f"schema_query:tenants_or_galleries:{type(exc).__name__}")

    expected_keys: set[str] = set()
    keys_to_hash: list[str] = []
    for ref in refs:
        expected_keys.add(ref.storage_key)
        blob = root / ref.storage_key
        if not blob.is_file():
            report.missing_blobs.append(ref.storage_key)
            continue
        size = blob.stat().st_size
        if ref.bytes is not None and ref.bytes > 0 and size != ref.bytes:
            report.size_mismatches.append(
                {
                    "storage_key": ref.storage_key,
                    "db_bytes": ref.bytes,
                    "disk_bytes": size,
                }
            )
        keys_to_hash.append(ref.storage_key)
        if ref.thumb_key:
            expected_keys.add(ref.thumb_key)
            thumb_path = root / ref.thumb_key
            if not thumb_path.is_file():
                report.missing_thumbs.append(ref.thumb_key)
            else:
                keys_to_hash.append(ref.thumb_key)

    if checksum and expected_checksums is not None:
        for key in keys_to_hash:
            path = root / key
            if not path.is_file():
                continue
            actual = file_sha256(path)
            expected = expected_checksums.get(key)
            if expected is None:
                report.checksum_mismatches.append(
                    {
                        "storage_key": key,
                        "reason": "no_expected_checksum",
                        "actual_sha256": actual,
                    }
                )
            elif actual != expected:
                report.checksum_mismatches.append(
                    {
                        "storage_key": key,
                        "expected_sha256": expected,
                        "actual_sha256": actual,
                    }
                )

    if root.is_dir():
        on_disk: list[str] = []
        for path in root.rglob("*"):
            if path.is_file():
                on_disk.append(str(path.relative_to(root)).replace("\\", "/"))
        report.media_files = len(on_disk)
        report.orphan_blobs = sorted(set(on_disk) - expected_keys)
    return report


def sqlite_integrity_ok(db_path: str | Path) -> str:
    """Run ``PRAGMA integrity_check``; return the first result string (``ok`` or error)."""
    path = Path(db_path)
    if not path.is_file():
        return "missing"
    if path.stat().st_size == 0:
        return "empty"
    conn = sqlite3.connect(str(path))
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def schema_version(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return None if row is None or row[0] is None else str(row[0])
    except sqlite3.Error:
        return None


def has_schema_migrations(conn: sqlite3.Connection) -> bool:
    """True when the connection's DB has a ``schema_migrations`` table."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def assert_supported_schema(
    conn: sqlite3.Connection,
    *,
    known_versions: set[str] | None = None,
    correlation_id: str | None = None,
) -> str:
    """Refuse a restore whose ledger claims a version this codebase has never shipped.

    When *known_versions* is omitted, the local ``hestia/migrations`` directory is
    the source of truth. Missing ``schema_migrations`` is always a refusal.
    """
    cid = correlation_id or new_correlation_id()
    if not has_schema_migrations(conn):
        structured_diag(
            "recovery.schema.missing_table",
            correlation_id=cid,
            level=logging.ERROR,
        )
        raise RecoveryError(
            "not a Hestia database: missing schema_migrations table — refuse restore"
        )
    version = schema_version(conn)
    if version is None:
        structured_diag(
            "recovery.schema.missing",
            correlation_id=cid,
            level=logging.ERROR,
        )
        raise RecoveryError(
            "database has an empty schema_migrations ledger — not a usable Hestia backup"
        )
    if known_versions is None:
        from .db import discover_migrations

        known_versions = {v for v, _name, _path in discover_migrations()}
    if version not in known_versions:
        structured_diag(
            "recovery.schema.unsupported",
            correlation_id=cid,
            level=logging.ERROR,
            schema_version=version,
            known_count=len(known_versions),
        )
        raise RecoveryError(
            f"unsupported schema version {version!r}: this release only knows "
            f"{len(known_versions)} migration versions; refuse restore rather than "
            "silently run against an unknown schema"
        )
    return version


def assert_restorable_backup(
    db_path: str | Path,
    *,
    correlation_id: str | None = None,
    known_versions: set[str] | None = None,
) -> str:
    """Gate a candidate backup *before* any live database is moved aside.

    Refuses empty files, non-SQLite payloads, integrity failures, non-Hestia DBs
    (no ``schema_migrations``), empty ledgers, and unsupported schema versions.
    Returns the supported schema version string on success.
    """
    cid = correlation_id or new_correlation_id()
    path = Path(db_path)
    if not path.is_file():
        structured_diag(
            "recovery.backup.missing",
            correlation_id=cid,
            level=logging.ERROR,
            path=str(path),
        )
        raise RecoveryError(f"no backup file at {path}")
    size = path.stat().st_size
    if size == 0:
        structured_diag(
            "recovery.backup.empty",
            correlation_id=cid,
            level=logging.ERROR,
            path=str(path),
        )
        raise RecoveryError(f"empty backup file at {path} — refuse restore")

    integrity = sqlite_integrity_ok(path)
    if integrity != "ok":
        structured_diag(
            "recovery.backup.integrity_failed",
            correlation_id=cid,
            level=logging.ERROR,
            path=str(path),
            integrity_check=integrity,
        )
        raise RecoveryError(f"backup failed integrity_check ({integrity}) — refuse restore")

    conn = sqlite3.connect(str(path))
    try:
        version = assert_supported_schema(conn, known_versions=known_versions, correlation_id=cid)
    finally:
        conn.close()

    structured_diag(
        "recovery.backup.accepted",
        correlation_id=cid,
        schema_version=version,
        size_bytes=size,
    )
    return version


# ── Backup-set generation manifest (DB + media bound to one generation) ─────


def manifest_path_for_backup(backup_path: str | Path) -> Path:
    """Sidecar path: ``hestia-STAMP.db.gz`` → ``hestia-STAMP.db.gz.manifest.json``."""
    p = Path(backup_path)
    return p.with_name(p.name + MANIFEST_FILENAME_SUFFIX)


def build_backup_manifest(
    *,
    db_artifact: str | Path,
    media_dir: str | Path | None = None,
    unpacked_db: str | Path | None = None,
    generation_id: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Build a privacy-safe versioned backup-set manifest for one generation.

    Binds the compressed (or raw) DB artifact checksum to an optional media
    inventory (relative path → size + sha256). Never includes client tokens,
    emails, or secrets.
    """
    cid = correlation_id or new_correlation_id()
    artifact = Path(db_artifact)
    if not artifact.is_file():
        raise RecoveryError(f"manifest.db_missing:{artifact.name}")
    db_sha = file_sha256(artifact)
    db_size = artifact.stat().st_size

    schema_ver: str | None = None
    inspect = Path(unpacked_db) if unpacked_db else None
    if inspect is None and not str(artifact).endswith(".gz"):
        inspect = artifact
    if inspect is not None and inspect.is_file() and inspect.stat().st_size > 0:
        try:
            schema_ver = assert_restorable_backup(inspect, correlation_id=cid)
        except RecoveryError:
            # Still record the artifact; schema gate at restore will refuse separately.
            try:
                conn = sqlite3.connect(str(inspect))
                try:
                    schema_ver = schema_version(conn)
                finally:
                    conn.close()
            except sqlite3.Error:
                schema_ver = None

    media_files: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    if media_dir is not None:
        root = Path(media_dir)
        if root.is_dir():
            for blob in media_inventory(root):
                media_files[blob.relative_path] = {
                    "sha256": blob.sha256,
                    "size_bytes": blob.size_bytes,
                }
                total_bytes += blob.size_bytes

    gen = generation_id or hashlib.sha256(
        f"{db_sha}:{db_size}:{datetime.now(UTC).isoformat()}".encode()
    ).hexdigest()[:32]

    manifest: dict[str, Any] = {
        "format_version": MANIFEST_FORMAT_VERSION,
        "generation_id": gen,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_version": schema_ver,
        "db": {
            "filename": artifact.name,
            "sha256": db_sha,
            "size_bytes": db_size,
        },
        "media": {
            "file_count": len(media_files),
            "total_bytes": total_bytes,
            "files": media_files,
        },
    }
    structured_diag(
        "recovery.manifest.built",
        correlation_id=cid,
        generation_id=gen,
        schema_version=schema_ver,
        media_file_count=len(media_files),
        db_size_bytes=db_size,
    )
    return manifest


def write_backup_manifest(manifest: dict[str, Any], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def load_backup_manifest(path: str | Path) -> dict[str, Any]:
    """Load and structurally validate a backup-set manifest (fail-closed)."""
    p = Path(path)
    if not p.is_file():
        raise RecoveryError(f"manifest.missing:{p.name}")
    if p.stat().st_size == 0:
        raise RecoveryError(f"manifest.empty:{p.name}")
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"manifest.corrupt:{type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise RecoveryError("manifest.corrupt:not_object")
    if data.get("format_version") != MANIFEST_FORMAT_VERSION:
        raise RecoveryError(
            f"manifest.unsupported_format:{data.get('format_version')!r}"
        )
    for key in ("generation_id", "created_at", "db", "media"):
        if key not in data:
            raise RecoveryError(f"manifest.incomplete:{key}")
    db = data["db"]
    media = data["media"]
    if not isinstance(db, dict) or not isinstance(media, dict):
        raise RecoveryError("manifest.corrupt:db_or_media_not_object")
    for key in ("filename", "sha256", "size_bytes"):
        if key not in db:
            raise RecoveryError(f"manifest.incomplete:db.{key}")
    if "files" not in media or not isinstance(media["files"], dict):
        raise RecoveryError("manifest.incomplete:media.files")
    if not re.fullmatch(r"[0-9a-f]{64}", str(db["sha256"])):
        raise RecoveryError("manifest.corrupt:db.sha256")
    return data


def verify_backup_set(
    *,
    db_artifact: str | Path,
    manifest: dict[str, Any] | str | Path,
    media_dir: str | Path | None = None,
    require_media: bool = False,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Verify DB artifact (+ optional media tree) against one generation manifest.

    Refuses missing, size-mismatched, checksum-mismatched, truncated, or
    cross-generation inputs **before** any live target replacement. Returns a
    privacy-safe summary dict on success; raises :class:`RecoveryError` on refuse.
    """
    cid = correlation_id or new_correlation_id()
    if not isinstance(manifest, dict):
        manifest = load_backup_manifest(manifest)
    else:
        # Re-validate structure even if caller passed a dict.
        if manifest.get("format_version") != MANIFEST_FORMAT_VERSION:
            raise RecoveryError(
                f"manifest.unsupported_format:{manifest.get('format_version')!r}"
            )

    artifact = Path(db_artifact)
    if not artifact.is_file():
        raise RecoveryError(f"manifest.db_missing:{artifact.name}")

    expected_name = str(manifest["db"]["filename"])
    if artifact.name != expected_name:
        # Allow restore from a copied artifact with a different name only when
        # checksums still match; flag name drift but do not treat as cross-generation.
        structured_diag(
            "recovery.manifest.filename_drift",
            correlation_id=cid,
            expected=expected_name,
            actual=artifact.name,
        )

    actual_size = artifact.stat().st_size
    expected_size = int(manifest["db"]["size_bytes"])
    if actual_size != expected_size:
        raise RecoveryError(
            f"manifest.db_size_mismatch:expected={expected_size}:actual={actual_size}"
        )
    if actual_size == 0:
        raise RecoveryError("manifest.db_empty")

    actual_sha = file_sha256(artifact)
    expected_sha = str(manifest["db"]["sha256"])
    if actual_sha != expected_sha:
        raise RecoveryError("manifest.db_checksum_mismatch")

    media_spec = manifest["media"]
    files: dict[str, Any] = media_spec.get("files") or {}
    if require_media and int(media_spec.get("file_count") or 0) > 0 and media_dir is None:
        raise RecoveryError("manifest.media_dir_required")

    if media_dir is not None:
        root = Path(media_dir)
        if not root.is_dir() and files:
            raise RecoveryError("manifest.media_dir_missing")
        on_disk = media_checksum_map(root) if root.is_dir() else {}
        # Required media from this generation must be present and match.
        for rel, meta in files.items():
            if not isinstance(meta, dict) or "sha256" not in meta:
                raise RecoveryError(f"manifest.corrupt:media_entry:{rel[:64]}")
            if rel not in on_disk:
                raise RecoveryError(f"manifest.media_missing:{rel[:128]}")
            if on_disk[rel] != meta["sha256"]:
                raise RecoveryError(f"manifest.media_checksum_mismatch:{rel[:128]}")
            expected_sz = meta.get("size_bytes")
            if expected_sz is not None:
                disk_sz = (root / rel).stat().st_size
                if int(expected_sz) != disk_sz:
                    raise RecoveryError(f"manifest.media_size_mismatch:{rel[:128]}")
        # Extra on-disk files are allowed (orphans); required set must be complete.

    summary = {
        "ok": True,
        "generation_id": manifest["generation_id"],
        "schema_version": manifest.get("schema_version"),
        "db_sha256": expected_sha,
        "media_file_count": int(media_spec.get("file_count") or 0),
        "correlation_id": cid,
    }
    structured_diag(
        "recovery.manifest.verified",
        correlation_id=cid,
        generation_id=summary["generation_id"],
        schema_version=summary["schema_version"],
        media_file_count=summary["media_file_count"],
    )
    return summary


def tenant_ownership_ok(conn: sqlite3.Connection) -> list[str]:
    """Return codes for cross-tenant ownership violations (empty = clean).

    Fail-closed: if an ownership query cannot run (missing tables), returns a
    ``schema_query:…`` failure code rather than treating the check as skipped/clean.
    """
    failures: list[str] = []
    checks = [
        (
            "images.gallery_tenant_mismatch",
            """
            SELECT COUNT(*) FROM images i
            LEFT JOIN galleries g ON g.id = i.gallery_id AND g.tenant_id = i.tenant_id
            WHERE g.id IS NULL
            """,
        ),
        (
            "galleries.orphan_tenant",
            """
            SELECT COUNT(*) FROM galleries g
            LEFT JOIN tenants t ON t.id = g.tenant_id
            WHERE t.id IS NULL
            """,
        ),
    ]
    for code, sql in checks:
        try:
            n = int(conn.execute(sql).fetchone()[0])
        except sqlite3.Error as exc:
            failures.append(f"schema_query:{code}:{type(exc).__name__}")
            continue
        if n:
            failures.append(f"{code}:{n}")
    return failures


def representative_gallery_access(
    conn: sqlite3.Connection,
    media_dir: str | Path | None,
) -> dict[str, Any] | None:
    """Pick one published (else any) gallery and confirm its rows + first blob exist.

    Raises :class:`RecoveryError` with ``schema_query:galleries`` / ``images`` when
    expected tables are unreadable — never returns a synthetic empty success.
    """
    try:
        row = conn.execute(
            "SELECT id, tenant_id, slug, title, status FROM galleries "
            "ORDER BY CASE status WHEN 'published' THEN 0 ELSE 1 END, id LIMIT 1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise RecoveryError(f"schema_query:galleries:{type(exc).__name__}") from exc
    if not row:
        return None
    g = dict(row)
    try:
        images = conn.execute(
            "SELECT id, storage_key, access_token FROM images "
            "WHERE gallery_id = ? AND tenant_id = ? ORDER BY position, id",
            (g["id"], g["tenant_id"]),
        ).fetchall()
    except sqlite3.Error as exc:
        raise RecoveryError(f"schema_query:images:{type(exc).__name__}") from exc
    first_blob_ok = None
    if images and media_dir:
        key = images[0]["storage_key"]
        first_blob_ok = bool(key) and (Path(media_dir) / key).is_file()
    return {
        "gallery_id": g["id"],
        "tenant_id": g["tenant_id"],
        "slug": g["slug"],
        "status": g["status"],
        "image_count": len(images),
        # access_token is intentionally NOT returned (privacy)
        "first_blob_present": first_blob_ok,
    }


def assert_writer_quiescent(
    data_dir: str | Path,
    *,
    force_live_wal: bool = False,
    correlation_id: str | None = None,
) -> None:
    """Refuse restore when a WAL sidecar indicates a live (or crash-mid-write) app.

    ``--force`` is **not** accepted here. The only override is ``force_live_wal=True``
    (CLI ``--force-live-wal``), which is loud and separately named so operators cannot
    confuse it with “the app is definitely stopped.”
    """
    cid = correlation_id or new_correlation_id()
    wal = Path(data_dir) / "hestia.db-wal"
    if not wal.exists():
        return
    if force_live_wal:
        structured_diag(
            "recovery.restore.force_live_wal",
            correlation_id=cid,
            level=logging.WARNING,
            data_dir=str(normalize_data_path(data_dir)),
            reason="operator_override_app_may_still_be_live",
        )
        return
    structured_diag(
        "recovery.restore.live_writer",
        correlation_id=cid,
        level=logging.ERROR,
        data_dir=str(normalize_data_path(data_dir)),
        reason="wal_present",
    )
    raise RecoveryError(
        f"refusing restore while {wal} exists — app looks live. "
        "Stop the app first, or pass the loud override --force-live-wal "
        "(not --force) only if you accept possible corruption risk "
        "(see docs/backup-restore.md)"
    )


def verify_restored_database(
    db_path: str | Path,
    *,
    media_dir: str | Path | None = None,
    backup_path: str | Path | None = None,
    require_media: bool = False,
    allow_unsupported_schema: bool = False,
    expected_checksums: dict[str, str] | None = None,
    correlation_id: str | None = None,
    started_at: float | None = None,
    measurement_kind: str = "operator_verify",
) -> RestoreVerification:
    """Post-restore verification: integrity, schema, ownership, media, RPO/RTO fields.

    When *expected_checksums* is provided (path → sha256), media content is
    deep-verified against that inventory in addition to presence/size checks.

    ``measurement_kind`` labels timing fields so synthetic CI drills are not
    mistaken for production incident RTO/RPO (use ``synthetic_scratch_drill``).
    """
    cid = correlation_id or new_correlation_id()
    t0 = started_at if started_at is not None else time.monotonic()
    path = Path(db_path)
    failures: list[str] = []

    integrity = sqlite_integrity_ok(path)
    if integrity != "ok":
        failures.append(f"integrity_check:{integrity}")

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        version: str | None
        try:
            if allow_unsupported_schema:
                version = schema_version(conn) if has_schema_migrations(conn) else None
            else:
                version = assert_supported_schema(conn, correlation_id=cid)
        except RecoveryError as exc:
            version = schema_version(conn) if has_schema_migrations(conn) else None
            failures.append(str(exc))

        try:
            tenant_count = int(conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0])
            tenant_ids = [str(r[0]) for r in conn.execute("SELECT id FROM tenants ORDER BY id")]
            gallery_count = int(conn.execute("SELECT COUNT(*) FROM galleries").fetchone()[0])
            published = int(
                conn.execute("SELECT COUNT(*) FROM galleries WHERE status='published'").fetchone()[
                    0
                ]
            )
            image_count = int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0])
        except sqlite3.Error as exc:
            # Fail-closed: reason code only (no SQL text that might echo paths/PII).
            failures.append(f"schema_query:core_tables:{type(exc).__name__}")
            tenant_count = gallery_count = published = image_count = 0
            tenant_ids = []

        for code in tenant_ownership_ok(conn):
            failures.append(code)

        consistency: ConsistencyReport | None = None
        rep: dict[str, Any] | None = None
        try:
            rep = representative_gallery_access(conn, media_dir)
        except RecoveryError as exc:
            failures.append(str(exc))
        if media_dir is not None:
            do_checksum = expected_checksums is not None
            consistency = check_db_media_consistency(
                conn,
                media_dir,
                checksum=do_checksum,
                expected_checksums=expected_checksums,
            )
            if consistency.query_errors:
                failures.extend(consistency.query_errors)
            if not consistency.ok:
                if consistency.missing_blobs:
                    failures.append(f"missing_blobs:{len(consistency.missing_blobs)}")
                if consistency.size_mismatches:
                    failures.append(f"size_mismatches:{len(consistency.size_mismatches)}")
                if consistency.checksum_mismatches:
                    failures.append(f"checksum_mismatches:{len(consistency.checksum_mismatches)}")
            if require_media and image_count > 0 and consistency and consistency.missing_blobs:
                failures.append("require_media:unsatisfied")
        elif require_media:
            failures.append("require_media:no_media_dir")
    finally:
        conn.close()

    backup_mtime_iso = None
    rpo_seconds = None
    if backup_path and Path(backup_path).is_file():
        mtime = Path(backup_path).stat().st_mtime
        backup_mtime_iso = datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        rpo_seconds = max(0.0, time.time() - mtime)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    ok = not failures and integrity == "ok"
    result = RestoreVerification(
        correlation_id=cid,
        db_path=str(path),
        media_dir=str(media_dir) if media_dir else None,
        integrity_check=integrity,
        schema_version=version,
        tenant_count=tenant_count,
        tenant_ids=tenant_ids,
        gallery_count=gallery_count,
        published_gallery_count=published,
        image_count=image_count,
        representative_gallery=rep,
        consistency=consistency,
        backup_mtime_iso=backup_mtime_iso,
        elapsed_ms=elapsed_ms,
        rpo_seconds=rpo_seconds,
        ok=ok,
        failures=failures,
        measurement_kind=measurement_kind,
    )
    structured_diag(
        "recovery.verify.complete",
        correlation_id=cid,
        level=logging.INFO if ok else logging.ERROR,
        integrity_check=integrity,
        schema_version=version,
        tenant_count=tenant_count,
        gallery_count=gallery_count,
        image_count=image_count,
        elapsed_ms=elapsed_ms,
        rpo_seconds=rpo_seconds,
        measurement_kind=measurement_kind,
        ok=ok,
        failure_count=len(failures),
    )
    return result


def write_verification_report(result: RestoreVerification, out_path: str | Path) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


# ── CLI (operator entry: python -m hestia.recovery) ─────────────────────────


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Hestia recovery verification (scratch/staging; never production by default).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="Post-restore verification of a database (+ optional media)")
    v.add_argument("db_path", type=Path)
    v.add_argument("--media-dir", type=Path, default=None)
    v.add_argument("--backup", type=Path, default=None, help="Backup artifact for RPO measurement")
    v.add_argument("--require-media", action="store_true")
    v.add_argument("--allow-unsupported-schema", action="store_true")
    v.add_argument(
        "--expected-checksums",
        type=Path,
        default=None,
        help="JSON object of relative media path → sha256 for content verify",
    )
    v.add_argument("--json-out", type=Path, default=None)
    v.add_argument("--correlation-id", default=None)
    v.add_argument(
        "--measurement-kind",
        default="operator_verify",
        help="Label for timing fields (use synthetic_scratch_drill for CI drills)",
    )

    s = sub.add_parser("check-target", help="Refuse if data dir looks like production")
    s.add_argument("data_dir", type=Path)
    s.add_argument("--allow-production", action="store_true")

    g = sub.add_parser(
        "gate-backup",
        help="Refuse empty/non-Hestia/unsupported backups (pre-restore gate)",
    )
    g.add_argument("db_path", type=Path)
    g.add_argument("--correlation-id", default=None)

    c = sub.add_parser("consistency", help="DB↔media consistency report only")
    c.add_argument("db_path", type=Path)
    c.add_argument("media_dir", type=Path)
    c.add_argument(
        "--expected-checksums",
        type=Path,
        default=None,
        help="JSON object of relative media path → sha256",
    )
    c.add_argument("--json-out", type=Path, default=None)

    mb = sub.add_parser(
        "manifest-build",
        help="Write a generation manifest binding one DB artifact to a media tree",
    )
    mb.add_argument("db_artifact", type=Path)
    mb.add_argument("--media-dir", type=Path, default=None)
    mb.add_argument("--unpacked-db", type=Path, default=None, help="Uncompressed DB for schema")
    mb.add_argument("--out", type=Path, default=None, help="Default: <artifact>.manifest.json")
    mb.add_argument("--correlation-id", default=None)

    mv = sub.add_parser(
        "manifest-verify",
        help="Verify DB (+ media) against a generation manifest before restore",
    )
    mv.add_argument("db_artifact", type=Path)
    mv.add_argument("manifest", type=Path)
    mv.add_argument("--media-dir", type=Path, default=None)
    mv.add_argument("--require-media", action="store_true")
    mv.add_argument("--correlation-id", default=None)

    args = parser.parse_args(argv)
    cid = getattr(args, "correlation_id", None) or new_correlation_id()

    if args.cmd == "check-target":
        try:
            assert_safe_restore_target(
                args.data_dir,
                allow_production=args.allow_production,
                correlation_id=cid,
            )
        except RecoveryError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "ok": True,
                    "data_dir": str(normalize_data_path(args.data_dir)),
                    "correlation_id": cid,
                }
            )
        )
        return 0

    if args.cmd == "gate-backup":
        try:
            version = assert_restorable_backup(args.db_path, correlation_id=cid)
        except RecoveryError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "schema_version": version, "correlation_id": cid}))
        return 0

    if args.cmd == "manifest-build":
        try:
            manifest = build_backup_manifest(
                db_artifact=args.db_artifact,
                media_dir=args.media_dir,
                unpacked_db=args.unpacked_db,
                correlation_id=cid,
            )
            out = args.out or manifest_path_for_backup(args.db_artifact)
            write_backup_manifest(manifest, out)
        except RecoveryError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "ok": True,
                    "path": str(out),
                    "generation_id": manifest["generation_id"],
                    "schema_version": manifest.get("schema_version"),
                    "media_file_count": manifest["media"]["file_count"],
                    "correlation_id": cid,
                }
            )
        )
        return 0

    if args.cmd == "manifest-verify":
        try:
            summary = verify_backup_set(
                db_artifact=args.db_artifact,
                manifest=args.manifest,
                media_dir=args.media_dir,
                require_media=args.require_media,
                correlation_id=cid,
            )
        except RecoveryError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    if args.cmd == "consistency":
        expected = None
        if args.expected_checksums:
            expected = json.loads(Path(args.expected_checksums).read_text(encoding="utf-8"))
        conn = sqlite3.connect(str(args.db_path))
        conn.row_factory = sqlite3.Row
        try:
            report = check_db_media_consistency(
                conn,
                args.media_dir,
                checksum=expected is not None,
                expected_checksums=expected,
            )
        finally:
            conn.close()
        payload = report.to_dict()
        payload["correlation_id"] = cid
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if report.ok else 1

    if args.cmd == "verify":
        expected = None
        if args.expected_checksums:
            expected = json.loads(Path(args.expected_checksums).read_text(encoding="utf-8"))
        result = verify_restored_database(
            args.db_path,
            media_dir=args.media_dir,
            backup_path=args.backup,
            require_media=args.require_media,
            allow_unsupported_schema=args.allow_unsupported_schema,
            expected_checksums=expected,
            correlation_id=cid,
            measurement_kind=args.measurement_kind,
        )
        if args.json_out:
            write_verification_report(result, args.json_out)
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        if result.ok:
            print(
                f"recovery verify OK correlation_id={result.correlation_id} "
                f"integrity={result.integrity_check} schema={result.schema_version} "
                f"tenants={result.tenant_count} galleries={result.gallery_count} "
                f"images={result.image_count} elapsed_ms={result.elapsed_ms} "
                f"artifact_age_s={result.rpo_seconds} "
                f"measurement_kind={result.measurement_kind}",
                file=sys.stderr,
            )
            return 0
        print(
            f"recovery verify FAILED correlation_id={result.correlation_id} "
            f"failures={result.failures}",
            file=sys.stderr,
        )
        return 1

    return 2


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(_cli(argv))


if __name__ == "__main__":
    main()
