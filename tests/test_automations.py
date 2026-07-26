"""Automations — event emission, durable execution, rendering, isolation, CRUD."""

import dataclasses
import json
import re

from conftest import login_owner, onboard_studio

from hestia.automations import (
    create_automation,
    emit_event,
    list_runs,
    set_automation_enabled,
)
from hestia.contracts import create_contract, send_contract, sign_contract
from hestia.crm import assign_gallery_to_project, create_client, create_project, set_project_status
from hestia.email import list_emails
from hestia.galleries import create_gallery, publish_gallery
from hestia.invoices import create_invoice, mark_paid
from hestia.jobs import drain, enqueue
from hestia.tenants import create_tenant


def _tenant(conn, name="Hearth Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def test_create_validates_trigger(conn):
    t = _tenant(conn)
    assert create_automation(conn, tenant_id=t["id"], name="x", trigger="nope.event",
                             subject="s", body="b") is None
    assert create_automation(
        conn,
        tenant_id=t["id"],
        name="wrong-placeholder",
        trigger="contract.signed",
        subject="Gallery ready",
        body="Review: {gallery_url}",
    ) is None
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


def test_gallery_published_renders_current_tenant_proofing_url(conn, settings):
    settings = dataclasses.replace(settings, public_url="https://app.hestia.example/base/")
    tenant = _tenant(conn, "Gallery Studio")
    client = create_client(
        conn,
        tenant_id=tenant["id"],
        name="Sarah",
        email="sarah@example.com",
    )
    project = create_project(
        conn,
        tenant_id=tenant["id"],
        name="Seaside Wedding",
        client_id=client["id"],
    )
    gallery = create_gallery(conn, tenant_id=tenant["id"], title="Seaside Proofs")
    assign_gallery_to_project(conn, tenant["id"], gallery["id"], project["id"])
    create_automation(
        conn,
        tenant_id=tenant["id"],
        name="proof-ready",
        trigger="gallery.published",
        subject="Your proofing gallery",
        body="Review and choose: {gallery_url}",
    )
    conn.commit()

    assert publish_gallery(conn, tenant["id"], gallery["id"]) is True
    job = conn.execute(
        "SELECT payload_json FROM jobs WHERE tenant_id = ? AND kind = 'automation.run'",
        (tenant["id"],),
    ).fetchone()
    context = json.loads(job["payload_json"])["context"]
    assert context == {
        "gallery_id": gallery["id"],
        "project_id": project["id"],
        "title": gallery["title"],
    }
    assert {"url", "slug", "pin", "token"}.isdisjoint(context)

    current_client = create_client(
        conn,
        tenant_id=tenant["id"],
        name="Alex",
        email="alex@example.com",
    )
    current_project = create_project(
        conn,
        tenant_id=tenant["id"],
        name="Current Project",
        client_id=current_client["id"],
    )
    assign_gallery_to_project(
        conn,
        tenant["id"],
        gallery["id"],
        current_project["id"],
    )
    conn.commit()

    drain(settings.db_path, settings)
    sent = [m for m in list_emails(conn, tenant["id"]) if m["subject"] == "Your proofing gallery"]
    assert [m["to_addr"] for m in sent] == ["alex@example.com"]
    assert [m["body"] for m in sent] == [
        f"Review and choose: https://app.hestia.example/base/g/{tenant['slug']}/{gallery['slug']}"
    ]
    assert not any(m["to_addr"] == "sarah@example.com" for m in list_emails(conn, tenant["id"]))

    assert publish_gallery(conn, tenant["id"], gallery["id"]) is False
    conn.commit()
    drain(settings.db_path, settings)
    sent = [m for m in list_emails(conn, tenant["id"]) if m["subject"] == "Your proofing gallery"]
    assert len(sent) == 1


def test_gallery_published_skips_after_current_project_is_cleared(conn, settings):
    tenant = _tenant(conn, "Gallery Studio")
    client = create_client(
        conn,
        tenant_id=tenant["id"],
        name="Sarah",
        email="sarah@example.com",
    )
    project = create_project(
        conn,
        tenant_id=tenant["id"],
        name="Seaside Wedding",
        client_id=client["id"],
    )
    gallery = create_gallery(conn, tenant_id=tenant["id"], title="Seaside Proofs")
    assign_gallery_to_project(conn, tenant["id"], gallery["id"], project["id"])
    create_automation(
        conn,
        tenant_id=tenant["id"],
        name="proof-ready",
        trigger="gallery.published",
        subject="Your proofing gallery",
        body="Ready for review.",
    )
    conn.commit()

    assert publish_gallery(conn, tenant["id"], gallery["id"]) is True
    assign_gallery_to_project(conn, tenant["id"], gallery["id"], None)
    conn.commit()

    drain(settings.db_path, settings)
    assert list_emails(conn, tenant["id"]) == []
    runs = list_runs(conn, tenant["id"])
    assert [(run["status"], run["detail"]) for run in runs] == [
        ("skipped", "no client email on file")
    ]


def test_gallery_url_placeholder_fails_closed_for_draft_or_foreign_gallery(conn, settings):
    owner = _tenant(conn, "Owner Studio")
    other = _tenant(conn, "Other Studio")
    client = create_client(
        conn,
        tenant_id=owner["id"],
        name="Sarah",
        email="sarah@example.com",
    )
    project = create_project(
        conn,
        tenant_id=owner["id"],
        name="Owner Project",
        client_id=client["id"],
    )
    draft = create_gallery(conn, tenant_id=owner["id"], title="Owner Draft")
    foreign = create_gallery(conn, tenant_id=other["id"], title="Foreign Published")
    assert publish_gallery(conn, other["id"], foreign["id"]) is True
    create_automation(
        conn,
        tenant_id=owner["id"],
        name="proof-ready",
        trigger="gallery.published",
        subject="Proof link",
        body="Link: {gallery_url}",
    )
    emit_event(
        conn,
        tenant_id=owner["id"],
        event="gallery.published",
        context={"gallery_id": draft["id"], "project_id": project["id"]},
    )
    emit_event(
        conn,
        tenant_id=owner["id"],
        event="gallery.published",
        context={"gallery_id": foreign["id"], "project_id": project["id"]},
    )
    conn.commit()

    drain(settings.db_path, settings)
    sent = [m for m in list_emails(conn, owner["id"]) if m["subject"] == "Proof link"]
    assert sent == []
    runs = list_runs(conn, owner["id"])
    assert len(runs) == 2
    assert {r["status"] for r in runs} == {"skipped"}
    assert {r["detail"] for r in runs} == {"gallery unavailable for automation"}


def test_gallery_published_rejects_malformed_durable_gallery_ids(conn, settings):
    tenant = _tenant(conn, "Gallery Studio")
    client = create_client(
        conn,
        tenant_id=tenant["id"],
        name="Sarah",
        email="sarah@example.com",
    )
    project = create_project(
        conn,
        tenant_id=tenant["id"],
        name="Seaside Wedding",
        client_id=client["id"],
    )
    gallery = create_gallery(conn, tenant_id=tenant["id"], title="Seaside Proofs")
    assign_gallery_to_project(conn, tenant["id"], gallery["id"], project["id"])
    assert publish_gallery(conn, tenant["id"], gallery["id"]) is True
    automation = create_automation(
        conn,
        tenant_id=tenant["id"],
        name="proof-ready",
        trigger="gallery.published",
        subject="Your proofing gallery",
        body="Review: {gallery_url}",
    )
    for malformed_id in (True, float(gallery["id"]), "１", "9" * 5000):
        emit_event(
            conn,
            tenant_id=tenant["id"],
            event="gallery.published",
            context={"gallery_id": malformed_id, "project_id": project["id"]},
        )
    enqueue(
        conn,
        kind="automation.run",
        tenant_id=tenant["id"],
        payload={
            "automation_id": automation["id"],
            "event": "gallery.published",
            "context": [],
        },
    )
    conn.commit()

    drain(settings.db_path, settings)
    assert list_emails(conn, tenant["id"]) == []
    runs = list_runs(conn, tenant["id"])
    assert len(runs) == 5
    assert {run["status"] for run in runs} == {"skipped"}
    assert {run["detail"] for run in runs} == {
        "gallery unavailable for automation",
        "queued gallery authority is missing",
    }


def test_gallery_published_legacy_job_uses_current_queued_project_client(conn, settings):
    tenant = _tenant(conn, "Gallery Studio")
    former_client = create_client(
        conn,
        tenant_id=tenant["id"],
        name="Former Client",
        email="former@example.com",
    )
    project = create_project(
        conn,
        tenant_id=tenant["id"],
        name="Former Project",
        client_id=former_client["id"],
    )
    current_client = create_client(
        conn,
        tenant_id=tenant["id"],
        name="Current Client",
        email="current@example.com",
    )
    create_automation(
        conn,
        tenant_id=tenant["id"],
        name="legacy-rebook",
        trigger="gallery.published",
        subject="It's been a year",
        body="Would you like to book again?",
    )
    emit_event(
        conn,
        tenant_id=tenant["id"],
        event="gallery.published",
        context={
            "project_id": project["id"],
            "client_id": former_client["id"],
            "title": "Legacy Gallery",
        },
    )
    conn.execute(
        "UPDATE projects SET client_id = ? WHERE id = ? AND tenant_id = ?",
        (current_client["id"], project["id"], tenant["id"]),
    )
    conn.commit()

    drain(settings.db_path, settings)
    sent = list_emails(conn, tenant["id"])
    assert [message["to_addr"] for message in sent] == ["current@example.com"]
    assert [(run["status"], run["detail"]) for run in list_runs(conn, tenant["id"])] == [
        ("sent", "emailed current@example.com")
    ]


def test_automation_delivery_pause_preserves_job_until_enabled(conn, settings):
    paused_settings = dataclasses.replace(settings, automation_email_enabled=False)
    tenant = _tenant(conn, "Paused Studio")
    client = create_client(
        conn,
        tenant_id=tenant["id"],
        name="Sarah",
        email="sarah@example.com",
    )
    project = create_project(
        conn,
        tenant_id=tenant["id"],
        name="Seaside Wedding",
        client_id=client["id"],
    )
    create_automation(
        conn,
        tenant_id=tenant["id"],
        name="booking-ready",
        trigger="project.booked",
        subject="Welcome",
        body="Welcome, {client_name}.",
    )
    conn.commit()
    set_project_status(conn, tenant["id"], project["id"], "booked")
    conn.commit()

    assert drain(paused_settings.db_path, paused_settings) == 0
    assert list_emails(conn, tenant["id"]) == []
    assert list_runs(conn, tenant["id"]) == []
    job = conn.execute(
        "SELECT status, attempts, last_error, started_at FROM jobs WHERE tenant_id = ?",
        (tenant["id"],),
    ).fetchone()
    assert dict(job) == {
        "status": "queued",
        "attempts": 0,
        "last_error": None,
        "started_at": None,
    }

    assert drain(settings.db_path, settings) == 1
    assert [message["to_addr"] for message in list_emails(conn, tenant["id"])] == [
        "sarah@example.com"
    ]
    assert [run["status"] for run in list_runs(conn, tenant["id"])] == ["sent"]


def test_gallery_published_records_delivery_failure_without_leaking_error(conn, settings):
    smtp_settings = dataclasses.replace(settings, email_backend="smtp", smtp_host="")
    tenant = _tenant(conn, "Gallery Studio")
    client = create_client(
        conn,
        tenant_id=tenant["id"],
        name="Sarah",
        email="sarah@example.com",
    )
    project = create_project(
        conn,
        tenant_id=tenant["id"],
        name="Seaside Wedding",
        client_id=client["id"],
    )
    gallery = create_gallery(conn, tenant_id=tenant["id"], title="Seaside Proofs")
    assign_gallery_to_project(conn, tenant["id"], gallery["id"], project["id"])
    create_automation(
        conn,
        tenant_id=tenant["id"],
        name="proof-ready",
        trigger="gallery.published",
        subject="Your proofing gallery",
        body="Review: {gallery_url}",
    )
    conn.commit()

    assert publish_gallery(conn, tenant["id"], gallery["id"]) is True
    conn.commit()
    drain(smtp_settings.db_path, smtp_settings)

    emails = list_emails(conn, tenant["id"])
    assert len(emails) == 1
    assert emails[0]["status"].startswith("error:")
    runs = list_runs(conn, tenant["id"])
    assert [(run["status"], run["detail"]) for run in runs] == [
        ("failed", "email delivery failed; manual review required")
    ]
    job = conn.execute(
        "SELECT status, attempts, last_error FROM jobs WHERE tenant_id = ?",
        (tenant["id"],),
    ).fetchone()
    assert dict(job) == {
        "status": "error",
        "attempts": 1,
        "last_error": "email delivery failed; manual review required",
    }


def test_non_gallery_delivery_failure_is_also_terminal(conn, settings):
    smtp_settings = dataclasses.replace(settings, email_backend="smtp", smtp_host="")
    tenant = _tenant(conn, "Booking Studio")
    client = create_client(
        conn,
        tenant_id=tenant["id"],
        name="Sarah",
        email="sarah@example.com",
    )
    project = create_project(
        conn,
        tenant_id=tenant["id"],
        name="Seaside Wedding",
        client_id=client["id"],
    )
    create_automation(
        conn,
        tenant_id=tenant["id"],
        name="booking-ready",
        trigger="project.booked",
        subject="Welcome",
        body="Welcome, {client_name}.",
    )
    conn.commit()

    set_project_status(conn, tenant["id"], project["id"], "booked")
    conn.commit()
    drain(smtp_settings.db_path, smtp_settings)

    assert [(run["status"], run["detail"]) for run in list_runs(conn, tenant["id"])] == [
        ("failed", "email delivery failed; manual review required")
    ]
    job = conn.execute(
        "SELECT status, attempts, last_error FROM jobs WHERE tenant_id = ?",
        (tenant["id"],),
    ).fetchone()
    assert dict(job) == {
        "status": "error",
        "attempts": 1,
        "last_error": "email delivery failed; manual review required",
    }


def test_gallery_url_is_rejected_for_non_gallery_trigger(conn, settings):
    tenant = _tenant(conn, "Gallery Studio")
    client = create_client(
        conn,
        tenant_id=tenant["id"],
        name="Sarah",
        email="sarah@example.com",
    )
    project = create_project(
        conn,
        tenant_id=tenant["id"],
        name="Seaside Wedding",
        client_id=client["id"],
    )
    gallery = create_gallery(conn, tenant_id=tenant["id"], title="Seaside Proofs")
    assign_gallery_to_project(conn, tenant["id"], gallery["id"], project["id"])
    assert publish_gallery(conn, tenant["id"], gallery["id"]) is True
    crafted = create_automation(
        conn,
        tenant_id=tenant["id"],
        name="crafted-rule",
        trigger="project.booked",
        subject="Unexpected proof link",
        body="Review the project.",
    )
    conn.execute(
        "UPDATE automations SET body = 'Review: {gallery_url}' WHERE id = ?",
        (crafted["id"],),
    )
    emit_event(
        conn,
        tenant_id=tenant["id"],
        event="project.booked",
        context={"gallery_id": gallery["id"], "project_id": project["id"]},
    )
    conn.commit()

    drain(settings.db_path, settings)
    assert list_emails(conn, tenant["id"]) == []
    runs = list_runs(conn, tenant["id"])
    assert [(run["status"], run["detail"]) for run in runs] == [
        ("skipped", "gallery_url unavailable for automation trigger")
    ]


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


def test_http_pages_disclose_paused_delivery(client, app):
    creds = onboard_studio(client, email="paused-auto@example.com")
    login_owner(client, creds)
    app.state.settings = dataclasses.replace(
        app.state.settings,
        automation_email_enabled=False,
    )

    assert "Automation email delivery is paused" in client.get("/automations").text
    assert "Delivery is paused" in client.get("/automations/new").text


def test_http_create_toggle_delete(client):
    creds = onboard_studio(client, email="auto@example.com")
    login_owner(client, creds)
    assert "{gallery_url}" in client.get("/automations/new").text
    invalid = client.post(
        "/automations",
        data={
            "name": "Wrong trigger",
            "trigger": "contract.signed",
            "subject": "Gallery ready",
            "body": "Review: {gallery_url}",
            "action": "email_client",
        },
        follow_redirects=False,
    )
    assert invalid.status_code == 303
    assert invalid.headers["location"] == "/automations/new?error=invalid"
    assert "Wrong trigger" not in client.get("/automations").text
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
