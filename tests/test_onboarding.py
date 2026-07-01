"""Onboarding presets seed real studio surfaces and stay tenant-scoped."""

from conftest import login_owner, onboard_studio

from hestia.booking import list_booking_types
from hestia.db import connect
from hestia.packages import list_packages
from hestia.presets import apply_preset, preset_applied
from hestia.questionnaires import list_questionnaires
from hestia.studio import get_profile
from hestia.tenants import create_tenant, get_tenant


def _tid(conn, email):
    return conn.execute(
        "SELECT t.id FROM tenants t JOIN users u ON u.tenant_id = t.id WHERE u.email = ?",
        (email,),
    ).fetchone()["id"]


def test_apply_wedding_preset_seeds_studio_objects(conn):
    tenant = create_tenant(conn, name="Starter Studio", shoot_type="other")
    conn.commit()

    summary = apply_preset(conn, tenant["id"], "wedding", include_demo=True)

    assert summary["label"] == "Wedding"
    assert summary["profile"] is True
    assert summary["booking_types"] == 2
    assert summary["packages"] == 3
    assert summary["questionnaires"] == 1
    assert summary["demo"] is not None
    assert get_tenant(conn, tenant["id"])["shoot_type"] == "wedding"

    profile = get_profile(conn, tenant["id"])
    assert "Wedding photography" in profile["headline"]
    assert profile["published"] == 0
    assert [b["title"] for b in list_booking_types(conn, tenant["id"], active_only=True)] == [
        "Wedding consultation",
        "Engagement session",
    ]
    assert [p["name"] for p in list_packages(conn, tenant["id"], active_only=True)] == [
        "Wedding Essentials",
        "Full Wedding Story",
        "Heirloom Album Add-On",
    ]
    forms = list_questionnaires(conn, tenant["id"])
    assert len(forms) == 1 and forms[0]["title"] == "Wedding intake" and forms[0]["item_count"] == 6
    demo = conn.execute(
        "SELECT c.name AS client_name, p.name AS project_name "
        "FROM clients c JOIN projects p ON p.client_id = c.id AND p.tenant_id = c.tenant_id "
        "WHERE c.tenant_id = ? AND c.email = 'demo+wedding@hestia.local'",
        (tenant["id"],),
    ).fetchone()
    assert dict(demo) == {
        "client_name": "Avery & Jordan Demo",
        "project_name": "Avery & Jordan Wedding Demo",
    }


def test_apply_preset_does_not_duplicate_seeded_surfaces(conn):
    tenant = create_tenant(conn, name="Repeat Studio", shoot_type="other")
    conn.commit()
    first = apply_preset(conn, tenant["id"], "food", include_demo=True)
    second = apply_preset(conn, tenant["id"], "food", include_demo=True)

    assert first["booking_types"] == 2 and first["packages"] == 3 and first["questionnaires"] == 1
    assert second["booking_types"] == 0 and second["packages"] == 0 and second["questionnaires"] == 0
    assert len(list_booking_types(conn, tenant["id"], active_only=True)) == 2
    assert len(list_packages(conn, tenant["id"], active_only=True)) == 3
    assert len(list_questionnaires(conn, tenant["id"])) == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM clients WHERE tenant_id = ? AND email = 'demo+food@hestia.local'",
        (tenant["id"],),
    ).fetchone()["n"] == 1


def test_preset_application_is_tenant_scoped(conn):
    a = create_tenant(conn, name="A", shoot_type="other")
    b = create_tenant(conn, name="B", shoot_type="other")
    conn.commit()

    apply_preset(conn, a["id"], "real_estate", include_demo=False)

    assert preset_applied(conn, a["id"]) is True
    assert preset_applied(conn, b["id"]) is False
    assert list_booking_types(conn, b["id"], active_only=True) == []
    assert list_packages(conn, b["id"], active_only=True) == []
    assert list_questionnaires(conn, b["id"]) == []
    assert get_tenant(conn, b["id"])["shoot_type"] == "other"


def test_onboarding_page_and_post_seed_food_preset(client, app):
    creds = onboard_studio(client, name="Launch Studio", email="launch@example.com")
    login_owner(client, creds)

    page = client.get("/onboarding")
    assert page.status_code == 200
    assert "Wedding" in page.text and "Food &amp; Beverage" in page.text and "Real Estate" in page.text

    response = client.post(
        "/onboarding",
        data={"preset": "food", "include_demo": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/dashboard"

    conn = connect(app.state.settings.db_path)
    try:
        tenant_id = _tid(conn, creds["email"])
        assert get_tenant(conn, tenant_id)["shoot_type"] == "food"
        assert [p["name"] for p in list_packages(conn, tenant_id, active_only=True)] == [
            "Menu Refresh",
            "Campaign Day",
            "Monthly Content Retainer",
        ]
        assert len(list_booking_types(conn, tenant_id, active_only=True)) == 2
        assert list_questionnaires(conn, tenant_id)[0]["title"] == "Food & beverage intake"
        assert "Food and beverage photography" in get_profile(conn, tenant_id)["headline"]
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM clients WHERE tenant_id = ? AND email = 'demo+food@hestia.local'",
            (tenant_id,),
        ).fetchone()["n"] == 1
    finally:
        conn.close()
