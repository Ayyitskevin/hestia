"""Client portal — token lifecycle, aggregation, isolation, and the public hub."""

from conftest import login_owner, onboard_studio

from hestia.contracts import create_contract, send_contract
from hestia.crm import assign_gallery_to_project, create_client, create_project
from hestia.galleries import create_gallery, publish_gallery
from hestia.invoices import create_invoice
from hestia.payment_plans import create_payment_plan, deposit_balance_installments
from hestia.portal import (
    assemble_portal,
    enable_portal,
    get_client_by_portal_token,
    regenerate_portal_token,
)
from hestia.tenants import create_tenant


def _tenant(conn, name="Portal Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def test_enable_is_idempotent(conn):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah")
    tok = enable_portal(conn, t["id"], c["id"])
    assert tok
    # enabling again preserves the link the client already has
    assert enable_portal(conn, t["id"], c["id"]) == tok


def test_regenerate_revokes_old(conn):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah")
    old = enable_portal(conn, t["id"], c["id"])
    new = regenerate_portal_token(conn, t["id"], c["id"])
    assert new and new != old
    assert get_client_by_portal_token(conn, old) is None
    assert get_client_by_portal_token(conn, new)["id"] == c["id"]


def test_enable_unknown_client(conn):
    t = _tenant(conn)
    assert enable_portal(conn, t["id"], 9999) is None


def test_assemble_aggregates_client_items(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah")
    p = create_project(conn, tenant_id=t["id"], name="Wedding", client_id=c["id"])
    ct = create_contract(conn, tenant_id=t["id"], title="Agreement", client_id=c["id"])
    send_contract(conn, t["id"], ct["id"])
    create_payment_plan(conn, settings, tenant_id=t["id"], title="Wedding", client_id=c["id"],
                        installments=deposit_balance_installments(total_cents=400000,
                                                                  deposit_cents=100000))
    create_invoice(conn, settings, tenant_id=t["id"], title="Extra print", amount_cents=5000,
                   client_id=c["id"])
    # a published gallery on the client's project shows; a draft one does not
    g_pub = create_gallery(conn, tenant_id=t["id"], title="Wedding Gallery")
    assign_gallery_to_project(conn, t["id"], g_pub["id"], p["id"])
    publish_gallery(conn, t["id"], g_pub["id"])
    g_draft = create_gallery(conn, tenant_id=t["id"], title="Draft Gallery")
    assign_gallery_to_project(conn, t["id"], g_draft["id"], p["id"])

    client = get_client_by_portal_token(conn, enable_portal(conn, t["id"], c["id"]))
    data = assemble_portal(conn, settings, client)
    assert [p_["name"] for p_ in data["projects"]] == ["Wedding"]
    assert data["contracts"][0]["sign_url"].endswith(f"/sign/{ct['token']}")
    assert data["plans"][0]["total_cents"] == 400000
    assert data["plans"][0]["installments"][0]["pay_url"]
    assert data["invoices"][0]["pay_url"]
    titles = [g["title"] for g in data["galleries"]]
    assert "Wedding Gallery" in titles and "Draft Gallery" not in titles


def test_isolation_token_resolves_only_its_client(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    c1 = create_client(conn, tenant_id=t1["id"], name="A-client")
    create_client(conn, tenant_id=t2["id"], name="B-client")
    tok = enable_portal(conn, t1["id"], c1["id"])
    resolved = get_client_by_portal_token(conn, tok)
    assert resolved["tenant_id"] == t1["id"] and resolved["name"] == "A-client"


def test_http_enable_and_view_portal(client):
    creds = onboard_studio(client, email="portal@example.com")
    login_owner(client, creds)
    rc = client.post("/clients", data={"name": "Sarah", "email": "sarah@example.com"})
    cid = rc.url.path.rstrip("/").split("/")[-1]

    # no portal link until enabled
    assert "/portal/" not in client.get(f"/clients/{cid}").text
    client.post(f"/clients/{cid}/portal")
    detail = client.get(f"/clients/{cid}").text
    assert "/portal/" in detail
    token = detail.split("/portal/")[1].split('"')[0].split("<")[0].strip()

    # give the client something to act on; read the new contract id off the redirect
    rct = client.post("/contracts", data={"title": "Booking", "body": "terms", "client_id": cid})
    contract_id = rct.url.path.rstrip("/").split("/")[-1]
    client.post(f"/contracts/{contract_id}/send")  # send so it shows a sign link

    page = client.get(f"/portal/{token}")
    assert page.status_code == 200
    assert "Welcome, Sarah" in page.text
    assert "/sign/" in page.text  # the unsigned contract's sign link


def test_http_regenerate_revokes(client):
    creds = onboard_studio(client, email="rot@example.com")
    login_owner(client, creds)
    rc = client.post("/clients", data={"name": "Sarah"})
    cid = rc.url.path.rstrip("/").split("/")[-1]
    client.post(f"/clients/{cid}/portal")
    old = client.get(f"/clients/{cid}").text.split("/portal/")[1].split('"')[0].split("<")[0].strip()
    assert client.get(f"/portal/{old}").status_code == 200

    client.post(f"/clients/{cid}/portal/regenerate")
    new = client.get(f"/clients/{cid}").text.split("/portal/")[1].split('"')[0].split("<")[0].strip()
    assert new != old
    assert client.get(f"/portal/{old}").status_code == 404
    assert client.get(f"/portal/{new}").status_code == 200


def test_http_unknown_portal_404(client):
    assert client.get("/portal/nope-not-a-real-token").status_code == 404
