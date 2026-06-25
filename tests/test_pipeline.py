"""Pipeline state machine: gallery → vision → offer, persisted and idempotent."""

import io

from hestia.galleries import add_image, create_gallery
from hestia.pipeline import execute_run, load_run, start_run
from hestia.tenants import create_tenant
from hestia.vision import MockVisionProvider, VisionError


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

        def analyze(self, *, filename, data):
            raise VisionError("vision provider down")

    tenant, gallery = _seed(conn, storage)
    run = start_run(conn, tenant=tenant, gallery_id=gallery["id"])
    result = execute_run(db_path, settings, run["id"], storage=storage, provider=FailingProvider())
    assert result["status"] == "error"
    assert "vision" in (result["error"] or "")
    statuses = {s["name"]: s["status"] for s in result["steps"]}
    assert statuses["vision"] == "error"
    assert statuses["offer"] == "pending"
    assert conn.execute("SELECT COUNT(*) AS n FROM offers").fetchone()["n"] == 0


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

        def analyze(self, *, filename, data):  # must NOT be called on resume
            raise AssertionError("vision should not re-run when already done")

    result = execute_run(db_path, settings, run["id"], storage=storage,
                         provider=ExplodingProvider())
    assert result["status"] == "done"
    assert analyses_before == conn.execute(
        "SELECT COUNT(*) AS n FROM image_analyses").fetchone()["n"]
