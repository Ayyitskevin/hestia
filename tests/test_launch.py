"""Operator beta launch kit."""

import dataclasses

from conftest import ADMIN_TOKEN, CSRFClient

from hestia.launch import beta_launch_kit
from hestia.main import create_app
from hestia.presets import apply_preset
from hestia.subscriptions import apply_plan
from hestia.tenants import create_tenant, create_user


def _owner(conn, tenant_id, email, *, verified=1):
    return create_user(
        conn,
        tenant_id=tenant_id,
        email=email,
        password="pw12345",
        role="owner",
        verified=verified,
    )


def test_beta_launch_kit_builds_invite_links_and_followup_queue(conn, settings):
    settings = dataclasses.replace(settings, public_url="https://hestia.example")
    stalled = create_tenant(conn, name="Stalled Studio", shoot_type="wedding",
                            signup_source="pricing", signup_landing_path="/pricing")
    _owner(conn, stalled["id"], "stalled@example.com", verified=0)

    active = create_tenant(conn, name="Active Trial", shoot_type="wedding",
                           signup_source="demo", signup_landing_path="/demo/wedding")
    _owner(conn, active["id"], "active@example.com")
    apply_preset(conn, active["id"], "wedding", include_demo=False)
    apply_plan(conn, active["id"], plan="studio", status="trialing", provider="mock")
    conn.commit()

    kit = beta_launch_kit(conn, settings)

    assert kit["target"] == 5
    assert kit["summary"]["studios"] == 2
    assert kit["summary"]["sourced"] == 2
    assert any(link["url"] == "https://hestia.example/signup?source=pricing&path=/pricing"
               for link in kit["invite_links"])
    assert any(item["label"] == "Start first hosted trial" and item["complete"]
               for item in kit["milestones"])
    assert kit["followups"][0]["name"] == "Stalled Studio"
    assert kit["followups"][0]["mailto"].startswith("mailto:stalled@example.com")


def test_admin_launch_page_renders_invites_and_followups(settings, conn):
    tenant = create_tenant(conn, name="Launch Admin", shoot_type="wedding",
                           signup_source="pricing", signup_landing_path="/pricing")
    _owner(conn, tenant["id"], "launch@example.com", verified=0)
    conn.commit()

    app = create_app(settings)
    admin = CSRFClient(app)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})

    page = admin.get("/admin/launch")

    assert page.status_code == 200
    assert "Beta launch kit" in page.text
    assert "5-studio beta checklist" in page.text
    assert "Pricing page" in page.text
    assert "Follow up today" in page.text
    assert 'href="/admin/launch/export.csv"' in page.text
    assert "Launch Admin" in page.text
    assert 'href="/admin/launch"' in admin.get("/admin/tenants").text


def test_admin_launch_export_csv_is_auth_gated_and_spreadsheet_safe(settings, conn):
    tenant = create_tenant(conn, name="=Formula Studio", shoot_type="wedding",
                           signup_source="pricing", signup_landing_path="/pricing")
    _owner(conn, tenant["id"], "formula@example.com", verified=0)
    conn.commit()

    app = create_app(settings)
    anon = CSRFClient(app)
    assert anon.get("/admin/launch/export.csv", follow_redirects=False).status_code == 303

    admin = CSRFClient(app)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})
    export = admin.get("/admin/launch/export.csv")

    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=\"hestia-beta-launch.csv\"" in export.headers["content-disposition"]
    assert "studio,slug,owner_email,owner_verified,source" in export.text
    assert "'=Formula Studio" in export.text
    assert "formula@example.com" in export.text
    assert "Verify owner email" in export.text
