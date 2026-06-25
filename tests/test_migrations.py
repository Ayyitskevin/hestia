"""Migration runner — numbered .sql files applied once via a ledger."""

from __future__ import annotations

from hestia.db import MIGRATIONS_DIR, connect, discover_migrations, init_db

CORE_TABLES = {
    "tenants", "users", "sessions", "tenant_api_keys", "clients", "projects",
    "galleries", "images", "image_analyses", "pipeline_runs", "offers", "albums",
    "product_sets", "content_packs", "invoices", "studio_profiles", "audit_log",
}


def _tables(conn) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_discovery_is_ordered_and_unique():
    migs = discover_migrations()
    assert migs, "expected at least the baseline migration"
    versions = [v for v, _, _ in migs]
    assert versions == sorted(versions, key=int)
    assert len(versions) == len(set(versions))
    assert versions[0] == "0001"
    # Every discovered file actually lives in the migrations dir.
    assert all(p.parent == MIGRATIONS_DIR for _, _, p in migs)


def test_init_builds_schema_and_records_ledger(tmp_path):
    db = tmp_path / "h.db"
    init_db(db)
    with connect(db) as conn:
        assert CORE_TABLES <= _tables(conn)
        recorded = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    assert recorded == {v for v, _, _ in discover_migrations()}
    assert "0001" in recorded


def test_init_is_idempotent(tmp_path):
    db = tmp_path / "h.db"
    init_db(db)
    init_db(db)  # second boot must not error or double-record
    with connect(db) as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]
    assert n == len(discover_migrations())


def test_baseline_includes_galleries_project_id(tmp_path):
    # Guards the fold-in of the old ad-hoc ALTER: fresh DBs must have the column.
    db = tmp_path / "h.db"
    init_db(db)
    with connect(db) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(galleries)")}
    assert "project_id" in cols


def test_legacy_db_without_ledger_is_adopted(tmp_path):
    # Simulate a pre-migration-system database: schema present, no ledger.
    db = tmp_path / "legacy.db"
    baseline = (MIGRATIONS_DIR / "0001_baseline.sql").read_text(encoding="utf-8")
    with connect(db) as conn:
        conn.executescript(baseline)
        conn.execute("INSERT INTO tenants (id, slug, name) VALUES ('t1', 'studio', 'Studio')")
        conn.commit()
        assert "schema_migrations" not in _tables(conn)

    init_db(db)  # adopt: re-apply baseline (no-op) and record it

    with connect(db) as conn:
        recorded = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
        keep = conn.execute("SELECT name FROM tenants WHERE id='t1'").fetchone()["name"]
    assert "0001" in recorded
    assert keep == "Studio"  # existing data untouched by adoption


def test_pending_migration_reapplies_after_ledger_reset(tmp_path):
    # Drop the ledger record → the runner must re-apply that version on next boot.
    db = tmp_path / "h.db"
    init_db(db)
    with connect(db) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version='0001'")
        conn.commit()
    init_db(db)  # 0001 is pending again; idempotent re-apply, then re-recorded
    with connect(db) as conn:
        recorded = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
        assert CORE_TABLES <= _tables(conn)
    assert "0001" in recorded
