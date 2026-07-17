"""Pipeline state machine: gallery → vision → offer, persisted and idempotent."""

import io
import json

from hestia.galleries import add_image, create_gallery
from hestia.pipeline import execute_run, load_run, start_run
from hestia.tenants import create_tenant
from hestia.vision import (
    MockVisionProvider,
    VisionError,
    VisionProviderError,
    VisionResult,
)


def _seed(conn, storage, n=5, shoot_type="wedding"):
    tenant = create_tenant(conn, name="Pipe Studio", shoot_type=shoot_type)
    gallery = create_gallery(conn, tenant_id=tenant["id"], title="Run Gallery")
    for i in range(n):
        add_image(conn, storage, tenant_id=tenant["id"], gallery_id=gallery["id"],
                  filename=f"f{i}.jpg", fileobj=io.BytesIO(bytes([i]) * 16))
    conn.commit()
    return tenant, gallery


def test_happy_path_runs_vision_then_offer(conn, storage, settings, db_path):
    tenant, gallery = _seed(conn, storage)
    run = start_run(conn, tenant=tenant, gallery_id=gallery["id"])
    result = execute_run(db_path, settings, run["id"], storage=storage,
                         provider=MockVisionProvider())
    assert result["status"] == "done"
    assert result["offer_url"] and "/s/" in result["offer_url"]
    statuses = {s["name"]: s["status"] for s in result["steps"]}
    assert statuses == {"vision": "done", "offer": "done"}
    assert conn.execute("SELECT COUNT(*) AS n FROM offers").fetchone()["n"] == 1


def test_double_run_yields_exactly_one_offer(conn, storage, settings, db_path):
    """The core invariant: re-processing never duplicates the client offer."""
    tenant, gallery = _seed(conn, storage)
    run1 = start_run(conn, tenant=tenant, gallery_id=gallery["id"])
    r1 = execute_run(db_path, settings, run1["id"], storage=storage, provider=MockVisionProvider())

    run2 = start_run(conn, tenant=tenant, gallery_id=gallery["id"])
    r2 = execute_run(db_path, settings, run2["id"], storage=storage, provider=MockVisionProvider())

    assert run1["id"] == run2["id"]                      # same run row reused
    assert r1["offer_url"] == r2["offer_url"]            # same client link
    assert conn.execute("SELECT COUNT(*) AS n FROM offers").fetchone()["n"] == 1
    tokens = conn.execute("SELECT DISTINCT token FROM offers").fetchall()
    assert len(tokens) == 1


def test_vision_failure_marks_run_error_and_no_offer(conn, storage, settings, db_path):
    class FailingProvider:
        backend = "fail"
        calls = 0

        def analyze(self, *, filename, data, style=""):
            self.calls += 1
            if self.calls == 1:
                return VisionResult(keywords=["partial"], keeper_score=0.8)
            raise VisionError("vision provider down")

    tenant, gallery = _seed(conn, storage)
    run = start_run(conn, tenant=tenant, gallery_id=gallery["id"])
    provider = FailingProvider()
    result = execute_run(db_path, settings, run["id"], storage=storage, provider=provider)
    assert provider.calls == 2
    assert result["status"] == "error"
    assert "vision" in (result["error"] or "")
    statuses = {s["name"]: s["status"] for s in result["steps"]}
    assert statuses["vision"] == "error"
    assert statuses["offer"] == "pending"
    assert conn.execute("SELECT COUNT(*) AS n FROM offers").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM image_analyses").fetchone()["n"] == 0


class _MidGalleryFailingXai:
    backend = "xai"

    def __init__(self):
        self.calls = 0

    def analyze(self, *, filename, data, style=""):
        self.calls += 1
        if self.calls == 2:
            raise VisionProviderError("xai transport unavailable: sk-test-sensitive")
        return VisionResult(
            keywords=["live-partial"],
            keeper_score=0.01,
            hero_potential=0.01,
            shot_type="detail",
            alt_text="Partial live result.",
        )


class _RecoveredXai:
    backend = "xai"

    def __init__(self):
        self.calls = 0

    def analyze(self, *, filename, data, style=""):
        self.calls += 1
        return VisionResult(
            keywords=["live-recovered"],
            keeper_score=0.9,
            hero_potential=0.8,
            shot_type="portrait",
            alt_text="Recovered live result.",
        )


def test_xai_mid_gallery_failure_recomputes_whole_gallery_and_creates_offer(
    conn, storage, settings, db_path
):
    tenant, gallery = _seed(conn, storage, n=3)
    run = start_run(conn, tenant=tenant, gallery_id=gallery["id"])
    provider = _MidGalleryFailingXai()

    result = execute_run(
        db_path,
        settings,
        run["id"],
        storage=storage,
        provider=provider,
    )

    assert provider.calls == 2
    assert result["status"] == "done"
    assert result["error"] is None
    assert result["offer_url"] and "/s/" in result["offer_url"]
    vision_step = next(step for step in result["steps"] if step["name"] == "vision")
    assert vision_step["status"] == "done"
    assert "deterministic mock used for the whole gallery" in vision_step["detail"]
    summary = vision_step["output"]["summary"]
    assert summary["backend"] == "mock"
    assert summary["fallback_from"] == "xai"
    assert summary["fallback_scope"] == "whole_gallery"

    rows = conn.execute(
        "SELECT i.filename, i.storage_key, a.* FROM image_analyses a "
        "JOIN images i ON i.id = a.image_id WHERE a.gallery_id = ? ORDER BY i.position",
        (gallery["id"],),
    ).fetchall()
    assert len(rows) == 3
    mock = MockVisionProvider()
    for row in rows:
        expected = mock.analyze(
            filename=row["filename"],
            data=storage.open(row["storage_key"]),
        )
        assert json.loads(row["keywords_json"]) == expected.keywords
        assert row["keeper_score"] == expected.keeper_score
        assert row["hero_potential"] == expected.hero_potential
        assert row["shot_type"] == expected.shot_type

    assert conn.execute("SELECT COUNT(*) AS n FROM offers").fetchone()["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM ai_usage_events WHERE tenant_id = ?",
        (tenant["id"],),
    ).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log "
        "WHERE tenant_id = ? AND action = 'pipeline.vision_fallback'",
        (tenant["id"],),
    ).fetchone()["n"] == 1
    assert "sk-test-sensitive" not in json.dumps(result)
    audit_details = conn.execute(
        "SELECT detail FROM audit_log WHERE tenant_id = ?",
        (tenant["id"],),
    ).fetchall()
    assert all("sk-test-sensitive" not in row["detail"] for row in audit_details)


def test_mock_fallback_failure_is_terminal_and_redacts_details(
    conn, storage, settings, db_path, monkeypatch
):
    class FailingMock:
        backend = "mock"

        def analyze(self, *, filename, data, style=""):
            raise RuntimeError("storage failed: sk-test-sensitive")

    monkeypatch.setattr("hestia.pipeline.MockVisionProvider", FailingMock)
    tenant, gallery = _seed(conn, storage, n=3)
    run = start_run(conn, tenant=tenant, gallery_id=gallery["id"])

    result = execute_run(
        db_path,
        settings,
        run["id"],
        storage=storage,
        provider=_MidGalleryFailingXai(),
    )

    assert result["status"] == "error"
    assert result["error"] == "vision fallback failed (RuntimeError)"
    vision_step = next(step for step in result["steps"] if step["name"] == "vision")
    assert vision_step["status"] == "error"
    assert vision_step["detail"] == "deterministic mock fallback failed (RuntimeError)"
    assert "sk-test-sensitive" not in json.dumps(result)
    assert conn.execute("SELECT COUNT(*) AS n FROM image_analyses").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM offers").fetchone()["n"] == 0
    audit_details = conn.execute(
        "SELECT detail FROM audit_log WHERE tenant_id = ?",
        (tenant["id"],),
    ).fetchall()
    assert all("sk-test-sensitive" not in row["detail"] for row in audit_details)


def test_reprocess_after_xai_fallback_retries_live_and_reuses_offer(
    conn, storage, settings, db_path
):
    tenant, gallery = _seed(conn, storage, n=3)
    run = start_run(conn, tenant=tenant, gallery_id=gallery["id"])
    first = execute_run(
        db_path,
        settings,
        run["id"],
        storage=storage,
        provider=_MidGalleryFailingXai(),
    )
    first_token = conn.execute("SELECT token FROM offers").fetchone()["token"]

    rearmed = start_run(conn, tenant=tenant, gallery_id=gallery["id"])
    recovered_provider = _RecoveredXai()
    second = execute_run(
        db_path,
        settings,
        rearmed["id"],
        storage=storage,
        provider=recovered_provider,
    )

    assert rearmed["id"] == run["id"]
    assert recovered_provider.calls == 3
    assert second["status"] == "done"
    assert second["offer_url"] == first["offer_url"]
    assert conn.execute("SELECT COUNT(*) AS n FROM offers").fetchone()["n"] == 1
    assert conn.execute("SELECT token FROM offers").fetchone()["token"] == first_token
    vision_step = next(step for step in second["steps"] if step["name"] == "vision")
    summary = vision_step["output"]["summary"]
    assert summary["backend"] == "xai"
    assert "fallback_from" not in summary
    keywords = conn.execute(
        "SELECT keywords_json FROM image_analyses WHERE gallery_id = ?",
        (gallery["id"],),
    ).fetchall()
    assert keywords and all(json.loads(row["keywords_json"]) == ["live-recovered"] for row in keywords)
    usage = conn.execute(
        "SELECT backend, units FROM ai_usage_events "
        "WHERE tenant_id = ? AND gallery_id = ? AND module = 'vision'",
        (tenant["id"], gallery["id"]),
    ).fetchall()
    assert [(row["backend"], row["units"]) for row in usage] == [("xai", 3)]


def test_resume_reuses_completed_vision(conn, storage, settings, db_path):
    tenant, gallery = _seed(conn, storage)
    run = start_run(conn, tenant=tenant, gallery_id=gallery["id"])
    execute_run(db_path, settings, run["id"], storage=storage, provider=MockVisionProvider())
    analyses_before = conn.execute("SELECT COUNT(*) AS n FROM image_analyses").fetchone()["n"]

    # Re-arm and re-run; vision step is already done → reused, not recomputed.
    start_run(conn, tenant=tenant, gallery_id=gallery["id"])
    assert load_run(conn, run["id"])["steps"][0]["status"] == "done"

    class ExplodingProvider:
        backend = "boom"

        def analyze(self, *, filename, data, style=""):  # must NOT be called on resume
            raise AssertionError("vision should not re-run when already done")

    result = execute_run(db_path, settings, run["id"], storage=storage,
                         provider=ExplodingProvider())
    assert result["status"] == "done"
    assert analyses_before == conn.execute(
        "SELECT COUNT(*) AS n FROM image_analyses").fetchone()["n"]
