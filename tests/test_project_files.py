"""Project file attachments — storage, tenant scoping, size guard, and the HTTP flow."""

import io

from conftest import CSRFClient, login_owner, onboard_studio

from hestia.crm import create_project
from hestia.db import connect
from hestia.project_files import (
    add_project_file,
    delete_project_file,
    get_project_file,
    list_project_files,
)
from hestia.tenants import create_tenant


def _tenant(conn, name="Files Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


# ── module ────────────────────────────────────────────────────────────────────


def test_add_stores_blob_and_row_and_is_tenant_scoped(conn, storage):
    t = _tenant(conn)
    p = create_project(conn, tenant_id=t["id"], name="Wedding")
    f = add_project_file(conn, storage, tenant_id=t["id"], project_id=p["id"],
                         filename="contract.pdf", fileobj=io.BytesIO(b"PDFDATA"),
                         content_type="application/pdf")
    assert f and f["filename"] == "contract.pdf" and f["bytes"] == 7
    assert storage.open(f["storage_key"]) == b"PDFDATA"
    assert [x["id"] for x in list_project_files(conn, t["id"], p["id"])] == [f["id"]]
    t2 = _tenant(conn, "B")
    assert get_project_file(conn, t2["id"], f["id"]) is None            # cross-tenant invisible
    assert list_project_files(conn, t2["id"], p["id"]) == []


def test_add_rejects_foreign_project_and_empty(conn, storage):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    p1 = create_project(conn, tenant_id=t1["id"], name="P1")
    assert add_project_file(conn, storage, tenant_id=t2["id"], project_id=p1["id"],
                            filename="x.pdf", fileobj=io.BytesIO(b"x")) is None   # not t2's project
    assert add_project_file(conn, storage, tenant_id=t1["id"], project_id=p1["id"],
                            filename="empty.pdf", fileobj=io.BytesIO(b"")) is None  # empty


def test_add_rejects_oversize(conn, storage, monkeypatch):
    import hestia.project_files as pf
    monkeypatch.setattr(pf, "_MAX_FILE_BYTES", 4)
    t = _tenant(conn)
    p = create_project(conn, tenant_id=t["id"], name="P")
    assert pf.add_project_file(conn, storage, tenant_id=t["id"], project_id=p["id"],
                               filename="big.bin", fileobj=io.BytesIO(b"12345")) is None
    assert list_project_files(conn, t["id"], p["id"]) == []


def test_delete_removes_row_and_blob(conn, storage):
    t = _tenant(conn)
    p = create_project(conn, tenant_id=t["id"], name="P")
    f = add_project_file(conn, storage, tenant_id=t["id"], project_id=p["id"],
                         filename="a.txt", fileobj=io.BytesIO(b"hello"))
    key = f["storage_key"]
    delete_project_file(conn, storage, t["id"], f["id"])
    assert get_project_file(conn, t["id"], f["id"]) is None
    try:
        storage.open(key)
        raise AssertionError("blob should have been deleted")
    except FileNotFoundError:
        pass


# ── HTTP ──────────────────────────────────────────────────────────────────────


def _tid(conn, email):
    return conn.execute(
        "SELECT t.id FROM tenants t JOIN users u ON u.tenant_id = t.id WHERE u.email = ?",
        (email,),
    ).fetchone()["id"]


def test_http_upload_download_delete(client, app):
    creds = onboard_studio(client, email="pf@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        pid = create_project(conn, tenant_id=_tid(conn, creds["email"]), name="Wedding")["id"]
        conn.commit()
    finally:
        conn.close()

    r = client.post(f"/projects/{pid}/files",
                    files={"file": ("plan.pdf", b"PLAN-BYTES", "application/pdf")})
    assert r.status_code in (200, 303)
    assert "plan.pdf" in client.get(f"/projects/{pid}").text

    conn = connect(app.state.settings.db_path)
    try:
        fid = list_project_files(conn, _tid(conn, creds["email"]), pid)[0]["id"]
    finally:
        conn.close()

    d = client.get(f"/projects/{pid}/files/{fid}")
    assert d.status_code == 200 and d.content == b"PLAN-BYTES"
    assert 'attachment; filename="plan.pdf"' in d.headers["content-disposition"]   # never inline

    client.post(f"/projects/{pid}/files/{fid}/delete")
    conn = connect(app.state.settings.db_path)
    try:
        assert list_project_files(conn, _tid(conn, creds["email"]), pid) == []
    finally:
        conn.close()


def test_http_download_is_tenant_scoped(client, app):
    a = onboard_studio(client, name="Own A", email="fa@example.com")
    login_owner(client, a)
    conn = connect(app.state.settings.db_path)
    try:
        a_tid = _tid(conn, a["email"])
        pa = create_project(conn, tenant_id=a_tid, name="A proj")
        f = add_project_file(conn, app.state.storage, tenant_id=a_tid, project_id=pa["id"],
                             filename="secret.pdf", fileobj=io.BytesIO(b"SECRET"))
        conn.commit()
        a_pid, a_fid = pa["id"], f["id"]
    finally:
        conn.close()

    b_client = CSRFClient(app)
    b = onboard_studio(b_client, name="Own B", email="fb@example.com")
    login_owner(b_client, b)
    r = b_client.get(f"/projects/{a_pid}/files/{a_fid}")
    assert b"SECRET" not in r.content                                  # another studio can't fetch it
