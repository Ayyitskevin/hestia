"""Referrals — per-client referral links that attribute new inquiries."""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client, get_project
from hestia.db import connect
from hestia.referrals import (
    attribute_referral,
    client_by_referral_code,
    referral_code_for,
    referral_link,
)
from hestia.studio import create_inquiry
from hestia.tenants import create_tenant, slugify


def _tenant(conn, name="Ref Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


# --- module logic -----------------------------------------------------------

def test_referral_code_lazy_and_idempotent(conn):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Referrer")
    code = referral_code_for(conn, t["id"], c["id"])
    assert code
    assert referral_code_for(conn, t["id"], c["id"]) == code   # idempotent
    assert referral_code_for(conn, t["id"], 999999) is None    # unknown client


def test_client_by_referral_code_scoped(conn):
    t1, t2 = _tenant(conn, "T1"), _tenant(conn, "T2")
    c = create_client(conn, tenant_id=t1["id"], name="A")
    code = referral_code_for(conn, t1["id"], c["id"])
    assert client_by_referral_code(conn, t1["id"], code)["id"] == c["id"]
    assert client_by_referral_code(conn, t2["id"], code) is None   # other tenant can't resolve it
    assert client_by_referral_code(conn, t1["id"], "") is None     # blank never matches a default row


def test_attribute_referral_tags_or_noops(conn):
    t = _tenant(conn)
    referrer = create_client(conn, tenant_id=t["id"], name="Referrer")
    code = referral_code_for(conn, t["id"], referrer["id"])
    lead = create_inquiry(conn, tenant=t, name="New Lead", email="new@x.com")
    assert attribute_referral(conn, t["id"], lead["id"], code) == referrer["id"]
    assert get_project(conn, t["id"], lead["id"])["referred_by_client_id"] == referrer["id"]
    # an unknown code is a no-op — the lead stays organic
    organic = create_inquiry(conn, tenant=t, name="Organic", email="org@x.com")
    assert attribute_referral(conn, t["id"], organic["id"], "nope") is None
    assert get_project(conn, t["id"], organic["id"])["referred_by_client_id"] is None


def test_referral_link_format(settings):
    assert referral_link(settings, "my-studio", "abc123") == \
        "http://testserver/studio/my-studio?ref=abc123"


# --- HTTP flow --------------------------------------------------------------

def _published(client, *, name, email):
    creds = onboard_studio(client, name=name, email=email)
    login_owner(client, creds)
    client.post("/settings/site", data={"headline": "x", "about": "y",
                                         "contact_email": "", "published": "1"})
    return slugify(name)


def _referrer_with_code(db_path, name="Past Client"):
    conn = connect(db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        cid = create_client(conn, tenant_id=tid, name=name)["id"]
        code = referral_code_for(conn, tid, cid)
        conn.commit()
        return tid, cid, code
    finally:
        conn.close()


def test_inquiry_with_ref_attributes_the_lead(client, app):
    slug = _published(client, name="WoM Studio", email="wom@example.com")
    _tid, rid, code = _referrer_with_code(app.state.settings.db_path)

    visitor = client.__class__(client.app)  # anonymous, arriving via the referral link
    r = visitor.post(f"/studio/{slug}/inquire",
                     data={"name": "Referred Lead", "email": "lead@x.com",
                           "shoot_type": "wedding", "ref": code})
    assert r.status_code == 200

    conn = connect(app.state.settings.db_path)
    try:
        row = conn.execute(
            "SELECT referred_by_client_id FROM projects WHERE referred_by_client_id IS NOT NULL"
        ).fetchone()
    finally:
        conn.close()
    assert row and row["referred_by_client_id"] == rid


def test_inquiry_without_ref_is_organic(client, app):
    slug = _published(client, name="Organic Studio", email="organic@example.com")
    visitor = client.__class__(client.app)
    visitor.post(f"/studio/{slug}/inquire", data={"name": "Walk In", "shoot_type": "wedding"})
    conn = connect(app.state.settings.db_path)
    try:
        rows = conn.execute("SELECT referred_by_client_id FROM projects").fetchall()
    finally:
        conn.close()
    assert rows and all(r["referred_by_client_id"] is None for r in rows)


def test_client_detail_shows_referral_link(client, app):
    _published(client, name="Link Studio", email="linkstudio@example.com")
    _tid, cid, _code = _referrer_with_code(app.state.settings.db_path, name="Linkable")
    page = client.get(f"/clients/{cid}")
    assert "Refer a friend" in page.text and "?ref=" in page.text


def test_project_detail_shows_referred_by(client, app):
    slug = _published(client, name="Attrib Studio", email="attrib@example.com")
    _tid, _rid, code = _referrer_with_code(app.state.settings.db_path, name="Sender")
    visitor = client.__class__(client.app)
    visitor.post(f"/studio/{slug}/inquire",
                 data={"name": "Recv", "shoot_type": "wedding", "ref": code})
    conn = connect(app.state.settings.db_path)
    try:
        pid = conn.execute(
            "SELECT id FROM projects WHERE referred_by_client_id IS NOT NULL").fetchone()["id"]
    finally:
        conn.close()
    page = client.get(f"/projects/{pid}")
    assert "referred by" in page.text and "Sender" in page.text
