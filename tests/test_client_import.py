"""CSV client import — parsing, dedup/idempotency, tenant scoping, and the HTTP flow."""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client, import_clients, list_clients, tags_for_client
from hestia.db import connect
from hestia.routes.crm import _parse_client_csv
from hestia.tenants import create_tenant


def _tenant(conn, name="Import Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


# ── CSV parsing ───────────────────────────────────────────────────────────────


def test_parse_csv_with_header_any_order():
    text = "email,name,tags\nz@x.com,Zoe,vip family\n"
    rows = _parse_client_csv(text)
    assert rows == [{"name": "Zoe", "email": "z@x.com", "phone": "", "notes": "",
                     "tags": ["vip", "family"]}]


def test_parse_csv_positional_without_header():
    rows = _parse_client_csv("Yan,yan@x.com,5551234\n")
    assert rows[0]["name"] == "Yan" and rows[0]["email"] == "yan@x.com"
    assert rows[0]["phone"] == "5551234"


def test_parse_csv_ignores_blank_lines_and_splits_tags_on_commas():
    rows = _parse_client_csv("name,email,tags\n\nKai,kai@x.com,\"vip, family\"\n\n")
    assert len(rows) == 1 and rows[0]["name"] == "Kai"
    assert rows[0]["tags"] == ["vip", "family"]


def test_parse_csv_foreign_headers_map_to_fields():
    # a CSV migrated from another tool, with differently-labelled columns, still imports
    text = "First Name,Email Address,Phone Number\nAlice,alice@x.com,555\nBob,bob@x.com,556\n"
    rows = _parse_client_csv(text)
    assert [r["name"] for r in rows] == ["Alice", "Bob"]
    assert rows[0]["email"] == "alice@x.com" and rows[0]["phone"] == "555"


def test_parse_csv_headerless_first_value_equal_to_field_token():
    # a header-less single-column list whose first name is literally "Phone" must NOT be
    # mistaken for a header — all three names import
    rows = _parse_client_csv("Phone\nAlice\nBob\n")
    assert [r["name"] for r in rows] == ["Phone", "Alice", "Bob"]


def test_parse_csv_client_named_like_a_field_with_email_is_data_not_header():
    rows = _parse_client_csv("Email,e@x.com\n")
    assert rows == [{"name": "Email", "email": "e@x.com", "phone": "", "notes": "", "tags": []}]


# ── import_clients (module) ───────────────────────────────────────────────────


def test_import_basic_skip_and_dedup(conn):
    t = _tenant(conn)
    create_client(conn, tenant_id=t["id"], name="Existing", email="dup@x.com")
    rows = [
        {"name": "Alice", "email": "alice@x.com", "phone": "555", "tags": ["vip"]},
        {"name": "", "email": "blank@x.com"},          # blank name → skipped
        {"name": "Dup", "email": "DUP@x.com"},         # already exists (case-insensitive)
        {"name": "Bob", "email": "bob@x.com"},
        {"name": "Bob Again", "email": "bob@x.com"},   # repeats earlier in the batch
        {"name": "No Email"},                          # no email → always imported
    ]
    s = import_clients(conn, tenant_id=t["id"], rows=rows)
    assert s == {"imported": 3, "skipped_duplicate": 2, "skipped_blank": 1}
    names = {c["name"] for c in list_clients(conn, t["id"])}
    assert {"Alice", "Bob", "No Email"} <= names and "Bob Again" not in names
    alice = next(c for c in list_clients(conn, t["id"]) if c["name"] == "Alice")
    assert "vip" in tags_for_client(conn, t["id"], alice["id"])


def test_import_reimport_is_idempotent(conn):
    t = _tenant(conn)
    rows = [{"name": "Cara", "email": "cara@x.com"}]
    assert import_clients(conn, tenant_id=t["id"], rows=rows)["imported"] == 1
    again = import_clients(conn, tenant_id=t["id"], rows=rows)
    assert again["imported"] == 0 and again["skipped_duplicate"] == 1
    assert len([c for c in list_clients(conn, t["id"]) if c["name"] == "Cara"]) == 1


def test_import_dedup_is_per_tenant(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    create_client(conn, tenant_id=t1["id"], name="A1", email="shared@x.com")
    # the same email under a different tenant is NOT a duplicate
    s = import_clients(conn, tenant_id=t2["id"], rows=[{"name": "B1", "email": "shared@x.com"}])
    assert s["imported"] == 1
    assert [c["name"] for c in list_clients(conn, t1["id"])] == ["A1"]   # no cross-tenant leak
    assert [c["name"] for c in list_clients(conn, t2["id"])] == ["B1"]


# ── HTTP flow ─────────────────────────────────────────────────────────────────


def _tid(conn, email):
    return conn.execute(
        "SELECT t.id FROM tenants t JOIN users u ON u.tenant_id = t.id WHERE u.email = ?",
        (email,),
    ).fetchone()["id"]


def test_http_import_flow(client, app):
    creds = onboard_studio(client, name="CSV Studio", email="csv@example.com")
    login_owner(client, creds)
    assert "Import clients" in client.get("/clients/import").text

    data = b"name,email,phone\nGina,gina@x.com,555\nHarry,harry@x.com,\n"
    r = client.post("/clients/import", files={"file": ("clients.csv", data, "text/csv")})
    assert r.status_code == 200 and "imported" in r.text

    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid(conn, creds["email"])
        names = {c["name"] for c in list_clients(conn, tid)}
        assert {"Gina", "Harry"} <= names
        before = len(list_clients(conn, tid))
    finally:
        conn.close()

    # re-importing the same file adds nothing (duplicates skipped)
    client.post("/clients/import", files={"file": ("clients.csv", data, "text/csv")})
    conn = connect(app.state.settings.db_path)
    try:
        assert len(list_clients(conn, tid)) == before
    finally:
        conn.close()


def test_http_import_binary_file_shows_friendly_error(client, app):
    creds = onboard_studio(client, name="Bin Studio", email="bin@example.com")
    login_owner(client, creds)
    blob = bytes(range(256)) * 8  # invalid utf-8 + NUL bytes → csv.Error, must not 500
    r = client.post("/clients/import", files={"file": ("x.csv", blob, "application/octet-stream")})
    assert r.status_code == 200 and "look like a CSV" in r.text   # friendly error, not a 500
    conn = connect(app.state.settings.db_path)
    try:
        assert list_clients(conn, _tid(conn, creds["email"])) == []   # nothing imported
    finally:
        conn.close()
