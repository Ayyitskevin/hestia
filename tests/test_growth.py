"""Growth flywheel — review + referral asks for happy clients."""

from conftest import login_owner, onboard_studio

from hestia.crm import assign_gallery_to_project, create_client, create_project
from hestia.db import connect
from hestia.email import list_emails
from hestia.galleries import create_gallery, publish_gallery
from hestia.growth import growth_opportunities, send_growth_ask
from hestia.invoices import create_invoice, mark_paid
from hestia.tenants import create_tenant
from hestia.testimonials import pending_testimonial, request_testimonial, submit_testimonial


def _tenant(conn, name="Growth Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def _paid_client(conn, settings, tenant_id, *, name="Happy Client", email="happy@example.com"):
    client = create_client(conn, tenant_id=tenant_id, name=name, email=email)
    invoice = create_invoice(
        conn,
        settings,
        tenant_id=tenant_id,
        title="Gallery order",
        amount_cents=120000,
        client_id=client["id"],
    )
    assert mark_paid(conn, token=invoice["token"], provider="mock", ref="paid") is True
    return client


def test_growth_opportunities_use_paid_and_gallery_signals(conn, settings):
    tenant = _tenant(conn)
    paid = _paid_client(conn, settings, tenant["id"])
    gallery_client = create_client(
        conn,
        tenant_id=tenant["id"],
        name="Gallery Client",
        email="gallery@example.com",
    )
    project = create_project(
        conn,
        tenant_id=tenant["id"],
        name="Portraits",
        client_id=gallery_client["id"],
        status="booked",
    )
    gallery = create_gallery(conn, tenant_id=tenant["id"], title="Portrait delivery")
    assign_gallery_to_project(conn, tenant["id"], gallery["id"], project["id"])
    publish_gallery(conn, tenant["id"], gallery["id"])

    rows = growth_opportunities(conn, tenant["id"])

    by_id = {row["id"]: row for row in rows}
    assert paid["id"] in by_id
    assert gallery_client["id"] in by_id
    assert by_id[paid["id"]]["paid_invoice_count"] == 1
    assert by_id[paid["id"]]["paid_display"] == "$1,200.00"
    assert by_id[gallery_client["id"]]["gallery_count"] == 1
    assert by_id[paid["id"]]["can_send"] is True


def test_growth_opportunities_are_tenant_scoped(conn, settings):
    a = _tenant(conn, "A")
    b = _tenant(conn, "B")
    foreign = _paid_client(conn, settings, b["id"], name="Foreign", email="foreign@example.com")

    rows = growth_opportunities(conn, a["id"])

    assert foreign["id"] not in {row["id"] for row in rows}


def test_send_growth_ask_creates_review_link_referral_link_and_cooldown(conn, settings):
    tenant = _tenant(conn)
    client = _paid_client(conn, settings, tenant["id"])

    first = send_growth_ask(conn, settings, tenant_id=tenant["id"], client_id=client["id"])
    second = send_growth_ask(conn, settings, tenant_id=tenant["id"], client_id=client["id"])

    assert first["sent"] is True
    assert first["review_url"].startswith("http://testserver/t/")
    assert "/studio/growth-studio?ref=" in first["referral_url"]
    assert second["sent"] is False
    assert second["reason"] == "cooldown"
    emails = list_emails(conn, tenant["id"], to_addr=client["email"])
    assert len(emails) == 1
    assert first["review_url"] in emails[0]["body"]
    assert first["referral_url"] in emails[0]["body"]
    assert pending_testimonial(conn, tenant["id"], client["id"]) is not None


def test_send_growth_ask_reuses_existing_pending_review_link(conn, settings):
    tenant = _tenant(conn)
    client = _paid_client(conn, settings, tenant["id"])
    pending = request_testimonial(
        conn,
        tenant_id=tenant["id"],
        client_id=client["id"],
        author_name=client["name"],
    )

    result = send_growth_ask(conn, settings, tenant_id=tenant["id"], client_id=client["id"])

    assert result["sent"] is True
    assert pending["token"] in result["review_url"]
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM testimonials WHERE tenant_id = ? AND client_id = ?",
        (tenant["id"], client["id"]),
    ).fetchone()
    assert rows["n"] == 1


def test_send_growth_ask_skips_new_review_when_review_already_collected(conn, settings):
    tenant = _tenant(conn)
    client = _paid_client(conn, settings, tenant["id"])
    review = request_testimonial(
        conn,
        tenant_id=tenant["id"],
        client_id=client["id"],
        author_name=client["name"],
    )
    submit_testimonial(conn, review["token"], rating=5, body="Loved it")

    result = send_growth_ask(conn, settings, tenant_id=tenant["id"], client_id=client["id"])

    assert result["sent"] is True
    assert result["review_url"] == ""
    emails = list_emails(conn, tenant["id"], to_addr=client["email"])
    assert "Your review already helps" in emails[0]["body"]
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM testimonials WHERE tenant_id = ? AND client_id = ?",
        (tenant["id"], client["id"]),
    ).fetchone()
    assert rows["n"] == 1


def test_testimonial_hub_lists_growth_queue_and_sends(client, app):
    creds = onboard_studio(client, name="Flywheel Studio", email="fly@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tenant = conn.execute("SELECT * FROM tenants WHERE slug = 'flywheel-studio'").fetchone()
        cid = _paid_client(
            conn,
            app.state.settings,
            tenant["id"],
            name="Ari",
            email="ari@example.com",
        )["id"]
        conn.commit()
    finally:
        conn.close()

    page = client.get("/settings/testimonials")
    assert "Review + referral flywheel" in page.text
    assert "Ari" in page.text
    assert f"/settings/testimonials/growth/{cid}" in page.text

    response = client.post(
        f"/settings/testimonials/growth/{cid}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/testimonials?growth=sent"

    conn = connect(app.state.settings.db_path)
    try:
        emails = list_emails(conn, tenant["id"], to_addr="ari@example.com")
    finally:
        conn.close()
    assert emails and "/studio/flywheel-studio?ref=" in emails[0]["body"]
