"""Hosted trial conversion cockpit for the operator admin."""

from conftest import ADMIN_TOKEN, CSRFClient

from hestia.presets import apply_preset
from hestia.subscriptions import apply_plan
from hestia.tenants import create_tenant, create_user
from hestia.trial_conversion import trial_conversion_cockpit, trial_conversion_for_tenant


def _owner(conn, tenant_id, email, *, verified=1):
    return create_user(
        conn,
        tenant_id=tenant_id,
        email=email,
        password="pw12345",
        role="owner",
        verified=verified,
    )


def test_trial_conversion_cockpit_prioritizes_stalled_studios(conn, settings):
    stalled = create_tenant(conn, name="Stalled Studio", shoot_type="wedding")
    _owner(conn, stalled["id"], "stalled@example.com", verified=0)
    conn.execute("UPDATE tenants SET created_at = datetime('now', '-4 days') WHERE id = ?",
                 (stalled["id"],))

    ready = create_tenant(conn, name="Preset Ready", shoot_type="wedding")
    _owner(conn, ready["id"], "ready@example.com")
    apply_preset(conn, ready["id"], "wedding", include_demo=False)

    trialing = create_tenant(conn, name="Active Trial", shoot_type="wedding")
    _owner(conn, trialing["id"], "trial@example.com")
    apply_plan(conn, trialing["id"], plan="studio", status="trialing", provider="mock")
    conn.commit()

    cockpit = trial_conversion_cockpit(conn, settings)
    by_name = {s["name"]: s for s in cockpit["studios"]}

    assert cockpit["summary"]["total"] == 3
    assert cockpit["summary"]["trial_ready"] == 2
    assert cockpit["summary"]["trialing"] == 1
    assert cockpit["studios"][0]["name"] == "Stalled Studio"
    assert by_name["Stalled Studio"]["risk"] == "high"
    assert by_name["Stalled Studio"]["next_action"] == "Verify owner email"
    assert by_name["Preset Ready"]["next_action"] == "Start trial checkout"
    assert by_name["Active Trial"]["trial_state"] == "trialing"
    assert by_name["Active Trial"]["trial_days_left"] <= settings.trial_days


def test_trial_conversion_for_tenant_counts_commercial_signals(conn, settings):
    tenant = create_tenant(conn, name="Signal Studio", shoot_type="wedding")
    _owner(conn, tenant["id"], "signals@example.com")
    conn.execute("INSERT INTO proposals (tenant_id, package_id, contract_id, invoice_id, title, token, status) "
                 "VALUES (?, 1, 1, 1, 'Proposal', 'tok-signal', 'sent')",
                 (tenant["id"],))
    conn.execute("UPDATE proposals SET view_count = 2 WHERE tenant_id = ?", (tenant["id"],))
    conn.execute("INSERT INTO galleries (tenant_id, slug, title, status, published_at) "
                 "VALUES (?, 'published', 'Published', 'published', datetime('now'))",
                 (tenant["id"],))
    gid = conn.execute("SELECT id FROM galleries WHERE tenant_id = ?", (tenant["id"],)).fetchone()["id"]
    conn.execute("INSERT INTO offers (tenant_id, gallery_id, token, status) "
                 "VALUES (?, ?, 'offer-token', 'active')", (tenant["id"], gid))
    conn.execute("INSERT INTO invoices (tenant_id, title, amount_cents, token, status) "
                 "VALUES (?, 'Retainer', 10000, 'invoice-token', 'sent')", (tenant["id"],))
    conn.commit()

    summary = trial_conversion_for_tenant(conn, tenant, settings)

    assert summary["proposals_sent"] == 1
    assert summary["proposal_views"] == 2
    assert summary["published_galleries"] == 1
    assert summary["active_offers"] == 1
    assert summary["money_links"] == 1


def test_admin_trial_conversion_pages_render(app, conn):
    tenant = create_tenant(conn, name="Admin Trial", shoot_type="wedding")
    _owner(conn, tenant["id"], "admintrial@example.com", verified=0)
    conn.commit()
    admin = CSRFClient(app)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})

    trials = admin.get("/admin/trials")
    assert trials.status_code == 200
    assert "Trial conversion" in trials.text
    assert "Admin Trial" in trials.text
    assert "Verify owner email" in trials.text
    assert 'href="/admin/tenants/' in trials.text

    tenants = admin.get("/admin/tenants")
    assert 'href="/admin/trials"' in tenants.text

    detail = admin.get(f"/admin/tenants/{tenant['id']}")
    assert detail.status_code == 200
    assert "Trial conversion" in detail.text
    assert "Open trial cockpit" in detail.text
