"""Owner pipeline health — actionable gallery processing without raw internals."""

from conftest import login_owner, onboard_studio

from hestia.dashboard import build_owner_digest, needs_attention, pipeline_health
from hestia.galleries import create_gallery
from hestia.pipeline import start_run
from hestia.routes import web as web_routes
from hestia.tenants import create_tenant


def _insert_run(
    conn,
    *,
    tenant_id: str,
    source_id: str,
    status: str,
    created_at: str,
    updated_at: str,
    steps_json: str = "[]",
    offer_url: str | None = None,
    error: str | None = None,
) -> int:
    run_id = conn.execute(
        """
        INSERT INTO pipeline_runs (
            tenant_id, source, source_id, status, steps_json, offer_url, error,
            created_at, updated_at
        )
        VALUES (?, 'gallery', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tenant_id,
            source_id,
            status,
            steps_json,
            offer_url,
            error,
            created_at,
            updated_at,
        ),
    ).lastrowid
    assert run_id is not None
    return run_id


def test_pipeline_health_prioritizes_actionable_state_and_old_rearmed_run(conn):
    studio = create_tenant(conn, name="Pipeline Health Studio", shoot_type="wedding")
    other = create_tenant(conn, name="Other Pipeline Studio", shoot_type="portrait")

    rearmed_gallery = create_gallery(
        conn, tenant_id=studio["id"], title="Old Re-armed Gallery"
    )
    rearmed = start_run(conn, tenant=studio, gallery_id=rearmed_gallery["id"])
    conn.execute(
        "UPDATE pipeline_runs "
        "SET status = 'done', created_at = '2000-01-01 00:00:00', "
        "updated_at = '2000-01-01 00:00:00' WHERE id = ?",
        (rearmed["id"],),
    )

    rows: dict[str, int] = {}
    for title, status, updated_at in (
        ("Failed Gallery", "error", "2001-01-08 12:00:00"),
        ("Running Gallery", "running", "2001-01-07 12:00:00"),
        ("Waiting Gallery", "queued", "2001-01-06 12:00:00"),
        ("Done Newest", "done", "2099-01-05 12:00:00"),
        ("Done Second", "done", "2099-01-04 12:00:00"),
        ("Done Third", "done", "2099-01-03 12:00:00"),
        ("Done Fourth", "done", "2099-01-02 12:00:00"),
    ):
        gallery = create_gallery(conn, tenant_id=studio["id"], title=title)
        rows[title] = _insert_run(
            conn,
            tenant_id=studio["id"],
            source_id=str(gallery["id"]),
            status=status,
            created_at=updated_at,
            updated_at=updated_at,
        )

    foreign_gallery = create_gallery(
        conn, tenant_id=other["id"], title="Foreign Failure"
    )
    _insert_run(
        conn,
        tenant_id=other["id"],
        source_id=str(foreign_gallery["id"]),
        status="error",
        created_at="2100-01-01 00:00:00",
        updated_at="2100-01-01 00:00:00",
    )
    conn.commit()

    rearmed_again = start_run(conn, tenant=studio, gallery_id=rearmed_gallery["id"])
    assert rearmed_again["id"] == rearmed["id"]

    visible = pipeline_health(conn, studio["id"], limit=6)

    assert [item["id"] for item in visible] == [
        rows["Failed Gallery"],
        rows["Running Gallery"],
        rearmed["id"],
        rows["Waiting Gallery"],
        rows["Done Newest"],
        rows["Done Second"],
    ]
    assert [
        (item["status"], item["status_label"], item["status_class"])
        for item in visible
    ] == [
        ("error", "Needs review", "danger"),
        ("running", "Processing", "info"),
        ("queued", "Queued", "warn"),
        ("queued", "Queued", "warn"),
        ("done", "Complete", "on"),
        ("done", "Complete", "on"),
    ]
    assert [item["gallery_title"] for item in visible[:4]] == [
        "Failed Gallery",
        "Running Gallery",
        "Old Re-armed Gallery",
        "Waiting Gallery",
    ]
    assert rows["Done Third"] not in {item["id"] for item in visible}
    assert all(item["gallery_title"] != "Foreign Failure" for item in visible)


def test_pipeline_health_validates_relationships_and_returns_only_safe_fields(conn):
    studio = create_tenant(conn, name="Redacted Pipeline Studio", shoot_type="wedding")
    other = create_tenant(conn, name="Foreign Pipeline Studio", shoot_type="portrait")
    valid_gallery = create_gallery(
        conn, tenant_id=studio["id"], title="Visible Pipeline Gallery"
    )
    unknown_gallery = create_gallery(
        conn, tenant_id=studio["id"], title="Unknown State Gallery"
    )
    foreign_gallery = create_gallery(
        conn, tenant_id=other["id"], title="FOREIGN-GALLERY-TITLE-SECRET"
    )

    valid_id = _insert_run(
        conn,
        tenant_id=studio["id"],
        source_id=str(valid_gallery["id"]),
        status="error",
        created_at="2030-01-05 12:00:00",
        updated_at="2030-01-05 12:00:00",
        steps_json='[{"detail":"STEPS-JSON-SECRET"}]',
        offer_url="https://offer.invalid/OFFER-URL-SECRET",
        error="RAW-ERROR-SECRET",
    )
    foreign_id = _insert_run(
        conn,
        tenant_id=studio["id"],
        source_id=str(foreign_gallery["id"]),
        status="running",
        created_at="2030-01-04 12:00:00",
        updated_at="2030-01-04 12:00:00",
    )
    noncanonical_id = _insert_run(
        conn,
        tenant_id=studio["id"],
        source_id=f"0{valid_gallery['id']}",
        status="queued",
        created_at="2030-01-03 12:00:00",
        updated_at="2030-01-03 12:00:00",
    )
    missing_id = _insert_run(
        conn,
        tenant_id=studio["id"],
        source_id="SOURCE-ID-SECRET",
        status="done",
        created_at="2030-01-02 12:00:00",
        updated_at="MALFORMED-TIMESTAMP-SECRET",
    )
    unknown_id = _insert_run(
        conn,
        tenant_id=studio["id"],
        source_id=str(unknown_gallery["id"]),
        status="RAW-STATUS-SECRET",
        created_at="2030-01-01 12:00:00",
        updated_at="2030-01-01 12:00:00",
    )
    conn.commit()

    rows = pipeline_health(conn, studio["id"], limit=10)
    by_id = {item["id"]: item for item in rows}
    safe_keys = {
        "id",
        "gallery_id",
        "gallery_title",
        "status",
        "status_label",
        "status_class",
        "updated_at",
        "updated_label",
    }
    assert all(set(item) == safe_keys for item in rows)
    assert by_id[valid_id]["gallery_id"] == valid_gallery["id"]
    assert by_id[valid_id]["gallery_title"] == "Visible Pipeline Gallery"
    assert by_id[foreign_id]["gallery_id"] is None
    assert by_id[foreign_id]["gallery_title"] is None
    assert by_id[noncanonical_id]["gallery_id"] is None
    assert by_id[missing_id]["gallery_id"] is None
    assert by_id[missing_id]["updated_at"] is None
    assert by_id[missing_id]["updated_label"] is None
    assert by_id[unknown_id]["status"] == "unknown"
    assert by_id[unknown_id]["status_label"] == "Needs review"
    assert by_id[unknown_id]["status_class"] == "danger"

    serialized = repr(rows)
    for secret in (
        "STEPS-JSON-SECRET",
        "OFFER-URL-SECRET",
        "RAW-ERROR-SECRET",
        "FOREIGN-GALLERY-TITLE-SECRET",
        "SOURCE-ID-SECRET",
        "MALFORMED-TIMESTAMP-SECRET",
        "RAW-STATUS-SECRET",
    ):
        assert secret not in serialized


def test_processing_failures_join_attention_and_digest_without_raw_detail(
    conn, settings
):
    studio = create_tenant(
        conn, name="Processing Attention Studio", shoot_type="wedding"
    )
    failed_gallery = create_gallery(
        conn, tenant_id=studio["id"], title="Visible Digest Gallery"
    )
    queued_gallery = create_gallery(
        conn, tenant_id=studio["id"], title="Queued Digest Gallery"
    )
    failed_id = _insert_run(
        conn,
        tenant_id=studio["id"],
        source_id=str(failed_gallery["id"]),
        status="error",
        created_at="2030-03-03 10:11:12",
        updated_at="2030-03-03 10:11:12",
        steps_json='[{"detail":"DIGEST-STEPS-SECRET"}]',
        offer_url="https://offer.invalid/DIGEST-OFFER-SECRET",
        error="DIGEST-RAW-ERROR-SECRET",
    )
    unknown_id = _insert_run(
        conn,
        tenant_id=studio["id"],
        source_id="DIGEST-SOURCE-ID-SECRET",
        status="DIGEST-RAW-STATUS-SECRET",
        created_at="2030-03-02 10:11:12",
        updated_at="DIGEST-TIMESTAMP-SECRET",
    )
    _insert_run(
        conn,
        tenant_id=studio["id"],
        source_id=str(queued_gallery["id"]),
        status="queued",
        created_at="2030-03-01 10:11:12",
        updated_at="2030-03-01 10:11:12",
    )
    conn.commit()

    runs = pipeline_health(conn, studio["id"], limit=6)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        attention = needs_attention(conn, studio["id"], pipeline_runs=runs)
    finally:
        conn.set_trace_callback(None)

    assert [item["id"] for item in attention["pipeline_issues"]] == [
        failed_id,
        unknown_id,
    ]
    assert attention["total"] == 2
    assert not any(
        statement.lstrip().upper().startswith("SELECT")
        and "PIPELINE_RUNS" in statement.upper()
        for statement in statements
    )

    digest = build_owner_digest(conn, studio["id"], settings)
    assert digest is not None
    assert digest["subject"] == (
        "Processing Attention Studio: 2 things need your attention"
    )
    assert "Gallery processing issues (2)" in digest["body"]
    assert f"Run #{failed_id} — Visible Digest Gallery — Needs review" in digest["body"]
    assert f"Run #{unknown_id} — Gallery unavailable — Needs review" in digest["body"]
    for secret in (
        "DIGEST-STEPS-SECRET",
        "DIGEST-OFFER-SECRET",
        "DIGEST-RAW-ERROR-SECRET",
        "DIGEST-SOURCE-ID-SECRET",
        "DIGEST-RAW-STATUS-SECRET",
        "DIGEST-TIMESTAMP-SECRET",
    ):
        assert secret not in digest["body"]


def test_pipeline_health_is_one_bounded_indexed_select(conn):
    studio = create_tenant(conn, name="Query Plan Studio", shoot_type="wedding")
    for index in range(10):
        gallery = create_gallery(
            conn, tenant_id=studio["id"], title=f"Query Gallery {index}"
        )
        _insert_run(
            conn,
            tenant_id=studio["id"],
            source_id=str(gallery["id"]),
            status=("error", "running", "queued", "done")[index % 4],
            created_at=f"2030-01-{index + 1:02d} 12:00:00",
            updated_at=f"2030-01-{index + 1:02d} 12:00:00",
        )
    conn.commit()

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        rows = pipeline_health(conn, studio["id"], limit=6)
    finally:
        conn.set_trace_callback(None)

    assert len(rows) == 6
    reads = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
        and "PIPELINE_RUNS" in statement.upper()
    ]
    assert len(reads) == 1
    query = reads[0]
    normalized = query.upper()
    assert "LIMIT 6" in normalized
    assert "SELECT *" not in normalized
    assert "STEPS_JSON" not in normalized
    assert "OFFER_URL" not in normalized
    assert "R.ERROR" not in normalized

    plan = [
        row["detail"]
        for row in conn.execute("EXPLAIN QUERY PLAN " + query).fetchall()
    ]
    assert any(
        detail.startswith("SEARCH r USING INDEX ")
        and "tenant_id=?" in detail
        and "source=?" in detail
        for detail in plan
    )
    assert any(
        "SEARCH g USING INTEGER PRIMARY KEY" in detail and "LEFT-JOIN" in detail
        for detail in plan
    )
    assert not any(detail.startswith(("SCAN r", "SCAN g")) for detail in plan)


def test_dashboard_renders_redacted_gallery_processing_health(
    client, conn, monkeypatch
):
    credentials = onboard_studio(
        client,
        name="Processing Dashboard Studio",
        email="processing-dashboard@example.com",
    )
    login_owner(client, credentials)
    tenant_id = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    studio = dict(
        conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    )
    valid_gallery = create_gallery(
        conn, tenant_id=tenant_id, title="Visible Processing Gallery"
    )
    valid_run = start_run(conn, tenant=studio, gallery_id=valid_gallery["id"])
    conn.execute(
        "UPDATE pipeline_runs SET status = 'error', "
        "steps_json = '[{\"detail\":\"HTML-STEPS-SECRET\"}]', "
        "offer_url = 'https://offer.invalid/HTML-OFFER-SECRET', "
        "error = 'HTML-ERROR-SECRET', updated_at = '2030-04-03 10:11:12' "
        "WHERE id = ?",
        (valid_run["id"],),
    )

    other = create_tenant(conn, name="Foreign HTML Studio", shoot_type="portrait")
    foreign_gallery = create_gallery(
        conn, tenant_id=other["id"], title="HTML-FOREIGN-TITLE-SECRET"
    )
    foreign_run = _insert_run(
        conn,
        tenant_id=tenant_id,
        source_id=str(foreign_gallery["id"]),
        status="running",
        created_at="2030-04-02 10:11:12",
        updated_at="2030-04-02 10:11:12",
    )
    missing_run = _insert_run(
        conn,
        tenant_id=tenant_id,
        source_id="HTML-SOURCE-ID-SECRET",
        status="RAW-HTML-STATUS-SECRET",
        created_at="2030-04-01 10:11:12",
        updated_at="HTML-TIMESTAMP-SECRET",
    )
    conn.commit()

    calls = []
    real_pipeline_health = web_routes.pipeline_health

    def observed_pipeline_health(conn, observed_tenant_id, *, limit):
        calls.append((observed_tenant_id, limit))
        return real_pipeline_health(conn, observed_tenant_id, limit=limit)

    def obsolete_full_hydration(*_args, **_kwargs):
        raise AssertionError("dashboard must not hydrate raw pipeline runs")

    monkeypatch.setattr(web_routes, "pipeline_health", observed_pipeline_health)
    monkeypatch.setattr(
        web_routes, "list_runs", obsolete_full_hydration, raising=False
    )

    page = client.get("/dashboard")

    assert page.status_code == 200
    assert calls == [(tenant_id, 6)]
    assert "Gallery processing" in page.text
    assert "Recent pipeline runs" not in page.text
    assert "Visible Processing Gallery" in page.text
    assert f'href="/pipeline/{valid_run["id"]}"' in page.text
    assert f'href="/galleries/{valid_gallery["id"]}"' in page.text
    assert f'href="/pipeline/{foreign_run}"' in page.text
    assert f'href="/pipeline/{missing_run}"' in page.text
    assert page.text.count("Gallery unavailable") == 3
    assert "Gallery processing issues · 2" in page.text
    assert "all caught up" not in page.text
    assert "Needs review" in page.text
    assert "Processing" in page.text
    assert 'class="pill danger">Needs review</span>' in page.text
    assert 'class="pill info">Processing</span>' in page.text
    assert (
        '<time datetime="2030-04-03T10:11:12Z">'
        "2030-04-03 10:11 UTC</time>"
    ) in page.text

    for secret in (
        "HTML-STEPS-SECRET",
        "HTML-OFFER-SECRET",
        "HTML-ERROR-SECRET",
        "HTML-FOREIGN-TITLE-SECRET",
        "HTML-SOURCE-ID-SECRET",
        "RAW-HTML-STATUS-SECRET",
        "HTML-TIMESTAMP-SECRET",
    ):
        assert secret not in page.text
