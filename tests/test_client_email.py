"""Email a client from inside Hestia — first-response compose, send, and record.

The studio replies to an inquiry without leaving the app: a compose page pre-filled
from the customizable "Reply to an inquiry" template, sent through the same notify()
chokepoint as every other Hestia email (so the signature is appended and the message
is recorded), then surfaced back on the client page as a sent message.
"""

from conftest import login_owner, onboard_studio


def _make_client(client, name, email):
    r = client.post("/clients", data={"name": name, "email": email})
    return r.url.path.rstrip("/").split("/")[-1]


def test_compose_is_prefilled(client):
    creds = onboard_studio(client, name="Lumen Studio", email="ce1@example.com")
    login_owner(client, creds)
    cid = _make_client(client, "Jordan Lee", "jordan@example.com")
    # the client page offers the action
    assert f"/clients/{cid}/email" in client.get(f"/clients/{cid}").text
    page = client.get(f"/clients/{cid}/email")
    assert page.status_code == 200
    assert "jordan@example.com" in page.text          # recipient shown
    assert "Hi Jordan Lee," in page.text              # body rendered with the client's name
    assert "Lumen Studio" in page.text                # studio name filled into the subject


def test_send_records_and_shows_on_client(client):
    creds = onboard_studio(client, name="Lumen Studio", email="ce2@example.com")
    login_owner(client, creds)
    cid = _make_client(client, "Sam Rivers", "sam@example.com")
    r = client.post(f"/clients/{cid}/email",
                    data={"subject": "Lovely to meet you", "body": "Let's chat this week."})
    assert r.status_code in (200, 303)
    # the message is recorded and surfaced back on the client page
    detail = client.get(f"/clients/{cid}")
    assert "Messages" in detail.text and "Lovely to meet you" in detail.text


def test_no_email_address_hides_action_and_blocks_send(client):
    creds = onboard_studio(client, email="ce3@example.com")
    login_owner(client, creds)
    cid = _make_client(client, "No Email", "")
    # no recipient → no action shown, and the compose page bounces back
    assert f"/clients/{cid}/email" not in client.get(f"/clients/{cid}").text
    assert client.get(f"/clients/{cid}/email").status_code in (200, 303)
    # a posted send is a no-op (nothing to surface)
    client.post(f"/clients/{cid}/email", data={"subject": "Hi", "body": "there"})
    assert "Messages" not in client.get(f"/clients/{cid}").text


def test_reply_template_is_customizable(client):
    """The new kind shows up in the Email templates editor like every other template."""
    creds = onboard_studio(client, email="ce4@example.com")
    login_owner(client, creds)
    assert "Reply to an inquiry" in client.get("/settings/messages").text


def test_foreign_client_redirects(client):
    """A client id that isn't this tenant's is a safe redirect — no crash, no send."""
    creds = onboard_studio(client, email="ce5@example.com")
    login_owner(client, creds)
    assert client.get("/clients/99999/email").status_code in (200, 303)
    assert client.post("/clients/99999/email",
                       data={"subject": "x", "body": "y"}).status_code in (200, 303)


def test_compose_offers_template_picker(client):
    """The composer lets the studio start from any of its general-purpose templates."""
    creds = onboard_studio(client, name="Lumen Studio", email="ce6@example.com")
    login_owner(client, creds)
    cid = _make_client(client, "Jordan Lee", "jordan@example.com")
    page = client.get(f"/clients/{cid}/email").text
    assert "Start from" in page and 'name="template"' in page
    assert "Announcement / broadcast" in page            # a second general template is offered


def test_compose_loads_selected_template(client):
    """Choosing a template re-renders the draft from it, filled with the client/studio."""
    creds = onboard_studio(client, name="Lumen Studio", email="ce7@example.com")
    login_owner(client, creds)
    cid = _make_client(client, "Jordan Lee", "jordan@example.com")
    page = client.get(f"/clients/{cid}/email?template=broadcast").text
    assert "I wanted to share a quick update" in page    # the broadcast body, not the inquiry one
    assert "Hi Jordan Lee," in page                      # rendered with the client's name


def test_compose_rejects_flow_specific_or_unknown_template(client):
    """A flow-specific (invoice/contract) or bogus template falls back to the inquiry
    reply — never rendered, so no raw {pay_url}/{amount} token leaks into the draft."""
    creds = onboard_studio(client, name="Lumen Studio", email="ce8@example.com")
    login_owner(client, creds)
    cid = _make_client(client, "Jordan Lee", "jordan@example.com")
    for bad in ("invoice_send", "contract_send", "definitely-not-a-kind"):
        page = client.get(f"/clients/{cid}/email?template={bad}").text
        assert "{pay_url}" not in page and "{amount}" not in page and "{sign_url}" not in page
        assert "Hi Jordan Lee," in page                  # fell back to the inquiry-reply default
