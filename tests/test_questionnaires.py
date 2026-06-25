"""Questionnaires — lifecycle, idempotent submit, isolation, and the fill flow."""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client, create_project
from hestia.email import list_emails
from hestia.questionnaires import (
    create_questionnaire,
    get_questionnaire,
    get_questionnaire_by_token,
    list_questionnaires,
    send_questionnaire,
    submit_questionnaire,
    void_questionnaire,
)
from hestia.tenants import create_tenant


def _tenant(conn, name="Form Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def test_create_with_items(conn):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah")
    p = create_project(conn, tenant_id=t["id"], name="Wedding", client_id=c["id"])
    q = create_questionnaire(conn, tenant_id=t["id"], title="Intake",
                             prompts=["Date?", "Venue?", "  ", "Vibe?"],
                             client_id=c["id"], project_id=p["id"])
    # blank prompts are dropped; sequence is 1-based
    assert [i["prompt"] for i in q["items"]] == ["Date?", "Venue?", "Vibe?"]
    assert [i["sequence"] for i in q["items"]] == [1, 2, 3]
    assert q["status"] == "draft" and q["client_name"] == "Sarah"


def test_status_transitions(conn):
    t = _tenant(conn)
    q = create_questionnaire(conn, tenant_id=t["id"], title="Q", prompts=["A?"])
    send_questionnaire(conn, t["id"], q["id"])
    assert get_questionnaire(conn, t["id"], q["id"])["status"] == "sent"
    void_questionnaire(conn, t["id"], q["id"])
    assert get_questionnaire(conn, t["id"], q["id"])["status"] == "void"


def test_cannot_submit_a_draft(conn):
    t = _tenant(conn)
    q = create_questionnaire(conn, tenant_id=t["id"], title="Q", prompts=["A?"])
    assert submit_questionnaire(conn, token=q["token"], answers={}) is False
    assert get_questionnaire(conn, t["id"], q["id"])["status"] == "draft"


def test_submit_is_idempotent(conn):
    t = _tenant(conn)
    q = create_questionnaire(conn, tenant_id=t["id"], title="Q", prompts=["Date?", "Venue?"])
    send_questionnaire(conn, t["id"], q["id"])
    items = get_questionnaire(conn, t["id"], q["id"])["items"]
    answers = {str(items[0]["id"]): "June 12", str(items[1]["id"]): "The Grand"}

    assert submit_questionnaire(conn, token=q["token"], answers=answers) is True
    done = get_questionnaire_by_token(conn, q["token"])
    assert done["status"] == "completed"
    assert [i["answer"] for i in done["items"]] == ["June 12", "The Grand"]

    # a second submit never overwrites the captured answers
    assert submit_questionnaire(conn, token=q["token"],
                                answers={str(items[0]["id"]): "CHANGED"}) is False
    again = get_questionnaire_by_token(conn, q["token"])
    assert again["items"][0]["answer"] == "June 12"


def test_completed_cannot_be_voided(conn):
    t = _tenant(conn)
    q = create_questionnaire(conn, tenant_id=t["id"], title="Q", prompts=["A?"])
    send_questionnaire(conn, t["id"], q["id"])
    submit_questionnaire(conn, token=q["token"], answers={})
    void_questionnaire(conn, t["id"], q["id"])
    assert get_questionnaire(conn, t["id"], q["id"])["status"] == "completed"  # unchanged


def test_tenant_isolation(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    create_questionnaire(conn, tenant_id=t1["id"], title="A-form", prompts=["A?"])
    assert list_questionnaires(conn, t2["id"]) == []


def test_send_emails_fill_link(conn, settings):
    from hestia.email import notify
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah", email="sarah@example.com")
    q = create_questionnaire(conn, tenant_id=t["id"], title="Intake", prompts=["A?"],
                             client_id=c["id"])
    send_questionnaire(conn, t["id"], q["id"])
    notify(conn, settings, to="sarah@example.com", tenant_id=t["id"],
           subject="form", body=f"/q/{q['token']}")
    conn.commit()
    assert any(q["token"] in m["body"] for m in list_emails(conn, t["id"]))


def test_http_questionnaire_and_fill_flow(client):
    creds = onboard_studio(client, email="forms@example.com")
    login_owner(client, creds)
    r = client.post("/questionnaires", data={
        "title": "Wedding intake", "prompts": "What is the date?\nWho is coming?",
    })
    qid = r.url.path.rstrip("/").split("/")[-1]
    detail = client.get(f"/questionnaires/{qid}")
    assert "Wedding intake" in detail.text and "/q/" not in detail.text  # draft, no link yet

    assert client.post(f"/questionnaires/{qid}/send").status_code in (200, 303)
    detail = client.get(f"/questionnaires/{qid}")
    token = detail.text.split("/q/")[1].split('"')[0].split("<")[0].strip()

    page = client.get(f"/q/{token}")
    assert page.status_code == 200 and "What is the date?" in page.text
    # the form fields are named item_<id>
    item_field = page.text.split('name="item_')[1].split('"')[0]
    client.post(f"/q/{token}", data={f"item_{item_field}": "June 12, 2026"})

    done = client.get(f"/q/{token}")
    assert "your answers have been sent" in done.text.lower()
    assert "June 12, 2026" in done.text
    # owner sees the answer
    assert "June 12, 2026" in client.get(f"/questionnaires/{qid}").text


def test_sent_questionnaire_surfaces_in_portal(conn, settings):
    """A sent questionnaire shows up (with its fill link) in the client portal."""
    from hestia.portal import assemble_portal, enable_portal, get_client_by_portal_token
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah")
    q = create_questionnaire(conn, tenant_id=t["id"], title="Intake", prompts=["A?"],
                             client_id=c["id"])
    send_questionnaire(conn, t["id"], q["id"])
    client = get_client_by_portal_token(conn, enable_portal(conn, t["id"], c["id"]))
    data = assemble_portal(conn, settings, client)
    assert data["questionnaires"][0]["title"] == "Intake"
    assert data["questionnaires"][0]["fill_url"].endswith(f"/q/{q['token']}")


def test_fill_unknown_token_404(client):
    assert client.get("/q/nope-not-a-token").status_code == 404


def test_voided_questionnaire_fill_404(client):
    creds = onboard_studio(client, email="v@example.com")
    login_owner(client, creds)
    r = client.post("/questionnaires", data={"title": "Q", "prompts": "A?"})
    qid = r.url.path.rstrip("/").split("/")[-1]
    client.post(f"/questionnaires/{qid}/send")
    token = client.get(f"/questionnaires/{qid}").text.split("/q/")[1].split('"')[0].split("<")[0].strip()
    assert client.get(f"/q/{token}").status_code == 200
    client.post(f"/questionnaires/{qid}/void")
    assert client.get(f"/q/{token}").status_code == 404
