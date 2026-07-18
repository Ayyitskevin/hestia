"""Read-only evidence for the known migration-0065 history split."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from hestia.db import MIGRATIONS_DIR, discover_migrations, init_db
from hestia.migration_audit import (
    EXIT_CURRENT,
    EXIT_DECISION_REQUIRED,
    EXIT_INCONSISTENT,
    EXIT_INPUT_ERROR,
    MigrationAuditError,
    audit_migration_state,
    main,
)


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _offline_copy(source_path: Path, target_path: Path) -> None:
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
    finally:
        target.close()
        source.close()


def _ledger(conn: sqlite3.Connection, *, through: str | None = None) -> None:
    conn.execute(
        "CREATE TABLE schema_migrations ("
        "version TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    for version, name, _ in discover_migrations():
        if through is not None and int(version) > int(through):
            break
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (version, name),
        )


def _baseline(conn: sqlite3.Connection) -> None:
    conn.executescript((MIGRATIONS_DIR / "0001_baseline.sql").read_text(encoding="utf-8"))


def _historical_0065_db(path: Path, *, through: str | None = None) -> None:
    with sqlite3.connect(path) as conn:
        _ledger(conn, through=through)
        _baseline(conn)
        conn.executescript(
            "ALTER TABLE images ADD COLUMN access_token TEXT NOT NULL DEFAULT '';"
            "CREATE UNIQUE INDEX idx_images_access_token ON images(access_token);"
        )


def test_current_database_is_observed_without_file_mutation(tmp_path):
    live = tmp_path / "live.db"
    db = tmp_path / "audit.db"
    init_db(live)
    _offline_copy(live, db)
    before = _file_digest(db)
    before_files = sorted(
        path.name for path in tmp_path.iterdir() if path.name.startswith(db.name)
    )

    report = audit_migration_state(db)

    assert report["classification"] == "observed_current"
    assert report["format_version"] == 1
    assert report["exit_code"] == EXIT_CURRENT
    assert report["read_only"] is True
    assert report["identity"]["recognized"] is True
    assert report["migration_0065"]["shape"] == "current"
    assert report["ledger"]["pending_versions"] == []
    assert report["ledger"]["unknown_versions"] == []
    assert report["ledger"]["checksum_verification"].startswith("unavailable:")
    assert len(report["source"]["migration_0065_sha256"]) == 64
    assert report["source"]["repository_state"] == "manifest_clean"
    assert report["checksum_evidence"]["database_applied_sha256"] is None
    assert _file_digest(db) == before
    assert sorted(
        path.name for path in tmp_path.iterdir() if path.name.startswith(db.name)
    ) == before_files


def test_original_0065_shape_requires_a_human_decision(tmp_path):
    db = tmp_path / "historical.db"
    _historical_0065_db(db)

    report = audit_migration_state(db)

    assert report["classification"] == "decision_required"
    assert report["exit_code"] == EXIT_DECISION_REQUIRED
    assert report["migration_0065"]["shape"] == "historical_original"
    assert report["migration_0065"]["column"] == {
        "declared_type": "TEXT",
        "not_null": True,
        "default_sql": "''",
        "primary_key_position": 0,
        "hidden": 0,
    }
    assert report["migration_0065"]["index"]["partial"] is False


def test_schema_without_ledger_row_is_partial_application(tmp_path):
    db = tmp_path / "partial.db"
    with sqlite3.connect(db) as conn:
        _ledger(conn, through="0064")
        _baseline(conn)
        conn.executescript(
            "ALTER TABLE images ADD COLUMN access_token TEXT;"
            "CREATE UNIQUE INDEX idx_images_access_token "
            "ON images(access_token) WHERE access_token IS NOT NULL;"
        )

    report = audit_migration_state(db)

    assert report["classification"] == "inconsistent"
    assert report["exit_code"] == EXIT_INCONSISTENT
    assert report["migration_0065"]["shape"] == "partial_application"
    assert "0065" in report["ledger"]["pending_versions"]


def test_ledger_row_without_schema_is_reported_as_mismatch(tmp_path):
    db = tmp_path / "ledger-only.db"
    with sqlite3.connect(db) as conn:
        _ledger(conn)
        _baseline(conn)

    report = audit_migration_state(db)

    assert report["classification"] == "inconsistent"
    assert report["exit_code"] == EXIT_INCONSISTENT
    assert report["migration_0065"]["shape"] == "ledger_schema_mismatch"


def test_unknown_ledger_version_fails_closed(tmp_path):
    live = tmp_path / "live.db"
    db = tmp_path / "unknown.db"
    init_db(live)
    conn = sqlite3.connect(live)
    try:
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES ('9999', '9999_unknown')"
        )
        conn.commit()
    finally:
        conn.close()
    _offline_copy(live, db)

    report = audit_migration_state(db)

    assert report["classification"] == "inconsistent"
    assert report["exit_code"] == EXIT_INCONSISTENT
    assert report["ledger"]["unknown_versions"] == ["9999"]


def test_cli_json_and_missing_database_exit_codes(tmp_path, capsys):
    live = tmp_path / "live.db"
    db = tmp_path / "audit.db"
    init_db(live)
    _offline_copy(live, db)

    assert main([str(db), "--json"]) == EXIT_CURRENT
    body = json.loads(capsys.readouterr().out)
    assert body["migration_0065"]["shape"] == "current"
    assert body["source"]["count"] == len(discover_migrations())

    missing = tmp_path / "missing.db"
    assert main([str(missing)]) == EXIT_INPUT_ERROR
    assert not missing.exists()
    assert "does not exist" in capsys.readouterr().err


def test_manifest_covers_sources_and_changed_source_fails_closed(tmp_path):
    manifest = json.loads((MIGRATIONS_DIR / "manifest.json").read_text(encoding="utf-8"))
    source_names = {path.name for path in MIGRATIONS_DIR.glob("*.sql")}
    assert set(manifest["files"]) == source_names

    live = tmp_path / "live.db"
    db = tmp_path / "audit.db"
    init_db(live)
    _offline_copy(live, db)
    copied = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, copied)
    changed = copied / "0065_image_access_token.sql"
    changed.write_text(changed.read_text(encoding="utf-8") + "\n-- drift\n", encoding="utf-8")

    report = audit_migration_state(db, migrations_dir=copied)

    assert report["classification"] == "inconsistent"
    assert report["exit_code"] == EXIT_INCONSISTENT
    assert report["source"]["repository_state"] == "repository_checksum_drift"
    assert report["source"]["changed_files"] == ["0065_image_access_token.sql"]


def test_active_wal_is_refused_instead_of_ignored(tmp_path, capsys):
    db = tmp_path / "active.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE evidence (id INTEGER)")
        conn.commit()
        conn.execute("INSERT INTO evidence VALUES (1)")
        conn.commit()
        wal = Path(f"{db}-wal")
        assert wal.exists() and wal.stat().st_size > 0

        assert main([str(db)]) == EXIT_INPUT_ERROR
        assert "journal sidecar evidence" in capsys.readouterr().err
    finally:
        conn.close()


def _database_with_0065_schema(
    path: Path,
    *,
    column_definition: str,
    index_sql: str,
    extra_sql: str = "",
) -> None:
    with sqlite3.connect(path) as conn:
        _ledger(conn)
        _baseline(conn)
        conn.executescript(
            f"ALTER TABLE images ADD COLUMN access_token {column_definition};"
            f"{index_sql}"
            f"{extra_sql}"
        )


def test_sidecar_free_wal_header_snapshot_creates_no_sidecars(tmp_path):
    db = tmp_path / "wal-header-snapshot.db"
    init_db(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    sidecars = [Path(f"{db}{suffix}") for suffix in ("-wal", "-shm", "-journal")]
    assert not Path(f"{db}-wal").exists() or Path(f"{db}-wal").stat().st_size == 0
    for sidecar in sidecars:
        sidecar.unlink(missing_ok=True)
    assert db.read_bytes()[18:20] == b"\x02\x02"
    before_digest = _file_digest(db)
    before_mtime = db.stat().st_mtime_ns
    before_names = sorted(path.name for path in tmp_path.iterdir())

    report = audit_migration_state(db)

    assert report["classification"] == "observed_current"
    assert report["snapshot_evidence"] == {
        "database_sha256": before_digest,
        "size_bytes": db.stat().st_size,
        "journal_sidecars": [],
        "unchanged_during_audit": True,
    }
    assert _file_digest(db) == before_digest
    assert db.stat().st_mtime_ns == before_mtime
    assert sorted(path.name for path in tmp_path.iterdir()) == before_names
    assert not any(path.exists() for path in sidecars)


@pytest.mark.parametrize("suffix,payload", [("-wal", b""), ("-shm", b"x"), ("-journal", b"x")])
def test_any_journal_sidecar_is_refused(tmp_path, capsys, suffix, payload):
    live = tmp_path / "live.db"
    db = tmp_path / "audit.db"
    init_db(live)
    _offline_copy(live, db)
    sidecar = Path(f"{db}{suffix}")
    sidecar.write_bytes(payload)

    assert main([str(db)]) == EXIT_INPUT_ERROR
    assert "journal sidecar evidence" in capsys.readouterr().err
    assert sidecar.read_bytes() == payload


def test_historical_shape_also_reports_pending_suffix(tmp_path):
    db = tmp_path / "historical-pending.db"
    _historical_0065_db(db, through="0065")

    report = audit_migration_state(db)

    assert report["classification"] == "decision_required"
    assert report["ledger"]["pending_versions"] == ["0066", "0067", "0068", "0069"]
    assert "pending_migrations" in {finding["code"] for finding in report["findings"]}


@pytest.mark.parametrize(
    ("column_definition", "index_sql"),
    [
        (
            "INTEGER",
            "CREATE UNIQUE INDEX idx_images_access_token "
            "ON images(access_token) WHERE access_token IS NOT NULL;",
        ),
        (
            "TEXT",
            "CREATE UNIQUE INDEX idx_images_access_token "
            "ON images(access_token) WHERE access_token IS NOT NULL AND 0;",
        ),
        (
            "TEXT",
            "CREATE UNIQUE INDEX idx_images_access_token "
            "ON images(access_token COLLATE NOCASE DESC) "
            "WHERE access_token IS NOT NULL;",
        ),
        (
            "TEXT",
            "CREATE UNIQUE INDEX idx_images_access_token "
            "ON images(lower(access_token)) WHERE access_token IS NOT NULL;",
        ),
        (
            "TEXT NOT NULL DEFAULT ''",
            "CREATE UNIQUE INDEX idx_images_access_token "
            "ON images(access_token) WHERE access_token IS NOT NULL;",
        ),
        (
            "TEXT",
            "CREATE UNIQUE INDEX idx_images_access_token ON images(access_token);",
        ),
    ],
)
def test_noncanonical_column_or_index_never_reports_current(
    tmp_path,
    column_definition,
    index_sql,
):
    db = tmp_path / "schema-drift.db"
    _database_with_0065_schema(
        db,
        column_definition=column_definition,
        index_sql=index_sql,
    )

    report = audit_migration_state(db)

    assert report["classification"] == "inconsistent"
    assert report["exit_code"] == EXIT_INCONSISTENT
    assert report["migration_0065"]["shape"] == "schema_drift"
    assert "migration_0065_schema_drift" in {
        finding["code"] for finding in report["findings"]
    }


@pytest.mark.parametrize(
    ("extra_sql", "expected_name"),
    [
        (
            "CREATE INDEX idx_images_access_token_desc "
            "ON images(access_token DESC);",
            "idx_images_access_token_desc",
        ),
        (
            "CREATE UNIQUE INDEX idx_images_access_token_lower "
            "ON images(lower(access_token));",
            "idx_images_access_token_lower",
        ),
    ],
)
def test_additional_access_token_index_is_schema_drift(
    tmp_path,
    extra_sql,
    expected_name,
):
    db = tmp_path / "extra-index.db"
    _database_with_0065_schema(
        db,
        column_definition="TEXT",
        index_sql=(
            "CREATE UNIQUE INDEX idx_images_access_token "
            "ON images(access_token) WHERE access_token IS NOT NULL;"
        ),
        extra_sql=extra_sql,
    )

    report = audit_migration_state(db)

    assert report["classification"] == "inconsistent"
    assert report["migration_0065"]["shape"] == "schema_drift"
    assert report["migration_0065"]["index"]["other_access_token_indexes"] == [
        expected_name
    ]


def test_noncanonical_ledger_and_duplicate_versions_fail_closed(tmp_path):
    db = tmp_path / "ledger-drift.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations "
            "(version TEXT, name TEXT, applied_at TEXT)"
        )
        _baseline(conn)
        for version, name, _ in discover_migrations():
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, '2026-07-17')",
                (version, name),
            )
        conn.execute(
            "INSERT INTO schema_migrations VALUES "
            "('0065', '0065_image_access_token', '2026-07-17')"
        )
        conn.execute(
            "UPDATE schema_migrations SET applied_at = NULL WHERE version = '0064'"
        )
        conn.executescript(
            "ALTER TABLE images ADD COLUMN access_token TEXT;"
            "CREATE UNIQUE INDEX idx_images_access_token "
            "ON images(access_token) WHERE access_token IS NOT NULL;"
        )

    report = audit_migration_state(db)
    codes = {finding["code"] for finding in report["findings"]}

    assert report["classification"] == "inconsistent"
    assert report["exit_code"] == EXIT_INCONSISTENT
    assert report["ledger"]["schema"]["canonical"] is False
    assert report["ledger"]["row_count"] == len(discover_migrations()) + 1
    assert report["ledger"]["duplicate_versions"] == ["0065"]
    assert report["ledger"]["null_applied_at_count"] == 1
    assert "migration_ledger_schema_drift" in codes
    assert "duplicate_migration_versions" in codes
    assert "null_migration_timestamps" in codes


@pytest.mark.parametrize(
    ("mutation", "field", "filename"),
    [
        ("missing", "missing_files", "0064_mini_sessions.sql"),
        ("extra", "extra_files", "0070_untracked.sql"),
    ],
)
def test_missing_or_extra_migration_source_fails_closed(
    tmp_path,
    mutation,
    field,
    filename,
):
    live = tmp_path / "live.db"
    db = tmp_path / "audit.db"
    init_db(live)
    _offline_copy(live, db)
    copied = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, copied)
    target = copied / filename
    if mutation == "missing":
        target.unlink()
    else:
        target.write_text("-- untracked migration\n", encoding="utf-8")

    report = audit_migration_state(db, migrations_dir=copied)

    assert report["classification"] == "inconsistent"
    assert report["source"]["repository_state"] == "repository_checksum_drift"
    assert report["source"][field] == [filename]


def test_manifest_freezes_full_historical_commit_and_rejects_malformed_evidence(tmp_path):
    manifest_path = MIGRATIONS_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    historical = manifest["known_historical"]["0065_image_access_token.sql"]
    assert historical == {
        "commit": "a3331e344d6eb86bc93cad5783bf850169bc3f08",
        "sha256": "f27e9d51aec3635a298fe7d76d9336e4e97e49bb7189de8a3ded357b106f57b1",
    }

    live = tmp_path / "live.db"
    db = tmp_path / "audit.db"
    init_db(live)
    _offline_copy(live, db)
    copied = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, copied)
    malformed_path = copied / "manifest.json"
    malformed = json.loads(malformed_path.read_text(encoding="utf-8"))
    malformed["known_historical"] = []
    malformed_path.write_text(json.dumps(malformed), encoding="utf-8")

    with pytest.raises(MigrationAuditError, match="malformed historical evidence"):
        audit_migration_state(db, migrations_dir=copied)
    malformed_path.write_text("[]", encoding="utf-8")
    with pytest.raises(MigrationAuditError, match="unsupported or malformed"):
        audit_migration_state(db, migrations_dir=copied)


def test_baseline_only_database_without_ledger_is_pre_0065(tmp_path):
    db = tmp_path / "baseline-only.db"
    with sqlite3.connect(db) as conn:
        _baseline(conn)

    report = audit_migration_state(db)

    assert report["identity"]["recognized"] is True
    assert report["ledger"]["exists"] is False
    assert report["migration_0065"]["shape"] == "pre_0065"
    assert report["classification"] == "decision_required"
    assert report["exit_code"] == EXIT_DECISION_REQUIRED


def test_unrelated_images_database_is_an_input_error(tmp_path, capsys):
    db = tmp_path / "not-hestia.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE images (id INTEGER PRIMARY KEY, filename TEXT)")

    assert main([str(db)]) == EXIT_INPUT_ERROR
    assert "not a recognizable Hestia database" in capsys.readouterr().err


def test_missing_ledger_version_below_head_is_a_gap(tmp_path):
    live = tmp_path / "live.db"
    db = tmp_path / "gap.db"
    init_db(live)
    with sqlite3.connect(live) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = '0030'")
    _offline_copy(live, db)

    report = audit_migration_state(db)

    assert report["classification"] == "inconsistent"
    assert report["exit_code"] == EXIT_INCONSISTENT
    assert report["ledger"]["gaps"] == ["0030"]
    assert "migration_ledger_gap" in {finding["code"] for finding in report["findings"]}


def test_ledger_name_mismatch_fails_closed(tmp_path):
    live = tmp_path / "live.db"
    db = tmp_path / "name-mismatch.db"
    init_db(live)
    with sqlite3.connect(live) as conn:
        conn.execute(
            "UPDATE schema_migrations SET name = '0030_wrong_name' WHERE version = '0030'"
        )
    _offline_copy(live, db)

    report = audit_migration_state(db)

    assert report["classification"] == "inconsistent"
    assert report["ledger"]["name_mismatches"] == [
        {
            "version": "0030",
            "ledger_name": "0030_wrong_name",
            "source_name": "0030_delivery_expiry",
        }
    ]
    assert "migration_name_mismatch" in {
        finding["code"] for finding in report["findings"]
    }


def test_malformed_ledger_name_fails_closed(tmp_path):
    live = tmp_path / "live.db"
    db = tmp_path / "malformed-row.db"
    init_db(live)
    with sqlite3.connect(live) as conn:
        conn.execute(
            "UPDATE schema_migrations SET name = 'not a migration' WHERE version = '0031'"
        )
    _offline_copy(live, db)

    report = audit_migration_state(db)

    assert report["classification"] == "inconsistent"
    assert report["ledger"]["malformed_row_count"] == 1
    assert "malformed_migration_rows" in {
        finding["code"] for finding in report["findings"]
    }


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-timestamp",
        "2026-07-17",
        "2026-7-1 1:2:3",
        "2026-02-30 12:00:00",
    ],
)
def test_invalid_migration_timestamp_fails_closed(tmp_path, value):
    live = tmp_path / "live.db"
    db = tmp_path / "invalid-time.db"
    init_db(live)
    with sqlite3.connect(live) as conn:
        conn.execute(
            "UPDATE schema_migrations SET applied_at = ? WHERE version = '0065'",
            (value,),
        )
    _offline_copy(live, db)

    report = audit_migration_state(db)

    assert report["classification"] == "inconsistent"
    assert report["exit_code"] == EXIT_INCONSISTENT
    assert report["ledger"]["invalid_applied_at_count"] == 1
    assert "invalid_migration_timestamps" in {
        finding["code"] for finding in report["findings"]
    }
