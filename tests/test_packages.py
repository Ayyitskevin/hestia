"""Service packages — catalog CRUD, tenant isolation, money parsing, and invoice prefill."""

from conftest import login_owner, onboard_studio

from hestia.db import connect
from hestia.packages import (
    create_package,
    get_package,
    list_packages,
    set_package_active,
    update_package,
)
from hestia.routes.packages import _to_cents
from hestia.tenants import create_tenant


def _tenant(conn, name="Pkg Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


# ── Money parsing ─────────────────────────────────────────────────────────────


def test_to_cents_parses_and_floors():
    assert _to_cents("3500") == 350000
    assert _to_cents("3,500.00") == 350000
    assert _to_cents("$1,000.50") == 100050
    assert _to_cents("") == 0 and _to_cents("abc") == 0
    # overflow-safe (mirrors the invoice/plan parsers)
    assert _to_cents("1e308") == 0 and _to_cents("1e400") == 0
    assert _to_cents("inf") == 0 and _to_cents("nan") == 0


# ── Module CRUD ───────────────────────────────────────────────────────────────


def test_create_and_list(conn):
    t = _tenant(conn)
    p = create_package(conn, tenant_id=t["id"], name="Wedding", description="8h coverage",
                       price_cents=350000, deposit_cents=100000)
    assert p["name"] == "Wedding" and p["price_cents"] == 350000 and p["deposit_cents"] == 100000
    assert p["active"] == 1
    assert [x["name"] for x in list_packages(conn, t["id"])] == ["Wedding"]
    # blank name is rejected
    assert create_package(conn, tenant_id=t["id"], name="  ") is None


def test_negative_money_floored_to_zero(conn):
    t = _tenant(conn)
    p = create_package(conn, tenant_id=t["id"], name="Odd", price_cents=-500, deposit_cents=-1)
    assert p["price_cents"] == 0 and p["deposit_cents"] == 0


def test_update_package(conn):
    t = _tenant(conn)
    p = create_package(conn, tenant_id=t["id"], name="Mini", price_cents=20000)
    assert update_package(conn, t["id"], p["id"], name="Mini Session",
                          description="30 min", price_cents=25000, deposit_cents=5000) is True
    got = get_package(conn, t["id"], p["id"])
    assert got["name"] == "Mini Session" and got["price_cents"] == 25000 and got["deposit_cents"] == 5000
    # blank name is rejected, leaves the row untouched
    assert update_package(conn, t["id"], p["id"], name=" ") is False
    assert get_package(conn, t["id"], p["id"])["name"] == "Mini Session"


def test_archive_and_restore(conn):
    t = _tenant(conn)
    p = create_package(conn, tenant_id=t["id"], name="Album add-on", price_cents=40000)
    set_package_active(conn, t["id"], p["id"], False)
    assert get_package(conn, t["id"], p["id"])["active"] == 0
    # archived ones are hidden from the invoice-builder picker
    assert list_packages(conn, t["id"], active_only=True) == []
    assert len(list_packages(conn, t["id"])) == 1          # but still shown in the catalog
    set_package_active(conn, t["id"], p["id"], True)
    assert get_package(conn, t["id"], p["id"])["active"] == 1


def test_tenant_isolation(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    p = create_package(conn, tenant_id=t1["id"], name="Secret", price_cents=999)
    # t2 can neither see nor mutate t1's package
    assert get_package(conn, t2["id"], p["id"]) is None
    assert list_packages(conn, t2["id"]) == []
    assert update_package(conn, t2["id"], p["id"], name="Hijacked", price_cents=1) is False
    set_package_active(conn, t2["id"], p["id"], False)     # no-op across tenants
    assert get_package(conn, t1["id"], p["id"])["active"] == 1
    assert get_package(conn, t1["id"], p["id"])["name"] == "Secret"


# ── HTTP flow ─────────────────────────────────────────────────────────────────


def _tid(conn, email):
    return conn.execute(
        "SELECT t.id FROM tenants t JOIN users u ON u.tenant_id = t.id WHERE u.email = ?",
        (email,),
    ).fetchone()["id"]


def test_http_create_and_use_on_invoice(client, app):
    creds = onboard_studio(client, name="Lens Studio", email="lens@example.com")
    login_owner(client, creds)

    # create a package via the catalog page
    client.post("/packages", data={"name": "Wedding Collection",
                                    "description": "8h coverage · album",
                                    "price": "3,500.00", "deposit": "1000"})
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid(conn, creds["email"])
        pkgs = list_packages(conn, tid)
        assert len(pkgs) == 1 and pkgs[0]["price_cents"] == 350000
        pid = pkgs[0]["id"]
    finally:
        conn.close()

    # the new-invoice page offers the package, and selecting it prefills the form
    page = client.get("/invoices/new").text
    assert "Wedding Collection" in page and "Start from a package" in page
    prefilled = client.get(f"/invoices/new?package_id={pid}").text
    assert 'value="Wedding Collection"' in prefilled and 'value="3500.00"' in prefilled

    # creating from the prefilled values yields an invoice with the package's amount
    r = client.post("/invoices", data={"title": "Wedding Collection", "amount": "3500.00",
                                       "note": "8h coverage · album"})
    assert r.status_code in (200, 303)
    iid = r.url.path.rstrip("/").split("/")[-1]
    conn = connect(app.state.settings.db_path)
    try:
        inv = conn.execute("SELECT amount_cents FROM invoices WHERE id = ?", (iid,)).fetchone()
        assert inv["amount_cents"] == 350000
    finally:
        conn.close()


def test_http_archived_package_not_in_picker(client, app):
    creds = onboard_studio(client, name="Archive Studio", email="arch@example.com")
    login_owner(client, creds)
    client.post("/packages", data={"name": "Retired Tier", "price": "100"})
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid(conn, creds["email"])
        pid = list_packages(conn, tid)[0]["id"]
    finally:
        conn.close()
    client.post(f"/packages/{pid}/archive")
    # archived → gone from the invoice picker, but restorable from the catalog
    assert "Retired Tier" not in client.get("/invoices/new").text
    assert "Retired Tier" in client.get("/packages").text


def test_http_cross_tenant_package_id_ignored_on_prefill(client, app):
    # tenant A owns a package; tenant B must not be able to prefill from it
    a = onboard_studio(client, name="Studio A", email="a@example.com")
    a_client = client
    login_owner(a_client, a)
    a_client.post("/packages", data={"name": "A-only", "price": "777"})
    conn = connect(app.state.settings.db_path)
    try:
        a_pid = list_packages(conn, _tid(conn, a["email"]))[0]["id"]
    finally:
        conn.close()

    from conftest import CSRFClient
    b_client = CSRFClient(app)
    b = onboard_studio(b_client, name="Studio B", email="b@example.com")
    login_owner(b_client, b)
    prefilled = b_client.get(f"/invoices/new?package_id={a_pid}").text
    assert "A-only" not in prefilled              # cross-tenant package is invisible
    # ...and likewise for the payment-plan builder
    assert "A-only" not in b_client.get(f"/payment-plans/new?package_id={a_pid}").text


def test_http_use_package_as_payment_plan(client, app):
    creds = onboard_studio(client, name="Plan Studio", email="plan@example.com")
    login_owner(client, creds)
    client.post("/packages", data={"name": "Wedding Collection", "price": "4,000.00",
                                   "deposit": "1000"})
    conn = connect(app.state.settings.db_path)
    try:
        pid = list_packages(conn, _tid(conn, creds["email"]))[0]["id"]
    finally:
        conn.close()

    # the plan builder offers the package and prefills total + deposit from it
    page = client.get("/payment-plans/new").text
    assert "Wedding Collection" in page and "Start from a package" in page
    prefilled = client.get(f"/payment-plans/new?package_id={pid}").text
    assert ('value="Wedding Collection"' in prefilled
            and 'value="4000.00"' in prefilled and 'value="1000.00"' in prefilled)

    # creating the plan from those values yields a deposit + balance schedule
    r = client.post("/payment-plans", data={"title": "Wedding Collection", "total": "4000.00",
                                            "deposit": "1000.00"})
    assert r.status_code in (200, 303)
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid(conn, creds["email"])
        amounts = sorted(row["amount_cents"] for row in conn.execute(
            "SELECT amount_cents FROM invoices WHERE tenant_id = ? AND plan_id IS NOT NULL", (tid,)))
        assert amounts == [100000, 300000]        # deposit 1000 + balance 3000
    finally:
        conn.close()
