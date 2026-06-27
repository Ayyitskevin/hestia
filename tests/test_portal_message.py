"""Client portal messaging — an existing client can message the studio from their portal.

Delivered as an owner alert (unsigned). Empty messages and unknown tokens are no-ops.
"""

from conftest import login_owner, onboard_studio

from hestia.email import list_emails


def _portal_token(client, conn, *, name="Pat", email="pat@example.com"):
    r = client.post("/clients", data={"name": name, "email": email})
    cid = r.url.path.rstrip("/").split("/")[-1]
    client.post(f"/clients/{cid}/portal")                       # enable the portal link
    return conn.execute("SELECT portal_token FROM clients WHERE id = ?",
                        (cid,)).fetchone()["portal_token"]


def _tid(conn):
    return conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]


def test_message_emails_the_owner(client, conn):
    creds = onboard_studio(client, name="Lumen Studio", email="pmsg1@example.com")
    login_owner(client, creds)
    tok = _portal_token(client, conn)
    r = client.post(f"/portal/{tok}/message", data={"message": "When is my gallery ready?"})
    assert r.status_code in (200, 303)
    mails = [m for m in list_emails(conn, _tid(conn)) if "Message from Pat" in m["subject"]]
    assert mails and "When is my gallery ready?" in mails[0]["body"]
    assert mails[0]["to_addr"] == "pmsg1@example.com"          # the studio owner's inbox


def test_empty_message_is_noop(client, conn):
    creds = onboard_studio(client, email="pmsg2@example.com")
    login_owner(client, creds)
    tok = _portal_token(client, conn)
    client.post(f"/portal/{tok}/message", data={"message": "   "})
    assert not any("Message from" in m["subject"] for m in list_emails(conn, _tid(conn)))


def test_unknown_token_404s(client):
    assert client.post("/portal/nope-not-a-real-token/message",
                       data={"message": "hi"}).status_code == 404


def test_portal_shows_form_and_sent_banner(client, conn):
    creds = onboard_studio(client, email="pmsg3@example.com")
    login_owner(client, creds)
    tok = _portal_token(client, conn)
    page = client.get(f"/portal/{tok}").text
    assert f"/portal/{tok}/message" in page                     # the form is there
    assert "Message sent" not in page                           # banner only after sending
    assert "Message sent" in client.get(f"/portal/{tok}?sent=1").text
