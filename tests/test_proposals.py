"""Proposals — package-backed quote, agreement, and deposit flow."""

from conftest import CSRFClient, login_owner, onboard_studio

from hestia.contracts import get_contract
from hestia.crm import create_client, create_project
from hestia.db import connect
from hestia.email import list_emails
from hestia.invoices import get_invoice
from hestia.packages import create_package, list_packages
from hestia.proposals import (
    accept_proposal,
    create_proposal,
    get_proposal,
    list_proposals,
    send_proposal,
)
from hestia.tenants import create_tenant


def _tenant(conn, name="Proposal Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def _tid(conn, email):
    return conn.execute(
        "SELECT t.id FROM tenants t JOIN users u ON u.tenant_id = t.id WHERE u.email = ?",
        (email,),
    ).fetchone()["id"]


def test_create_proposal_creates_linked_contract_and_invoice(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah", email="sarah@example.com")
    p = create_project(conn, tenant_id=t["id"], name="June wedding", client_id=c["id"])
    pkg = create_package(conn, tenant_id=t["id"], name="Wedding Collection",
                         description="8h coverage", price_cents=350000,
                         deposit_cents=100000)

    proposal = create_proposal(
        conn,
        settings,
        tenant_id=t["id"],
        package_id=pkg["id"],
        title="June wedding proposal",
        client_id=c["id"],
        project_id=p["id"],
    )

    assert proposal["status"] == "draft"
    assert proposal["client_name"] == "Sarah"
    assert proposal["project_name"] == "June wedding"
    assert proposal["package_name"] == "Wedding Collection"
    assert proposal["package_price_display"] == "$3,500.00"
    assert proposal["invoice_amount_display"] == "$1,000.00"

    contract = get_contract(conn, t["id"], proposal["contract_id"])
    invoice = get_invoice(conn, t["id"], proposal["invoice_id"])
    assert contract["status"] == "draft" and "8h coverage" in contract["body"]
    assert invoice["status"] == "draft" and invoice["amount_cents"] == 100000

    sent = send_proposal(conn, t["id"], proposal["id"])
    assert sent["status"] == "sent"
    assert get_contract(conn, t["id"], proposal["contract_id"])["status"] == "sent"
    assert get_invoice(conn, t["id"], proposal["invoice_id"])["status"] == "sent"


def test_proposal_acceptance_is_idempotent(conn, settings):
    t = _tenant(conn)
    pkg = create_package(conn, tenant_id=t["id"], name="Portraits", price_cents=50000)
    proposal = create_proposal(conn, settings, tenant_id=t["id"], package_id=pkg["id"],
                               title="Portrait proposal")
    send_proposal(conn, t["id"], proposal["id"])

    assert accept_proposal(conn, token=proposal["token"], accepted_name="Ava") is True
    accepted = get_proposal(conn, t["id"], proposal["id"])
    assert accepted["status"] == "accepted" and accepted["accepted_name"] == "Ava"

    assert accept_proposal(conn, token=proposal["token"], accepted_name="Someone Else") is False
    again = get_proposal(conn, t["id"], proposal["id"])
    assert again["accepted_name"] == "Ava"


def test_proposal_tenant_isolation(conn, settings):
    a = _tenant(conn, "A")
    b = _tenant(conn, "B")
    pkg = create_package(conn, tenant_id=a["id"], name="A-only", price_cents=99900)
    proposal = create_proposal(conn, settings, tenant_id=a["id"], package_id=pkg["id"],
                               title="Secret")

    assert list_proposals(conn, b["id"]) == []
    assert get_proposal(conn, b["id"], proposal["id"]) is None
    assert create_proposal(conn, settings, tenant_id=b["id"], package_id=pkg["id"],
                           title="Cross-tenant") is None


def test_http_proposal_publish_and_accept_flow(client, app):
    creds = onboard_studio(client, name="Lens Studio", email="lens@example.com")
    login_owner(client, creds)
    client.post("/clients", data={"name": "Sarah Client", "email": "sarah@example.com"})

    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid(conn, creds["email"])
        cid = conn.execute("SELECT id FROM clients WHERE tenant_id = ?", (tid,)).fetchone()["id"]
    finally:
        conn.close()
    client.post("/projects", data={"name": "June wedding", "client_id": str(cid),
                                   "shoot_type": "wedding", "status": "lead"})
    client.post("/packages", data={"name": "Wedding Collection",
                                    "description": "8h coverage and gallery",
                                    "price": "3500", "deposit": "1000"})

    conn = connect(app.state.settings.db_path)
    try:
        pid = list_packages(conn, tid)[0]["id"]
        project_id = conn.execute("SELECT id FROM projects WHERE tenant_id = ?",
                                  (tid,)).fetchone()["id"]
    finally:
        conn.close()

    new_page = client.get(f"/proposals/new?package_id={pid}&client_id={cid}&project_id={project_id}")
    assert new_page.status_code == 200
    assert "Wedding Collection" in new_page.text and "New proposal" in new_page.text

    r = client.post("/proposals", data={"package_id": str(pid), "title": "June wedding proposal",
                                        "summary": "A polished booking path.",
                                        "client_id": str(cid), "project_id": str(project_id)})
    proposal_id = r.url.path.rstrip("/").split("/")[-1]
    detail = client.get(f"/proposals/{proposal_id}")
    assert "Publish proposal" in detail.text
    assert "/proposal/" not in detail.text

    conn = connect(app.state.settings.db_path)
    try:
        proposal = get_proposal(conn, tid, int(proposal_id))
        token = proposal["token"]
    finally:
        conn.close()

    public = CSRFClient(app)
    assert public.get(f"/proposal/{token}").status_code == 404

    client.post(f"/proposals/{proposal_id}/send")
    detail = client.get(f"/proposals/{proposal_id}")
    assert f"/proposal/{token}" in detail.text
    page = public.get(f"/proposal/{token}")
    assert page.status_code == 200
    assert "Wedding Collection" in page.text and "$1,000.00 due to reserve" in page.text

    rejected = public.post(f"/proposal/{token}/accept", data={"accepted_name": "Sarah"})
    assert rejected.status_code == 400

    public.post(f"/proposal/{token}/accept", data={"accepted_name": "Sarah Client",
                                                   "accepted_email": "sarah@example.com",
                                                   "agree": "1"})
    accepted_page = public.get(f"/proposal/{token}")
    assert "Proposal accepted" in accepted_page.text
    assert "Sign agreement" in accepted_page.text and "Pay booking invoice" in accepted_page.text

    conn = connect(app.state.settings.db_path)
    try:
        proposal = get_proposal(conn, tid, int(proposal_id))
        assert proposal["status"] == "accepted"
        assert proposal["contract_status"] == "sent"
        assert proposal["invoice_status"] == "sent"
        outbox = list_emails(conn, tid)
        assert any(f"/proposal/{token}" in m["body"] for m in outbox)
    finally:
        conn.close()


def test_http_cross_tenant_package_prefill_hidden(client, app):
    a = onboard_studio(client, name="A", email="a@example.com")
    login_owner(client, a)
    client.post("/packages", data={"name": "A-only", "price": "777"})
    conn = connect(app.state.settings.db_path)
    try:
        a_pid = list_packages(conn, _tid(conn, a["email"]))[0]["id"]
    finally:
        conn.close()

    b_client = CSRFClient(app)
    b = onboard_studio(b_client, name="B", email="b@example.com")
    login_owner(b_client, b)
    page = b_client.get(f"/proposals/new?package_id={a_pid}")
    assert "A-only" not in page.text
