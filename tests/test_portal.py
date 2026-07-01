"""Client portal — token lifecycle, aggregation, isolation, and the public hub."""

import io

from conftest import login_owner, onboard_studio

from hestia.contracts import create_contract, send_contract
from hestia.crm import assign_gallery_to_project, create_client, create_project, list_projects
from hestia.db import connect
from hestia.delivery import enable_delivery
from hestia.galleries import create_gallery, publish_gallery
from hestia.invoices import create_invoice
from hestia.payment_plans import create_payment_plan, deposit_balance_installments
from hestia.portal import (
    assemble_portal,
    enable_portal,
    get_client_by_portal_token,
    regenerate_portal_token,
)
from hestia.project_files import add_project_file
from hestia.questionnaires import create_questionnaire, send_questionnaire
from hestia.scheduler import create_appointment
from hestia.tenants import create_tenant
from hestia.testimonials import request_testimonial


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


def test_project_gallery_count_ignores_foreign_gallery_rows(conn):
    owner = _tenant(conn, "Owner")
    foreign = _tenant(conn, "Foreign")
    c = create_client(conn, tenant_id=owner["id"], name="Sarah")
    p = create_project(conn, tenant_id=owner["id"], name="Wedding", client_id=c["id"])
    # Malformed legacy row: a foreign-tenant gallery points at this tenant's project id.
    create_gallery(conn, tenant_id=foreign["id"], title="Foreign", client_name="")
    conn.execute(
        "UPDATE galleries SET project_id = ? WHERE tenant_id = ?",
        (p["id"], foreign["id"]),
    )
    projects = list_projects(conn, owner["id"], client_id=c["id"])
    assert projects[0]["gallery_count"] == 0


def test_portal_surfaces_download_and_review(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah")
    p = create_project(conn, tenant_id=t["id"], name="Wedding", client_id=c["id"])
    g = create_gallery(conn, tenant_id=t["id"], title="Finals")
    assign_gallery_to_project(conn, t["id"], g["id"], p["id"])
    publish_gallery(conn, t["id"], g["id"])
    dtoken = enable_delivery(conn, t["id"], g["id"])                       # digital delivery on
    tt = request_testimonial(conn, tenant_id=t["id"], client_id=c["id"])  # pending review
    conn.commit()

    client = get_client_by_portal_token(conn, enable_portal(conn, t["id"], c["id"]))
    data = assemble_portal(conn, settings, client)
    gallery = next(x for x in data["galleries"] if x["title"] == "Finals")
    assert gallery["download_url"] and dtoken in gallery["download_url"]
    assert data["review_url"] and tt["token"] in data["review_url"]


def test_portal_action_room_prioritizes_client_next_steps(conn, settings, storage):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah")
    p = create_project(conn, tenant_id=t["id"], name="Wedding", client_id=c["id"])
    ct = create_contract(conn, tenant_id=t["id"], title="Agreement", client_id=c["id"])
    send_contract(conn, t["id"], ct["id"])
    create_appointment(conn, tenant_id=t["id"], title="Consultation",
                       options=["2030-01-01 10:00"], client_id=c["id"])
    q = create_questionnaire(conn, tenant_id=t["id"], title="Timeline",
                             prompts=["What time do you arrive?"], client_id=c["id"])
    send_questionnaire(conn, t["id"], q["id"])
    create_payment_plan(conn, settings, tenant_id=t["id"], title="Wedding", client_id=c["id"],
                        installments=deposit_balance_installments(total_cents=400000,
                                                                  deposit_cents=100000))
    create_invoice(conn, settings, tenant_id=t["id"], title="Print credit", amount_cents=5000,
                   client_id=c["id"])
    g = create_gallery(conn, tenant_id=t["id"], title="Finals")
    assign_gallery_to_project(conn, t["id"], g["id"], p["id"])
    publish_gallery(conn, t["id"], g["id"])
    enable_delivery(conn, t["id"], g["id"])
    add_project_file(conn, storage, tenant_id=t["id"], project_id=p["id"],
                     filename="timeline.pdf", fileobj=io.BytesIO(b"PLAN"))
    request_testimonial(conn, tenant_id=t["id"], client_id=c["id"])
    conn.commit()

    token = enable_portal(conn, t["id"], c["id"])
    data = assemble_portal(conn, settings, get_client_by_portal_token(conn, token))
    actions = data["actions"]
    kinds = [a["kind"] for a in actions]

    assert kinds[:3] == ["sign", "book", "form"]
    assert kinds.count("pay") == 3                    # two plan installments + one standalone invoice
    assert "download" in kinds and "file" in kinds and kinds[-1] == "review"
    assert data["action_summary"] == {"todo_count": 7, "ready_count": 2}
    assert actions[0]["href"].endswith(f"/sign/{ct['token']}")


def test_portal_omits_download_and_review_when_absent(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Plain")
    p = create_project(conn, tenant_id=t["id"], name="Shoot", client_id=c["id"])
    g = create_gallery(conn, tenant_id=t["id"], title="Plain")
    assign_gallery_to_project(conn, t["id"], g["id"], p["id"])
    publish_gallery(conn, t["id"], g["id"])
    conn.commit()
    client = get_client_by_portal_token(conn, enable_portal(conn, t["id"], c["id"]))
    data = assemble_portal(conn, settings, client)
    assert data["galleries"][0]["download_url"] is None                   # delivery not enabled
    assert data["review_url"] is None                                     # no pending review


def test_http_portal_shows_download_and_review(client, app):
    creds = onboard_studio(client, email="hub@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        c = create_client(conn, tenant_id=tid, name="Hub Client")
        p = create_project(conn, tenant_id=tid, name="Wedding", client_id=c["id"])
        g = create_gallery(conn, tenant_id=tid, title="Finals")
        assign_gallery_to_project(conn, tid, g["id"], p["id"])
        publish_gallery(conn, tid, g["id"])
        enable_delivery(conn, tid, g["id"])
        request_testimonial(conn, tenant_id=tid, client_id=c["id"])
        portal_tok = enable_portal(conn, tid, c["id"])
        conn.commit()
    finally:
        conn.close()
    page = client.get(f"/portal/{portal_tok}")
    assert page.status_code == 200
    assert "Download" in page.text and "Leave a review" in page.text


def test_http_pending_review_is_in_attention_banner(client, app):
    creds = onboard_studio(client, email="review@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        c = create_client(conn, tenant_id=tid, name="Reviewer")
        request_testimonial(conn, tenant_id=tid, client_id=c["id"])   # pending review request
        portal_tok = enable_portal(conn, tid, c["id"])
        conn.commit()
    finally:
        conn.close()
    page = client.get(f"/portal/{portal_tok}").text
    assert "Action room" in page                                      # the to-do banner renders
    assert "Leave review" in page                                     # the review is a surfaced to-do


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


def test_portal_confirmed_appointment_has_calendar_link(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah")
    conf = create_appointment(conn, tenant_id=t["id"], title="Engagement", options=["x"],
                              client_id=c["id"])
    conn.execute("UPDATE appointments SET status='confirmed', starts_at='2030-01-01 10:00' WHERE id=?",
                 (conf["id"],))
    create_appointment(conn, tenant_id=t["id"], title="Maybe", options=["y"], client_id=c["id"])
    conn.commit()

    client = get_client_by_portal_token(conn, enable_portal(conn, t["id"], c["id"]))
    appts = {a["title"]: a for a in assemble_portal(conn, settings, client)["appointments"]}
    assert appts["Engagement"]["calendar_url"].endswith(f"/book/{conf['token']}/calendar.ics")
    assert appts["Maybe"]["calendar_url"] is None         # proposed → no calendar link yet


def test_portal_surfaces_invoice_note(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah")
    create_invoice(conn, settings, tenant_id=t["id"], title="Balance", amount_cents=5000,
                   client_id=c["id"], note="Venmo also accepted")
    conn.commit()
    client = get_client_by_portal_token(conn, enable_portal(conn, t["id"], c["id"]))
    assert assemble_portal(conn, settings, client)["invoices"][0]["note"] == "Venmo also accepted"


def test_http_portal_renders_calendar_link_and_note(client, app):
    login_owner(client, onboard_studio(client, email="pp@example.com"))
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        c = create_client(conn, tenant_id=tid, name="Sarah")
        appt = create_appointment(conn, tenant_id=tid, title="Engagement", options=["x"],
                                  client_id=c["id"])
        conn.execute("UPDATE appointments SET status='confirmed', starts_at='2030-01-01 10:00' "
                     "WHERE id=?", (appt["id"],))
        create_invoice(conn, app.state.settings, tenant_id=tid, title="Balance", amount_cents=5000,
                       client_id=c["id"], note="Venmo also accepted")
        tok = enable_portal(conn, tid, c["id"])
        conn.commit()
    finally:
        conn.close()
    page = client.get(f"/portal/{tok}")
    assert page.status_code == 200
    assert "Add to calendar" in page.text and "Venmo also accepted" in page.text


def test_http_portal_links_receipts_for_paid_items(client, app):
    """A paid invoice and a paid plan installment each link to their printable receipt."""
    login_owner(client, onboard_studio(client, email="rcpt@example.com"))
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        c = create_client(conn, tenant_id=tid, name="Paid Client")
        inv = create_invoice(conn, app.state.settings, tenant_id=tid, title="Deposit",
                             amount_cents=5000, client_id=c["id"])
        conn.execute("UPDATE invoices SET status='paid', paid_at=datetime('now') WHERE id=?", (inv["id"],))
        create_payment_plan(conn, app.state.settings, tenant_id=tid, title="Wedding", client_id=c["id"],
                            installments=deposit_balance_installments(total_cents=400000, deposit_cents=100000))
        conn.execute("UPDATE invoices SET status='paid', paid_at=datetime('now') "
                     "WHERE tenant_id=? AND plan_id IS NOT NULL AND amount_cents=100000", (tid,))
        tok = enable_portal(conn, tid, c["id"])
        conn.commit()
        inv_tok = inv["token"]
    finally:
        conn.close()
    page = client.get(f"/portal/{tok}").text
    assert "View receipt" in page                          # paid standalone invoice
    assert f"/pay/{inv_tok}/receipt" in page
    assert page.count("/receipt") >= 2                      # invoice + paid installment both link a receipt


def test_portal_shows_balance_summary(conn, settings):
    from hestia.invoices import send_invoice
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Bal")
    paid = create_invoice(conn, settings, tenant_id=t["id"], title="Deposit", amount_cents=20000,
                          client_id=c["id"])
    conn.execute("UPDATE invoices SET status='paid', paid_at=datetime('now') WHERE id=?", (paid["id"],))
    owed = create_invoice(conn, settings, tenant_id=t["id"], title="Balance", amount_cents=30000,
                          client_id=c["id"])
    send_invoice(conn, t["id"], owed["id"])                       # issued, unpaid
    conn.commit()
    data = assemble_portal(conn, settings,
                           get_client_by_portal_token(conn, enable_portal(conn, t["id"], c["id"])))
    s = data["statement"]
    assert s["billed_cents"] == 50000 and s["paid_cents"] == 20000 and s["outstanding_cents"] == 30000


def test_http_portal_renders_balance(client, app):
    login_owner(client, onboard_studio(client, email="bal@example.com"))
    conn = connect(app.state.settings.db_path)
    try:
        from hestia.invoices import send_invoice
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        c = create_client(conn, tenant_id=tid, name="Bal")
        owed = create_invoice(conn, app.state.settings, tenant_id=tid, title="Balance",
                              amount_cents=30000, client_id=c["id"])
        send_invoice(conn, tid, owed["id"])
        tok = enable_portal(conn, tid, c["id"])
        conn.commit()
    finally:
        conn.close()
    page = client.get(f"/portal/{tok}").text
    assert "outstanding" in page and "$300.00" in page


# ── shared files ────────────────────────────────────────────────────────────────


def test_portal_assembles_client_files_with_download_urls(conn, settings, storage):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah")
    p = create_project(conn, tenant_id=t["id"], name="Wedding", client_id=c["id"])
    f = add_project_file(conn, storage, tenant_id=t["id"], project_id=p["id"],
                         filename="timeline.pdf", fileobj=io.BytesIO(b"PLAN"))
    conn.commit()
    tok = enable_portal(conn, t["id"], c["id"])
    data = assemble_portal(conn, settings, get_client_by_portal_token(conn, tok))
    assert [x["filename"] for x in data["files"]] == ["timeline.pdf"]
    assert data["files"][0]["download_url"].endswith(f"/portal/{tok}/files/{f['id']}")


def test_http_portal_lists_and_downloads_file(client, app):
    login_owner(client, onboard_studio(client, email="pfiles@example.com"))
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        c = create_client(conn, tenant_id=tid, name="Sarah")
        p = create_project(conn, tenant_id=tid, name="Wedding", client_id=c["id"])
        f = add_project_file(conn, app.state.storage, tenant_id=tid, project_id=p["id"],
                             filename="timeline.pdf", fileobj=io.BytesIO(b"PLAN-BYTES"),
                             content_type="application/pdf")
        tok = enable_portal(conn, tid, c["id"])
        conn.commit()
        fid = f["id"]
    finally:
        conn.close()

    page = client.get(f"/portal/{tok}")
    assert page.status_code == 200
    assert "timeline.pdf" in page.text and f"/portal/{tok}/files/{fid}" in page.text

    d = client.get(f"/portal/{tok}/files/{fid}")
    assert d.status_code == 200 and d.content == b"PLAN-BYTES"
    assert 'attachment; filename="timeline.pdf"' in d.headers["content-disposition"]  # never inline


def test_http_portal_token_cannot_reach_another_clients_file(client, app):
    """The security gate: a client's portal token only downloads files on its OWN
    projects. A sibling client in the same tenant (and an unassigned project's file)
    are both unreachable — even though the row lives under the same tenant."""
    login_owner(client, onboard_studio(client, email="gate@example.com"))
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        alice = create_client(conn, tenant_id=tid, name="Alice")
        bob = create_client(conn, tenant_id=tid, name="Bob")
        pb = create_project(conn, tenant_id=tid, name="Bob shoot", client_id=bob["id"])
        bob_file = add_project_file(conn, app.state.storage, tenant_id=tid, project_id=pb["id"],
                                    filename="bob-secret.pdf", fileobj=io.BytesIO(b"BOB-SECRET"))
        loose = create_project(conn, tenant_id=tid, name="Unassigned")  # no client_id
        loose_file = add_project_file(conn, app.state.storage, tenant_id=tid, project_id=loose["id"],
                                      filename="loose.pdf", fileobj=io.BytesIO(b"LOOSE"))
        alice_tok = enable_portal(conn, tid, alice["id"])
        conn.commit()
        bob_fid, loose_fid = bob_file["id"], loose_file["id"]
    finally:
        conn.close()

    r = client.get(f"/portal/{alice_tok}/files/{bob_fid}")
    assert r.status_code == 404 and b"BOB-SECRET" not in r.content
    r2 = client.get(f"/portal/{alice_tok}/files/{loose_fid}")
    assert r2.status_code == 404 and b"LOOSE" not in r2.content


def test_http_portal_file_download_bad_token_or_id_404(client, app):
    login_owner(client, onboard_studio(client, email="badtok@example.com"))
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        c = create_client(conn, tenant_id=tid, name="Sarah")
        p = create_project(conn, tenant_id=tid, name="Wedding", client_id=c["id"])
        f = add_project_file(conn, app.state.storage, tenant_id=tid, project_id=p["id"],
                             filename="x.pdf", fileobj=io.BytesIO(b"X"))
        tok = enable_portal(conn, tid, c["id"])
        conn.commit()
        fid = f["id"]
    finally:
        conn.close()
    assert client.get(f"/portal/nope-not-real/files/{fid}").status_code == 404  # bad token
    assert client.get(f"/portal/{tok}/files/999999").status_code == 404         # unknown file id
