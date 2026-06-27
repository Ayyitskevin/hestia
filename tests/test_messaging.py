"""Customizable email templates — defaults, per-tenant overrides, safe variable fill,
and the wired client emails (appointment confirm, invoice send)."""

from conftest import login_owner, onboard_studio

from hestia import messaging
from hestia.contracts import (
    create_contract,
    get_contract,
    send_contract,
    send_contract_reminder,
)
from hestia.crm import create_client
from hestia.email import list_emails
from hestia.questionnaires import (
    create_questionnaire,
    get_questionnaire,
    send_questionnaire_reminder,
)
from hestia.scheduler import _notify, create_appointment
from hestia.tenants import create_tenant

# ── module ───────────────────────────────────────────────────────────────────


def test_render_uses_default(conn):
    t = create_tenant(conn, name="Studio", shoot_type="wedding")
    msg = messaging.render(conn, t["id"], "invoice_send",
                           {"client": "Sam", "studio": "Studio", "title": "Balance",
                            "amount": "$500.00", "pay_url": "/pay/x", "note": ""})
    assert msg["subject"] == "Studio: invoice for Balance ($500.00)"
    assert "Hi Sam," in msg["body"] and "/pay/x" in msg["body"]


def test_render_uses_custom_override(conn):
    t = create_tenant(conn, name="Studio", shoot_type="wedding")
    messaging.set_template(conn, t["id"], "invoice_send",
                           subject="Your invoice, {client}", body="Pay {amount}: {pay_url}")
    msg = messaging.render(conn, t["id"], "invoice_send",
                           {"client": "Sam", "amount": "$500.00", "pay_url": "/pay/x"})
    assert msg["subject"] == "Your invoice, Sam"
    assert msg["body"] == "Pay $500.00: /pay/x"


def test_fill_leaves_unknown_tokens(conn):
    t = create_tenant(conn, name="S", shoot_type="wedding")
    messaging.set_template(conn, t["id"], "invoice_send", subject="{client} {bogus}", body="x")
    msg = messaging.render(conn, t["id"], "invoice_send", {"client": "Sam"})
    assert msg["subject"] == "Sam {bogus}"                       # unknown token preserved, no crash


def test_set_blank_resets_to_default(conn):
    t = create_tenant(conn, name="S", shoot_type="wedding")
    messaging.set_template(conn, t["id"], "invoice_send", subject="Custom", body="Custom")
    assert messaging.get_template(conn, t["id"], "invoice_send")["subject"] == "Custom"
    messaging.set_template(conn, t["id"], "invoice_send", subject="", body="")   # blank → reset
    assert messaging.get_template(conn, t["id"], "invoice_send")["subject"] \
        == messaging.TEMPLATES["invoice_send"]["subject"]


def test_list_templates_flags_customized(conn):
    t = create_tenant(conn, name="S", shoot_type="wedding")
    messaging.set_template(conn, t["id"], "appointment_confirm", subject="Hi", body="There")
    by_kind = {x["kind"]: x for x in messaging.list_templates(conn, t["id"])}
    assert by_kind["appointment_confirm"]["customized"] is True
    assert by_kind["invoice_send"]["customized"] is False


def test_templates_are_tenant_scoped(conn):
    a = create_tenant(conn, name="A", shoot_type="wedding")
    b = create_tenant(conn, name="B", shoot_type="wedding")
    messaging.set_template(conn, a["id"], "invoice_send", subject="A only", body="x")
    assert messaging.get_template(conn, b["id"], "invoice_send")["subject"] \
        == messaging.TEMPLATES["invoice_send"]["subject"]       # B sees the default


# ── wired emails ─────────────────────────────────────────────────────────────


def test_invoice_send_uses_custom_template(client, conn):
    login_owner(client, onboard_studio(client, name="Lux", email="owner@lux.com"))
    tid = conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]
    messaging.set_template(conn, tid, "invoice_send",
                           subject="Invoice from Lux for {title}",
                           body="Hey {client}, please pay {amount} at {pay_url}. — Lux")
    conn.commit()
    client.post("/clients", data={"name": "Pat", "email": "pat@lux.com"})
    cid = conn.execute("SELECT id FROM clients WHERE email='pat@lux.com'").fetchone()["id"]
    r = client.post("/invoices", data={"title": "Wedding", "amount": "2500", "client_id": str(cid)})
    iid = int(str(r.url).rstrip("/").split("/")[-1])
    client.post(f"/invoices/{iid}/send")

    row = conn.execute("SELECT subject, body FROM emails WHERE to_addr='pat@lux.com'").fetchone()
    assert row["subject"] == "Invoice from Lux for Wedding"
    assert "Hey Pat, please pay $2,500.00 at" in row["body"] and "/pay/" in row["body"]


def test_appointment_confirm_uses_custom_template(conn, settings):
    t = create_tenant(conn, name="Sch", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Sam", email="sam@ex.com")
    appt = create_appointment(conn, tenant_id=t["id"], title="Engagement", options=["x"],
                              client_id=c["id"])
    conn.execute("UPDATE appointments SET status='confirmed', starts_at='2030-01-01 10:00' "
                 "WHERE id=?", (appt["id"],))
    messaging.set_template(conn, t["id"], "appointment_confirm",
                           subject="See you for {title}!", body="{client}, you're booked for {when}.")
    conn.commit()
    _notify(settings, {"appointment_id": appt["id"], "kind": "confirm"})
    row = [m for m in list_emails(conn, t["id"]) if m["to_addr"] == "sam@ex.com"][0]
    assert row["subject"] == "See you for Engagement!"
    assert row["body"].startswith("Sam, you're booked for 2030-01-01 10:00.")


# ── settings editor ──────────────────────────────────────────────────────────


def test_messages_page_save_and_reset(client, conn):
    login_owner(client, onboard_studio(client, email="msg@owner.com"))
    tid = conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]
    page = client.get("/settings/messages")
    assert page.status_code == 200 and "Session confirmed" in page.text and "{client}" in page.text

    client.post("/settings/messages/invoice_send",
                data={"subject": "My invoice {title}", "body": "Pay {amount}"})
    assert messaging.get_template(conn, tid, "invoice_send")["subject"] == "My invoice {title}"
    assert "customized" in client.get("/settings/messages").text

    client.post("/settings/messages/invoice_send", data={"subject": "", "body": ""})  # reset
    assert messaging.get_template(conn, tid, "invoice_send")["subject"] \
        == messaging.TEMPLATES["invoice_send"]["subject"]


# ── contract & questionnaire templates (slice 2) ─────────────────────────────


def test_all_kinds_registered_in_editor(conn):
    t = create_tenant(conn, name="S", shoot_type="wedding")
    kinds = {x["kind"] for x in messaging.list_templates(conn, t["id"])}
    assert {"appointment_confirm", "appointment_reminder", "invoice_send",
            "contract_send", "contract_reminder",
            "questionnaire_send", "questionnaire_reminder"} <= kinds


def test_contract_send_uses_custom_template(client, conn):
    login_owner(client, onboard_studio(client, name="Lux", email="owner@ct.com"))
    tid = conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]
    messaging.set_template(conn, tid, "contract_send",
                           subject="Sign here, {client}", body="{studio} needs your sig: {sign_url}")
    conn.commit()                                                # release the write lock before HTTP
    client.post("/clients", data={"name": "Pat", "email": "pat@ct.com"})
    cid = conn.execute("SELECT id FROM clients WHERE email='pat@ct.com'").fetchone()["id"]
    rct = client.post("/contracts", data={"title": "Booking", "body": "terms", "client_id": str(cid)})
    contract_id = int(str(rct.url).rstrip("/").split("/")[-1])
    client.post(f"/contracts/{contract_id}/send")
    row = conn.execute("SELECT subject, body FROM emails WHERE to_addr='pat@ct.com'").fetchone()
    assert row["subject"] == "Sign here, Pat"
    assert "needs your sig:" in row["body"] and "/sign/" in row["body"]


def test_contract_reminder_uses_custom_template(conn, settings):
    t = create_tenant(conn, name="Ct", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Sam", email="sam@ct.com")
    ct = create_contract(conn, tenant_id=t["id"], title="Agreement", client_id=c["id"])
    send_contract(conn, t["id"], ct["id"])
    messaging.set_template(conn, t["id"], "contract_reminder",
                           subject="Still need your sig on {title}", body="Sign: {sign_url}")
    conn.commit()
    send_contract_reminder(conn, settings, get_contract(conn, t["id"], ct["id"]))
    row = [m for m in list_emails(conn, t["id"]) if m["to_addr"] == "sam@ct.com"][0]
    assert row["subject"] == "Still need your sig on Agreement" and "/sign/" in row["body"]


def test_questionnaire_send_uses_custom_template(client, conn):
    login_owner(client, onboard_studio(client, name="Q", email="owner@q.com"))
    tid = conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]
    messaging.set_template(conn, tid, "questionnaire_send",
                           subject="Quick Qs for {client}", body="{studio} asks: {fill_url}")
    c = create_client(conn, tenant_id=tid, name="Pat", email="pat@q.com")
    q = create_questionnaire(conn, tenant_id=tid, title="Details", prompts=["Venue?"], client_id=c["id"])
    conn.commit()
    client.post(f"/questionnaires/{q['id']}/send")
    row = conn.execute("SELECT subject, body FROM emails WHERE to_addr='pat@q.com'").fetchone()
    assert row["subject"] == "Quick Qs for Pat" and "/q/" in row["body"]


def test_questionnaire_reminder_uses_custom_template(conn, settings):
    t = create_tenant(conn, name="Qr", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Sam", email="sam@q.com")
    q = create_questionnaire(conn, tenant_id=t["id"], title="Details", prompts=["Venue?"],
                             client_id=c["id"])
    conn.execute("UPDATE questionnaires SET status='sent' WHERE id=?", (q["id"],))
    messaging.set_template(conn, t["id"], "questionnaire_reminder",
                           subject="Still need {title}", body="Fill: {fill_url}")
    conn.commit()
    send_questionnaire_reminder(conn, settings, get_questionnaire(conn, t["id"], q["id"]))
    row = [m for m in list_emails(conn, t["id"]) if m["to_addr"] == "sam@q.com"][0]
    assert row["subject"] == "Still need Details" and "/q/" in row["body"]


# ── live preview (slice 3) ───────────────────────────────────────────────────


def test_list_templates_includes_sample_preview(conn):
    t = create_tenant(conn, name="Lux Studio", shoot_type="wedding")
    by_kind = {x["kind"]: x for x in messaging.list_templates(conn, t["id"], studio="Lux Studio")}
    conf = by_kind["appointment_confirm"]
    assert "Jordan Lee" in conf["preview_body"] and "Summer Session" in conf["preview_body"]
    assert "Lux Studio" in conf["preview_body"]                  # the studio name flows into the sample
    assert "{client}" not in conf["preview_body"]                # no raw tokens left in the preview


def test_preview_reflects_custom_template(conn):
    t = create_tenant(conn, name="S", shoot_type="wedding")
    messaging.set_template(conn, t["id"], "invoice_send",
                           subject="Pay up {client}", body="You owe {amount}.")
    by_kind = {x["kind"]: x for x in messaging.list_templates(conn, t["id"])}
    assert by_kind["invoice_send"]["preview_subject"] == "Pay up Jordan Lee"
    assert by_kind["invoice_send"]["preview_body"] == "You owe $1,500.00."


def test_messages_page_renders_preview(client):
    login_owner(client, onboard_studio(client, name="Preview Studio", email="prev@owner.com"))
    page = client.get("/settings/messages")
    assert page.status_code == 200
    assert "Preview with sample data" in page.text and "Jordan Lee" in page.text


# ── invoice reminder templates (slice 4) ─────────────────────────────────────


def test_invoice_reminder_picks_template_by_due_status(conn, settings):
    from hestia.invoices import create_invoice, get_invoice, send_invoice_reminder
    t = create_tenant(conn, name="Bill", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Sam", email="sam@bill.com")
    messaging.set_template(conn, t["id"], "invoice_reminder",
                           subject="Heads up: {title}", body="Still owe {amount}: {pay_url}")
    messaging.set_template(conn, t["id"], "invoice_overdue",
                           subject="OVERDUE: {title}", body="Pay now {amount}: {pay_url}")
    upcoming = create_invoice(conn, settings, tenant_id=t["id"], title="Soon", amount_cents=10000,
                              client_id=c["id"], due_date="2999-01-01")    # not yet due
    late = create_invoice(conn, settings, tenant_id=t["id"], title="Late", amount_cents=20000,
                          client_id=c["id"], due_date="2000-01-01")        # past due
    conn.commit()

    send_invoice_reminder(conn, settings, get_invoice(conn, t["id"], upcoming["id"]))
    send_invoice_reminder(conn, settings, get_invoice(conn, t["id"], late["id"]))
    subjects = [m["subject"] for m in list_emails(conn, t["id"]) if m["to_addr"] == "sam@bill.com"]
    assert "Heads up: Soon" in subjects        # not-yet-due → invoice_reminder
    assert "OVERDUE: Late" in subjects         # past-due → invoice_overdue
