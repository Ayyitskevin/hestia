"""Gallery publication is a one-way, claim-before-act transition."""

import dataclasses

from conftest import CSRFClient, login_owner, onboard_studio

from hestia.automations import create_automation, set_automation_enabled
from hestia.crm import assign_gallery_to_project, create_client, create_project
from hestia.db import list_audit
from hestia.email import list_emails
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
    client.post(
        f"/galleries/{gallery_id}/images",
        files=[("files", ("proof.jpg", b"image-bytes", "image/jpeg"))],
    )

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


def test_publish_surfaces_configured_proofing_handoff_without_sending(client, conn, app):
    creds = onboard_studio(client, email="proof-handoff@example.com")
    login_owner(client, creds)
    other_browser = CSRFClient(app)
    other_creds = onboard_studio(
        other_browser,
        name="Other Studio",
        email="other-proof-owner@example.com",
    )
    login_owner(other_browser, other_creds)
    app.state.settings = dataclasses.replace(
        app.state.settings,
        public_url="https://app.hestia.example/base/",
    )
    created = client.post(
        "/galleries",
        data={"title": "Seaside Proofs", "pin": "2468"},
        follow_redirects=False,
    )
    gallery_id = int(created.headers["location"].rstrip("/").split("/")[-1])
    gallery = conn.execute(
        "SELECT * FROM galleries WHERE id = ?",
        (gallery_id,),
    ).fetchone()
    tenant = conn.execute(
        "SELECT * FROM tenants WHERE id = ?",
        (gallery["tenant_id"],),
    ).fetchone()

    draft = client.get(f"/galleries/{gallery_id}")
    expected = f"https://app.hestia.example/base/g/{tenant['slug']}/{gallery['slug']}"
    assert draft.status_code == 200
    assert 'id="client-proofing"' not in draft.text
    assert 'id="proofing-preflight"' in draft.text
    assert "Review &amp; publish" in draft.text
    assert "queues existing" in draft.text
    assert "Gallery published" in draft.text
    assert f"gallery_id={gallery_id}" in draft.text
    assert "0 visible photos" in draft.text
    assert "Add at least one visible photo before publishing" in draft.text
    assert "Keep the PIN you set" in draft.text
    assert expected not in draft.text

    foreign_setup = other_browser.get(
        f"/automations/new?trigger=gallery.published&gallery_id={gallery_id}"
    )
    assert foreign_setup.status_code == 200
    assert f'name="gallery_id" value="{gallery_id}"' not in foreign_setup.text
    assert 'href="/automations">Cancel</a>' in foreign_setup.text

    blocked = client.post(
        f"/galleries/{gallery_id}/publish",
        follow_redirects=False,
    )
    assert blocked.status_code == 303
    assert blocked.headers["location"] == (
        f"/galleries/{gallery_id}?publish_blocked=1#proofing-preflight"
    )
    assert get_gallery(conn, tenant["id"], gallery_id)["status"] == "draft"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE tenant_id = ?",
        (tenant["id"],),
    ).fetchone()["n"] == 0

    client.post(
        f"/galleries/{gallery_id}/images",
        files=[("files", ("proof.jpg", b"image-bytes", "image/jpeg"))],
    )
    ready = client.get(f"/galleries/{gallery_id}")
    assert "1 visible photo" in ready.text
    assert "Add at least one visible photo before publishing" not in ready.text

    sent_before = list_emails(conn, tenant["id"])
    published = client.post(
        f"/galleries/{gallery_id}/publish",
        follow_redirects=False,
    )
    assert published.status_code == 303
    assert published.headers["location"] == f"/galleries/{gallery_id}#client-proofing"

    detail = client.get(f"/galleries/{gallery_id}", headers={"host": "attacker.invalid"})
    assert detail.status_code == 200
    assert 'id="client-proofing"' in detail.text
    assert expected in detail.text
    assert "PIN prompt enabled" in detail.text
    assert "Copy link" in detail.text
    assert "opens in new tab" in detail.text
    assert "Visible originals remain openable one at a time" in detail.text
    assert "Queued is not delivered" in detail.text
    assert "applies only to future galleries" in detail.text
    assert list_emails(conn, tenant["id"]) == sent_before

    foreign = other_browser.get(f"/galleries/{gallery_id}", follow_redirects=False)
    assert foreign.status_code == 303
    assert foreign.headers["location"] == "/galleries"

    repeated = client.post(
        f"/galleries/{gallery_id}/publish",
        follow_redirects=False,
    )
    assert repeated.status_code == 303
    assert repeated.headers["location"] == f"/galleries/{gallery_id}"
    assert expected in client.get(f"/galleries/{gallery_id}").text


def test_published_handoff_warns_when_gallery_has_no_pin(client, conn, app):
    app.state.settings = dataclasses.replace(
        app.state.settings,
        automation_email_enabled=False,
    )
    creds = onboard_studio(client, email="proof-no-pin@example.com")
    login_owner(client, creds)
    created = client.post(
        "/galleries",
        data={"title": "Open Proofs"},
        follow_redirects=False,
    )
    gallery_id = int(created.headers["location"].rstrip("/").split("/")[-1])
    client.post(
        f"/galleries/{gallery_id}/images",
        files=[("files", ("proof.jpg", b"image-bytes", "image/jpeg"))],
    )

    assert client.post(
        f"/galleries/{gallery_id}/publish", follow_redirects=False
    ).status_code == 303
    page = client.get(f"/galleries/{gallery_id}")
    assert '<span class="pill off">No PIN</span>' in page.text
    assert "Anyone with this link can open the proofing gallery" in page.text
    assert "Automation delivery paused" in page.text
    assert "Any still-pending job remains queued" in page.text
    assert "Recent runs" in page.text


def test_publish_blocks_gallery_when_every_photo_is_hidden(client, conn):
    creds = onboard_studio(client, email="hidden-proof@example.com")
    login_owner(client, creds)
    created = client.post(
        "/galleries",
        data={"title": "Hidden Proofs"},
        follow_redirects=False,
    )
    gallery_id = int(created.headers["location"].rstrip("/").split("/")[-1])
    tenant_id = conn.execute(
        "SELECT tenant_id FROM galleries WHERE id = ?",
        (gallery_id,),
    ).fetchone()["tenant_id"]
    client.post(
        f"/galleries/{gallery_id}/images",
        files=[("files", ("hidden.jpg", b"image-bytes", "image/jpeg"))],
    )
    image_id = conn.execute(
        "SELECT id FROM images WHERE gallery_id = ?",
        (gallery_id,),
    ).fetchone()["id"]
    client.post(f"/galleries/{gallery_id}/images/{image_id}/hide")

    page = client.get(f"/galleries/{gallery_id}")
    assert "0 visible photos" in page.text
    blocked = client.post(f"/galleries/{gallery_id}/publish", follow_redirects=False)
    assert blocked.headers["location"] == (
        f"/galleries/{gallery_id}?publish_blocked=1#proofing-preflight"
    )
    assert get_gallery(conn, tenant_id, gallery_id)["status"] == "draft"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE tenant_id = ?",
        (tenant_id,),
    ).fetchone()["n"] == 0


def test_publish_preflight_counts_only_enabled_link_rules_and_prefills_setup(client, conn):
    creds = onboard_studio(client, email="proof-rules@example.com")
    login_owner(client, creds)
    created = client.post(
        "/galleries",
        data={"title": "Rule Proofs"},
        follow_redirects=False,
    )
    gallery_id = int(created.headers["location"].rstrip("/").split("/")[-1])
    tenant_id = conn.execute(
        "SELECT tenant_id FROM galleries WHERE id = ?",
        (gallery_id,),
    ).fetchone()["tenant_id"]
    client.post(
        f"/galleries/{gallery_id}/images",
        files=[("files", ("proof.jpg", b"image-bytes", "image/jpeg"))],
    )

    create_automation(
        conn,
        tenant_id=tenant_id,
        name="Proof now",
        trigger="gallery.published",
        subject="Your proofs",
        body="Review now: {gallery_url}",
    )
    create_automation(
        conn,
        tenant_id=tenant_id,
        name="Proof later",
        trigger="gallery.published",
        subject="Your proofs",
        body="Review later: {gallery_url}",
        delay_days=2,
    )
    create_automation(
        conn,
        tenant_id=tenant_id,
        name="Anniversary only",
        trigger="gallery.published",
        subject="A year already",
        body="Ready for another session?",
    )
    create_automation(
        conn,
        tenant_id=tenant_id,
        name="Contract only",
        trigger="contract.signed",
        subject="Welcome",
        body="Thanks for signing.",
    )
    disabled = create_automation(
        conn,
        tenant_id=tenant_id,
        name="Disabled proof",
        trigger="gallery.published",
        subject="Your proofs",
        body="Review: {gallery_url}",
    )
    set_automation_enabled(conn, tenant_id, disabled["id"], False)
    conn.commit()

    page = client.get(f"/galleries/{gallery_id}")
    assert "2 enabled proof-link rules" in page.text
    assert "Proof now" in page.text and "queues immediately" in page.text
    assert "Proof later" in page.text and "becomes runnable after 2 days" in page.text
    assert "Anniversary only" not in page.text
    assert "Contract only" not in page.text
    assert "Disabled proof" not in page.text
    assert "No project is linked" in page.text

    recipient = create_client(
        conn,
        tenant_id=tenant_id,
        name="Ready Client",
        email="ready@example.com",
    )
    project = create_project(
        conn,
        tenant_id=tenant_id,
        name="Ready Project",
        client_id=recipient["id"],
    )
    assign_gallery_to_project(conn, tenant_id, gallery_id, project["id"])
    conn.commit()
    ready = client.get(f"/galleries/{gallery_id}")
    assert "Ready Client" in ready.text
    assert "Client email is ready" in ready.text
    assert "is linked now" in ready.text
    assert "re-checks the gallery's current" in ready.text

    setup = client.get(
        f"/automations/new?trigger=gallery.published&gallery_id={gallery_id}"
    )
    assert 'option value="gallery.published" selected' in setup.text
    assert f'name="gallery_id" value="{gallery_id}"' in setup.text
    assert 'value="Send gallery proofing link"' in setup.text

    created_rule = client.post(
        "/automations",
        data={
            "name": "Proof from setup",
            "trigger": "gallery.published",
            "delay_days": "0",
            "action": "email_client",
            "subject": "Your gallery is ready",
            "body": "Review: {gallery_url}",
            "gallery_id": str(gallery_id),
        },
        follow_redirects=False,
    )
    assert created_rule.status_code == 303
    assert created_rule.headers["location"] == f"/galleries/{gallery_id}#proofing-preflight"
    assert "3 enabled proof-link rules" in client.get(f"/galleries/{gallery_id}").text
