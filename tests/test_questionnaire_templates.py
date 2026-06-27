"""Reusable questionnaire templates — save an intake question set, start a form from it.

Covers the data layer (save/list/get/delete, empty-name guard, prompt normalization +
count, tenant isolation) and the studio-side routes (manage page, create, delete, and
server-side pre-fill of a new questionnaire's questions from a chosen template — no JS).
"""

from conftest import login_owner, onboard_studio

from hestia.questionnaires import (
    delete_questionnaire_template,
    get_questionnaire_template,
    list_questionnaire_templates,
    save_questionnaire_template,
)
from hestia.tenants import create_tenant


def _tenant(conn, name="Intake Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def test_save_list_get(conn):
    t = _tenant(conn)
    tpl = save_questionnaire_template(conn, tenant_id=t["id"], name="Wedding intake",
                                      prompts="Date and venue?\nKey people?")
    assert tpl["name"] == "Wedding intake" and tpl["prompts"] == "Date and venue?\nKey people?"
    listed = list_questionnaire_templates(conn, t["id"])
    assert [r["id"] for r in listed] == [tpl["id"]] and listed[0]["prompt_count"] == 2
    assert get_questionnaire_template(conn, t["id"], tpl["id"])["prompts"].endswith("Key people?")


def test_empty_name_ignored(conn):
    """A blank name saves nothing (returns None) — the form's name field is required,
    but the data layer guards too."""
    t = _tenant(conn)
    assert save_questionnaire_template(conn, tenant_id=t["id"], name="   ", prompts="Q1") is None
    assert list_questionnaire_templates(conn, t["id"]) == []


def test_prompts_normalized(conn):
    """Blank lines are dropped and each question is trimmed — so the count is reliable."""
    t = _tenant(conn)
    tpl = save_questionnaire_template(conn, tenant_id=t["id"], name="Prep",
                                      prompts="  Q1  \n\n   \n Q2 \n")
    assert tpl["prompts"] == "Q1\nQ2"
    assert list_questionnaire_templates(conn, t["id"])[0]["prompt_count"] == 2


def test_name_trimmed_and_capped(conn):
    t = _tenant(conn)
    tpl = save_questionnaire_template(conn, tenant_id=t["id"], name="  Newborn prep  ", prompts="")
    assert tpl["name"] == "Newborn prep" and tpl["prompts"] == ""
    assert list_questionnaire_templates(conn, t["id"])[0]["prompt_count"] == 0
    long = save_questionnaire_template(conn, tenant_id=t["id"], name="x" * 500, prompts="")
    assert len(long["name"]) == 200


def test_delete(conn):
    t = _tenant(conn)
    tpl = save_questionnaire_template(conn, tenant_id=t["id"], name="Temp", prompts="Q")
    delete_questionnaire_template(conn, t["id"], tpl["id"])
    assert get_questionnaire_template(conn, t["id"], tpl["id"]) is None
    assert list_questionnaire_templates(conn, t["id"]) == []


def test_tenant_isolation(conn):
    a = _tenant(conn, "A Studio")
    b = _tenant(conn, "B Studio")
    tpl = save_questionnaire_template(conn, tenant_id=a["id"], name="A's intake", prompts="secret?")
    # B can't see, read, or delete A's template.
    assert list_questionnaire_templates(conn, b["id"]) == []
    assert get_questionnaire_template(conn, b["id"], tpl["id"]) is None
    delete_questionnaire_template(conn, b["id"], tpl["id"])
    assert get_questionnaire_template(conn, a["id"], tpl["id"]) is not None


def test_http_create_and_manage(client):
    creds = onboard_studio(client, email="qtpl1@example.com")
    login_owner(client, creds)
    page = client.get("/questionnaires/templates")
    assert page.status_code == 200 and "No templates yet" in page.text
    client.post("/questionnaires/templates",
                data={"name": "Wedding intake", "prompts": "Date?\nVenue?\nVibe?"})
    page = client.get("/questionnaires/templates")
    assert "Wedding intake" in page.text and "3 questions" in page.text


def test_http_prefills_new_questionnaire(client):
    """Choosing a template pre-fills the new-questionnaire Questions textarea, server-side."""
    creds = onboard_studio(client, email="qtpl2@example.com")
    login_owner(client, creds)
    client.post("/questionnaires/templates",
                data={"name": "Standard", "prompts": "First question here?\nSecond question?"})
    new = client.get("/questionnaires/new")
    assert "Start from a saved template" in new.text and "Standard" in new.text
    tid = new.text.split("template_id=")[1].split("&")[0].split('"')[0].strip()
    prefilled = client.get(f"/questionnaires/new?template_id={tid}")
    assert "First question here?" in prefilled.text and "Second question?" in prefilled.text


def test_http_delete(client):
    creds = onboard_studio(client, email="qtpl3@example.com")
    login_owner(client, creds)
    client.post("/questionnaires/templates", data={"name": "Throwaway", "prompts": "Q"})
    page = client.get("/questionnaires/templates")
    tid = page.text.split("/questionnaires/templates/")[1].split("/delete")[0].strip()
    client.post(f"/questionnaires/templates/{tid}/delete")
    assert "Throwaway" not in client.get("/questionnaires/templates").text


def test_unknown_template_id_prefills_nothing(client):
    """A stale/foreign ?template_id just yields a blank questionnaire — no crash, no leak."""
    creds = onboard_studio(client, email="qtpl4@example.com")
    login_owner(client, creds)
    r = client.get("/questionnaires/new?template_id=99999")
    assert r.status_code == 200
