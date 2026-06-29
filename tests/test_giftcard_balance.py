"""Public gift-card balance check — code lookup (scoped, case-insensitive) and the states
(balance / fully used / expired / not found), plus the publish gate."""

from conftest import CSRFClient, login_owner, onboard_studio

from hestia.db import connect
from hestia.giftcards import create_gift_card, find_card_by_code
from hestia.tenants import create_tenant, slugify


def _tenant(conn, name="Balance Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def _tid_of(conn, email):
    return conn.execute(
        "SELECT t.id FROM tenants t JOIN users u ON u.tenant_id = t.id WHERE u.email = ?",
        (email,),
    ).fetchone()["id"]


def _publish(client):
    client.post("/settings/site", data={"headline": "x", "about": "y", "contact_email": "",
                                        "published": "1"})


def test_find_card_by_code_scoped_and_case_insensitive(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    create_gift_card(conn, tenant_id=t1["id"], initial_cents=5000, code="GC")
    conn.commit()
    assert find_card_by_code(conn, t1["id"], " gc ")["balance_cents"] == 5000   # normalized
    assert find_card_by_code(conn, t2["id"], "GC") is None                      # not this tenant's
    assert find_card_by_code(conn, t1["id"], "") is None


def test_http_balance_shows_amount_and_not_found(client, app):
    creds = onboard_studio(client, name="Show Studio", email="bal_show@example.com")
    login_owner(client, creds)
    slug = slugify("Show Studio")
    _publish(client)
    conn = connect(app.state.settings.db_path)
    try:
        create_gift_card(conn, tenant_id=_tid_of(conn, creds["email"]), initial_cents=5000, code="SHOW")
        conn.commit()
    finally:
        conn.close()
    pub = CSRFClient(app)
    assert "Check a gift card balance" in pub.get(f"/studio/{slug}/gift/balance").text
    ok = pub.post(f"/studio/{slug}/gift/balance", data={"code": "show"})
    assert "Balance" in ok.text and "$50.00" in ok.text
    miss = pub.post(f"/studio/{slug}/gift/balance", data={"code": "NOPE"})
    assert "find a gift card" in miss.text                       # not-found message


def test_http_balance_states(client, app):
    creds = onboard_studio(client, name="States Studio", email="bal_state@example.com")
    login_owner(client, creds)
    slug = slugify("States Studio")
    _publish(client)
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        create_gift_card(conn, tenant_id=tid, initial_cents=5000, code="SPENT")
        create_gift_card(conn, tenant_id=tid, initial_cents=5000, code="GONE", expires_on="2000-01-01")
        conn.execute("UPDATE gift_cards SET balance_cents = 0 WHERE code = 'SPENT'")
        conn.commit()
    finally:
        conn.close()
    pub = CSRFClient(app)
    assert "fully used" in pub.post(f"/studio/{slug}/gift/balance", data={"code": "SPENT"}).text
    assert "expired" in pub.post(f"/studio/{slug}/gift/balance", data={"code": "GONE"}).text


def test_balance_page_gated_on_publish(client, app):
    creds = onboard_studio(client, name="Hidden Bal", email="bal_hidden@example.com")
    login_owner(client, creds)
    slug = slugify("Hidden Bal")
    assert "coming soon" in client.get(f"/studio/{slug}/gift/balance").text.lower()
    assert client.get("/studio/no-such-studio/gift/balance").status_code == 404
