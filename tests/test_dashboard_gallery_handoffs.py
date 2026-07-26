"""Owner gallery handoffs — proofing state surfaced without per-gallery hydration."""

from io import BytesIO

from conftest import login_owner, onboard_studio

from hestia.dashboard import build_owner_digest, needs_attention
from hestia.delivery import enable_delivery
from hestia.galleries import add_image, create_gallery, publish_gallery, submit_selections
from hestia.proofing import add_comment, set_favorite
from hestia.tenants import create_tenant


def _published_gallery(conn, storage, tenant_id: str, title: str) -> tuple[dict, dict]:
    gallery = create_gallery(conn, tenant_id=tenant_id, title=title)
    image = add_image(
        conn,
        storage,
        tenant_id=tenant_id,
        gallery_id=gallery["id"],
        filename=f"{title.lower().replace(' ', '-')}.jpg",
        fileobj=BytesIO(b"not-a-real-jpeg"),
        content_type="image/jpeg",
    )
    assert image is not None
    assert publish_gallery(conn, tenant_id, gallery["id"])
    return gallery, image


def test_gallery_handoffs_rank_states_count_valid_rows_and_stay_bounded(conn, storage):
    studio = create_tenant(conn, name="Handoff Studio", shoot_type="wedding")
    other = create_tenant(conn, name="Other Studio", shoot_type="portrait")

    submitted, submitted_image = _published_gallery(
        conn, storage, studio["id"], "Submitted Wedding"
    )
    assert set_favorite(
        conn,
        tenant_id=studio["id"],
        gallery_id=submitted["id"],
        image_id=submitted_image["id"],
        favorited=True,
    )
    assert add_comment(
        conn,
        tenant_id=studio["id"],
        gallery_id=submitted["id"],
        image_id=submitted_image["id"],
        body="Use this one",
    )
    assert submit_selections(
        conn, tenant_id=studio["id"], gallery_id=submitted["id"]
    )

    choosing, choosing_image = _published_gallery(
        conn, storage, studio["id"], "Choosing Portraits"
    )
    assert set_favorite(
        conn,
        tenant_id=studio["id"],
        gallery_id=choosing["id"],
        image_id=choosing_image["id"],
        favorited=True,
    )
    assert add_comment(
        conn,
        tenant_id=studio["id"],
        gallery_id=choosing["id"],
        image_id=choosing_image["id"],
        body="Maybe crop tighter",
    )

    awaiting, _ = _published_gallery(
        conn, storage, studio["id"], "Awaiting Family"
    )
    draft = create_gallery(conn, tenant_id=studio["id"], title="Draft Secret")
    delivered, _ = _published_gallery(
        conn, storage, studio["id"], "Already Delivered"
    )
    assert enable_delivery(conn, studio["id"], delivered["id"])
    foreign, foreign_image = _published_gallery(
        conn, storage, other["id"], "Foreign Gallery"
    )
    assert set_favorite(
        conn,
        tenant_id=other["id"],
        gallery_id=foreign["id"],
        image_id=foreign_image["id"],
        favorited=True,
    )

    malformed_image_id = conn.execute(
        "INSERT INTO images "
        "(gallery_id, tenant_id, filename, storage_key, access_token, content_type) "
        "VALUES (?, ?, 'foreign-secret.jpg', 'foreign-key', 'foreign-access', 'image/jpeg')",
        (submitted["id"], other["id"]),
    ).lastrowid
    conn.execute(
        "INSERT INTO image_favorites (tenant_id, gallery_id, image_id) VALUES (?, ?, ?)",
        (studio["id"], submitted["id"], malformed_image_id),
    )
    conn.execute(
        "INSERT INTO image_comments "
        "(tenant_id, gallery_id, image_id, author_name, body) VALUES (?, ?, ?, '', ?)",
        (studio["id"], submitted["id"], malformed_image_id, "foreign note"),
    )
    conn.commit()

    attention = needs_attention(conn, studio["id"])
    handoffs = attention["to_deliver"]
    assert [item["id"] for item in handoffs] == [
        submitted["id"],
        choosing["id"],
        awaiting["id"],
    ]
    assert [
        (
            item["proofing_status"],
            item["handoff_label"],
            item["favorite_count"],
            item["comment_count"],
        )
        for item in handoffs
    ] == [
        ("submitted", "Selections submitted", 1, 1),
        ("in_progress", "Client choosing", 1, 1),
        ("awaiting", "Awaiting selections", 0, 0),
    ]
    assert draft["id"] not in {item["id"] for item in handoffs}
    assert delivered["id"] not in {item["id"] for item in handoffs}
    assert foreign["id"] not in {item["id"] for item in handoffs}

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        limited = needs_attention(conn, studio["id"], limit=2)["to_deliver"]
    finally:
        conn.set_trace_callback(None)
    assert [item["id"] for item in limited] == [submitted["id"], choosing["id"]]
    proofing_reads = [
        statement
        for statement in statements
        if "image_favorites" in statement or "image_comments" in statement
    ]
    assert len(proofing_reads) == 1
    assert "LIMIT 2" in proofing_reads[0]
    plan = [
        row["detail"]
        for row in conn.execute("EXPLAIN QUERY PLAN " + proofing_reads[0]).fetchall()
    ]
    assert any(
        "SEARCH f USING INDEX idx_image_favorites_gallery" in detail
        and "gallery_id=?" in detail
        for detail in plan
    )
    assert any(
        "SEARCH c USING INDEX idx_image_comments_gallery" in detail
        and "gallery_id=?" in detail
        for detail in plan
    )
    assert not any(detail.startswith(("SCAN f", "SCAN c")) for detail in plan)


def test_gallery_handoffs_keep_oldest_submissions_inside_limit(conn, storage):
    studio = create_tenant(conn, name="Waiting Studio", shoot_type="wedding")
    submitted = []
    submitted_days = (5, 1, 9, 3, 7, 2, 8, 4, 6)
    for index, day in enumerate(submitted_days, start=1):
        gallery, _ = _published_gallery(
            conn, storage, studio["id"], f"Submitted {index}"
        )
        assert submit_selections(
            conn, tenant_id=studio["id"], gallery_id=gallery["id"]
        )
        conn.execute(
            "UPDATE galleries SET selections_submitted_at = ? "
            "WHERE id = ? AND tenant_id = ?",
            (f"2030-01-{day:02d} 12:00:00", gallery["id"], studio["id"]),
        )
        submitted.append((day, gallery["id"]))
    conn.commit()

    oldest_first = [gallery_id for _, gallery_id in sorted(submitted)]
    visible = needs_attention(conn, studio["id"], limit=8)["to_deliver"]
    assert [item["id"] for item in visible] == oldest_first[:8]
    assert oldest_first[-1] not in {item["id"] for item in visible}


def test_gallery_handoff_reopens_then_leaves_after_delivery(conn, storage):
    studio = create_tenant(conn, name="Lifecycle Studio", shoot_type="wedding")
    gallery, image = _published_gallery(conn, storage, studio["id"], "Lifecycle")
    assert set_favorite(
        conn,
        tenant_id=studio["id"],
        gallery_id=gallery["id"],
        image_id=image["id"],
        favorited=True,
    )
    assert submit_selections(conn, tenant_id=studio["id"], gallery_id=gallery["id"])
    conn.commit()

    first = needs_attention(conn, studio["id"])["to_deliver"][0]
    assert first["proofing_status"] == "submitted"

    assert add_comment(
        conn,
        tenant_id=studio["id"],
        gallery_id=gallery["id"],
        image_id=image["id"],
        body="One more adjustment",
    )
    reopened = needs_attention(conn, studio["id"])["to_deliver"][0]
    assert reopened["proofing_status"] == "in_progress"
    assert reopened["favorite_count"] == 1
    assert reopened["comment_count"] == 1

    assert enable_delivery(conn, studio["id"], gallery["id"])
    assert needs_attention(conn, studio["id"])["to_deliver"] == []


def test_dashboard_and_digest_render_truthful_gallery_handoffs(
    client, conn, storage, settings
):
    creds = onboard_studio(
        client, name="Review Studio", email="review-handoffs@example.com"
    )
    login_owner(client, creds)
    tenant_id = conn.execute("SELECT id FROM tenants").fetchone()["id"]

    submitted, submitted_image = _published_gallery(
        conn, storage, tenant_id, "Submitted Gallery"
    )
    assert set_favorite(
        conn,
        tenant_id=tenant_id,
        gallery_id=submitted["id"],
        image_id=submitted_image["id"],
        favorited=True,
    )
    assert add_comment(
        conn,
        tenant_id=tenant_id,
        gallery_id=submitted["id"],
        image_id=submitted_image["id"],
        body="Album opener",
    )
    assert submit_selections(
        conn, tenant_id=tenant_id, gallery_id=submitted["id"]
    )

    choosing, choosing_image = _published_gallery(
        conn, storage, tenant_id, "Choosing Gallery"
    )
    assert add_comment(
        conn,
        tenant_id=tenant_id,
        gallery_id=choosing["id"],
        image_id=choosing_image["id"],
        body="Client is still deciding",
    )
    awaiting, _ = _published_gallery(
        conn, storage, tenant_id, "Awaiting Gallery"
    )
    conn.commit()

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Gallery handoffs · 3" in page.text
    assert "Selections submitted" in page.text
    assert "Client choosing" in page.text
    assert "Awaiting selections" in page.text
    assert "enable download" not in page.text
    review_href = f'href="/galleries/{submitted["id"]}#proofing-activity"'
    assert page.text.count(review_href) == 2
    assert ">Review proofing</a>" in page.text
    assert ">Review selections</a>" not in page.text
    assert "1 favorite" in page.text and "1 note" in page.text
    assert "Album opener" not in page.text
    assert "submitted-gallery.jpg" not in page.text
    assert f'href="/galleries/{awaiting["id"]}"' in page.text

    detail = client.get(f"/galleries/{submitted['id']}")
    assert detail.status_code == 200
    assert "#proofing-activity:target" in detail.text
    assert "scroll-margin-top: 72px" in detail.text
    assert 'id="proofing-activity"' in detail.text
    assert 'aria-labelledby="proofing-activity-title"' in detail.text
    assert 'id="proofing-activity-title"' in detail.text

    digest = build_owner_digest(conn, tenant_id, settings)
    assert digest is not None
    assert "Gallery handoffs (3)" in digest["body"]
    assert "Submitted Gallery — Selections submitted; 1 favorite, 1 note" in digest["body"]
    assert "Choosing Gallery — Client choosing; 0 favorites, 1 note" in digest["body"]
    assert "Awaiting Gallery — Awaiting selections; 0 favorites, 0 notes" in digest["body"]
