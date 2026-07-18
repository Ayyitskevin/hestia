"""Automations — event emission, durable execution, rendering, isolation, CRUD."""

import re

from conftest import login_owner, onboard_studio

from hestia.automations import (
    create_automation,
    emit_event,
    list_runs,
    set_automation_enabled,
)
from hestia.contracts import create_contract, send_contract, sign_contract
from hestia.crm import create_client, create_project, set_project_status
from hestia.email import list_emails
from hestia.invoices import create_invoice, mark_paid
from hestia.jobs import drain
from hestia.tenants import create_tenant


def _tenant(conn, name="Hearth Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def test_create_validates_trigger(conn):
    t = _tenant(conn)
    assert create_automation(conn, tenant_id=t["id"], name="x", trigger="nope.event",
                             subject="s", body="b") is None
    ok = create_automation(conn, tenant_id=t["id"], name="x", trigger="contract.signed",
                           subject="s", body="b")
    assert ok and ok["trigger"] == "contract.signed" and ok["enabled"] == 1


def test_emit_enqueues_only_enabled_matching(conn):
    t = _tenant(conn)
    a = create_automation(conn, tenant_id=t["id"], name="on-sign", trigger="contract.signed",
                          subject="s", body="b")
    create_automation(conn, tenant_id=t["id"], name="on-pay", trigger="invoice.paid",
                      subject="s", body="b")
    # matching + enabled → 1 job
    assert emit_event(conn, tenant_id=t["id"], event="contract.signed", context={}) == 1
    # disable it → 0
    set_automation_enabled(conn, t["id"], a["id"], False)
    assert emit_event(conn, tenant_id=t["id"], event="contract.signed", context={}) == 0
    # unknown event → 0
    assert emit_event(conn, tenant_id=t["id"], event="gallery.published", context={}) == 0


def test_contract_signed_sends_rendered_email(conn, settings):
    t = _tenant(conn, name="Willow & Oak")
    c = create_client(conn, tenant_id=t["id"], name="Sarah", email="sarah@example.com")
    p = create_project(conn, tenant_id=t["id"], name="June Wedding", client_id=c["id"])
    ct = create_contract(conn, tenant_id=t["id"], title="Booking", client_id=c["id"],
                         project_id=p["id"])
    send_contract(conn, t["id"], ct["id"])
    create_automation(conn, tenant_id=t["id"], name="welcome", trigger="contract.signed",
                      subject="Welcome, {client_name}!",
                      body="Thanks for booking {project_name} with {studio_name}.")
    conn.commit()

    sign_contract(conn, token=ct["token"], signature_name="Sarah Smith")
    conn.commit()
    drain(settings.db_path, settings)

    emails = list_emails(conn, t["id"])
    sent = [m for m in emails if m["to_addr"] == "sarah@example.com"]
    assert any(m["subject"] == "Welcome, Sarah!" for m in sent)
    assert any("June Wedding" in m["body"] and "Willow & Oak" in m["body"] for m in sent)
    # the run is recorded as sent
    assert any(r["status"] == "sent" for r in list_runs(conn, t["id"]))


def test_invoice_paid_fires(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah", email="sarah@example.com")
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="Deposit", amount_cents=10000,
                         client_id=c["id"])
    create_automation(conn, tenant_id=t["id"], name="thanks", trigger="invoice.paid",
                      subject="Payment received", body="Thank you, {client_name}.")
    conn.commit()
    mark_paid(conn, token=inv["token"], provider="mock", ref="r1")
    conn.commit()
    drain(settings.db_path, settings)
    assert any("Payment received" == m["subject"] for m in list_emails(conn, t["id"]))


def test_project_booked_resolves_client_from_project(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah", email="sarah@example.com")
    p = create_project(conn, tenant_id=t["id"], name="Engagement", client_id=c["id"])
    create_automation(conn, tenant_id=t["id"], name="booked", trigger="project.booked",
                      subject="You're booked for {project_name}", body="Yay {client_name}!")
    conn.commit()
    set_project_status(conn, t["id"], p["id"], "booked")
    conn.commit()
    drain(settings.db_path, settings)
    assert any("Engagement" in m["subject"] for m in list_emails(conn, t["id"]))


def test_project_booked_identical_retry_does_not_duplicate_client_email(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah", email="sarah@example.com")
    p = create_project(conn, tenant_id=t["id"], name="Wedding", client_id=c["id"])
    create_automation(
        conn,
        tenant_id=t["id"],
        name="welcome",
        trigger="project.booked",
        subject="Welcome to {project_name}",
        body="Yay {client_name}!",
    )
    conn.commit()

    set_project_status(conn, t["id"], p["id"], "booked")
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 1
    set_project_status(conn, t["id"], p["id"], "booked")
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 1
    conn.commit()

    drain(settings.db_path, settings)
    sent = [m for m in list_emails(conn, t["id"]) if m["subject"] == "Welcome to Wedding"]
    assert len(sent) == 1
    assert len(list_runs(conn, t["id"])) == 1


def test_project_booked_effects_follow_real_status_transitions(conn):
    t = _tenant(conn)
    p = create_project(conn, tenant_id=t["id"], name="Wedding")
    create_automation(conn, tenant_id=t["id"], name="welcome", trigger="project.booked",
                      subject="Welcome", body="Welcome")
    conn.commit()

    set_project_status(conn, t["id"], p["id"], "booked")
    set_project_status(conn, t["id"], p["id"], "shooting")
    set_project_status(conn, t["id"], p["id"], "booked")

    assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 2


def test_project_booked_missing_or_foreign_project_emits_nothing(conn):
    owner = _tenant(conn, "Owner")
    other = _tenant(conn, "Other")
    foreign = create_project(conn, tenant_id=other["id"], name="Other project")
    create_automation(conn, tenant_id=owner["id"], name="welcome", trigger="project.booked",
                      subject="Welcome", body="Welcome")
    conn.commit()

    set_project_status(conn, owner["id"], foreign["id"], "booked")
    set_project_status(conn, owner["id"], foreign["id"] + 10_000, "booked")

    assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 0


def test_no_client_email_is_skipped_not_failed(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="No Email")  # no email
    ct = create_contract(conn, tenant_id=t["id"], title="Booking", client_id=c["id"])
    send_contract(conn, t["id"], ct["id"])
    create_automation(conn, tenant_id=t["id"], name="welcome", trigger="contract.signed",
                      subject="hi", body="hi")
    conn.commit()
    sign_contract(conn, token=ct["token"], signature_name="Sarah")
    conn.commit()
    drain(settings.db_path, settings)
    runs = list_runs(conn, t["id"])
    assert runs and runs[0]["status"] == "skipped"
    assert list_emails(conn, t["id"]) == []


def test_tenant_isolation(conn, settings):
    t1 = _tenant(conn, "A")
    t2 = _tenant(conn, "B")
    # t2 has a rule on contract.signed; t1's signed contract must NOT fire it
    create_automation(conn, tenant_id=t2["id"], name="b-rule", trigger="contract.signed",
                      subject="s", body="b")
    c1 = create_client(conn, tenant_id=t1["id"], name="Sarah", email="s@example.com")
    ct = create_contract(conn, tenant_id=t1["id"], title="X", client_id=c1["id"])
    send_contract(conn, t1["id"], ct["id"])
    conn.commit()
    sign_contract(conn, token=ct["token"], signature_name="Sarah")
    conn.commit()
    drain(settings.db_path, settings)
    assert list_runs(conn, t2["id"]) == []
    assert list_emails(conn, t2["id"]) == []


def test_http_create_toggle_delete(client):
    creds = onboard_studio(client, email="auto@example.com")
    login_owner(client, creds)
    # a name unique to this rule (not shared with any retention-recipe UI text)
    client.post("/automations", data={
        "name": "ZephyrSignal", "trigger": "contract.signed",
        "subject": "Hi {client_name}", "body": "Thanks", "action": "email_client",
    })
    page = client.get("/automations")
    assert "ZephyrSignal" in page.text and "Contract signed" in page.text

    # find the automation id from its toggle form action (regex avoids the /new link)
    aid = re.search(r"/automations/(\d+)/toggle", page.text).group(1)
    client.post(f"/automations/{aid}/toggle")
    assert "Enable" in client.get("/automations").text  # now disabled → shows "Enable"
    client.post(f"/automations/{aid}/delete")
    assert "ZephyrSignal" not in client.get("/automations").text


def test_http_end_to_end_through_routes(client, app):
    """A rule created in the UI fires when the client signs via the public route."""
    creds = onboard_studio(client, email="e2e@example.com")
    login_owner(client, creds)
    client.post("/automations", data={
        "name": "Welcome", "trigger": "contract.signed",
        "subject": "Welcome {client_name}", "body": "Thanks from {studio_name}",
        "action": "email_client",
    })
    rc = client.post("/clients", data={"name": "Sarah", "email": "sarah@example.com"})
    cid = rc.url.path.rstrip("/").split("/")[-1]
    rct = client.post("/contracts", data={"title": "Booking", "body": "terms", "client_id": cid})
    ctid = rct.url.path.rstrip("/").split("/")[-1]
    client.post(f"/contracts/{ctid}/send")
    token = client.get(f"/contracts/{ctid}").text.split("/sign/")[1].split('"')[0].split("<")[0].strip()
    client.post(f"/sign/{token}", data={"signature_name": "Sarah Smith", "agree": "yes"})

    drain(app.state.settings.db_path, app.state.settings)

    from hestia.db import connect
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        emails = list_emails(conn, tid)
    finally:
        conn.close()
    assert any(m["subject"] == "Welcome Sarah" for m in emails)
