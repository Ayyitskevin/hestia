"""Clients CSV export — the segmented book of business, with the tag filter."""

from conftest import login_owner, onboard_studio

from hestia.crm import add_client_tag, create_client
from hestia.db import connect
from hestia.invoices import create_invoice


def test_clients_export_csv_with_tag_filter(client, app):
    creds = onboard_studio(client, email="exp@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        vip = create_client(conn, tenant_id=tid, name="Sarah", email="s@example.com", phone="555")
        add_client_tag(conn, tid, vip["id"], "vip")
        inv = create_invoice(conn, app.state.settings, tenant_id=tid, title="Bal",
                             amount_cents=250000, client_id=vip["id"])
        conn.execute("UPDATE invoices SET status = 'paid' WHERE id = ?", (inv["id"],))
        create_client(conn, tenant_id=tid, name="Other", email="o@example.com")
        conn.commit()
    finally:
        conn.close()

    r = client.get("/clients/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "name,email,phone,tags,projects,lifetime_value" in r.text
    assert "Sarah,s@example.com,555,vip" in r.text and "2500.00" in r.text
    assert "Other" in r.text

    r2 = client.get("/clients/export.csv?tag=vip")                 # filter narrows the export
    assert "Sarah" in r2.text and "Other" not in r2.text


def test_clients_export_neutralizes_formula_injection(client, app):
    creds = onboard_studio(client, email="inj2@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        create_client(conn, tenant_id=tid, name="=cmd()", email="x@example.com")
        conn.commit()
    finally:
        conn.close()
    text = client.get("/clients/export.csv").text
    assert "'=cmd()" in text                                        # quoted → treated as text
    assert ",=cmd()" not in text                                   # never a bare leading-= cell


def test_clients_export_requires_login(client):
    assert client.get("/clients/export.csv", follow_redirects=False).status_code == 303
