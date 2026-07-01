"""Operator beta launch kit."""

import dataclasses

from conftest import ADMIN_TOKEN, CSRFClient

from hestia.interest import record_beta_interest
from hestia.launch import beta_launch_export_rows, beta_launch_kit
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
    pulse = {item["label"]: item["count"] for item in kit["cohort"]["pulse"]}
    assert pulse["New signups"] == 2
    assert pulse["Pricing/demo signups"] == 2
    assert pulse["Trialing or active"] == 1
    assert {"label": "Pricing", "count": 1, "percent": 50} in kit["cohort"]["sources"]
    assert {"label": "Demo", "count": 1, "percent": 50} in kit["cohort"]["sources"]
    assert {"label": "Never nudged", "count": 2, "percent": 100} in kit["cohort"]["contact"]
    assert {"label": "Trialing", "count": 1, "percent": 50} in kit["cohort"]["trial_states"]
    assert [item["label"] for item in kit["operating_checklist"]] == [
        "Nudge at-risk studios",
        "Verify owner emails",
        "Get niche presets installed",
    ]
    assert kit["operating_checklist"][0]["rank"] == 1
    assert kit["operating_checklist"][0]["href"] == "/admin/launch"
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
    assert "Founder operating checklist" in page.text
    assert "Nudge at-risk studios" in page.text
    assert "Beta cohort" in page.text
    assert "Source mix" in page.text
    assert "Contact freshness" in page.text
    assert "5-studio beta checklist" in page.text
    assert "Pricing page" in page.text
    assert "Follow up today" in page.text
    assert 'href="/admin/launch/export.csv"' in page.text
    assert "Launch Admin" in page.text
    assert "Send nudge" in page.text
    assert 'href="/admin/launch"' in admin.get("/admin/tenants").text


def test_admin_launch_nudge_sends_email_and_records_audit(settings, conn):
    tenant = create_tenant(conn, name="Nudge Studio", shoot_type="wedding",
                           signup_source="demo", signup_landing_path="/demo/wedding")
    _owner(conn, tenant["id"], "nudge@example.com", verified=0)
    conn.commit()

    app = create_app(settings)
    admin = CSRFClient(app)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})
    response = admin.post(f"/admin/launch/{tenant['id']}/nudge", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/launch?nudge=sent"
    email = conn.execute(
        "SELECT * FROM emails WHERE tenant_id = ? AND to_addr = ?",
        (tenant["id"], "nudge@example.com"),
    ).fetchone()
    assert email["subject"] == "Next Hestia step for Nudge Studio"
    assert "Can I help you verify your owner email" in email["body"]
    audit = conn.execute(
        "SELECT action, detail FROM audit_log WHERE tenant_id = ?",
        (tenant["id"],),
    ).fetchone()
    assert audit["action"] == "launch.nudge_sent"
    assert audit["detail"] == "nudge@example.com"
    assert "Nudge recorded" in admin.get("/admin/launch?nudge=sent").text


def test_admin_launch_nudge_cools_down_duplicate_sends(settings, conn):
    tenant = create_tenant(conn, name="Cooldown Studio", shoot_type="wedding",
                           signup_source="pricing", signup_landing_path="/pricing")
    _owner(conn, tenant["id"], "cooldown@example.com", verified=0)
    conn.commit()

    app = create_app(settings)
    admin = CSRFClient(app)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})
    admin.post(f"/admin/launch/{tenant['id']}/nudge", follow_redirects=False)
    duplicate = admin.post(f"/admin/launch/{tenant['id']}/nudge", follow_redirects=False)

    assert duplicate.status_code == 303
    assert duplicate.headers["location"] == "/admin/launch?nudge=cooldown"
    sent = conn.execute(
        "SELECT COUNT(*) AS n FROM emails WHERE tenant_id = ? AND to_addr = ?",
        (tenant["id"], "cooldown@example.com"),
    ).fetchone()["n"]
    assert sent == 1
    actions = [
        row["action"]
        for row in conn.execute(
            "SELECT action FROM audit_log WHERE tenant_id = ? ORDER BY id",
            (tenant["id"],),
        ).fetchall()
    ]
    assert actions == ["launch.nudge_sent", "launch.nudge_skipped"]

    page = admin.get("/admin/launch")
    assert "Contact freshness" in page.text
    assert "Cooling down 3 days" in page.text
    assert "Last nudge:" in page.text
    assert "Nudge skipped: this studio is still inside the outreach cooldown." in (
        admin.get("/admin/launch?nudge=cooldown").text
    )
    row = next(r for r in beta_launch_export_rows(conn, settings)
               if r["slug"] == "cooldown-studio")
    assert row["nudge_status"] == "Cooling down 3 days"
    assert row["last_nudged_at"]


def test_admin_launch_surfaces_beta_interest_leads(settings, conn):
    record_beta_interest(
        conn,
        settings,
        name="Interest Owner",
        studio_name="Interest Studio",
        email="interest@example.com",
        shoot_type="wedding",
        note="Replacing HoneyBook and galleries.",
        source="pricing",
        landing_path="/pricing",
    )
    conn.commit()

    kit = beta_launch_kit(conn, settings)
    assert kit["interest"]["total"] == 1
    assert kit["interest"]["recent"][0]["studio_name"] == "Interest Studio"
    assert kit["interest"]["recent"][0]["source_label"] == "Pricing"
    assert kit["operating_checklist"][0]["label"] == "Review beta interest leads"

    app = create_app(settings)
    admin = CSRFClient(app)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})
    page = admin.get("/admin/launch")

    assert page.status_code == 200
    assert "Beta interest" in page.text
    assert "Interest Studio" in page.text
    assert "interest@example.com" in page.text
    assert "Replacing HoneyBook and galleries." in page.text


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
    assert "last_nudged_at,nudge_status" in export.text
    assert "'=Formula Studio" in export.text
    assert "formula@example.com" in export.text
    assert "Verify owner email" in export.text
