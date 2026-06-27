"""Save-as-template — turn an existing contract or questionnaire into a reusable template.

The natural companion to the template libraries: after drafting a one-off, a studio can
promote it to a reusable template in one click. These routes compose the already-tested
save_*_template data layer, so the tests focus on the end-to-end studio flow (promote →
appears in the library → usable as a prefill source) and the safe no-op on a bad id.
"""

from conftest import login_owner, onboard_studio


def _new_contract(client, title, body):
    r = client.post("/contracts", data={"title": title, "body": body})
    return r.url.path.rstrip("/").split("/")[-1]


def _new_questionnaire(client, title, prompts):
    r = client.post("/questionnaires", data={"title": title, "prompts": prompts})
    return r.url.path.rstrip("/").split("/")[-1]


def test_contract_save_as_template(client):
    creds = onboard_studio(client, email="sat1@example.com")
    login_owner(client, creds)
    cid = _new_contract(client, "Booking agreement", "You agree to the terms.")
    # the detail page offers the action
    assert "save-as-template" in client.get(f"/contracts/{cid}").text
    # promote it
    client.post(f"/contracts/{cid}/save-as-template")
    lib = client.get("/contracts/templates")
    assert "Booking agreement" in lib.text
    # and it's a working prefill source for a new contract
    new = client.get("/contracts/new")
    tid = new.text.split("template_id=")[1].split("&")[0].split('"')[0].strip()
    assert "You agree to the terms." in client.get(f"/contracts/new?template_id={tid}").text


def test_questionnaire_save_as_template(client):
    creds = onboard_studio(client, email="sat2@example.com")
    login_owner(client, creds)
    qid = _new_questionnaire(client, "Wedding intake", "Date?\nVenue?\nVibe?")
    assert "save-as-template" in client.get(f"/questionnaires/{qid}").text
    client.post(f"/questionnaires/{qid}/save-as-template")
    lib = client.get("/questionnaires/templates")
    assert "Wedding intake" in lib.text and "3 questions" in lib.text
    # usable as a prefill source, questions preserved in order
    new = client.get("/questionnaires/new")
    tid = new.text.split("template_id=")[1].split("&")[0].split('"')[0].strip()
    prefilled = client.get(f"/questionnaires/new?template_id={tid}")
    assert "Date?" in prefilled.text and "Vibe?" in prefilled.text


def test_contract_save_as_template_unknown_id_noop(client):
    """Promoting a contract that isn't yours/doesn't exist is a silent no-op — no crash,
    no template created (get_contract is tenant-scoped and returns None)."""
    creds = onboard_studio(client, email="sat3@example.com")
    login_owner(client, creds)
    r = client.post("/contracts/99999/save-as-template")
    assert r.status_code in (200, 303)
    assert "No templates yet" in client.get("/contracts/templates").text


def test_questionnaire_save_as_template_unknown_id_noop(client):
    creds = onboard_studio(client, email="sat4@example.com")
    login_owner(client, creds)
    r = client.post("/questionnaires/99999/save-as-template")
    assert r.status_code in (200, 303)
    assert "No templates yet" in client.get("/questionnaires/templates").text


def test_empty_questionnaire_hides_save_action(client):
    """A questionnaire with no questions has nothing to templatize — no button shown."""
    creds = onboard_studio(client, email="sat5@example.com")
    login_owner(client, creds)
    qid = _new_questionnaire(client, "Empty", "")
    assert "save-as-template" not in client.get(f"/questionnaires/{qid}").text
