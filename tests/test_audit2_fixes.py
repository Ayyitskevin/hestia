"""Regression tests for the three bugs caught by the second adversarial audit (#103-#112).

1. _to_cents crashed on a huge-but-finite amount (1e308) — isfinite was checked before
   the * 100 overflowed to inf. Newly reachable via the line-items parser.
2. A 'blocked' personal time-block leaked into the dashboard "Upcoming sessions" and the
   owner digest (it's not a client session).
3. A block's token was honored by the public booking page / self-cancel.
"""

from conftest import login_owner, onboard_studio

from hestia.dashboard import build_owner_digest, needs_attention
from hestia.routes.invoices import _to_cents as inv_to_cents
from hestia.routes.payment_plans import _to_cents as plan_to_cents
from hestia.scheduler import create_block, get_appointment_by_token
from hestia.tenants import create_tenant


def test_to_cents_survives_overflow_inputs():
    for fn in (inv_to_cents, plan_to_cents):
        assert fn("1e308") == 0          # finite, but * 100 overflows to inf
        assert fn("-1e308") == 0
        assert fn("1e400") == 0          # parses straight to inf
        assert fn("inf") == 0 and fn("nan") == 0
        assert fn("2500") == 250000 and fn("12.50") == 1250    # normal still works


def test_itemized_overflow_amount_does_not_500(client):
    creds = onboard_studio(client, email="ov1@example.com")
    login_owner(client, creds)
    r = client.post("/invoices", data={"title": "X", "items": "Boom | 1e308"})
    assert r.status_code in (200, 303)                          # no unhandled OverflowError
    iid = r.url.path.rstrip("/").split("/")[-1]
    assert client.get(f"/invoices/{iid}").status_code == 200


def test_block_excluded_from_dashboard_and_digest(conn, settings):
    t = create_tenant(conn, name="Block Studio", shoot_type="wedding")
    future = conn.execute("SELECT datetime('now', '+2 days')").fetchone()[0]
    create_block(conn, tenant_id=t["id"], title="Editing day", starts_at=future)
    conn.commit()
    att = needs_attention(conn, t["id"])
    assert att["upcoming"] == [] and att["total"] == 0         # not a client "session"
    assert build_owner_digest(conn, t["id"], settings) is None  # idle studio → no digest


def test_block_token_is_not_bookable(client, conn):
    t = create_tenant(conn, name="Block Studio", shoot_type="wedding")
    b = create_block(conn, tenant_id=t["id"], title="Busy", starts_at="2026-07-01 10:00")
    conn.commit()
    assert get_appointment_by_token(conn, b["token"]) is None  # invisible to the public flow
    assert client.get(f"/book/{b['token']}").status_code == 404
    assert client.post(f"/book/{b['token']}/cancel").status_code == 404
