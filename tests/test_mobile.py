"""Mobile responsiveness — the responsive CSS ships and the nav toggle is wired."""

from hestia.main import TEMPLATES_DIR


def test_responsive_css_is_served(client):
    css = client.get("/static/hestia.css")
    assert css.status_code == 200
    body = css.text
    # the responsive layer and its key pieces are present
    assert "@media (max-width: 760px)" in body
    assert ".nav-burger" in body
    assert ".inline-grid-form" in body
    # the wide-table-scroll rule
    assert "overflow-x: auto" in body


def test_nav_toggle_wired_in_base(client):
    # any base-rendered page carries the CSS-only nav toggle (no JS)
    page = client.get("/login")
    assert page.status_code == 200
    assert 'id="nav-toggle"' in page.text
    assert 'class="nav-burger"' in page.text


def test_every_standalone_page_declares_viewport():
    """Client-facing pages render their own <html>; each must set the viewport
    meta or it won't scale on a phone. Guards against a new page forgetting it."""
    missing = []
    for path in TEMPLATES_DIR.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        if "<!DOCTYPE" in text and 'name="viewport"' not in text:
            missing.append(path.name)
    assert not missing, f"standalone templates missing viewport meta: {missing}"
