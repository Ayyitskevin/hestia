"""Global search — free-text across the studio's clients (name/email) and projects (name).

Tenant-scoped, case-insensitive substring, with LIKE wildcards escaped so a literal % or _
in the query isn't treated as a pattern.
"""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client, create_project, search_crm
from hestia.tenants import create_tenant


def _studio(conn, name="Search Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def test_finds_clients_by_name_and_email(conn):
    t = _studio(conn)
    create_client(conn, tenant_id=t["id"], name="Alice Wonder", email="alice@ex.com")
    create_client(conn, tenant_id=t["id"], name="Bob Marsh", email="bob@ex.com")
    conn.commit()
    assert [c["name"] for c in search_crm(conn, t["id"], "wonder")["clients"]] == ["Alice Wonder"]
    assert [c["name"] for c in search_crm(conn, t["id"], "BOB@ex")["clients"]] == ["Bob Marsh"]


def test_finds_projects_with_client_name(conn):
    t = _studio(conn)
    c = create_client(conn, tenant_id=t["id"], name="Cli", email="c@x.com")
    create_project(conn, tenant_id=t["id"], name="Beach Wedding", client_id=c["id"])
    conn.commit()
    res = search_crm(conn, t["id"], "beach")
    assert [p["name"] for p in res["projects"]] == ["Beach Wedding"]
    assert res["projects"][0]["client_name"] == "Cli"


def test_empty_query_returns_nothing(conn):
    t = _studio(conn)
    assert search_crm(conn, t["id"], "   ") == {"clients": [], "projects": []}


def test_tenant_scoped(conn):
    a = _studio(conn, "A")
    b = _studio(conn, "B")
    create_client(conn, tenant_id=a["id"], name="Alice", email="a@x.com")
    conn.commit()
    assert search_crm(conn, b["id"], "alice")["clients"] == []


def test_like_wildcards_are_literal(conn):
    t = _studio(conn)
    create_client(conn, tenant_id=t["id"], name="50% Off Promo", email="p@x.com")
    create_client(conn, tenant_id=t["id"], name="Normal Client", email="n@x.com")
    conn.commit()
    # a bare "%" must match only a literal percent, not act as a match-everything wildcard
    res = search_crm(conn, t["id"], "%")
    assert [c["name"] for c in res["clients"]] == ["50% Off Promo"]


def test_http_search_page_and_nav_box(client):
    creds = onboard_studio(client, email="srch@example.com")
    login_owner(client, creds)
    client.post("/clients", data={"name": "Searchable Sam", "email": "sam@ex.com"})
    assert "Searchable Sam" in client.get("/search?q=searchable").text
    assert 'action="/search"' in client.get("/dashboard").text     # nav search box present


def test_http_search_no_match(client):
    creds = onboard_studio(client, email="srch2@example.com")
    login_owner(client, creds)
    assert "No clients or projects match" in client.get("/search?q=zzznotfound").text
