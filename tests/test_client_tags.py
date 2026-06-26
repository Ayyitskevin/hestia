"""Client tags — segment clients and filter the list by tag."""

from conftest import login_owner, onboard_studio

from hestia.crm import (
    add_client_tag,
    all_tags,
    create_client,
    list_clients,
    remove_client_tag,
    tags_for_client,
)
from hestia.tenants import create_tenant


def test_add_normalizes_idempotent_and_remove(conn):
    t = create_tenant(conn, name="Tag", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="C")
    assert add_client_tag(conn, t["id"], c["id"], "  VIP ") == "vip"       # normalized
    add_client_tag(conn, t["id"], c["id"], "vip")                          # idempotent (no dup)
    add_client_tag(conn, t["id"], c["id"], "repeat")
    conn.commit()
    assert tags_for_client(conn, t["id"], c["id"]) == ["repeat", "vip"]    # sorted
    assert remove_client_tag(conn, t["id"], c["id"], "VIP") is True        # matches normalized
    assert tags_for_client(conn, t["id"], c["id"]) == ["repeat"]
    assert add_client_tag(conn, t["id"], c["id"], "   ") is None           # empty → no tag


def test_add_tag_rejects_foreign_client(conn):
    a = create_tenant(conn, name="A", shoot_type="wedding")
    b = create_tenant(conn, name="B", shoot_type="wedding")
    cb = create_client(conn, tenant_id=b["id"], name="B-client")
    conn.commit()
    assert add_client_tag(conn, a["id"], cb["id"], "vip") is None          # not studio A's client
    assert tags_for_client(conn, b["id"], cb["id"]) == []


def test_all_tags_with_counts(conn):
    t = create_tenant(conn, name="T", shoot_type="wedding")
    c1 = create_client(conn, tenant_id=t["id"], name="C1")
    c2 = create_client(conn, tenant_id=t["id"], name="C2")
    add_client_tag(conn, t["id"], c1["id"], "vip")
    add_client_tag(conn, t["id"], c2["id"], "vip")
    add_client_tag(conn, t["id"], c1["id"], "repeat")
    conn.commit()
    assert all_tags(conn, t["id"]) == [{"tag": "repeat", "count": 1}, {"tag": "vip", "count": 2}]


def test_list_clients_attaches_tags_and_filters(conn):
    t = create_tenant(conn, name="F", shoot_type="wedding")
    vip = create_client(conn, tenant_id=t["id"], name="Vip")
    create_client(conn, tenant_id=t["id"], name="Other")
    add_client_tag(conn, t["id"], vip["id"], "vip")
    conn.commit()
    by = {c["name"]: c for c in list_clients(conn, t["id"])}
    assert by["Vip"]["tags"] == ["vip"] and by["Other"]["tags"] == []      # tags attached per client
    assert [c["name"] for c in list_clients(conn, t["id"], tag="VIP")] == ["Vip"]   # filter (normalized)


# --- HTTP -------------------------------------------------------------------

def test_http_tag_add_filter_and_remove(client):
    creds = onboard_studio(client, email="tags@example.com")
    login_owner(client, creds)
    rc = client.post("/clients", data={"name": "Sarah"})
    cid = rc.url.path.rstrip("/").split("/")[-1]

    client.post(f"/clients/{cid}/tags", data={"tag": "VIP"})
    detail = client.get(f"/clients/{cid}")
    assert "Tags" in detail.text and "vip" in detail.text

    assert "Sarah" in client.get("/clients?tag=vip").text                  # filter finds her
    client.post(f"/clients/{cid}/tags/delete", data={"tag": "vip"})
    assert "Sarah" not in client.get("/clients?tag=vip").text              # no longer tagged


def test_tag_routes_require_login(client):
    assert client.post("/clients/1/tags", data={"tag": "x"},
                       follow_redirects=False).status_code == 303
