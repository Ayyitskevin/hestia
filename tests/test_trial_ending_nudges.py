"""Automatic trial-ending nudges — the worker sweep sends the personalized launch
nudge to every studio whose trial is about to close, on the same audit-backed
cooldown as the admin's manual button (so the two can never double-send)."""

from hestia.email import list_emails
from hestia.launch import send_trial_ending_nudges
from hestia.subscriptions import apply_plan
from hestia.tenants import create_tenant, create_user


def _trialing_studio(conn, *, name, email, days_into_trial=0):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    create_user(conn, tenant_id=t["id"], email=email, password="pw12345678",
                role="owner", verified=1)
    apply_plan(conn, t["id"], plan="studio", status="trialing")
    if days_into_trial:
        conn.execute(
            "UPDATE subscriptions SET created_at = datetime('now', ?) WHERE tenant_id = ?",
            (f"-{int(days_into_trial)} days", t["id"]),
        )
    conn.commit()
    return t


def test_ending_trial_gets_exactly_one_nudge(conn, settings):
    t = _trialing_studio(conn, name="Closing Studio", email="close@x.test",
                         days_into_trial=12)          # 14-day trial → 2 days left
    assert send_trial_ending_nudges(conn, settings) == 1
    conn.commit()
    sent = [e for e in list_emails(conn, t["id"]) if e["to_addr"] == "close@x.test"]
    assert len(sent) == 1
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'launch.nudge_sent' "
        "AND tenant_id = ?", (t["id"],),
    ).fetchone()
    assert row["n"] == 1                              # the shared cooldown ledger row
    # second sweep inside the cooldown window sends nothing more
    assert send_trial_ending_nudges(conn, settings) == 0
    conn.commit()
    assert len([e for e in list_emails(conn, t["id"])
                if e["to_addr"] == "close@x.test"]) == 1


def test_fresh_trials_and_non_trialing_are_skipped(conn, settings):
    _trialing_studio(conn, name="Fresh Studio", email="fresh@x.test",
                     days_into_trial=2)               # 12 days left → not yet
    active = create_tenant(conn, name="Paid Studio", shoot_type="portrait")
    create_user(conn, tenant_id=active["id"], email="paid@x.test", password="pw12345678",
                role="owner", verified=1)
    apply_plan(conn, active["id"], plan="studio", status="active")
    conn.commit()
    assert send_trial_ending_nudges(conn, settings) == 0
    conn.commit()
    assert list_emails(conn, active["id"]) == []


def test_manual_nudge_blocks_the_sweep(conn, settings):
    """A founder who already clicked the admin nudge is inside the cooldown — the
    automatic sweep must not double-send."""
    from hestia.db import audit
    from hestia.launch import send_beta_launch_nudge
    t = _trialing_studio(conn, name="Clicked Studio", email="clicked@x.test",
                         days_into_trial=13)          # 1 day left
    result = send_beta_launch_nudge(conn, settings, t["id"])   # the manual button path
    audit(conn, actor="admin", action="launch.nudge_sent", tenant_id=t["id"],
          detail=result["owner_email"])
    conn.commit()
    assert send_trial_ending_nudges(conn, settings) == 0       # cooldown honored
    conn.commit()
    assert len([e for e in list_emails(conn, t["id"])
                if e["to_addr"] == "clicked@x.test"]) == 1
