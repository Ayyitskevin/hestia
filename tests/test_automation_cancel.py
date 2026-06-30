"""Cancellation automations — appointment.canceled is now a hookable trigger (it was already
emitted but couldn't be selected), plus the new prep / win-back retention recipes.
"""

from hestia.automations import TRIGGERS, create_automation, create_from_recipe
from hestia.crm import create_client
from hestia.scheduler import book_appointment, cancel_by_token, create_appointment
from hestia.tenants import create_tenant


def _automation_jobs(conn, tenant_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE tenant_id = ? AND kind = 'automation.run'",
        (tenant_id,),
    ).fetchone()["n"]


def test_appointment_canceled_is_now_a_trigger(conn):
    assert "appointment.canceled" in TRIGGERS
    t = create_tenant(conn, name="Trig", shoot_type="wedding")
    # previously create_automation rejected this trigger (not in TRIGGERS); now it's accepted
    auto = create_automation(conn, tenant_id=t["id"], name="Win-back",
                             trigger="appointment.canceled", subject="s", body="b")
    assert auto is not None and auto["trigger"] == "appointment.canceled"


def test_cancellation_fires_the_hooked_automation(conn, settings):
    t = create_tenant(conn, name="Cancel", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Sam", email="sam@ex.com")
    create_automation(conn, tenant_id=t["id"], name="Win-back", trigger="appointment.canceled",
                      subject="We miss you", body="Reschedule {title}?")
    appt = create_appointment(conn, tenant_id=t["id"], title="Engagement",
                              options=["2030-01-01 10:00"], client_id=c["id"])
    book_appointment(conn, token=appt["token"], option_id=appt["options"][0]["id"])  # → confirmed
    conn.commit()
    assert cancel_by_token(conn, settings, appt["token"]) is True
    assert _automation_jobs(conn, t["id"]) == 1     # the cancellation enqueued the win-back rule


def test_prep_and_winback_recipes(conn):
    t = create_tenant(conn, name="Recipes", shoot_type="wedding")
    prep = create_from_recipe(conn, t["id"], "prep")
    winback = create_from_recipe(conn, t["id"], "winback")
    assert prep and prep["trigger"] == "appointment.confirmed"
    assert winback and winback["trigger"] == "appointment.canceled"
