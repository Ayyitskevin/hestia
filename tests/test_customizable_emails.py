"""The remaining transactional emails — gallery-ready, print-sale, payment-schedule,
review-request — now render from the message-template system instead of hardcoded
strings, so a studio can customize them. Each test renders with the EXACT context its
route passes, so a missing variable would show up as a leftover {token}.
"""

from conftest import login_owner, onboard_studio

import hestia.messaging as messaging
from hestia.email import list_emails
from hestia.tenants import create_tenant

# kind -> the context its route actually passes (beyond client/studio)
ROUTE_CONTEXT = {
    "gallery_ready": {"download_url": "https://x/download/abc"},
    "print_offer": {"discount": 15, "headline": "Holiday sale", "offer_url": "https://x/s/abc"},
    "payment_schedule": {"title": "Wedding", "total": "$2,000.00",
                         "schedule": "- Deposit: $500.00\n  https://x/pay/d"},
    "review_request": {"review_url": "https://x/t/abc"},
}


def test_each_kind_renders_with_no_leftover_placeholder(conn):
    t = create_tenant(conn, name="Render Studio", shoot_type="wedding")
    conn.commit()
    for kind, extra in ROUTE_CONTEXT.items():
        assert kind in messaging.TEMPLATES
        ctx = {"client": "Sam Rivers", "studio": "Render Studio", **extra}
        out = messaging.render(conn, t["id"], kind, ctx)
        # every placeholder in the default template is supplied → nothing left unfilled
        assert "{" not in out["subject"] and "{" not in out["body"], kind
        assert "Sam Rivers" in out["body"]                       # client filled in


def test_all_kinds_appear_in_editor(client):
    creds = onboard_studio(client, email="cz@example.com")
    login_owner(client, creds)
    page = client.get("/settings/messages").text
    for label in ["Gallery ready to download", "Print sale", "Payment schedule", "Review request"]:
        assert label in page


def test_review_request_uses_custom_template(client, conn):
    creds = onboard_studio(client, name="Lumen Studio", email="rr@example.com")
    login_owner(client, creds)
    r = client.post("/clients", data={"name": "Pat", "email": "pat@example.com"})
    cid = r.url.path.rstrip("/").split("/")[-1]
    tid = conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]

    # default request → built-in subject
    client.post("/settings/testimonials/request", data={"client_id": cid})
    newest = [m for m in list_emails(conn, tid) if m["to_addr"] == "pat@example.com"][0]
    assert "how was your experience" in newest["subject"]

    # customize the template (commit before the HTTP call so the app's connection sees it)
    messaging.set_template(conn, tid, "review_request",
                           subject="Mind leaving {studio} a review?",
                           body="Hi {client}, please review us: {review_url}")
    conn.commit()
    client.post("/settings/testimonials/request", data={"client_id": cid})
    newest = [m for m in list_emails(conn, tid) if m["to_addr"] == "pat@example.com"][0]
    assert newest["subject"] == "Mind leaving Lumen Studio a review?"
    assert "please review us:" in newest["body"]
