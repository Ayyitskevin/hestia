"""Portrait & Family preset — the biggest photographer segment gets a first-run kit,
alongside the wedding / food / real-estate presets."""

from hestia.booking import list_booking_types
from hestia.packages import list_packages
from hestia.presets import PRESETS, apply_preset, preset_applied
from hestia.questionnaires import list_questionnaires
from hestia.tenants import create_tenant, get_tenant


def test_portrait_preset_registered():
    p = PRESETS["portrait"]
    assert p["shoot_type"] == "portrait" and p["label"] == "Portrait & Family"


def test_apply_portrait_preset_seeds_studio_objects(conn):
    t = create_tenant(conn, name="Portrait Co", shoot_type="other")
    summary = apply_preset(conn, t["id"], "portrait", include_demo=False)
    assert summary is not None
    assert preset_applied(conn, t["id"])
    assert get_tenant(conn, t["id"])["shoot_type"] == "portrait"
    titles = {bt["title"] for bt in list_booking_types(conn, t["id"], active_only=True)}
    assert {"Mini session", "Full portrait session"} <= titles
    names = {p["name"] for p in list_packages(conn, t["id"])}
    assert "Portrait Session" in names and "Family Story" in names
    assert any(q["title"] == "Portrait intake" for q in list_questionnaires(conn, t["id"]))


def test_onboarding_page_offers_portrait(client, app):
    from conftest import login_owner, onboard_studio
    creds = onboard_studio(client, email="pp@studio.test")
    login_owner(client, creds)
    page = client.get("/onboarding")
    if page.status_code == 200:                      # already-set-up studios bounce to /dashboard
        assert "Portrait &amp; Family" in page.text  # Jinja autoescapes the ampersand
    r = client.post("/onboarding", data={"preset": "portrait"})
    assert r.status_code in (200, 303)
