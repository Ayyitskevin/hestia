"""Client activity timeline — chronological aggregation across the whole loop."""

from conftest import login_owner, onboard_studio

from hestia.contracts import create_contract
from hestia.crm import (
    assign_gallery_to_project,
    client_timeline,
    create_client,
    create_project,
)
from hestia.db import connect
from hestia.galleries import create_gallery, publish_gallery
from hestia.invoices import create_invoice
from hestia.questionnaires import create_questionnaire
from hestia.scheduler import create_appointment
from hestia.tenants import create_tenant


def test_timeline_aggregates_the_loop(conn, settings):
    t = create_tenant(conn, name="Studio", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Sarah", email="s@example.com")
    p = create_project(conn, tenant_id=t["id"], name="Wedding", client_id=c["id"])

    ct = create_contract(conn, tenant_id=t["id"], title="Booking", client_id=c["id"])
    conn.execute("UPDATE contracts SET status = 'signed', signed_at = datetime('now') WHERE id = ?",
                 (ct["id"],))
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="Balance",
                         amount_cents=250000, client_id=c["id"])
    conn.execute("UPDATE invoices SET status = 'paid', paid_at = datetime('now') WHERE id = ?",
                 (inv["id"],))
    ap = create_appointment(conn, tenant_id=t["id"], title="Engagement",
                            options=["2026-08-01 10:00"], client_id=c["id"])
    conn.execute("UPDATE appointments SET status = 'confirmed', starts_at = '2026-08-01 10:00' "
                 "WHERE id = ?", (ap["id"],))
    q = create_questionnaire(conn, tenant_id=t["id"], title="Intake", prompts=["Q"], client_id=c["id"])
    conn.execute("UPDATE questionnaires SET status = 'completed' WHERE id = ?", (q["id"],))
    g = create_gallery(conn, tenant_id=t["id"], title="Finals")
    assign_gallery_to_project(conn, t["id"], g["id"], p["id"])
    publish_gallery(conn, t["id"], g["id"])
    conn.commit()

    tl = client_timeline(conn, t["id"], c["id"])
    labels = " | ".join(e["label"] for e in tl)
    assert "Added as a client" in labels
    assert "Project created — Wedding" in labels
    assert "Signed contract — Booking" in labels
    assert "Paid invoice — Balance ($2,500.00)" in labels
    assert "Session booked — Engagement" in labels
    assert "Completed questionnaire — Intake" in labels
    assert "Gallery delivered — Finals" in labels
    inv_ev = next(e for e in tl if e["label"].startswith("Paid invoice"))
    assert inv_ev["url"] == f"/invoices/{inv['id']}"
    # the future-dated booked session sorts to the top (newest first)
    assert tl[0]["label"].startswith("Session booked")


def test_timeline_is_tenant_scoped(conn, settings):
    a = create_tenant(conn, name="A", shoot_type="wedding")
    b = create_tenant(conn, name="B", shoot_type="wedding")
    ca = create_client(conn, tenant_id=a["id"], name="A-client")
    cb = create_client(conn, tenant_id=b["id"], name="B-client")
    create_invoice(conn, settings, tenant_id=b["id"], title="B-inv", amount_cents=100, client_id=cb["id"])
    conn.commit()
    assert [e["label"] for e in client_timeline(conn, a["id"], ca["id"])] == ["Added as a client"]
    assert client_timeline(conn, a["id"], 99999) == []                # unknown client → empty


def test_timeline_skips_drafts_and_void(conn, settings):
    t = create_tenant(conn, name="D", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="C")
    create_contract(conn, tenant_id=t["id"], title="Draft", client_id=c["id"])           # draft → shown as 'Drafted'
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="Void", amount_cents=100, client_id=c["id"])
    conn.execute("UPDATE invoices SET status = 'void' WHERE id = ?", (inv["id"],))         # void → excluded
    create_questionnaire(conn, tenant_id=t["id"], title="Qdraft", prompts=["x"], client_id=c["id"])  # draft → excluded
    conn.commit()
    labels = [e["label"] for e in client_timeline(conn, t["id"], c["id"])]
    assert any("Void" not in lbl for lbl in labels) and not any("Void" in lbl for lbl in labels)
    assert not any("Qdraft" in lbl for lbl in labels)                 # draft questionnaire hidden
    assert any("Drafted contract — Draft" in lbl for lbl in labels)   # draft contract shown as drafted


# --- HTTP -------------------------------------------------------------------

def test_client_detail_shows_activity(client, app):
    creds = onboard_studio(client, email="tl@example.com")
    login_owner(client, creds)
    rc = client.post("/clients", data={"name": "Tina", "email": "t@example.com"})
    cid = rc.url.path.rstrip("/").split("/")[-1]
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        create_invoice(conn, app.state.settings, tenant_id=tid, title="Deposit",
                       amount_cents=50000, client_id=int(cid))
        conn.commit()
    finally:
        conn.close()
    page = client.get(f"/clients/{cid}")
    assert "Activity" in page.text
    assert "Added as a client" in page.text and "Deposit" in page.text
