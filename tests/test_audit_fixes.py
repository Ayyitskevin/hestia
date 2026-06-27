"""Regression tests for two bugs caught by the post-run adversarial audit.

1. The client "Messages" panel built its list from list_emails(tenant) — a tenant-wide
   newest-50 — then filtered to the client in Python, so a client's older messages
   vanished once the studio's total email volume passed 50. Fixed by a recipient-scoped
   query (the limit now applies per client).
2. The manual "email me this" digest sent first and stamped after (no claim-before-act),
   so a double-click emailed the owner twice. Fixed with an atomic claim before the send.
"""

from hestia.crm import create_client, create_project
from hestia.dashboard import send_owner_digest_now
from hestia.email import list_emails
from hestia.studio import upsert_profile
from hestia.tenants import create_tenant


def test_client_messages_not_truncated_by_tenant_volume(conn):
    t = create_tenant(conn, name="Busy Studio", shoot_type="wedding")
    conn.execute("INSERT INTO emails (tenant_id, to_addr, subject) VALUES (?, ?, ?)",
                 (t["id"], "target@x.com", "Your very first note"))          # oldest row
    for i in range(60):
        conn.execute("INSERT INTO emails (tenant_id, to_addr, subject) VALUES (?, ?, ?)",
                     (t["id"], f"other{i}@x.com", "blast"))
    conn.commit()
    # the old per-client message falls outside the newest-50 tenant-wide window...
    assert all(e["subject"] != "Your very first note" for e in list_emails(conn, t["id"]))
    # ...but the recipient-scoped query still surfaces it
    scoped = list_emails(conn, t["id"], to_addr="target@x.com")
    assert [e["subject"] for e in scoped] == ["Your very first note"]


def test_to_addr_match_is_case_insensitive(conn):
    t = create_tenant(conn, name="Case Studio", shoot_type="wedding")
    conn.execute("INSERT INTO emails (tenant_id, to_addr, subject) VALUES (?, ?, ?)",
                 (t["id"], "Mixed@Case.com", "hi"))
    conn.commit()
    assert len(list_emails(conn, t["id"], to_addr="mixed@case.com")) == 1


def test_manual_digest_send_is_double_submit_safe(conn, settings):
    t = create_tenant(conn, name="Digest Studio", shoot_type="wedding")
    upsert_profile(conn, tenant_id=t["id"], headline="", about="",
                   contact_email="owner@x.com", published=True)
    c = create_client(conn, tenant_id=t["id"], name="Cli", email="c@x.com")
    create_project(conn, tenant_id=t["id"], name="A lead", client_id=c["id"], status="lead")
    conn.commit()
    first = send_owner_digest_now(conn, settings, t["id"])
    second = send_owner_digest_now(conn, settings, t["id"])     # immediate double-submit
    assert first is not None and second is None                 # claim-before-act blocks #2
    digests = [m for m in list_emails(conn, t["id"]) if "attention" in m["subject"]]
    assert len(digests) == 1
