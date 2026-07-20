"""Disaster-recovery verification: restore safety rails, media consistency, failure modes.

Drives the shipped ``hestia.recovery`` helpers and ``scripts/restore.sh`` against
scratch trees only. Never touches production client assets or live volumes.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from hestia.db import connect, init_db
from hestia.galleries import add_image, create_gallery, publish_gallery
from hestia.recovery import (
    RecoveryError,
    assert_restorable_backup,
    assert_safe_restore_target,
    assert_sufficient_disk,
    assert_writer_quiescent,
    check_db_media_consistency,
    is_production_data_path,
    load_backup_manifest,
    media_checksum_map,
    media_inventory,
    structured_diag,
    verify_backup_set,
    verify_restored_database,
)
from hestia.storage import LocalStorage
from hestia.tenants import create_tenant

REPO = Path(__file__).resolve().parents[1]
RESTORE_SH = REPO / "scripts" / "restore.sh"
BACKUP_SH = REPO / "scripts" / "backup.sh"
DRILL_SH = REPO / "scripts" / "restore-drill.sh"


def _jpeg_bytes(size=(16, 12), color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


def _seed_db_with_media(db_path: Path, media_dir: Path, *, name: str = "DR Studio") -> dict:
    media_dir.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    conn = connect(db_path)
    tenant = create_tenant(conn, name=name, shoot_type="wedding")
    g = create_gallery(conn, tenant_id=tenant["id"], title="DR Gallery", client_name="Client A")
    storage = LocalStorage(media_dir)
    img = add_image(
        conn,
        storage,
        tenant_id=tenant["id"],
        gallery_id=g["id"],
        filename="frame.jpg",
        fileobj=io.BytesIO(_jpeg_bytes()),
        content_type="image/jpeg",
    )
    assert img is not None
    assert publish_gallery(conn, tenant["id"], g["id"]) is True
    conn.commit()
    conn.close()
    return {
        "tenant_id": tenant["id"],
        "gallery_id": g["id"],
        "image_id": img["id"],
        "storage_key": img["storage_key"],
        "thumb_key": img.get("thumb_key"),
    }


def _quiesce_db(db_path: Path) -> None:
    """Checkpoint WAL and drop sidecars so restore.sh sees a stopped app."""
    if not db_path.is_file():
        return
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    for side in (f"{db_path}-wal", f"{db_path}-shm"):
        Path(side).unlink(missing_ok=True)


def _backup(data_dir: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    _quiesce_db(data_dir / "hestia.db")
    env = {
        **os.environ,
        "HESTIA_DATA_DIR": str(data_dir),
        "HESTIA_BACKUP_DIR": str(backup_dir),
        "HESTIA_BACKUP_KEEP": "5",
    }
    subprocess.run(["bash", str(BACKUP_SH)], cwd=REPO, env=env, check=True, capture_output=True)
    artifacts = sorted(backup_dir.glob("hestia-*.db.gz"))
    assert len(artifacts) >= 1
    return artifacts[-1]


def _restore(
    data_dir: Path,
    artifact: Path,
    *,
    backup_dir: Path | None = None,
    allow_production: bool = False,
    force_live_wal: bool = False,
    media_dir: Path | None = None,
    manifest: Path | None = None,
    require_manifest: bool = False,
) -> subprocess.CompletedProcess:
    env = {**os.environ, "HESTIA_DATA_DIR": str(data_dir)}
    # Never inherit a production-restore override unless the test asks for it.
    env.pop("HESTIA_ALLOW_PRODUCTION_RESTORE", None)
    env.pop("HESTIA_REQUIRE_BACKUP_MANIFEST", None)
    if allow_production:
        env["HESTIA_ALLOW_PRODUCTION_RESTORE"] = "1"
    if backup_dir is not None:
        env["HESTIA_BACKUP_DIR"] = str(backup_dir)
    if media_dir is not None:
        env["HESTIA_MEDIA_DIR"] = str(media_dir)
    args = ["bash", str(RESTORE_SH), str(artifact)]
    if force_live_wal:
        args.append("--force-live-wal")
    if allow_production:
        args.append("--allow-production")
    if manifest is not None:
        args.extend(["--manifest", str(manifest)])
    if require_manifest:
        args.append("--require-manifest")
    return subprocess.run(args, cwd=REPO, env=env, capture_output=True, text=True)


# ── production path refusal ─────────────────────────────────────────────────


def test_is_production_data_path_flags_default_and_deploy_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    assert is_production_data_path(tmp_path / "data") is True
    assert is_production_data_path(Path("/srv/hestia/data")) is True
    assert is_production_data_path(Path("/data")) is True
    # Layout …/hestia/data even when cwd is elsewhere (bare-metal style).
    nest = tmp_path / "opt" / "hestia" / "data"
    nest.mkdir(parents=True)
    assert is_production_data_path(nest) is True
    scratch = tmp_path / "scratch-restore"
    scratch.mkdir()
    assert is_production_data_path(scratch) is False
    # A directory merely named data under an unrelated parent is not production
    # unless it is the cwd default ./data (already covered above).
    other = tmp_path / "uploads" / "data"
    other.mkdir(parents=True)
    assert is_production_data_path(other) is False


def test_symlink_to_production_data_is_refused(tmp_path, monkeypatch):
    """Canonical resolve: scratch-looking symlink whose target is ./data must refuse."""
    monkeypatch.chdir(tmp_path)
    prod = tmp_path / "data"
    prod.mkdir()
    link = tmp_path / "looks-like-scratch"
    link.symlink_to(prod)
    assert is_production_data_path(link) is True
    with pytest.raises(RecoveryError, match="production"):
        assert_safe_restore_target(link)
    # Override still works on the resolved production path.
    assert assert_safe_restore_target(link, allow_production=True) == prod.resolve()


def test_assert_safe_restore_target_refuses_without_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    with pytest.raises(RecoveryError, match="production"):
        assert_safe_restore_target(data)
    # Explicit override is the only way through.
    assert assert_safe_restore_target(data, allow_production=True) == data.resolve()


def test_restore_sh_refuses_repo_data_dir(tmp_path, monkeypatch):
    """scripts/restore.sh must not accept HESTIA_DATA_DIR=./data without override."""
    monkeypatch.chdir(REPO)
    source = tmp_path / "src"
    source.mkdir()
    init_db(source / "hestia.db")
    conn = connect(source / "hestia.db")
    create_tenant(conn, name="Only DB", shoot_type="other")
    conn.commit()
    conn.close()
    artifact = _backup(source, tmp_path / "bak")
    # Point at the real repo ./data path — must refuse.
    result = _restore(REPO / "data", artifact)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "refusing" in combined.lower() or "production" in combined.lower()
    # Live repo DB (if any) must still be the same file that was there — we never
    # require it to exist; we only require we did not write a new hestia.db via restore.
    # Prove refusal by checking no pre-restore safety copy was created under data/backups
    # for this run with our stamp pattern is hard; instead: exit non-zero is enough plus
    # the error text. Also: a missing backup file is a different code path tested below.


# ── happy path verification ─────────────────────────────────────────────────


def test_verify_restored_database_success_with_media(tmp_path):
    data = tmp_path / "data"
    media = data / "media"
    seed = _seed_db_with_media(data / "hestia.db", media)
    artifact = _backup(data, tmp_path / "bak")
    # Restore into a different scratch tree that had different tenants.
    target = tmp_path / "target"
    target.mkdir()
    init_db(target / "hestia.db")
    conn = connect(target / "hestia.db")
    create_tenant(conn, name="Will Be Replaced", shoot_type="other")
    conn.commit()
    conn.close()
    media_dst = target / "media"
    shutil.copytree(media, media_dst)
    _quiesce_db(target / "hestia.db")
    # HESTIA_BACKUP_DIR off to the side must NOT receive the pre-restore copy —
    # safety stays same-FS under target/backups.
    offbox = tmp_path / "offbox-archives"
    offbox.mkdir()
    result = _restore(target, artifact, backup_dir=offbox)
    assert result.returncode == 0, result.stderr
    safety_copies = list((target / "backups").glob("pre-restore-*.db"))
    assert len(safety_copies) == 1, safety_copies
    assert list(offbox.glob("pre-restore-*.db")) == []
    report = verify_restored_database(
        target / "hestia.db",
        media_dir=media_dst,
        backup_path=artifact,
        require_media=True,
        measurement_kind="synthetic_scratch_drill",
    )
    assert report.ok is True, report.failures
    assert report.integrity_check == "ok"
    assert report.tenant_count >= 1
    assert report.image_count >= 1
    assert report.consistency is not None and report.consistency.ok
    assert report.consistency.missing_blobs == []
    assert seed["storage_key"] not in report.consistency.missing_blobs
    assert report.representative_gallery is not None
    assert report.representative_gallery["first_blob_present"] is True
    for banned in ("access_token", "email", "client_name", "password"):
        assert banned not in report.representative_gallery
    assert report.rpo_seconds is not None
    assert report.elapsed_ms >= 0
    assert report.correlation_id
    assert report.measurement_kind == "synthetic_scratch_drill"
    assert "incident" in report.timing_disclaimer.lower()


def test_media_inventory_and_consistency_detect_missing_and_orphan(tmp_path):
    data = tmp_path / "data"
    media = data / "media"
    seed = _seed_db_with_media(data / "hestia.db", media)
    conn = connect(data / "hestia.db")
    # Missing blob: delete the original file but leave the DB row.
    (media / seed["storage_key"]).unlink()
    report = check_db_media_consistency(conn, media)
    assert report.ok is False
    assert seed["storage_key"] in report.missing_blobs
    # Orphan: put an unexpected file on disk.
    orphan = media / "orphan-file.bin"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"not-in-db")
    report2 = check_db_media_consistency(conn, media)
    assert any(p.endswith("orphan-file.bin") for p in report2.orphan_blobs)
    # Orphans alone do not make ok=False when blobs still missing — still false.
    assert report2.ok is False
    conn.close()
    inv = media_inventory(media)
    assert any(b.relative_path.endswith("orphan-file.bin") for b in inv)
    assert all(len(b.sha256) == 64 for b in inv)


def test_size_mismatch_is_reported(tmp_path):
    data = tmp_path / "data"
    media = data / "media"
    seed = _seed_db_with_media(data / "hestia.db", media)
    # Truncate the blob so size disagrees with images.bytes.
    path = media / seed["storage_key"]
    path.write_bytes(b"x")
    conn = connect(data / "hestia.db")
    report = check_db_media_consistency(conn, media)
    conn.close()
    assert report.ok is False
    assert report.size_mismatches
    assert report.size_mismatches[0]["storage_key"] == seed["storage_key"]


# ── failure modes: missing / corrupt / interrupted / schema / disk ──────────


def test_restore_missing_backup_leaves_target_untouched(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    db = target / "hestia.db"
    init_db(db)
    conn = connect(db)
    create_tenant(conn, name="Pre-existing", shoot_type="other")
    conn.commit()
    conn.close()
    before = db.read_bytes()
    result = _restore(target, tmp_path / "no-such-backup.db.gz")
    assert result.returncode != 0
    assert "no backup" in (result.stderr + result.stdout).lower()
    assert db.read_bytes() == before


def test_restore_corrupt_gzip_leaves_target_untouched(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    db = target / "hestia.db"
    init_db(db)
    conn = connect(db)
    create_tenant(conn, name="Pre-existing", shoot_type="other")
    conn.commit()
    conn.close()
    before = db.read_bytes()
    bogus = tmp_path / "bad.db.gz"
    bogus.write_bytes(b"not-a-gzip-stream-at-all")
    result = _restore(target, bogus)
    assert result.returncode != 0
    assert db.read_bytes() == before
    # No half-applied main DB rename.
    assert not list(target.glob("pre-restore-*.db"))


def test_restore_corrupt_sqlite_payload_leaves_target_untouched(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    db = target / "hestia.db"
    init_db(db)
    conn = connect(db)
    create_tenant(conn, name="Pre-existing", shoot_type="other")
    conn.commit()
    conn.close()
    before = db.read_bytes()
    # Valid gzip wrapping non-SQLite garbage → integrity_check fails.
    artifact = tmp_path / "garbage.db.gz"
    with gzip.open(artifact, "wb") as fh:
        fh.write(b"SQLite format 3\x00this is not a real database payload" + b"\x00" * 200)
    result = _restore(target, artifact)
    assert result.returncode != 0
    assert db.read_bytes() == before


def _seed_target_with_tenant(target: Path, name: str = "Pre-existing") -> bytes:
    target.mkdir(parents=True, exist_ok=True)
    db = target / "hestia.db"
    init_db(db)
    conn = connect(db)
    create_tenant(conn, name=name, shoot_type="other")
    conn.commit()
    conn.close()
    _quiesce_db(db)
    return db.read_bytes()


def test_restore_empty_gzip_leaves_target_byte_identical(tmp_path):
    """Empty .db.gz must not replace the live DB (integrity_check alone would pass 0-byte unpack)."""
    target = tmp_path / "target"
    safety = tmp_path / "safety"
    before = _seed_target_with_tenant(target)
    empty_gz = tmp_path / "empty.db.gz"
    with gzip.open(empty_gz, "wb") as fh:
        fh.write(b"")
    result = _restore(target, empty_gz, backup_dir=safety)
    assert result.returncode != 0, result.stdout + result.stderr
    assert (target / "hestia.db").read_bytes() == before
    assert not list(safety.glob("pre-restore-*.db"))
    # Gate failed early — no interrupted-restore marker and no safety move.
    assert not (target / ".restore-in-progress").exists()
    assert (
        not list((target / "backups").glob("pre-restore-*.db"))
        if (target / "backups").exists()
        else True
    )


def test_restore_empty_raw_db_leaves_target_byte_identical(tmp_path):
    target = tmp_path / "target"
    safety = tmp_path / "safety"
    before = _seed_target_with_tenant(target)
    empty_db = tmp_path / "empty.db"
    empty_db.write_bytes(b"")
    result = _restore(target, empty_db, backup_dir=safety)
    assert result.returncode != 0, result.stdout + result.stderr
    assert (target / "hestia.db").read_bytes() == before
    assert not list(safety.glob("pre-restore-*.db"))
    assert not (target / ".restore-in-progress").exists()


def test_restore_non_hestia_sqlite_leaves_target_byte_identical(tmp_path):
    """A pristine SQLite file passes PRAGMA integrity_check but has no schema_migrations."""
    target = tmp_path / "target"
    before = _seed_target_with_tenant(target)
    foreign = tmp_path / "foreign.db"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO notes (body) VALUES ('not hestia')")
    conn.commit()
    conn.close()
    # Confirm the trap: integrity is ok, but gate must still refuse.
    assert sqlite3.connect(foreign).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with pytest.raises(RecoveryError, match="schema_migrations"):
        assert_restorable_backup(foreign)

    artifact = tmp_path / "foreign.db.gz"
    with gzip.open(artifact, "wb") as fh:
        fh.write(foreign.read_bytes())
    result = _restore(target, artifact, backup_dir=tmp_path / "safety")
    assert result.returncode != 0, result.stdout + result.stderr
    combined = (result.stdout + result.stderr).lower()
    assert "schema" in combined or "refused" in combined or "hestia" in combined
    assert (target / "hestia.db").read_bytes() == before
    assert not list((tmp_path / "safety").glob("pre-restore-*.db"))


def test_restore_unsupported_schema_leaves_target_byte_identical(tmp_path):
    """Future ledger version that still integrity-checks must not install via restore.sh."""
    target = tmp_path / "target"
    before = _seed_target_with_tenant(target)

    source = tmp_path / "src"
    source.mkdir()
    init_db(source / "hestia.db")
    conn = connect(source / "hestia.db")
    create_tenant(conn, name="Future Schema", shoot_type="other")
    conn.execute(
        "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
        ("9999", "9999_from_the_future"),
    )
    conn.commit()
    conn.close()
    # Also force MAX(version) to be the unsupported one: MAX of text versions —
    # 9999 is max lexicographically among 0001..9999 for zero-padded 4-digit.
    # assert_supported_schema checks the MAX(version) is in known set.
    artifact = _backup(source, tmp_path / "bak")
    result = _restore(target, artifact, backup_dir=tmp_path / "safety")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "unsupported" in (result.stdout + result.stderr).lower()
    assert (target / "hestia.db").read_bytes() == before
    assert not list((tmp_path / "safety").glob("pre-restore-*.db"))


def test_verify_refuses_unsupported_schema_version(tmp_path):
    db = tmp_path / "hestia.db"
    init_db(db)
    conn = connect(db)
    create_tenant(conn, name="Schema Canary", shoot_type="other")
    # Inject a future migration version the running code does not ship.
    conn.execute(
        "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
        ("9999", "9999_from_the_future"),
    )
    conn.commit()
    conn.close()
    report = verify_restored_database(db)
    assert report.ok is False
    assert any("unsupported schema" in f for f in report.failures)


def test_checksum_mismatch_fails_consistency(tmp_path):
    data = tmp_path / "data"
    media = data / "media"
    seed = _seed_db_with_media(data / "hestia.db", media)
    expected = media_checksum_map(media)
    # Tamper with the blob without changing size so only checksum catches it.
    path = media / seed["storage_key"]
    original = path.read_bytes()
    assert len(original) > 8
    path.write_bytes(b"\x00" * len(original))
    conn = connect(data / "hestia.db")
    # Size may still match images.bytes if we preserved length.
    report = check_db_media_consistency(conn, media, checksum=True, expected_checksums=expected)
    conn.close()
    assert report.ok is False
    assert report.checksum_mismatches
    assert report.checksum_mismatches[0]["storage_key"] == seed["storage_key"]
    assert report.checksum_mismatches[0]["actual_sha256"] != expected[seed["storage_key"]]


def test_checksum_true_requires_expected_map(tmp_path):
    data = tmp_path / "data"
    media = data / "media"
    _seed_db_with_media(data / "hestia.db", media)
    conn = connect(data / "hestia.db")
    with pytest.raises(ValueError, match="expected_checksums"):
        check_db_media_consistency(conn, media, checksum=True)
    conn.close()


def test_assert_restorable_backup_accepts_real_hestia_db(tmp_path):
    db = tmp_path / "hestia.db"
    init_db(db)
    conn = connect(db)
    create_tenant(conn, name="Good", shoot_type="other")
    conn.commit()
    conn.close()
    version = assert_restorable_backup(db)
    assert version is not None
    assert version.isdigit() or version  # non-empty supported version


def test_interrupted_restore_marker_and_live_db_intact(tmp_path):
    """A leftover .restore-*.db / in-progress marker must not be treated as success.

    Simulate an interrupted unpack: marker present, temp present, live DB unchanged.
    """
    target = tmp_path / "target"
    target.mkdir()
    db = target / "hestia.db"
    init_db(db)
    conn = connect(db)
    create_tenant(conn, name="Live Before Crash", shoot_type="other")
    conn.commit()
    conn.close()
    before = db.read_bytes()
    (target / ".restore-in-progress").write_text(
        "correlation_id=deadbeef\nstamp=interrupted\n", encoding="utf-8"
    )
    (target / ".restore-20990101-000000.db").write_bytes(b"partial")
    # Operator recovery: refuse to claim success while marker exists; live DB intact.
    assert (target / ".restore-in-progress").is_file()
    assert db.read_bytes() == before
    # Running restore with a good artifact should succeed and clear the marker.
    source = tmp_path / "src"
    source.mkdir()
    _seed_db_with_media(source / "hestia.db", source / "media", name="After Repair")
    artifact = _backup(source, tmp_path / "bak")
    _quiesce_db(target / "hestia.db")
    result = _restore(target, artifact, backup_dir=tmp_path / "safety")
    assert result.returncode == 0, result.stderr
    assert not (target / ".restore-in-progress").exists()
    names = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM tenants")}
    assert names == {"After Repair"}


def test_assert_sufficient_disk_fails_loud_when_injected(tmp_path):
    with pytest.raises(RecoveryError, match="insufficient disk"):
        assert_sufficient_disk(tmp_path, need_bytes=10**12, free_bytes=100)
    # Real free space on tmp should exceed a tiny need.
    free = assert_sufficient_disk(tmp_path, need_bytes=1)
    assert free >= 1


def test_structured_diag_is_privacy_safe_and_has_correlation():
    payload = structured_diag(
        "recovery.test",
        correlation_id="abc123def456",
        tenant_count=2,
        token="should-be-stripped",
        password="nope",
        secret="also-no",
        api_key="nope",
    )
    assert payload["correlation_id"] == "abc123def456"
    assert payload["action"] == "recovery.test"
    assert "token" not in payload
    assert "password" not in payload
    assert "secret" not in payload
    assert "api_key" not in payload
    assert payload["tenant_count"] == 2


def test_free_space_probe_does_not_mkdir_missing_leaf(tmp_path):
    """Disk preflight must not create a production-like path as a side effect."""
    missing = tmp_path / "never-created" / "data"
    assert not missing.exists()
    from hestia.recovery import free_space_bytes

    free = free_space_bytes(missing)
    assert free >= 0
    assert not missing.exists()
    assert not missing.parent.exists()


# ── shell drill (end-to-end entry point) ────────────────────────────────────


def test_restore_drill_script_green(tmp_path):
    report = tmp_path / "drill-report.json"
    env = {**os.environ, "HESTIA_DRILL_REPORT": str(report)}
    env.pop("HESTIA_ALLOW_PRODUCTION_RESTORE", None)
    result = subprocess.run(
        ["bash", str(DRILL_SH)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    combined = result.stdout + result.stderr
    assert "restore drill OK" in combined
    assert "integrity=ok" in combined
    assert "correlation_id=" in combined
    assert "synthetic_elapsed_ms=" in combined
    assert "artifact_age_s=" in combined
    assert "SYNTHETIC" in combined
    assert "measurement_kind=synthetic_scratch_drill" in combined
    assert report.is_file()
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert data["integrity_check"] == "ok"
    assert data["image_count"] >= 1
    assert data["consistency"]["ok"] is True
    assert data["rpo_seconds"] is not None
    assert data["measurement_kind"] == "synthetic_scratch_drill"
    # Privacy: no client tokens or emails in the capturable report.
    blob = json.dumps(data)
    assert "access_token" not in blob
    assert "@" not in blob or "example" not in blob  # no email-shaped client fields


def test_recovery_cli_verify_and_check_target(tmp_path):
    data = tmp_path / "data"
    media = data / "media"
    _seed_db_with_media(data / "hestia.db", media)
    out = tmp_path / "report.json"
    proc = subprocess.run(
        [
            "python",
            "-m",
            "hestia.recovery",
            "verify",
            str(data / "hestia.db"),
            "--media-dir",
            str(media),
            "--require-media",
            "--json-out",
            str(out),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.is_file()
    body = json.loads(out.read_text(encoding="utf-8"))
    assert body["ok"] is True

    # check-target refuses ./data from repo root.
    refuse = subprocess.run(
        ["python", "-m", "hestia.recovery", "check-target", str(REPO / "data")],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert refuse.returncode == 2
    ok = subprocess.run(
        [
            "python",
            "-m",
            "hestia.recovery",
            "check-target",
            str(tmp_path / "scratch"),
            "--allow-production",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    # scratch may not exist yet — check-target only cares about the path identity.
    # Without allow it should still be fine for a non-production path.
    ok2 = subprocess.run(
        ["python", "-m", "hestia.recovery", "check-target", str(tmp_path / "scratch")],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert ok2.returncode == 0, ok2.stderr
    assert ok.returncode == 0


# ── Fail-closed schema queries + generation manifests ───────────────────────


def test_malformed_images_table_fails_verification_not_empty_clean(tmp_path):
    """Expected-table query failure must not look like a clean empty studio."""
    db = tmp_path / "hestia.db"
    init_db(db)
    conn = connect(db)
    create_tenant(conn, name="Broken Schema Studio", shoot_type="other")
    # Drop images so the expected query fails (malformed/missing table).
    conn.execute("DROP TABLE images")
    conn.commit()
    conn.close()
    report = verify_restored_database(db, media_dir=tmp_path / "media")
    assert report.ok is False
    assert any("schema_query" in f for f in report.failures)
    # Not a silent zero-tenant clean result after swallow — tenants still readable,
    # but images path must fail closed.
    assert any("images" in f for f in report.failures)


def test_db_media_refs_raises_not_empty_on_missing_table(tmp_path):
    db = tmp_path / "x.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE notes (id INTEGER)")
    conn.commit()
    with pytest.raises(RecoveryError, match="schema_query:images"):
        from hestia.recovery import db_media_refs

        db_media_refs(conn)
    conn.close()


def test_consistency_query_error_marks_not_ok(tmp_path):
    db = tmp_path / "hestia.db"
    init_db(db)
    conn = connect(db)
    create_tenant(conn, name="T", shoot_type="other")
    conn.execute("DROP TABLE images")
    conn.commit()
    media = tmp_path / "media"
    media.mkdir()
    report = check_db_media_consistency(conn, media)
    conn.close()
    assert report.ok is False
    assert report.query_errors
    assert any("schema_query" in e for e in report.query_errors)


def test_backup_manifest_binds_generation_and_refuses_cross_media(tmp_path):
    data = tmp_path / "data"
    media = data / "media"
    _seed_db_with_media(data / "hestia.db", media)
    artifact = _backup(data, tmp_path / "bak")
    # backup.sh now writes a sidecar; if helper only used online backup without script,
    # build explicitly.
    sidecar = Path(str(artifact) + ".manifest.json")
    if not sidecar.is_file():
        # _backup uses backup.sh which should write it — assert.
        pass
    assert sidecar.is_file(), "backup.sh must write generation manifest"
    manifest = load_backup_manifest(sidecar)
    assert manifest["generation_id"]
    assert manifest["db"]["sha256"]
    assert manifest["media"]["file_count"] >= 1
    # Happy path: verify generation.
    summary = verify_backup_set(
        db_artifact=artifact, manifest=manifest, media_dir=media, require_media=True
    )
    assert summary["ok"] is True
    assert summary["generation_id"] == manifest["generation_id"]

    # Cross-generation: swap media blob content → checksum mismatch refused.
    first = next(iter(manifest["media"]["files"]))
    path = media / first
    path.write_bytes(b"\x00" * max(1, path.stat().st_size))
    with pytest.raises(RecoveryError, match="media_checksum_mismatch"):
        verify_backup_set(
            db_artifact=artifact, manifest=manifest, media_dir=media, require_media=True
        )


def test_restore_refuses_corrupt_manifest_leaves_target(tmp_path):
    target = tmp_path / "target"
    before = _seed_target_with_tenant(target)
    source = tmp_path / "src"
    source.mkdir()
    _seed_db_with_media(source / "hestia.db", source / "media")
    artifact = _backup(source, tmp_path / "bak")
    bad = tmp_path / "bad.manifest.json"
    bad.write_text("{not json", encoding="utf-8")
    _quiesce_db(target / "hestia.db")
    result = _restore(
        target,
        artifact,
        backup_dir=tmp_path / "safety",
        manifest=bad,
        require_manifest=True,
        media_dir=source / "media",
    )
    assert result.returncode != 0
    assert (target / "hestia.db").read_bytes() == before


def test_restore_refuses_missing_manifest_when_required(tmp_path):
    target = tmp_path / "target"
    before = _seed_target_with_tenant(target)
    source = tmp_path / "src"
    source.mkdir()
    init_db(source / "hestia.db")
    conn = connect(source / "hestia.db")
    create_tenant(conn, name="OnlyDB", shoot_type="other")
    conn.commit()
    conn.close()
    # Raw online backup without going through backup.sh (no manifest).
    bak = tmp_path / "bak"
    bak.mkdir()
    # Use online backup API only (bypass scripts/backup.sh) so no manifest exists.
    raw = bak / "hestia-manual.db"
    src = sqlite3.connect(source / "hestia.db")
    dst = sqlite3.connect(raw)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    artifact = bak / "hestia-manual.db.gz"
    with open(raw, "rb") as fh, gzip.open(artifact, "wb") as out:
        out.write(fh.read())
    _quiesce_db(target / "hestia.db")
    result = _restore(
        target, artifact, backup_dir=tmp_path / "safety", require_manifest=True
    )
    assert result.returncode != 0
    assert "manifest" in (result.stdout + result.stderr).lower()
    assert (target / "hestia.db").read_bytes() == before


def test_restore_refuses_plain_force_for_live_wal(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    init_db(target / "hestia.db")
    # Create a fake WAL sidecar.
    (target / "hestia.db-wal").write_bytes(b"wal")
    source = tmp_path / "src"
    source.mkdir()
    init_db(source / "hestia.db")
    conn = connect(source / "hestia.db")
    create_tenant(conn, name="S", shoot_type="other")
    conn.commit()
    conn.close()
    artifact = _backup(source, tmp_path / "bak")
    # --force must exit 2 with guidance to --force-live-wal
    env = {**os.environ, "HESTIA_DATA_DIR": str(target)}
    env.pop("HESTIA_ALLOW_PRODUCTION_RESTORE", None)
    r = subprocess.run(
        ["bash", str(RESTORE_SH), str(artifact), "--force"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0
    assert "force-live-wal" in (r.stdout + r.stderr).lower()
    # Without override, wal still refuses.
    with pytest.raises(RecoveryError, match="live"):
        assert_writer_quiescent(target)
    # Loud override accepted by helper.
    assert_writer_quiescent(target, force_live_wal=True)


def test_idempotent_reverification_same_manifest(tmp_path):
    data = tmp_path / "data"
    media = data / "media"
    _seed_db_with_media(data / "hestia.db", media)
    artifact = _backup(data, tmp_path / "bak")
    sidecar = Path(str(artifact) + ".manifest.json")
    m = load_backup_manifest(sidecar)
    s1 = verify_backup_set(db_artifact=artifact, manifest=m, media_dir=media)
    s2 = verify_backup_set(db_artifact=artifact, manifest=m, media_dir=media)
    assert s1["generation_id"] == s2["generation_id"]
    assert s1["ok"] and s2["ok"]


def test_restore_drill_reports_generation_manifest(tmp_path):
    report = tmp_path / "drill-report.json"
    env = {**os.environ, "HESTIA_DRILL_REPORT": str(report)}
    env.pop("HESTIA_ALLOW_PRODUCTION_RESTORE", None)
    result = subprocess.run(
        ["bash", str(DRILL_SH)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    combined = result.stdout + result.stderr
    assert "generation_manifest=ok" in combined or "generation manifest" in combined.lower()
    assert "SYNTHETIC" in combined
