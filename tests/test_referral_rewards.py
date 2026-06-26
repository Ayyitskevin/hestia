"""Referral rewards — a referred lead that books pays its referrer a credit."""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client, create_project, set_project_status
from hestia.db import connect
from hestia.referral_rewards import (
    award_referral_credit,
    credit_balance,
    list_credits,
    redeem_credit,
)
from hestia.tenants import create_tenant


def _setup(conn, *, reward=5000):
    t = create_tenant(conn, name="Rewards Studio", shoot_type="wedding")
    if reward != 5000:
        conn.execute("UPDATE tenants SET referral_reward_cents = ? WHERE id = ?", (reward, t["id"]))
    referrer = create_client(conn, tenant_id=t["id"], name="Referrer")
    proj = create_project(conn, tenant_id=t["id"], name="Referred wedding",
                          client_id=None, shoot_type="wedding", status="lead")
    conn.execute("UPDATE projects SET referred_by_client_id = ? WHERE id = ?",
                 (referrer["id"], proj["id"]))
    conn.commit()
    return t, referrer, proj


# --- module logic -----------------------------------------------------------

def test_award_is_idempotent(conn):
    t, ref, proj = _setup(conn)
    assert award_referral_credit(conn, t["id"], proj["id"])          # credited
    assert credit_balance(conn, t["id"], ref["id"]) == 5000
    # a second booking of the same project must NOT double-credit
    assert award_referral_credit(conn, t["id"], proj["id"]) is None
    assert credit_balance(conn, t["id"], ref["id"]) == 5000


def test_no_credit_without_a_referrer(conn):
    t = create_tenant(conn, name="Organic Studio", shoot_type="wedding")
    proj = create_project(conn, tenant_id=t["id"], name="Walk-in",
                          client_id=None, shoot_type="wedding", status="lead")
    conn.commit()
    assert award_referral_credit(conn, t["id"], proj["id"]) is None


def test_no_credit_when_reward_disabled(conn):
    t, ref, proj = _setup(conn, reward=0)
    assert award_referral_credit(conn, t["id"], proj["id"]) is None
    assert credit_balance(conn, t["id"], ref["id"]) == 0


def test_booking_awards_via_the_status_hook(conn):
    # the domain hook: moving a referred project to 'booked' grants the credit
    t, ref, proj = _setup(conn)
    set_project_status(conn, t["id"], proj["id"], "booked")
    conn.commit()
    assert credit_balance(conn, t["id"], ref["id"]) == 5000


def test_redeem_is_idempotent(conn):
    t, ref, proj = _setup(conn)
    award_referral_credit(conn, t["id"], proj["id"])
    cid = list_credits(conn, t["id"], ref["id"])[0]["id"]
    assert redeem_credit(conn, t["id"], cid) is True
    assert credit_balance(conn, t["id"], ref["id"]) == 0      # redeemed no longer counts
    assert redeem_credit(conn, t["id"], cid) is False         # second click is a no-op
    assert list_credits(conn, t["id"], ref["id"])[0]["status"] == "redeemed"


# --- HTTP flow --------------------------------------------------------------

def test_http_booking_credits_then_redeem(client, app):
    creds = onboard_studio(client, email="rewards@example.com")
    login_owner(client, creds)
    db = app.state.settings.db_path
    conn = connect(db)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        referrer = create_client(conn, tenant_id=tid, name="Sender")
        proj = create_project(conn, tenant_id=tid, name="Referred",
                              client_id=None, shoot_type="wedding", status="lead")
        conn.execute("UPDATE projects SET referred_by_client_id = ? WHERE id = ?",
                     (referrer["id"], proj["id"]))
        conn.commit()
        rid, pid = referrer["id"], proj["id"]
    finally:
        conn.close()

    client.post(f"/projects/{pid}/status", data={"status": "booked"})  # books → awards
    page = client.get(f"/clients/{rid}")
    assert "Referral credit" in page.text and "$50.00" in page.text

    conn = connect(db)
    try:
        cid = conn.execute("SELECT id FROM referral_credits LIMIT 1").fetchone()["id"]
    finally:
        conn.close()
    client.post(f"/clients/{rid}/credits/{cid}/redeem")
    assert "redeemed" in client.get(f"/clients/{rid}").text
