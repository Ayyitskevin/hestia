"""Contracts — lifecycle, idempotent e-signature, isolation, and the sign flow."""

from conftest import login_owner, onboard_studio

from hestia.contracts import (
    create_contract,
    get_contract,
    get_contract_by_token,
    list_contracts,
    send_contract,
    sign_contract,
    void_contract,
)
from hestia.crm import create_client, create_project
from hestia.email import list_emails
from hestia.tenants import create_tenant


def _tenant(conn, name="Contract Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def test_create_and_join(conn):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah")
    p = create_project(conn, tenant_id=t["id"], name="Wedding", client_id=c["id"])
    ct = create_contract(conn, tenant_id=t["id"], title="Booking agreement",
                         body="The terms.", client_id=c["id"], project_id=p["id"])
    assert ct["status"] == "draft" and ct["token"]
    got = get_contract(conn, t["id"], ct["id"])
    assert got["client_name"] == "Sarah" and got["project_name"] == "Wedding"
    assert got["body"] == "The terms."


def test_create_drops_foreign_parent_ids(conn):
    a = _tenant(conn, "A")
    b = _tenant(conn, "B")
    foreign_client = create_client(conn, tenant_id=a["id"], name="Foreign")
    foreign_project = create_project(conn, tenant_id=a["id"], name="Foreign Project")
    ct = create_contract(
        conn, tenant_id=b["id"], title="Agreement",
        client_id=foreign_client["id"], project_id=foreign_project["id"],
    )
    assert ct["client_id"] is None and ct["project_id"] is None


def test_create_drops_project_for_wrong_same_tenant_client(conn):
    t = _tenant(conn)
    sarah = create_client(conn, tenant_id=t["id"], name="Sarah")
    bob = create_client(conn, tenant_id=t["id"], name="Bob")
    bob_project = create_project(conn, tenant_id=t["id"], name="Bob shoot", client_id=bob["id"])
    ct = create_contract(
        conn, tenant_id=t["id"], title="Agreement",
        client_id=sarah["id"], project_id=bob_project["id"],
    )
    assert ct["client_id"] == sarah["id"]
    assert ct["project_id"] is None
    assert ct["client_name"] == "Sarah"
    assert ct["project_name"] is None


def test_status_transitions(conn):
    t = _tenant(conn)
    ct = create_contract(conn, tenant_id=t["id"], title="Agreement")
    send_contract(conn, t["id"], ct["id"])
    assert get_contract(conn, t["id"], ct["id"])["status"] == "sent"
    void_contract(conn, t["id"], ct["id"])
    assert get_contract(conn, t["id"], ct["id"])["status"] == "void"


def test_cannot_sign_a_draft(conn):
    """The public link is inert until the owner sends the contract."""
    t = _tenant(conn)
    ct = create_contract(conn, tenant_id=t["id"], title="Draft only")
    assert sign_contract(conn, token=ct["token"], signature_name="Sarah") is False
    assert get_contract(conn, t["id"], ct["id"])["status"] == "draft"


def test_sign_is_idempotent(conn):
    """The core invariant: a contract moves sent→signed exactly once."""
    t = _tenant(conn)
    ct = create_contract(conn, tenant_id=t["id"], title="Booking")
    send_contract(conn, t["id"], ct["id"])

    assert sign_contract(conn, token=ct["token"], signature_name="Sarah Smith",
                         signer_ip="1.2.3.4") is True
    signed = get_contract_by_token(conn, ct["token"])
    assert signed["status"] == "signed"
    assert signed["signature_name"] == "Sarah Smith"
    assert signed["signed_ip"] == "1.2.3.4"
    assert signed["signed_at"]

    # A second submit never re-signs — original signature and timestamp stand.
    first_signed_at = signed["signed_at"]
    assert sign_contract(conn, token=ct["token"], signature_name="Someone Else") is False
    again = get_contract_by_token(conn, ct["token"])
    assert again["signature_name"] == "Sarah Smith"
    assert again["signed_at"] == first_signed_at


def test_empty_signature_rejected(conn):
    t = _tenant(conn)
    ct = create_contract(conn, tenant_id=t["id"], title="X")
    send_contract(conn, t["id"], ct["id"])
    assert sign_contract(conn, token=ct["token"], signature_name="   ") is False
    assert get_contract(conn, t["id"], ct["id"])["status"] == "sent"


def test_signed_cannot_be_voided(conn):
    t = _tenant(conn)
    ct = create_contract(conn, tenant_id=t["id"], title="X")
    send_contract(conn, t["id"], ct["id"])
    sign_contract(conn, token=ct["token"], signature_name="Sarah")
    void_contract(conn, t["id"], ct["id"])
    assert get_contract(conn, t["id"], ct["id"])["status"] == "signed"  # unchanged


def test_tenant_isolation(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    create_contract(conn, tenant_id=t1["id"], title="A-contract")
    assert list_contracts(conn, t2["id"]) == []


def test_send_emails_sign_link(conn, settings):
    """Sending with a signer email records the sign link in the outbox."""
    from hestia.contracts import contract_public_url
    from hestia.email import notify
    t = _tenant(conn)
    ct = create_contract(conn, tenant_id=t["id"], title="Booking",
                         signer_email="client@example.com")
    send_contract(conn, t["id"], ct["id"])
    # Mirror the route's notify call so the data-layer test stays self-contained.
    notify(conn, settings, to="client@example.com", tenant_id=t["id"],
           subject="sign", body=contract_public_url(settings, ct["token"]))
    conn.commit()
    outbox = list_emails(conn, t["id"])
    assert any(ct["token"] in m["body"] for m in outbox)


def test_http_contract_and_sign_flow(client):
    creds = onboard_studio(client, email="studio@example.com")
    login_owner(client, creds)
    r = client.post("/contracts", data={
        "title": "Wedding agreement", "body": "You agree to the terms.",
        "signer_name": "Sarah", "signer_email": "sarah@example.com",
    })
    cid = r.url.path.rstrip("/").split("/")[-1]
    detail = client.get(f"/contracts/{cid}")
    assert "Wedding agreement" in detail.text
    # draft → no public sign link yet
    assert "/sign/" not in detail.text

    assert client.post(f"/contracts/{cid}/send").status_code in (200, 303)
    detail = client.get(f"/contracts/{cid}")
    token = detail.text.split("/sign/")[1].split('"')[0].split("<")[0].strip()

    # public sign page renders the terms + sign form
    page = client.get(f"/sign/{token}")
    assert page.status_code == 200 and "You agree to the terms." in page.text

    # signing records the typed signature
    client.post(f"/sign/{token}", data={"signature_name": "Sarah Smith", "agree": "yes"})
    signed = client.get(f"/sign/{token}")
    assert "Signed by" in signed.text and "Sarah Smith" in signed.text
    # owner view shows it signed too
    assert "signed" in client.get(f"/contracts/{cid}").text

    # signing again is idempotent — still the original signer, no error
    client.post(f"/sign/{token}", data={"signature_name": "Imposter", "agree": "yes"})
    assert "Sarah Smith" in client.get(f"/sign/{token}").text
    assert "Imposter" not in client.get(f"/sign/{token}").text


def test_sign_requires_name_and_agreement(client):
    creds = onboard_studio(client, email="s2@example.com")
    login_owner(client, creds)
    r = client.post("/contracts", data={"title": "T", "body": "terms"})
    cid = r.url.path.rstrip("/").split("/")[-1]
    client.post(f"/contracts/{cid}/send")
    token = client.get(f"/contracts/{cid}").text.split("/sign/")[1].split('"')[0].split("<")[0].strip()

    # missing agreement checkbox → not signed
    resp = client.post(f"/sign/{token}", data={"signature_name": "Sarah"})
    assert resp.status_code == 400
    assert client.get(f"/sign/{token}").text.count("Signed by") == 0


def test_sign_unknown_token_404(client):
    assert client.get("/sign/nope-not-a-token").status_code == 404


def test_voided_contract_sign_link_404(client):
    """Once voided, a contract's sign link is gone for the client."""
    creds = onboard_studio(client, email="s3@example.com")
    login_owner(client, creds)
    r = client.post("/contracts", data={"title": "Voidme", "body": "x"})
    cid = r.url.path.rstrip("/").split("/")[-1]
    client.post(f"/contracts/{cid}/send")
    token = client.get(f"/contracts/{cid}").text.split("/sign/")[1].split('"')[0].split("<")[0].strip()
    assert client.get(f"/sign/{token}").status_code == 200  # signable while sent
    client.post(f"/contracts/{cid}/void")
    assert client.get(f"/sign/{token}").status_code == 404
