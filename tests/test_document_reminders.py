"""Automated document reminders — nudge clients sitting on an unsigned contract or
an unfilled questionnaire, on a per-document cooldown. Mirrors the overdue-invoice
sweep: claim-before-send, idempotent, skips the done/no-email cases."""

from hestia.contracts import create_contract, send_contract, send_unsigned_reminders
from hestia.crm import create_client
from hestia.email import list_emails
from hestia.questionnaires import (
    create_questionnaire,
    send_incomplete_reminders,
    send_questionnaire,
)
from hestia.tenants import create_tenant


def _sent_contract(conn, tid, *, title="Wedding agreement", client_id=None, signer_email=""):
    ct = create_contract(conn, tenant_id=tid, title=title, client_id=client_id,
                         signer_email=signer_email)
    send_contract(conn, tid, ct["id"])
    return ct


def _sent_questionnaire(conn, tid, *, title="Wedding details", client_id=None):
    q = create_questionnaire(conn, tenant_id=tid, title=title, prompts=["Venue?"],
                             client_id=client_id)
    send_questionnaire(conn, tid, q["id"])
    return q


# ── contracts ────────────────────────────────────────────────────────────────


def test_unsigned_contract_reminder_sends_sign_link(conn, settings):
    t = create_tenant(conn, name="Ct Studio", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Sam", email="sam@ex.com")
    ct = _sent_contract(conn, t["id"], client_id=c["id"])
    conn.commit()

    assert send_unsigned_reminders(conn, settings) == 1
    msg = [m for m in list_emails(conn, t["id"]) if m["to_addr"] == "sam@ex.com"][0]
    assert "/sign/" in msg["body"] and "sign" in msg["subject"].lower()
    row = conn.execute("SELECT reminder_count, last_reminder_at FROM contracts WHERE id=?",
                       (ct["id"],)).fetchone()
    assert row["reminder_count"] == 1 and row["last_reminder_at"] is not None


def test_unsigned_contract_reminds_named_signer_without_a_client(conn, settings):
    t = create_tenant(conn, name="Signer Studio", shoot_type="wedding")
    _sent_contract(conn, t["id"], signer_email="signer@ex.com")          # no client, just a signer
    conn.commit()
    assert send_unsigned_reminders(conn, settings) == 1
    assert any(m["to_addr"] == "signer@ex.com" for m in list_emails(conn, t["id"]))


def test_unsigned_reminder_respects_cooldown(conn, settings):
    t = create_tenant(conn, name="Cd Studio", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Sam", email="sam@ex.com")
    _sent_contract(conn, t["id"], client_id=c["id"])
    conn.commit()
    assert send_unsigned_reminders(conn, settings) == 1     # first nudge
    assert send_unsigned_reminders(conn, settings) == 0     # within cooldown → no second


def test_unsigned_reminder_skips_signed_and_emailless(conn, settings):
    t = create_tenant(conn, name="Sk Studio", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Sam", email="sam@ex.com")
    signed = _sent_contract(conn, t["id"], client_id=c["id"], title="Signed")
    conn.execute("UPDATE contracts SET status='signed' WHERE id=?", (signed["id"],))
    _sent_contract(conn, t["id"], title="No email")          # no client, no signer email
    conn.commit()
    assert send_unsigned_reminders(conn, settings) == 0


# ── questionnaires ───────────────────────────────────────────────────────────


def test_incomplete_questionnaire_reminder_sends_fill_link(conn, settings):
    t = create_tenant(conn, name="Qz Studio", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Sam", email="sam@ex.com")
    q = _sent_questionnaire(conn, t["id"], client_id=c["id"])
    conn.commit()
    assert send_incomplete_reminders(conn, settings) == 1
    msg = [m for m in list_emails(conn, t["id"]) if m["to_addr"] == "sam@ex.com"][0]
    assert "/q/" in msg["body"]
    assert conn.execute("SELECT reminder_count FROM questionnaires WHERE id=?",
                        (q["id"],)).fetchone()["reminder_count"] == 1


def test_incomplete_reminder_cooldown_and_skips_completed(conn, settings):
    t = create_tenant(conn, name="Qd Studio", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Sam", email="sam@ex.com")
    _sent_questionnaire(conn, t["id"], client_id=c["id"])
    done = _sent_questionnaire(conn, t["id"], client_id=c["id"], title="Done")
    conn.execute("UPDATE questionnaires SET status='completed' WHERE id=?", (done["id"],))
    conn.commit()
    assert send_incomplete_reminders(conn, settings) == 1    # only the still-open one
    assert send_incomplete_reminders(conn, settings) == 0    # within cooldown → no second
