"""Tenant-site SEO + private-link privacy — each studio's public page carries real
meta/OG/JSON-LD (so it ranks and unfurls), while every token-gated client surface is
noindexed and robots-disallowed (so a leaked private link never lands in a search index).
Complements test_seo.py, which covers Hestia's own marketing pages."""

import io

from conftest import login_owner, onboard_studio

from hestia.delivery import enable_delivery
from hestia.galleries import add_image, create_gallery, publish_gallery
from hestia.tenants import create_tenant


def _publish(client):
    client.post("/settings/site", data={"headline": "Warm, candid wedding photography",
                                        "about": "Serving the coast.", "contact_email": "hi@x.test",
                                        "published": "1"})


def test_studio_page_has_seo_head(client, app):
    creds = onboard_studio(client, name="Sunlit Studio", email="seo@studio.test")
    login_owner(client, creds)
    _publish(client)
    page = client.get("/studio/sunlit-studio").text
    assert '<meta name="description" content="Warm, candid wedding photography">' in page
    assert 'rel="canonical"' in page and "/studio/sunlit-studio" in page
    assert '<meta property="og:title"' in page and '<meta property="og:description"' in page
    assert '"@type": "ProfessionalService"' in page          # JSON-LD present
    assert "noindex" not in page                             # the studio page IS indexable


def test_token_surfaces_are_noindexed(client, conn, storage):
    t = create_tenant(conn, name="Private Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="Finals")
    add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"], filename="a.jpg",
              fileobj=io.BytesIO(b"x" * 8), content_type="image/jpeg")
    publish_gallery(conn, t["id"], g["id"])
    token = enable_delivery(conn, t["id"], g["id"])
    conn.commit()
    assert '<meta name="robots" content="noindex">' in client.get(f"/d/{token}").text
    assert '<meta name="robots" content="noindex">' in \
        client.get(f"/g/{t['slug']}/{g['slug']}").text


def test_robots_txt_disallows_private_paths(client):
    body = client.get("/robots.txt").text
    assert "User-agent: *" in body
    for path in ("/portal/", "/d/", "/pay/", "/a/", "/sign/", "/g/", "/t/", "/q/",
                 "/invite/", "/media/"):
        assert f"Disallow: {path}" in body
    assert "Allow: /" in body                                # everything else is crawlable
