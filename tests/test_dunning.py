"""Dunning ladder — escalating, capped overdue reminders (gentle → final notice)."""

from hestia.crm import create_client
from hestia.invoices import (
    MAX_DUNNING_REMINDERS,
    create_invoice,
    send_invoice,
    send_overdue_reminders,
)
from hestia.tenants import create_tenant

PAST = "2020-01-01"


def _overdue(conn, settings, tid, cid, cents=15000):
    inv = create_invoice(conn, settings, tenant_id=tid, title="Late", amount_cents=cents,
                         client_id=cid, due_date=PAST)
    send_invoice(conn, tid, inv["id"])
    return inv


def _last_subject(conn):
    return conn.execute("SELECT subject FROM emails ORDER BY id DESC LIMIT 1").fetchone()["subject"]


def _backdate(conn, days):
    conn.execute("UPDATE invoices SET last_reminder_at = datetime('now', ?)", (f"-{days} days",))
    conn.commit()


def test_ladder_caps_at_three_with_final_notice(conn, settings):
    t = create_tenant(conn, name="Dun", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Late", email="late@x.com")
    _overdue(conn, settings, t["id"], c["id"])
    conn.commit()

    subjects = []
    for _ in range(5):                                   # sweep well past the cap
        if send_overdue_reminders(conn, settings):
            subjects.append(_last_subject(conn))
        _backdate(conn, 60)                              # force the next step's gap to elapse

    assert len(subjects) == MAX_DUNNING_REMINDERS == 3   # capped — no perpetual nagging
    assert "Final notice" not in subjects[0]             # first is the gentle past-due nudge
    assert "Final notice" in subjects[-1]                # last step escalates the tone
    cnt = conn.execute("SELECT reminder_count FROM invoices WHERE tenant_id = ?",
                       (t["id"],)).fetchone()["reminder_count"]
    assert cnt == 3


def test_ladder_gaps_widen_each_step(conn, settings):
    t = create_tenant(conn, name="Gap", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="L", email="l@x.com")
    _overdue(conn, settings, t["id"], c["id"])
    conn.commit()

    # 1st reminder fires as soon as it's overdue
    assert send_overdue_reminders(conn, settings, cooldown_days=7) == 1
    conn.commit()
    # 2nd needs a 7-day gap (1 × cooldown); 8 days elapsed → fires
    _backdate(conn, 8)
    assert send_overdue_reminders(conn, settings, cooldown_days=7) == 1
    conn.commit()
    # 3rd needs a 14-day gap (2 × cooldown); 8 days is NOT enough → held
    _backdate(conn, 8)
    assert send_overdue_reminders(conn, settings, cooldown_days=7) == 0
    # …but 15 days clears it
    _backdate(conn, 15)
    assert send_overdue_reminders(conn, settings, cooldown_days=7) == 1


def test_ladder_stops_when_paid_midway(conn, settings):
    t = create_tenant(conn, name="Paid", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="P", email="p@x.com")
    inv = _overdue(conn, settings, t["id"], c["id"])
    conn.commit()
    assert send_overdue_reminders(conn, settings) == 1           # 1st nudge
    conn.execute("UPDATE invoices SET status = 'paid' WHERE id = ?", (inv["id"],))
    conn.commit()
    _backdate(conn, 60)
    assert send_overdue_reminders(conn, settings) == 0           # paid → ladder halts
