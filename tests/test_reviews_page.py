"""Public reviews page — a shareable /studio/{slug}/reviews listing featured testimonials.

Published-gated like the site; only featured reviews show; linked from the site.
"""

from conftest import login_owner, onboard_studio

from hestia.testimonials import request_testimonial, set_status, submit_testimonial


def _published_with_review(client, conn, *, email):
    creds = onboard_studio(client, name="Lumen Studio", email=email)
    login_owner(client, creds)
    client.post("/settings/site", data={"published": "1"})
    tid = conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]
    slug = conn.execute("SELECT slug FROM tenants WHERE id = ?", (tid,)).fetchone()["slug"]
    t = request_testimonial(conn, tenant_id=tid, client_id=None, author_name="Happy Couple")
    submit_testimonial(conn, t["token"], rating=5, body="Amazing photos!", author_name="Happy Couple")
    set_status(conn, tid, t["id"], "featured")
    conn.commit()
    return tid, slug


def test_reviews_page_lists_featured(client, conn):
    _tid, slug = _published_with_review(client, conn, email="rev1@example.com")
    page = client.get(f"/studio/{slug}/reviews").text
    assert "Amazing photos!" in page and "Happy Couple" in page


def test_site_links_to_reviews(client, conn):
    _tid, slug = _published_with_review(client, conn, email="rev2@example.com")
    assert f"/studio/{slug}/reviews" in client.get(f"/studio/{slug}").text


def test_only_featured_reviews_show(client, conn):
    creds = onboard_studio(client, email="rev3@example.com")
    login_owner(client, creds)
    client.post("/settings/site", data={"published": "1"})
    tid = conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]
    slug = conn.execute("SELECT slug FROM tenants WHERE id = ?", (tid,)).fetchone()["slug"]
    t = request_testimonial(conn, tenant_id=tid, client_id=None, author_name="Quiet One")
    submit_testimonial(conn, t["token"], rating=4, body="Hidden gem", author_name="Quiet One")
    conn.commit()                                              # left 'submitted', never featured
    assert "Hidden gem" not in client.get(f"/studio/{slug}/reviews").text


def test_unknown_slug_404(client):
    assert client.get("/studio/nope-studio/reviews").status_code == 404


def test_unpublished_shows_coming_soon(client, conn):
    creds = onboard_studio(client, email="rev4@example.com")
    login_owner(client, creds)                                # site not published
    slug = conn.execute("SELECT slug FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["slug"]
    r = client.get(f"/studio/{slug}/reviews")
    assert r.status_code == 200 and "Kind words" not in r.text  # coming-soon, not the reviews page
