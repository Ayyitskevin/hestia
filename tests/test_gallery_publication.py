"""Gallery publication is a one-way, claim-before-act transition."""

from conftest import login_owner, onboard_studio

from hestia.automations import create_automation
from hestia.db import list_audit
from hestia.galleries import create_gallery, get_gallery, publish_gallery
from hestia.tenants import create_tenant


def test_publish_gallery_claims_draft_once_and_is_tenant_scoped(conn):
    studio = create_tenant(conn, name="Publish Studio", shoot_type="other")
    other = create_tenant(conn, name="Other Studio", shoot_type="other")
    gallery = create_gallery(conn, tenant_id=studio["id"], title="Wedding")
    create_automation(
        conn,
        tenant_id=studio["id"],
        name="Delivery follow-up",
        trigger="gallery.published",
        subject="Your gallery is ready",
        body="Take a look",
    )

    assert publish_gallery(conn, other["id"], gallery["id"]) is False
    assert get_gallery(conn, studio["id"], gallery["id"])["status"] == "draft"

    assert publish_gallery(conn, studio["id"], gallery["id"]) is True
    first = get_gallery(conn, studio["id"], gallery["id"])
    assert first["status"] == "published" and first["published_at"]

    # A retry must not move the publication date or enqueue another automation.
    conn.execute(
        "UPDATE galleries SET published_at = '2030-01-02 03:04:05' WHERE id = ?",
        (gallery["id"],),
    )
    assert publish_gallery(conn, studio["id"], gallery["id"]) is False
    assert get_gallery(conn, studio["id"], gallery["id"])["published_at"] == "2030-01-02 03:04:05"
    jobs = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE tenant_id = ? AND kind = 'automation.run'",
        (studio["id"],),
    ).fetchone()
    assert jobs["n"] == 1


def test_repeated_publish_post_audits_only_the_winning_transition(client, conn):
    creds = onboard_studio(client, email="publish-once@example.com")
    login_owner(client, creds)
    created = client.post("/galleries", data={"title": "Seaside Wedding"})
    gallery_id = int(created.url.path.rstrip("/").split("/")[-1])
    tenant_id = conn.execute(
        "SELECT tenant_id FROM galleries WHERE id = ?", (gallery_id,)
    ).fetchone()["tenant_id"]
    create_automation(
        conn,
        tenant_id=tenant_id,
        name="Gallery follow-up",
        trigger="gallery.published",
        subject="Ready",
        body="Your gallery is ready",
    )
    conn.commit()

    assert client.post(
        f"/galleries/{gallery_id}/publish", follow_redirects=False
    ).status_code == 303
    conn.execute(
        "UPDATE galleries SET published_at = '2031-02-03 04:05:06' WHERE id = ?",
        (gallery_id,),
    )
    conn.commit()
    assert client.post(
        f"/galleries/{gallery_id}/publish", follow_redirects=False
    ).status_code == 303

    fresh = get_gallery(conn, tenant_id, gallery_id)
    assert fresh["published_at"] == "2031-02-03 04:05:06"
    published = [
        row for row in list_audit(conn, tenant_id) if row["action"] == "gallery.published"
    ]
    assert len(published) == 1 and published[0]["detail"] == "Seaside Wedding"
    jobs = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE tenant_id = ? AND kind = 'automation.run'",
        (tenant_id,),
    ).fetchone()
    assert jobs["n"] == 1
