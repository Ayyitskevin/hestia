"""Project checklist — per-project tasks: add (strip/skip-empty), list (open first),
toggle, delete, progress roll-up. Tenant-scoped on every read and write."""

from conftest import login_owner, onboard_studio

from hestia.crm import create_project
from hestia.project_tasks import (
    add_task,
    delete_task,
    list_tasks,
    task_progress,
    toggle_task,
)
from hestia.tenants import create_tenant


def _project(conn, tid, name="Smith Wedding"):
    return create_project(conn, tenant_id=tid, name=name, shoot_type="wedding", status="booked")


# ── unit ─────────────────────────────────────────────────────────────────────


def test_add_list_toggle_delete(conn):
    t = create_tenant(conn, name="Studio", shoot_type="wedding")
    p = _project(conn, t["id"])
    a = add_task(conn, tenant_id=t["id"], project_id=p["id"], label="  Send contract  ")
    add_task(conn, tenant_id=t["id"], project_id=p["id"], label="Collect deposit")
    assert add_task(conn, tenant_id=t["id"], project_id=p["id"], label="   ") is None  # empty ignored
    assert a["label"] == "Send contract" and a["done"] == 0      # stripped, starts open

    assert [x["label"] for x in list_tasks(conn, t["id"], p["id"])] == ["Send contract", "Collect deposit"]

    toggle_task(conn, t["id"], a["id"])                          # mark done
    assert task_progress(conn, t["id"], p["id"]) == {"total": 2, "done": 1, "pct": 50}
    # a completed task sinks below the open ones
    assert [x["label"] for x in list_tasks(conn, t["id"], p["id"])] == ["Collect deposit", "Send contract"]

    toggle_task(conn, t["id"], a["id"])                          # back to open
    assert task_progress(conn, t["id"], p["id"])["done"] == 0

    delete_task(conn, t["id"], a["id"])
    assert [x["label"] for x in list_tasks(conn, t["id"], p["id"])] == ["Collect deposit"]


def test_tasks_are_tenant_scoped(conn):
    a = create_tenant(conn, name="A", shoot_type="wedding")
    b = create_tenant(conn, name="B", shoot_type="wedding")
    pa = _project(conn, a["id"])
    task = add_task(conn, tenant_id=a["id"], project_id=pa["id"], label="A task")
    conn.commit()
    # B can neither see, toggle, nor delete A's task
    assert list_tasks(conn, b["id"], pa["id"]) == []
    toggle_task(conn, b["id"], task["id"])
    delete_task(conn, b["id"], task["id"])
    remaining = list_tasks(conn, a["id"], pa["id"])
    assert len(remaining) == 1 and remaining[0]["done"] == 0     # untouched by B


def test_empty_progress(conn):
    t = create_tenant(conn, name="Empty", shoot_type="wedding")
    p = _project(conn, t["id"])
    assert task_progress(conn, t["id"], p["id"]) == {"total": 0, "done": 0, "pct": 0}


# ── HTTP ─────────────────────────────────────────────────────────────────────


def test_checklist_http_flow(client, conn):
    login_owner(client, onboard_studio(client, email="task@owner.com"))
    tid = conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]
    pid = _project(conn, tid)["id"]
    conn.commit()

    client.post(f"/projects/{pid}/tasks", data={"label": "Deliver gallery"})
    page = client.get(f"/projects/{pid}")
    assert "Checklist" in page.text and "Deliver gallery" in page.text

    task_id = conn.execute("SELECT id FROM project_tasks WHERE project_id=?", (pid,)).fetchone()["id"]
    client.post(f"/projects/{pid}/tasks/{task_id}/toggle")
    assert conn.execute("SELECT done FROM project_tasks WHERE id=?", (task_id,)).fetchone()["done"] == 1

    client.post(f"/projects/{pid}/tasks/{task_id}/delete")
    assert conn.execute("SELECT COUNT(*) AS n FROM project_tasks WHERE id=?", (task_id,)).fetchone()["n"] == 0
