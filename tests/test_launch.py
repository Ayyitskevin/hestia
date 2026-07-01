"""Operator beta launch kit."""

import dataclasses

from conftest import ADMIN_TOKEN, CSRFClient

from hestia.interest import (
    mark_beta_interest_converted,
    record_beta_interest,
    send_beta_interest_invite,
)
from hestia.launch import (
    beta_launch_export_rows,
    beta_launch_kit,
    build_beta_launch_digest,
    send_beta_launch_digest,
)
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
    assert kit["operations"]["base_url"] == "https://hestia.example"
    assert any(item["label"] == "Public URL" and item["ok"]
               for item in kit["operations"]["readiness"])
    assert any(item["label"] == "Billing" and not item["ok"]
               for item in kit["operations"]["readiness"])
    assert any(cmd["command"] == "bash scripts/hosted-preflight.sh --url https://hestia.example"
               for cmd in kit["operations"]["commands"])
    assert any(link["url"] == "https://hestia.example/beta"
               for link in kit["operations"]["links"])
    assert any(link["url"] == "https://hestia.example/beta?source=beta&path=/beta"
               for link in kit["invite_links"])
    assert any(link["url"] == "https://hestia.example/signup?source=pricing&path=/pricing"
               for link in kit["invite_links"])
    assert any(item["label"] == "Start first hosted trial" and item["complete"]
               for item in kit["milestones"])
    assert kit["followups"][0]["name"] == "Stalled Studio"
    assert kit["followups"][0]["mailto"].startswith("mailto:stalled@example.com")


def test_beta_launch_kit_tracks_revenue_pipeline(conn, settings):
    record_beta_interest(
        conn,
        settings,
        name="New Lead",
        studio_name="New Lead Studio",
        email="new-lead@example.com",
        shoot_type="wedding",
    )
    invited = record_beta_interest(
        conn,
        settings,
        name="Invited Lead",
        studio_name="Invited Lead Studio",
        email="invited-lead@example.com",
        shoot_type="wedding",
    )
    send_beta_interest_invite(conn, settings, invited["id"])

    def converted_stage(name: str, email: str, *, verified=1, preset=False, plan_status=""):
        interest = record_beta_interest(
            conn,
            settings,
            name=f"{name} Owner",
            studio_name=name,
            email=email,
            shoot_type="wedding",
        )
        tenant = create_tenant(conn, name=name, shoot_type="wedding",
                               signup_source="interest", signup_landing_path="/interest")
        _owner(conn, tenant["id"], email, verified=verified)
        mark_beta_interest_converted(conn, interest["id"], tenant["id"])
        if preset:
            apply_preset(conn, tenant["id"], "wedding", include_demo=False)
        if plan_status:
            apply_plan(conn, tenant["id"], plan="studio", status=plan_status, provider="mock")
        return tenant

    converted_stage("Created Studio", "created@example.com", verified=0)
    converted_stage("Verified Studio", "verified@example.com")
    converted_stage("Preset Studio", "preset@example.com", preset=True)
    converted_stage("Trialing Studio", "trialing@example.com", preset=True,
                    plan_status="trialing")
    converted_stage("Paid Studio", "paid@example.com", preset=True, plan_status="active")
    conn.commit()

    kit = beta_launch_kit(conn, settings)
    pipeline = kit["revenue_pipeline"]
    stages = {stage["label"]: stage for stage in pipeline["stages"]}

    assert [stage["label"] for stage in pipeline["stages"]] == [
        "Interest",
        "Invited",
        "Studio created",
        "Verified",
        "Preset started",
        "Trialing",
        "Paid",
    ]
    assert [stage["count"] for stage in pipeline["stages"]] == [7, 6, 5, 4, 3, 2, 1]
    assert stages["Invited"]["dropoff"] == 1
    assert stages["Invited"]["action"] == "Send 1 private invite"
    assert stages["Studio created"]["detail"] == "1 invited lead has not created a studio yet."
    assert stages["Paid"]["detail"] == "$40/month in current flat-plan MRR."
    assert pipeline["bottleneck"]["label"] == "Invited"
    assert pipeline["paid"] == 1
    assert pipeline["mrr_cents"] == 4000


def test_beta_launch_digest_summarizes_pipeline_and_cooldown(conn, settings):
    settings = dataclasses.replace(settings, smtp_from="founder@hestia.test")
    record_beta_interest(
        conn,
        settings,
        name="Digest Lead",
        studio_name="Digest Lead Studio",
        email="digest-lead@example.com",
        shoot_type="wedding",
    )
    paid = create_tenant(conn, name="Paid Digest", shoot_type="wedding",
                         signup_source="pricing", signup_landing_path="/pricing")
    _owner(conn, paid["id"], "paid-digest@example.com")
    apply_preset(conn, paid["id"], "wedding", include_demo=False)
    apply_plan(conn, paid["id"], plan="studio", status="active", provider="mock")
    conn.commit()

    digest = build_beta_launch_digest(conn, settings)

    assert digest["subject"] == "Hestia launch digest: 1 paid, 0 stalled, 1 open interest"
    assert "Current flat-plan MRR: $40/month" in digest["body"]
    assert "Revenue pipeline" in digest["body"]
    assert "- Paid: 1" in digest["body"]
    assert "Beta interest" in digest["body"]
    assert "Open launch kit:" in digest["body"]

    sent = send_beta_launch_digest(conn, settings, actor="system")
    repeat = send_beta_launch_digest(conn, settings, actor="system")
    forced = send_beta_launch_digest(conn, settings, force=True, actor="admin")

    assert sent["sent"] and sent["to"] == "founder@hestia.test"
    assert repeat == {"sent": False, "status": "cooldown", "to": "founder@hestia.test"}
    assert forced["sent"]
    emails = conn.execute(
        "SELECT * FROM emails WHERE to_addr = ? AND subject LIKE 'Hestia launch digest:%'",
        ("founder@hestia.test",),
    ).fetchall()
    assert len(emails) == 2
    audits = conn.execute(
        "SELECT actor, action, detail FROM audit_log WHERE action = 'launch.digest_sent' "
        "ORDER BY id",
    ).fetchall()
    assert [row["actor"] for row in audits] == ["system", "admin"]
    assert all("founder@hestia.test" in row["detail"] for row in audits)


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
    assert "Beta revenue pipeline" in page.text
    assert "Interest → invite → studio → verified → preset → trial → paid." in page.text
    assert "Launch operations" in page.text
    assert "Runbook commands" in page.text
    assert "Share and inspect" in page.text
    assert "docker compose up --build -d" in page.text
    assert "bash scripts/hosted-preflight.sh --url http://testserver" in page.text
    assert "http://testserver/beta" in page.text
    assert "Studio created" in page.text
    assert "Paid" in page.text
    assert "Nudge at-risk studios" in page.text
    assert "Beta cohort" in page.text
    assert "Source mix" in page.text
    assert "Contact freshness" in page.text
    assert "5-studio beta checklist" in page.text
    assert "Pricing page" in page.text
    assert "Follow up today" in page.text
    assert "Email digest" in page.text
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


def test_admin_launch_digest_sends_to_founder(settings, conn):
    settings = dataclasses.replace(settings, smtp_from="founder@hestia.test")
    tenant = create_tenant(conn, name="Digest Admin", shoot_type="wedding",
                           signup_source="pricing", signup_landing_path="/pricing")
    _owner(conn, tenant["id"], "digest-admin@example.com")
    conn.commit()

    app = create_app(settings)
    admin = CSRFClient(app)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})
    response = admin.post("/admin/launch/digest", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/launch?digest=sent"
    email = conn.execute(
        "SELECT * FROM emails WHERE to_addr = ? ORDER BY id DESC LIMIT 1",
        ("founder@hestia.test",),
    ).fetchone()
    assert email["subject"].startswith("Hestia launch digest:")
    assert "Revenue pipeline" in email["body"]
    audit = conn.execute(
        "SELECT actor, action, detail FROM audit_log WHERE action = 'launch.digest_sent'",
    ).fetchone()
    assert audit["actor"] == "admin"
    assert "founder@hestia.test" in audit["detail"]
    assert "Founder launch digest emailed" in admin.get("/admin/launch?digest=sent").text


def test_admin_launch_digest_reports_missing_recipient(settings, conn):
    app = create_app(settings)
    admin = CSRFClient(app)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})
    response = admin.post("/admin/launch/digest", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/launch?digest=missing"
    assert conn.execute("SELECT COUNT(*) AS n FROM emails").fetchone()["n"] == 0
    assert "Digest skipped: set SMTP" in admin.get("/admin/launch?digest=missing").text


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
    assert "1 open" in page.text
    assert "Send invite" in page.text
    assert "Interest Studio" in page.text
    assert "interest@example.com" in page.text
    assert "Replacing HoneyBook and galleries." in page.text


def test_admin_launch_sends_beta_interest_invite(settings, conn):
    interest = record_beta_interest(
        conn,
        settings,
        name="Invite Owner",
        studio_name="Invite Studio",
        email="launch-interest@example.com",
        shoot_type="food",
        note="Need one studio command center.",
        source="demo",
        landing_path="/demo/food",
    )
    conn.commit()

    app = create_app(settings)
    admin = CSRFClient(app)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})
    response = admin.post(
        f"/admin/launch/interest/{interest['id']}/invite",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/launch?interest=sent"
    lead = conn.execute("SELECT * FROM beta_interests WHERE id = ?",
                        (interest["id"],)).fetchone()
    assert lead["status"] == "invited"
    assert lead["invite_token_hash"]
    email = conn.execute(
        "SELECT * FROM emails WHERE to_addr = ? ORDER BY id DESC LIMIT 1",
        ("launch-interest@example.com",),
    ).fetchone()
    assert email["subject"] == "You're invited to start your Hestia studio beta"
    assert "/invite/" in email["body"]
    audit = conn.execute(
        "SELECT action, detail FROM audit_log WHERE action = 'interest.invite_sent'",
    ).fetchone()
    assert audit["detail"] == "launch-interest@example.com"
    page = admin.get("/admin/launch?interest=sent")
    assert "Beta invite sent" in page.text
    assert "Invited" in page.text
    assert "Resend invite" in page.text


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
