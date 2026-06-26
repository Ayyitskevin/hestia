"""Clients list — lifetime value (collected revenue) per client, biggest first."""

from hestia.crm import create_client, list_clients
from hestia.invoices import create_invoice
from hestia.tenants import create_tenant


def _paid(conn, settings, t, client, cents):
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="I", amount_cents=cents,
                         client_id=client["id"])
    conn.execute("UPDATE invoices SET status = 'paid' WHERE id = ?", (inv["id"],))


def test_clients_lifetime_value_and_ordering(conn, settings):
    t = create_tenant(conn, name="LTV", shoot_type="wedding")
    small = create_client(conn, tenant_id=t["id"], name="Small")
    big = create_client(conn, tenant_id=t["id"], name="Big")
    create_client(conn, tenant_id=t["id"], name="Nada")
    _paid(conn, settings, t, small, 50000)
    _paid(conn, settings, t, big, 300000)
    create_invoice(conn, settings, tenant_id=t["id"], title="U", amount_cents=99999,
                   client_id=small["id"])                       # unpaid → excluded
    conn.commit()
    clients = list_clients(conn, t["id"])
    by = {c["name"]: c for c in clients}
    assert by["Big"]["lifetime_cents"] == 300000 and by["Big"]["lifetime_display"] == "$3,000.00"
    assert by["Small"]["lifetime_cents"] == 50000               # only the paid invoice counts
    assert by["Nada"]["lifetime_cents"] == 0
    assert [c["name"] for c in clients] == ["Big", "Small", "Nada"]   # ordered by value desc


def test_lifetime_value_is_tenant_scoped(conn, settings):
    a = create_tenant(conn, name="A", shoot_type="wedding")
    b = create_tenant(conn, name="B", shoot_type="wedding")
    create_client(conn, tenant_id=a["id"], name="A-client")
    _paid(conn, settings, b, create_client(conn, tenant_id=b["id"], name="B-client"), 100000)
    conn.commit()
    assert list_clients(conn, a["id"])[0]["lifetime_cents"] == 0   # B's revenue doesn't leak in
