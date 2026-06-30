"""booking.requested automation — a proposed booking request (no confirmed slot) now fires
a hookable event, so studios can automate a follow-up; plus the booking-follow-up recipe.
"""

from hestia.automations import TRIGGERS, create_automation, create_from_recipe
from hestia.booking import create_booking_type, request_booking
from hestia.tenants import create_tenant


def _automation_jobs(conn, tenant_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE tenant_id = ? AND kind = 'automation.run'",
        (tenant_id,),
    ).fetchone()["n"]


def test_booking_requested_is_a_trigger(conn):
    assert "booking.requested" in TRIGGERS
    t = create_tenant(conn, name="BT", shoot_type="wedding")
    assert create_automation(conn, tenant_id=t["id"], name="Follow up",
                             trigger="booking.requested", subject="s", body="b") is not None


def test_proposed_booking_fires_the_event(conn, settings):
    t = create_tenant(conn, name="Req", shoot_type="wedding")
    bt = create_booking_type(conn, tenant_id=t["id"], title="Engagement")
    create_automation(conn, tenant_id=t["id"], name="Follow up", trigger="booking.requested",
                      subject="Following up on {title}", body="More about {title}?")
    request_booking(conn, settings, tenant=t, booking_type=bt, name="Sam", email="sam@ex.com",
                    requested_at="2030-01-01 10:00")          # proposed (confirm defaults False)
    conn.commit()
    assert _automation_jobs(conn, t["id"]) == 1               # the request enqueued the follow-up


def test_confirmed_booking_does_not_fire_booking_requested(conn, settings):
    t = create_tenant(conn, name="Conf", shoot_type="wedding")
    bt = create_booking_type(conn, tenant_id=t["id"], title="Mini")
    create_automation(conn, tenant_id=t["id"], name="Follow up", trigger="booking.requested",
                      subject="s", body="b")
    request_booking(conn, settings, tenant=t, booking_type=bt, name="Pat", email="pat@ex.com",
                    requested_at="2030-01-01 10:00", confirm=True)   # a confirmed slot
    conn.commit()
    assert _automation_jobs(conn, t["id"]) == 0               # appointment.confirmed fires instead


def test_booking_followup_recipe(conn):
    t = create_tenant(conn, name="Recipe", shoot_type="wedding")
    auto = create_from_recipe(conn, t["id"], "booking_followup")
    assert auto and auto["trigger"] == "booking.requested"
