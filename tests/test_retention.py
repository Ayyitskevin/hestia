"""Retention automations — scheduled (delayed) rules on the workflow engine."""

from conftest import login_owner, onboard_studio

from hestia.automations import (
    RETENTION_RECIPES,
    create_automation,
    create_from_recipe,
    emit_event,
    get_automation,
)
from hestia.contracts import create_contract, send_contract, sign_contract
from hestia.crm import create_client
from hestia.email import list_emails
from hestia.jobs import drain
from hestia.tenants import create_tenant


def _tenant(conn, name="Retention Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def _automation_jobs(conn):
    return conn.execute(
        "SELECT run_at FROM jobs WHERE kind = 'automation.run'"
    ).fetchall()


def test_delayed_rule_schedules_into_the_future(conn):
    t = _tenant(conn)
    create_automation(conn, tenant_id=t["id"], name="rebook", trigger="gallery.published",
                      subject="s", body="b", delay_days=365)
    emit_event(conn, tenant_id=t["id"], event="gallery.published", context={})
    jobs = _automation_jobs(conn)
    now = conn.execute("SELECT datetime('now') AS n").fetchone()["n"]
    assert len(jobs) == 1 and jobs[0]["run_at"] > now  # not runnable yet


def test_immediate_rule_unchanged(conn):
    t = _tenant(conn)
    create_automation(conn, tenant_id=t["id"], name="now", trigger="gallery.published",
                      subject="s", body="b", delay_days=0)
    emit_event(conn, tenant_id=t["id"], event="gallery.published", context={})
    now = conn.execute("SELECT datetime('now') AS n").fetchone()["n"]
    # run_at defaults to now → immediately claimable
    assert _automation_jobs(conn)[0]["run_at"] <= now


def test_delayed_email_only_fires_after_its_time(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah", email="sarah@example.com")
    ct = create_contract(conn, tenant_id=t["id"], title="Booking", client_id=c["id"])
    send_contract(conn, t["id"], ct["id"])
    create_automation(conn, tenant_id=t["id"], name="followup", trigger="contract.signed",
                      subject="Following up, {client_name}", body="hello", delay_days=30)
    conn.commit()

    sign_contract(conn, token=ct["token"], signature_name="Sarah")
    conn.commit()
    drain(settings.db_path, settings)  # job is 30 days out → nothing sent yet
    assert list_emails(conn, t["id"]) == []

    # when its time arrives, it sends
    conn.execute("UPDATE jobs SET run_at = datetime('now', '-1 minute') WHERE kind = 'automation.run'")
    conn.commit()
    drain(settings.db_path, settings)
    assert any(m["subject"] == "Following up, Sarah" for m in list_emails(conn, t["id"]))


def test_create_from_recipe(conn):
    t = _tenant(conn)
    auto = create_from_recipe(conn, t["id"], "review")
    assert auto["trigger"] == "invoice.paid" and auto["delay_days"] == 3
    assert "{client_name}" in auto["subject"]
    assert get_automation(conn, t["id"], auto["id"])["name"] == "Review request"
    assert create_from_recipe(conn, t["id"], "nope") is None


def test_recipes_are_valid_triggers():
    from hestia.automations import TRIGGERS
    for r in RETENTION_RECIPES.values():
        assert r["trigger"] in TRIGGERS and r["delay_days"] >= 0


def test_http_create_delayed_and_recipe(client):
    creds = onboard_studio(client, email="ret@example.com")
    login_owner(client, creds)
    # a custom delayed rule
    client.post("/automations", data={
        "name": "Anniversary", "trigger": "gallery.published",
        "subject": "A year!", "body": "hi", "delay_days": "365", "action": "email_client",
    })
    page = client.get("/automations").text
    assert "Anniversary" in page and "+365d" in page
    # a one-click recipe
    client.post("/automations/recipe", data={"key": "review"})
    assert "Review request" in client.get("/automations").text
