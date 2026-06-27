"""Segment broadcast — one personalized email to everyone in a tag.

Builds on the per-client email and the existing tag system: the studio filters clients
by a tag, composes once (pre-filled from the customizable Announcement template), and
Hestia sends a personalized copy to each tagged client who has an email — {client}
filled per recipient, signature appended, each message recorded.
"""

from conftest import login_owner, onboard_studio


def _client_with_tag(client, name, email, tag):
    r = client.post("/clients", data={"name": name, "email": email})
    cid = r.url.path.rstrip("/").split("/")[-1]
    client.post(f"/clients/{cid}/tags", data={"tag": tag})
    return cid


def test_compose_lists_only_emailable_recipients(client):
    creds = onboard_studio(client, name="Lumen Studio", email="seg1@example.com")
    login_owner(client, creds)
    _client_with_tag(client, "Alice", "alice@example.com", "vip")
    _client_with_tag(client, "Bob", "bob@example.com", "vip")
    _client_with_tag(client, "NoEmailNell", "", "vip")        # tagged but unreachable
    page = client.get("/clients/broadcast?tag=vip")
    assert page.status_code == 200
    assert "Alice" in page.text and "Bob" in page.text
    assert "NoEmailNell" not in page.text                     # excluded — no address
    assert "Send to 2" in page.text


def test_send_personalizes_each_recipient(client):
    creds = onboard_studio(client, name="Lumen Studio", email="seg2@example.com")
    login_owner(client, creds)
    a = _client_with_tag(client, "Alice", "alice@example.com", "vip")
    b = _client_with_tag(client, "Bob", "bob@example.com", "vip")
    client.post("/clients/broadcast",
                data={"tag": "vip", "subject": "Hello {client}", "body": "Hi {client}, news!"})
    # each client's page shows their own personalized copy (subject filled per recipient)
    assert "Hello Alice" in client.get(f"/clients/{a}").text
    assert "Hello Bob" in client.get(f"/clients/{b}").text


def test_only_segment_members_are_emailed(client):
    creds = onboard_studio(client, name="Lumen Studio", email="seg3@example.com")
    login_owner(client, creds)
    _client_with_tag(client, "Vip Vera", "vera@example.com", "vip")
    # an untagged client with an email must NOT receive the blast
    r = client.post("/clients", data={"name": "Outsider Ozzie", "email": "ozzie@example.com"})
    outsider = r.url.path.rstrip("/").split("/")[-1]
    client.post("/clients/broadcast",
                data={"tag": "vip", "subject": "VIPs only", "body": "Hi {client}"})
    assert "Messages" not in client.get(f"/clients/{outsider}").text


def test_no_tag_redirects(client):
    creds = onboard_studio(client, email="seg4@example.com")
    login_owner(client, creds)
    assert client.get("/clients/broadcast").status_code in (200, 303)


def test_button_only_when_tag_filter_active(client):
    creds = onboard_studio(client, email="seg5@example.com")
    login_owner(client, creds)
    _client_with_tag(client, "Alice", "alice@example.com", "vip")
    assert "/clients/broadcast?tag=vip" in client.get("/clients?tag=vip").text
    assert "/clients/broadcast" not in client.get("/clients").text


def test_broadcast_template_is_customizable(client):
    creds = onboard_studio(client, email="seg6@example.com")
    login_owner(client, creds)
    assert "Announcement / broadcast" in client.get("/settings/messages").text
