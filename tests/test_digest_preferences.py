"""Owner-digest preferences — on/off switch + the manual-send cooldown stamp.

Default on; an owner can disable the weekly sweep; the manual "email me this" still
works and now stamps the cooldown so the sweep won't mail a near-duplicate that week.
"""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client, create_project
from hestia.dashboard import send_owner_digest_now, send_owner_digests, set_digest_enabled
from hestia.email import list_emails
from hestia.studio import upsert_profile
from hestia.tenants import create_tenant


def _studio_with_content(conn, *, name="Digest Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    upsert_profile(conn, tenant_id=t["id"], headline="", about="",
                   contact_email="owner@x.com", published=True)   # gives a recipient
    c = create_client(conn, tenant_id=t["id"], name="Cli", email="c@x.com")
    create_project(conn, tenant_id=t["id"], name="A lead", client_id=c["id"], status="lead")
    conn.commit()
    return t


def _last_digest_at(conn, tenant_id):
    return conn.execute("SELECT last_digest_at FROM tenants WHERE id = ?",
                        (tenant_id,)).fetchone()["last_digest_at"]


def test_default_enabled(conn):
    t = create_tenant(conn, name="Fresh", shoot_type="wedding")
    conn.commit()
    assert conn.execute("SELECT digest_enabled FROM tenants WHERE id = ?",
                        (t["id"],)).fetchone()["digest_enabled"] == 1


def test_enabled_studio_is_sent(conn, settings):
    _studio_with_content(conn)
    assert send_owner_digests(conn, settings) == 1


def test_disabled_studio_is_skipped(conn, settings):
    t = _studio_with_content(conn)
    set_digest_enabled(conn, t["id"], False)
    conn.commit()
    assert send_owner_digests(conn, settings) == 0
    assert list_emails(conn, t["id"]) == []
    assert _last_digest_at(conn, t["id"]) is None        # skipped, not claimed


def test_reenabling_sends_again(conn, settings):
    t = _studio_with_content(conn)
    set_digest_enabled(conn, t["id"], False)
    conn.commit()
    assert send_owner_digests(conn, settings) == 0
    set_digest_enabled(conn, t["id"], True)
    conn.commit()
    assert send_owner_digests(conn, settings) == 1


def test_manual_send_stamps_cooldown(conn, settings):
    """The fix: a manual send claims the weekly slot, so the sweep won't double-mail."""
    t = _studio_with_content(conn)
    assert send_owner_digest_now(conn, settings, t["id"]) is not None
    assert _last_digest_at(conn, t["id"]) is not None
    # the weekly sweep now finds it within cooldown → sends nothing more
    assert send_owner_digests(conn, settings) == 0
    digests = [m for m in list_emails(conn, t["id"]) if "attention" in m["subject"]]
    assert len(digests) == 1


def test_toggle_route(client, conn):
    creds = onboard_studio(client, email="dp@example.com")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]
    # unchecked box submits nothing → disabled
    client.post("/settings/digest", data={})
    assert conn.execute("SELECT digest_enabled FROM tenants WHERE id = ?",
                        (tid,)).fetchone()["digest_enabled"] == 0
    # checked → enabled
    client.post("/settings/digest", data={"digest_enabled": "1"})
    assert conn.execute("SELECT digest_enabled FROM tenants WHERE id = ?",
                        (tid,)).fetchone()["digest_enabled"] == 1
