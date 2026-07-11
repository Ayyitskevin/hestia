"""Subscription dunning — when a studio's own card fails (past_due via the Stripe
webhook sync), the studio keeps access (grace period) and the owner gets one polite
fix-your-card email per cooldown window, on the same audit-row pattern as every
other outreach sweep. Distinct from tests/test_dunning.py, which covers the
client-side overdue-invoice ladder."""

from hestia.db import audit
from hestia.email import list_emails
from hestia.launch import DUNNING_ACTION, send_past_due_dunning
from hestia.subscriptions import apply_plan
from hestia.tenants import create_tenant, create_user, get_tenant
from hestia.trial_conversion import trial_conversion_cockpit


def _studio(conn, *, name, email, status):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    create_user(conn, tenant_id=t["id"], email=email, password="pw12345678",
                role="owner", verified=1)
    apply_plan(conn, t["id"], plan="studio", status=status, provider="stripe")
    conn.commit()
    return t


def test_past_due_studio_gets_exactly_one_dunning_email(conn, settings):
    t = _studio(conn, name="Lapsed Card Studio", email="lapsed@x.test",
                status="past_due")
    assert send_past_due_dunning(conn, settings) == 1
    conn.commit()

    sent = [e for e in list_emails(conn, t["id"]) if e["to_addr"] == "lapsed@x.test"]
    assert len(sent) == 1
    assert "payment needs attention" in sent[0]["subject"]
    assert "/settings/billing" in sent[0]["body"]
    assert get_tenant(conn, t["id"])["plan"] == "studio"      # grace period — no downgrade

    row = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action = ? AND tenant_id = ?",
        (DUNNING_ACTION, t["id"]),
    ).fetchone()
    assert row["n"] == 1                                      # the cooldown ledger row

    assert send_past_due_dunning(conn, settings) == 0         # inside the cooldown
    conn.commit()
    assert len([e for e in list_emails(conn, t["id"])
                if e["to_addr"] == "lapsed@x.test"]) == 1


def test_healthy_and_downgraded_studios_are_left_alone(conn, settings):
    trialing = _studio(conn, name="Trialing Studio", email="trialing@x.test",
                       status="trialing")
    active = _studio(conn, name="Active Studio", email="active@x.test",
                     status="active")
    downgraded = _studio(conn, name="Gone Studio", email="gone@x.test",
                         status="past_due")
    apply_plan(conn, downgraded["id"], plan="beta", status="canceled", provider="stripe")
    conn.commit()

    assert send_past_due_dunning(conn, settings) == 0
    for t, email in ((trialing, "trialing@x.test"), (active, "active@x.test"),
                     (downgraded, "gone@x.test")):
        assert [e for e in list_emails(conn, t["id"]) if e["to_addr"] == email] == []


def test_cooldown_expires_and_the_reminder_repeats(conn, settings):
    """After the cooldown lapses with the card still broken, one more email goes
    out — dunning is a drumbeat, not a single shot."""
    t = _studio(conn, name="Still Broken Studio", email="still@x.test",
                status="past_due")
    audit(conn, actor="worker", action=DUNNING_ACTION, tenant_id=t["id"],
          detail="still@x.test")
    conn.execute(
        "UPDATE audit_log SET created_at = datetime('now', '-5 days') "
        "WHERE action = ? AND tenant_id = ?",
        (DUNNING_ACTION, t["id"]),
    )
    conn.commit()

    assert send_past_due_dunning(conn, settings) == 1         # 5 days > 4-day cooldown
    conn.commit()
    assert len([e for e in list_emails(conn, t["id"])
                if e["to_addr"] == "still@x.test"]) == 1


def test_cockpit_counts_past_due(conn, settings):
    _studio(conn, name="Counted Studio", email="counted@x.test", status="past_due")
    _studio(conn, name="Fine Studio", email="fine@x.test", status="active")

    summary = trial_conversion_cockpit(conn, settings)["summary"]
    assert summary["past_due"] == 1
    assert summary["active"] == 1


def test_failed_dunning_does_not_start_cooldown(conn, settings, monkeypatch):
    """A failed SMTP send must not write the dunning audit row — otherwise the
    studio goes silent for the cooldown with a still-broken card and no email."""
    t = _studio(conn, name="Silent Fail Studio", email="silent@x.test",
                status="past_due")
    monkeypatch.setattr("hestia.launch.actions.notify",
                        lambda *a, **k: "error: smtp down")
    assert send_past_due_dunning(conn, settings) == 0
    conn.commit()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action = ? AND tenant_id = ?",
        (DUNNING_ACTION, t["id"]),
    ).fetchone()
    assert row["n"] == 0
    monkeypatch.setattr("hestia.launch.actions.notify",
                        lambda *a, **k: "recorded")
    assert send_past_due_dunning(conn, settings) == 1
    conn.commit()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action = ? AND tenant_id = ?",
        (DUNNING_ACTION, t["id"]),
    ).fetchone()
    assert row["n"] == 1
    # Failed send left no cooldown — a recovered backend can still dunning-email.
