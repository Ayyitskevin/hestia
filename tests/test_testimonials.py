"""Testimonials — request → public submit → feature on the studio site."""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client
from hestia.db import connect
from hestia.email import list_emails
from hestia.tenants import create_tenant, slugify
from hestia.testimonials import (
    featured_testimonials,
    get_by_token,
    list_testimonials,
    request_testimonial,
    set_status,
    submit_testimonial,
)


def _tenant(conn, name="Review Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


# --- module logic -----------------------------------------------------------

def test_request_creates_pending_with_token(conn):
    t = _tenant(conn)
    tt = request_testimonial(conn, tenant_id=t["id"], author_name="Sarah")
    assert tt["status"] == "requested" and tt["token"]
    assert tt["author_name"] == "Sarah"
    assert get_by_token(conn, tt["token"])["id"] == tt["id"]


def test_request_drops_foreign_client_id(conn):
    a = _tenant(conn, "A")
    b = _tenant(conn, "B")
    foreign = create_client(conn, tenant_id=a["id"], name="Foreign")
    tt = request_testimonial(conn, tenant_id=b["id"], client_id=foreign["id"])
    assert tt["client_id"] is None


def test_submit_records_and_is_idempotent(conn):
    t = _tenant(conn)
    tt = request_testimonial(conn, tenant_id=t["id"])
    assert submit_testimonial(conn, tt["token"], rating=5, body="Wonderful!") is True
    row = get_by_token(conn, tt["token"])
    assert row["status"] == "submitted" and row["body"] == "Wonderful!" and row["submitted_at"]
    # a second submit on the same link is a no-op — it can't overwrite the review
    assert submit_testimonial(conn, tt["token"], rating=1, body="HACKED") is False
    assert get_by_token(conn, tt["token"])["body"] == "Wonderful!"


def test_submit_clamps_rating_and_keeps_prefilled_name(conn):
    t = _tenant(conn)
    tt = request_testimonial(conn, tenant_id=t["id"], author_name="Pre Filled")
    submit_testimonial(conn, tt["token"], rating=99, body="x", author_name="")
    row = get_by_token(conn, tt["token"])
    assert row["rating"] == 5                  # clamped to 1..5
    assert row["author_name"] == "Pre Filled"  # blank submit keeps the prefilled name


def test_set_status_only_moves_real_reviews(conn):
    t = _tenant(conn)
    tt = request_testimonial(conn, tenant_id=t["id"])
    assert set_status(conn, t["id"], tt["id"], "featured") is False  # not yet submitted
    submit_testimonial(conn, tt["token"], rating=5, body="Great")
    assert set_status(conn, t["id"], tt["id"], "featured") is True
    assert set_status(conn, t["id"], tt["id"], "bogus") is False     # invalid status
    other = _tenant(conn, "Other Studio")
    assert set_status(conn, other["id"], tt["id"], "hidden") is False  # tenant-scoped


def test_featured_only_returns_featured(conn):
    t = _tenant(conn)
    a = request_testimonial(conn, tenant_id=t["id"], author_name="A")
    b = request_testimonial(conn, tenant_id=t["id"], author_name="B")
    submit_testimonial(conn, a["token"], rating=5, body="aaa")
    submit_testimonial(conn, b["token"], rating=4, body="bbb")
    set_status(conn, t["id"], a["id"], "featured")
    assert [x["author_name"] for x in featured_testimonials(conn, t["id"])] == ["A"]
    assert len(list_testimonials(conn, t["id"])) == 2


# --- HTTP flow --------------------------------------------------------------

def _published_studio(client, *, name, email):
    creds = onboard_studio(client, name=name, email=email)
    login_owner(client, creds)
    client.post("/settings/site", data={"headline": "x", "about": "y",
                                         "contact_email": "", "published": "1"})
    return slugify(name)


def _first_testimonial(app):
    conn = connect(app.state.settings.db_path)
    try:
        row = conn.execute("SELECT id, token FROM testimonials ORDER BY id LIMIT 1").fetchone()
        return (row["id"], row["token"]) if row else (None, None)
    finally:
        conn.close()


def test_request_and_public_submit_flow(client, app):
    _published_studio(client, name="Flow Studio", email="flow@example.com")
    client.post("/settings/testimonials/request", data={"client_id": "", "author_name": "Guest"})
    hub = client.get("/settings/testimonials")
    assert "Guest" in hub.text and "/t/" in hub.text  # the share link is shown

    _id, token = _first_testimonial(app)
    visitor = client.__class__(client.app)  # fresh, anonymous
    form = visitor.get(f"/t/{token}")
    assert form.status_code == 200 and "How was your experience" in form.text
    done = visitor.post(f"/t/{token}", data={"rating": "5", "body": "Best day ever",
                                             "author_name": "Guest"})
    assert done.status_code == 200 and "Thank you" in done.text
    # the used link now shows the closed state, not the form
    assert "already have your review" in visitor.get(f"/t/{token}").text


def test_featured_testimonial_shows_on_public_site(client, app):
    slug = _published_studio(client, name="Proof Studio", email="proof@example.com")
    client.post("/settings/testimonials/request", data={"client_id": "", "author_name": "Happy Client"})
    tid, token = _first_testimonial(app)
    visitor = client.__class__(client.app)
    visitor.post(f"/t/{token}", data={"rating": "5", "body": "Absolutely stunning work",
                                      "author_name": "Happy Client"})

    # submitted but not featured → not on the public site yet
    assert "Absolutely stunning work" not in client.get(f"/studio/{slug}").text
    client.post(f"/testimonials/{tid}/feature")
    site = client.get(f"/studio/{slug}")
    assert "Kind words" in site.text
    assert "Absolutely stunning work" in site.text and "Happy Client" in site.text

    # hiding it pulls it back off the site
    client.post(f"/testimonials/{tid}/hide")
    assert "Absolutely stunning work" not in client.get(f"/studio/{slug}").text


def test_request_emails_the_client_their_link(client, app):
    _published_studio(client, name="Mailer Studio", email="mail@example.com")
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        cid = create_client(conn, tenant_id=tid, name="Emailed Client",
                            email="client@example.com")["id"]
        conn.commit()
    finally:
        conn.close()
    client.post("/settings/testimonials/request",
                data={"client_id": str(cid), "author_name": ""})
    _id, token = _first_testimonial(app)
    conn = connect(app.state.settings.db_path)
    try:
        emails = list_emails(conn, tid)
    finally:
        conn.close()
    sent = [e for e in emails if e["to_addr"] == "client@example.com"]
    assert sent and token in sent[0]["body"]  # the email carries the review link


def test_manage_and_request_require_login(client):
    assert client.get("/settings/testimonials", follow_redirects=False).status_code == 303
    r = client.post("/settings/testimonials/request", data={"client_id": ""},
                    follow_redirects=False)
    assert r.status_code == 303


def test_public_form_404_on_bad_token(client):
    assert client.get("/t/not-a-real-token").status_code == 404
