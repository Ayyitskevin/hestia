"""Owner digest — the dashboard delivered as a periodic email.

Reuses the same needs_attention + reconnect aggregation as the dashboard, sends at most
once per cooldown (claim-before-send, like the reminder sweeps), only when there's
something to report and a recipient to reach. A manual button sends it on demand.
"""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client, create_project
from hestia.dashboard import (
    build_owner_digest,
    owner_digest_recipient,
    send_owner_digest_now,
    send_owner_digests,
)
from hestia.email import list_emails
from hestia.invoices import create_invoice
from hestia.studio import upsert_profile
from hestia.tenants import create_tenant, create_user


def _studio(conn, name="Digest Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def test_recipient_prefers_contact_email_then_owner(conn):
    t = _studio(conn)
    create_user(conn, tenant_id=t["id"], email="Owner@X.com", password="pw")
    conn.commit()
    assert owner_digest_recipient(conn, t["id"]) == "owner@x.com"        # owner login
    upsert_profile(conn, tenant_id=t["id"], headline="", about="",
                   contact_email="hello@studio.com", published=True)
    assert owner_digest_recipient(conn, t["id"]) == "hello@studio.com"   # contact overrides


def test_no_digest_when_nothing_to_report(conn, settings):
    t = _studio(conn)
    assert build_owner_digest(conn, t["id"], settings) is None


def test_digest_lists_actionable_items(conn, settings):
    t = _studio(conn)
    c = create_client(conn, tenant_id=t["id"], name="Jordan Lee", email="j@x.com")
    create_project(conn, tenant_id=t["id"], name="Summer Wedding", client_id=c["id"],
                   shoot_type="wedding", status="lead")
    create_invoice(conn, settings, tenant_id=t["id"], title="Deposit",
                   amount_cents=15000, client_id=c["id"])
    conn.commit()
    digest = build_owner_digest(conn, t["id"], settings)
    assert digest is not None
    assert "Digest Studio" in digest["subject"]
    assert "New leads" in digest["body"] and "Summer Wedding" in digest["body"]
    assert "Unpaid invoices" in digest["body"] and "Deposit" in digest["body"]


def test_send_claims_once_then_cools_down(conn, settings):
    t = _studio(conn)
    upsert_profile(conn, tenant_id=t["id"], headline="", about="",
                   contact_email="owner@x.com", published=True)
    c = create_client(conn, tenant_id=t["id"], name="Cli", email="c@x.com")
    create_project(conn, tenant_id=t["id"], name="A lead", client_id=c["id"], status="lead")
    conn.commit()
    assert send_owner_digests(conn, settings) == 1
    mail = list_emails(conn, t["id"])
    assert any("attention" in m["subject"] and m["to_addr"] == "owner@x.com" for m in mail)
    assert conn.execute("SELECT last_digest_at FROM tenants WHERE id = ?",
                        (t["id"],)).fetchone()["last_digest_at"] is not None
    # within the cooldown → nothing more sent
    assert send_owner_digests(conn, settings) == 0
    assert len(list_emails(conn, t["id"])) == 1


def test_send_skips_idle_studio_without_claiming(conn, settings):
    t = _studio(conn)
    upsert_profile(conn, tenant_id=t["id"], headline="", about="",
                   contact_email="owner@x.com", published=True)   # recipient, but nothing to report
    conn.commit()
    assert send_owner_digests(conn, settings) == 0
    assert list_emails(conn, t["id"]) == []
    # not claimed — revisited as soon as something comes up
    assert conn.execute("SELECT last_digest_at FROM tenants WHERE id = ?",
                        (t["id"],)).fetchone()["last_digest_at"] is None


def test_send_skips_when_no_recipient(conn, settings):
    t = _studio(conn)
    c = create_client(conn, tenant_id=t["id"], name="Cli", email="c@x.com")
    create_project(conn, tenant_id=t["id"], name="A lead", client_id=c["id"], status="lead")
    conn.commit()                                  # content, but no owner user / contact email
    assert send_owner_digests(conn, settings) == 0


def test_digest_is_tenant_scoped(conn, settings):
    a = _studio(conn, "A Studio")
    b = _studio(conn, "B Studio")
    ca = create_client(conn, tenant_id=a["id"], name="A Client", email="a@x.com")
    create_project(conn, tenant_id=a["id"], name="A's lead", client_id=ca["id"], status="lead")
    conn.commit()
    da = build_owner_digest(conn, a["id"], settings)
    assert da and "A's lead" in da["body"]
    assert build_owner_digest(conn, b["id"], settings) is None   # B has nothing


def test_manual_send_now(conn, settings):
    t = _studio(conn)
    create_user(conn, tenant_id=t["id"], email="owner@x.com", password="pw")
    c = create_client(conn, tenant_id=t["id"], name="Cli", email="c@x.com")
    create_project(conn, tenant_id=t["id"], name="A lead", client_id=c["id"], status="lead")
    conn.commit()
    assert send_owner_digest_now(conn, settings, t["id"]) is not None
    assert any(m["to_addr"] == "owner@x.com" for m in list_emails(conn, t["id"]))


def test_dashboard_button_appears_and_sends(client, conn):
    creds = onboard_studio(client, email="dig@example.com")
    login_owner(client, creds)
    # fresh studio: nothing to report → no digest button
    assert "/dashboard/digest" not in client.get("/dashboard").text
    # create a lead so there's something to report
    r = client.post("/clients", data={"name": "Lead Client", "email": "lead@example.com"})
    cid = r.url.path.rstrip("/").split("/")[-1]
    client.post("/projects", data={"name": "New lead", "client_id": cid})
    assert "/dashboard/digest" in client.get("/dashboard").text
    # send it to the owner
    assert client.post("/dashboard/digest").status_code in (200, 303)
    tid = conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]
    assert any(m["to_addr"] == "dig@example.com" for m in list_emails(conn, tid))
