"""Project checklist templates — per-shoot-type lists, idempotent apply, auto-on-booking."""

from conftest import login_owner, onboard_studio

from hestia.checklists import (
    add_template_task,
    apply_checklist,
    delete_template_task,
    get_template_task,
    list_template_tasks,
)
from hestia.crm import create_project, set_project_status
from hestia.db import connect
from hestia.project_tasks import list_tasks
from hestia.tenants import create_tenant


def _tenant(conn, name="Checklist Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


# ── Template CRUD ─────────────────────────────────────────────────────────────


def test_add_list_delete(conn):
    t = _tenant(conn)
    a = add_template_task(conn, tenant_id=t["id"], shoot_type="wedding", label="Send contract")
    add_template_task(conn, tenant_id=t["id"], shoot_type="any", label="Send thank-you")
    assert a["shoot_type"] == "wedding"
    assert {x["label"] for x in list_template_tasks(conn, t["id"])} == {"Send contract", "Send thank-you"}
    assert add_template_task(conn, tenant_id=t["id"], shoot_type="any", label="  ") is None  # blank
    delete_template_task(conn, t["id"], a["id"])
    assert {x["label"] for x in list_template_tasks(conn, t["id"])} == {"Send thank-you"}


def test_templates_tenant_scoped(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    tmpl = add_template_task(conn, tenant_id=t1["id"], shoot_type="any", label="X")
    assert list_template_tasks(conn, t2["id"]) == []
    delete_template_task(conn, t2["id"], tmpl["id"])                # cross-tenant no-op
    assert get_template_task(conn, t1["id"], tmpl["id"]) is not None
    proj = create_project(conn, tenant_id=t2["id"], name="P", shoot_type="wedding")
    assert apply_checklist(conn, t2["id"], proj["id"]) == 0         # none of t1's templates leak


# ── apply_checklist ───────────────────────────────────────────────────────────


def test_apply_matches_shoot_type_plus_any(conn):
    t = _tenant(conn)
    add_template_task(conn, tenant_id=t["id"], shoot_type="wedding", label="Wedding timeline")
    add_template_task(conn, tenant_id=t["id"], shoot_type="portrait", label="Outfit guide")
    add_template_task(conn, tenant_id=t["id"], shoot_type="any", label="Send thank-you")
    proj = create_project(conn, tenant_id=t["id"], name="W", shoot_type="wedding")
    added = apply_checklist(conn, t["id"], proj["id"])
    labels = {x["label"] for x in list_tasks(conn, t["id"], proj["id"])}
    assert added == 2 and labels == {"Wedding timeline", "Send thank-you"}   # portrait excluded


def test_apply_is_idempotent(conn):
    t = _tenant(conn)
    add_template_task(conn, tenant_id=t["id"], shoot_type="any", label="Send contract")
    proj = create_project(conn, tenant_id=t["id"], name="P", shoot_type="wedding")
    assert apply_checklist(conn, t["id"], proj["id"]) == 1
    assert apply_checklist(conn, t["id"], proj["id"]) == 0          # already present → no dup
    assert len(list_tasks(conn, t["id"], proj["id"])) == 1


# ── auto-apply on booking ─────────────────────────────────────────────────────


def test_booking_applies_checklist_without_duplicating(conn):
    t = _tenant(conn)
    add_template_task(conn, tenant_id=t["id"], shoot_type="wedding", label="Collect deposit")
    proj = create_project(conn, tenant_id=t["id"], name="W", shoot_type="wedding", status="lead")
    set_project_status(conn, t["id"], proj["id"], "booked")
    assert [x["label"] for x in list_tasks(conn, t["id"], proj["id"])] == ["Collect deposit"]
    # status churn (re-book) must not duplicate the checklist
    set_project_status(conn, t["id"], proj["id"], "shooting")
    set_project_status(conn, t["id"], proj["id"], "booked")
    assert len(list_tasks(conn, t["id"], proj["id"])) == 1


# ── HTTP flow ─────────────────────────────────────────────────────────────────


def _tid(conn, email):
    return conn.execute(
        "SELECT t.id FROM tenants t JOIN users u ON u.tenant_id = t.id WHERE u.email = ?",
        (email,),
    ).fetchone()["id"]


def test_http_manage_and_apply(client, app):
    creds = onboard_studio(client, name="CL Studio", email="cl@example.com")
    login_owner(client, creds)
    assert "Checklist templates" in client.get("/checklists").text
    client.post("/checklists", data={"shoot_type": "wedding", "label": "Send contract"})
    assert "Send contract" in client.get("/checklists").text

    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid(conn, creds["email"])
        pid = create_project(conn, tenant_id=tid, name="Big Day", shoot_type="wedding")["id"]
        conn.commit()
    finally:
        conn.close()

    client.post(f"/projects/{pid}/apply-checklist")
    conn = connect(app.state.settings.db_path)
    try:
        assert [x["label"] for x in list_tasks(conn, _tid(conn, creds["email"]), pid)] == ["Send contract"]
    finally:
        conn.close()
