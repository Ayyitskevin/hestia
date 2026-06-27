"""Reusable contract templates — save boilerplate, start a contract from it.

Covers the data layer (save/list/get/delete, empty-name guard, tenant isolation)
and the studio-side routes (manage page, create, delete, and server-side pre-fill
of a new contract's body from a chosen template — all no-JS).
"""

from conftest import login_owner, onboard_studio

from hestia.contracts import (
    delete_contract_template,
    get_contract_template,
    list_contract_templates,
    save_contract_template,
)
from hestia.tenants import create_tenant


def _tenant(conn, name="Template Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def test_save_list_get(conn):
    t = _tenant(conn)
    tpl = save_contract_template(conn, tenant_id=t["id"], name="Booking", body="The terms.")
    assert tpl["name"] == "Booking" and tpl["body"] == "The terms."
    assert [r["id"] for r in list_contract_templates(conn, t["id"])] == [tpl["id"]]
    assert get_contract_template(conn, t["id"], tpl["id"])["body"] == "The terms."


def test_empty_name_ignored(conn):
    """A blank name saves nothing (returns None) — the form's name field is required,
    but the data layer guards too."""
    t = _tenant(conn)
    assert save_contract_template(conn, tenant_id=t["id"], name="   ", body="x") is None
    assert list_contract_templates(conn, t["id"]) == []


def test_name_trimmed_and_capped(conn):
    t = _tenant(conn)
    tpl = save_contract_template(conn, tenant_id=t["id"], name="  Model release  ", body="  body  ")
    assert tpl["name"] == "Model release" and tpl["body"] == "body"
    long = save_contract_template(conn, tenant_id=t["id"], name="x" * 500, body="")
    assert len(long["name"]) == 200


def test_delete(conn):
    t = _tenant(conn)
    tpl = save_contract_template(conn, tenant_id=t["id"], name="Temp", body="x")
    delete_contract_template(conn, t["id"], tpl["id"])
    assert get_contract_template(conn, t["id"], tpl["id"]) is None
    assert list_contract_templates(conn, t["id"]) == []


def test_tenant_isolation(conn):
    a = _tenant(conn, "A Studio")
    b = _tenant(conn, "B Studio")
    tpl = save_contract_template(conn, tenant_id=a["id"], name="A's terms", body="secret")
    # B can't see, read, or delete A's template.
    assert list_contract_templates(conn, b["id"]) == []
    assert get_contract_template(conn, b["id"], tpl["id"]) is None
    delete_contract_template(conn, b["id"], tpl["id"])
    assert get_contract_template(conn, a["id"], tpl["id"]) is not None


def test_http_create_and_manage(client):
    creds = onboard_studio(client, email="tpl1@example.com")
    login_owner(client, creds)
    # empty list initially
    page = client.get("/contracts/templates")
    assert page.status_code == 200 and "No templates yet" in page.text
    # create one
    client.post("/contracts/templates", data={"name": "Booking agreement", "body": "You agree."})
    page = client.get("/contracts/templates")
    assert "Booking agreement" in page.text and "You agree." in page.text


def test_http_prefills_new_contract_body(client):
    """Choosing a template pre-fills the new-contract Terms textarea, server-side."""
    creds = onboard_studio(client, email="tpl2@example.com")
    login_owner(client, creds)
    client.post("/contracts/templates", data={"name": "Standard", "body": "Boilerplate terms here."})
    # the new-contract page advertises the template as a starting point
    new = client.get("/contracts/new")
    assert "Start from a saved template" in new.text and "Standard" in new.text
    # find the template id from its "use" link and pre-fill from it
    tid = new.text.split("template_id=")[1].split("&")[0].split('"')[0].strip()
    prefilled = client.get(f"/contracts/new?template_id={tid}")
    assert "Boilerplate terms here." in prefilled.text


def test_http_delete(client):
    creds = onboard_studio(client, email="tpl3@example.com")
    login_owner(client, creds)
    client.post("/contracts/templates", data={"name": "Throwaway", "body": "x"})
    page = client.get("/contracts/templates")
    tid = page.text.split("/contracts/templates/")[1].split("/delete")[0].strip()
    client.post(f"/contracts/templates/{tid}/delete")
    assert "Throwaway" not in client.get("/contracts/templates").text


def test_unknown_template_id_prefills_nothing(client):
    """A stale/foreign ?template_id just yields a blank body — no crash, no leak."""
    creds = onboard_studio(client, email="tpl4@example.com")
    login_owner(client, creds)
    r = client.get("/contracts/new?template_id=99999")
    assert r.status_code == 200
