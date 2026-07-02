"""Public share metadata for hosted SaaS pages."""

from html import unescape


def _assert_share_meta(page, *, title: str, description: str, canonical: str):
    text = unescape(page.text)
    assert f"<title>{title}</title>" in text
    assert f'<meta name="description" content="{description}">' in text
    assert f'<link rel="canonical" href="{canonical}">' in text
    assert '<meta property="og:site_name" content="Hestia">' in text
    assert '<meta property="og:type" content="website">' in text
    assert f'<meta property="og:title" content="{title}">' in text
    assert f'<meta property="og:description" content="{description}">' in text
    assert f'<meta property="og:url" content="{canonical}">' in text
    # A real preview image, sized, so links unfurl as a large card (not a bare title).
    assert '<meta property="og:image" content="http://testserver/static/og-cover.png">' in text
    assert '<meta property="og:image:width" content="1200">' in text
    assert '<meta property="og:image:height" content="630">' in text
    assert '<meta name="twitter:card" content="summary_large_image">' in text
    assert '<meta name="twitter:image" content="http://testserver/static/og-cover.png">' in text
    assert f'<meta name="twitter:title" content="{title}">' in text
    assert f'<meta name="twitter:description" content="{description}">' in text
    assert '<meta name="theme-color" content="#c0552f">' in text


def test_landing_page_has_share_metadata(client):
    _assert_share_meta(
        client.get("/"),
        title="Hestia — the AI-native studio for photographers",
        description=(
            "Run a professional photography studio from gallery to paid in one hosted "
            "command center: AI-powered galleries, offers, payments, and follow-up for "
            "$40/month."
        ),
        canonical="http://testserver/",
    )


def test_pricing_page_has_share_metadata(client):
    _assert_share_meta(
        client.get("/pricing?utm_source=x"),
        title="Pricing - Hestia",
        description=(
            "Hestia has one flat $40/month hosted plan after a 14-day free trial, "
            "replacing booking, CRM, contracts, galleries, invoices, AI offers, and "
            "follow-up tools."
        ),
        canonical="http://testserver/pricing",
    )


def test_beta_page_has_share_metadata(client):
    _assert_share_meta(
        client.get("/beta?source=landing&path=/"),
        title="Hestia beta - $40/month photography studio OS",
        description=(
            "Join the Hestia hosted beta for growing photography studios: a 14-day "
            "trial, wedding, food and real-estate presets, and one flat $40/month plan."
        ),
        canonical="http://testserver/beta",
    )


def test_portrait_demo_is_its_own_tour(client):
    """Portrait must render its own tour — not silently fall back to wedding —
    so all four onboarding niches have a real demo landing page."""
    _assert_share_meta(
        client.get("/demo/portrait"),
        title="Portrait & family demo - Hestia",
        description=(
            "Preview Hestia's portrait & family workflow for photographers: inquiry, "
            "booking, contract, gallery delivery, payment, and retention in one "
            "$40/month studio OS."
        ),
        canonical="http://testserver/demo/portrait",
    )
    page = client.get("/demo/portrait").text
    assert "mini-session drop" in page                 # portrait workflow, not wedding's


def test_demo_page_has_niche_share_metadata(client):
    _assert_share_meta(
        client.get("/demo/food"),
        title="Food & beverage demo - Hestia",
        description=(
            "Preview Hestia's food & beverage workflow for photographers: inquiry, "
            "booking, contract, gallery delivery, payment, and retention in one "
            "$40/month studio OS."
        ),
        canonical="http://testserver/demo/food",
    )


def test_og_cover_image_is_served_and_correctly_sized(client):
    """The unfurl preview asset must actually exist, serve as a PNG, and be the
    1200x630 the meta tags advertise — a broken og:image is worse than none."""
    import struct

    resp = client.get("/static/og-cover.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    data = resp.content
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (1200, 630)
