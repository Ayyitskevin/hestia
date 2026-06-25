"""SQLite control-plane schema and connection helpers.

One boring SQLite file under ``HESTIA_DATA_DIR``, WAL mode, shared by request
handlers and the background pipeline worker.

Schema lives in versioned, numbered ``.sql`` files under :mod:`hestia.migrations`
(``0001_baseline.sql`` is the whole current schema). :func:`init_db` applies any
file whose version is not yet recorded in the ``schema_migrations`` ledger, in
order, exactly once — so a fresh database is built from the baseline and an older
one is brought forward by appending the next numbered file.

Migration rules
---------------
- Name files ``NNNN_short_description.sql``; the leading integer is the version.
- Keep each file idempotent (``CREATE TABLE/INDEX IF NOT EXISTS``). A baseline is
  then safe to (re)apply over a database that already has it — which is how a
  pre-ledger database adopts the migration system on first boot.
- Never edit a migration that may already be applied somewhere; add a new one.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_audit_log = logging.getLogger("hestia.audit")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_VERSION_RE = re.compile(r"^(\d+)_")

# The ledger of applied migrations. Created before anything else so the runner
# always has somewhere to record progress.
_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def discover_migrations() -> list[tuple[str, str, Path]]:
    """Return ``(version, name, path)`` for each ``NNNN_*.sql`` file, ordered."""
    found: list[tuple[int, str, str, Path]] = []
    for path in MIGRATIONS_DIR.glob("*.sql"):
        m = _VERSION_RE.match(path.name)
        if not m:
            continue
        found.append((int(m.group(1)), m.group(1), path.stem, path))
    found.sort(key=lambda r: r[0])
    return [(ver, name, path) for _, ver, name, path in found]


def applied_migrations(conn: sqlite3.Connection) -> list[dict]:
    """The migration ledger, oldest-first (for the operator/system view)."""
    rows = conn.execute(
        "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [dict(r) for r in rows]


def init_db(db_path: str | Path) -> None:
    """Create/upgrade the database by applying any un-applied migrations.

    Idempotent: already-applied versions (per the ``schema_migrations`` ledger)
    are skipped, so this is safe to call on every boot.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(_LEDGER_SQL)
        conn.commit()
        applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
        for version, name, mpath in discover_migrations():
            if version in applied:
                continue
            _apply_migration(conn, version, name, mpath.read_text(encoding="utf-8"))


def _apply_migration(conn: sqlite3.Connection, version: str, name: str, sql: str) -> None:
    """Run one migration's SQL and record it. Records only on full success.

    ``executescript`` autocommits the DDL; the ledger row is written immediately
    after. If the script raises, the version stays unrecorded and is retried next
    boot — which is safe because migrations are required to be idempotent.
    """
    try:
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (version, name),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def get_db(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def audit(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    tenant_id: str | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (tenant_id, actor, action, detail) VALUES (?, ?, ?, ?)",
        (tenant_id, actor, action, detail),
    )
    _audit_log.info("audit", extra={"action": action, "tenant_id": tenant_id})


def list_audit(conn: sqlite3.Connection, tenant_id: str, *, limit: int = 100) -> list[dict]:
    """Most-recent-first audit entries for one tenant (the owner's activity feed)."""
    rows = conn.execute(
        "SELECT actor, action, detail, created_at FROM audit_log "
        "WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
        (tenant_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
