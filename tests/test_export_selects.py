"""Export gallery selects — the client's favorites as a downloadable filename list.

Lets the studio pull the album/print picks straight into Lightroom/their editor.
Owner-only, tenant-scoped; ordered by position; favorites only.
"""

from conftest import login_owner, onboard_studio

from hestia.galleries import create_gallery


def _img(conn, tid, gid, filename, pos):
    cur = conn.execute(
        "INSERT INTO images (gallery_id, tenant_id, filename, storage_key, position) "
        "VALUES (?, ?, ?, ?, ?)",
        (gid, tid, filename, f"key-{filename}", pos),
    )
    return cur.lastrowid


def _fav(conn, tid, gid, image_id):
    conn.execute("INSERT INTO image_favorites (tenant_id, gallery_id, image_id) VALUES (?, ?, ?)",
                 (tid, gid, image_id))


def _tenant_id(conn):
    return conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]


def test_selects_download_lists_favorites_in_order(client, conn):
    creds = onboard_studio(client, email="sel1@example.com")
    login_owner(client, creds)
    tid = _tenant_id(conn)
    g = create_gallery(conn, tenant_id=tid, title="Beach day")
    a = _img(conn, tid, g["id"], "IMG_001.jpg", 1)
    _img(conn, tid, g["id"], "IMG_002.jpg", 2)             # not favorited
    c = _img(conn, tid, g["id"], "IMG_003.jpg", 3)
    _fav(conn, tid, g["id"], c)                            # favorite out of order...
    _fav(conn, tid, g["id"], a)
    conn.commit()
    r = client.get(f"/galleries/{g['id']}/selects.txt")
    assert r.status_code == 200 and "text/plain" in r.headers["content-type"]
    # only favorites, ordered by position, one per line
    assert r.text == "IMG_001.jpg\nIMG_003.jpg\n"
    assert "attachment" in r.headers.get("content-disposition", "")


def test_selection_packet_download_includes_favorites_notes_and_status(client, conn):
    creds = onboard_studio(client, email="sel-packet@example.com")
    login_owner(client, creds)
    tid = _tenant_id(conn)
    g = create_gallery(conn, tenant_id=tid, title="Album handoff", client_name="Ari")
    keep = _img(conn, tid, g["id"], "KEEP.jpg", 1)
    note_only = _img(conn, tid, g["id"], "NOTE_ONLY.jpg", 2)
    _fav(conn, tid, g["id"], keep)
    conn.execute(
        "INSERT INTO image_comments (tenant_id, gallery_id, image_id, author_name, body) "
        "VALUES (?, ?, ?, ?, ?)",
        (tid, g["id"], keep, "Ari", "spread opener"),
    )
    conn.execute(
        "INSERT INTO image_comments (tenant_id, gallery_id, image_id, author_name, body) "
        "VALUES (?, ?, ?, ?, ?)",
        (tid, g["id"], note_only, "Ari", "retouch exit sign"),
    )
    conn.commit()

    r = client.get(f"/galleries/{g['id']}/selection-packet.txt")

    assert r.status_code == 200 and "text/plain" in r.headers["content-type"]
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "Selection packet: Album handoff" in r.text
    assert "Client: Ari" in r.text
    assert "Status: Selection in progress" in r.text
    assert "KEEP.jpg - 1 note(s)" in r.text
    assert "NOTE_ONLY.jpg - Ari: retouch exit sign" in r.text


def test_selects_empty_when_no_favorites(client, conn):
    creds = onboard_studio(client, email="sel2@example.com")
    login_owner(client, creds)
    tid = _tenant_id(conn)
    g = create_gallery(conn, tenant_id=tid, title="No picks")
    _img(conn, tid, g["id"], "IMG_009.jpg", 1)
    conn.commit()
    r = client.get(f"/galleries/{g['id']}/selects.txt")
    assert r.status_code == 200 and r.text == ""


def test_selects_is_tenant_scoped(client, conn):
    # log in as studio A
    creds = onboard_studio(client, email="sel3@example.com")
    login_owner(client, creds)
    a_tid = _tenant_id(conn)
    # a foreign gallery owned by a different tenant (seed directly)
    from hestia.tenants import create_tenant
    other = create_tenant(conn, name="Other Studio", shoot_type="wedding")
    og = create_gallery(conn, tenant_id=other["id"], title="Secret")
    oi = _img(conn, other["id"], og["id"], "SECRET_001.jpg", 1)
    _fav(conn, other["id"], og["id"], oi)
    conn.commit()
    r = client.get(f"/galleries/{og['id']}/selects.txt")        # A requests B's gallery
    assert "SECRET_001.jpg" not in r.text                       # no cross-tenant leak
    assert a_tid != other["id"]


def test_selection_packet_is_tenant_scoped(client, conn):
    creds = onboard_studio(client, email="sel-packet-scope@example.com")
    login_owner(client, creds)

    from hestia.tenants import create_tenant
    other = create_tenant(conn, name="Other Studio", shoot_type="wedding")
    og = create_gallery(conn, tenant_id=other["id"], title="Secret")
    oi = _img(conn, other["id"], og["id"], "SECRET_001.jpg", 1)
    _fav(conn, other["id"], og["id"], oi)
    conn.commit()

    r = client.get(f"/galleries/{og['id']}/selection-packet.txt")

    assert r.status_code == 200
    assert "SECRET_001.jpg" not in r.text


def test_proofing_panel_links_selects_download(client, conn):
    creds = onboard_studio(client, email="sel4@example.com")
    login_owner(client, creds)
    tid = _tenant_id(conn)
    g = create_gallery(conn, tenant_id=tid, title="Linked")
    i = _img(conn, tid, g["id"], "IMG_100.jpg", 1)
    _fav(conn, tid, g["id"], i)
    conn.commit()
    assert f"/galleries/{g['id']}/selects.txt" in client.get(f"/galleries/{g['id']}").text


def test_proofing_panel_links_selection_packet_download(client, conn):
    creds = onboard_studio(client, email="sel-packet-link@example.com")
    login_owner(client, creds)
    tid = _tenant_id(conn)
    g = create_gallery(conn, tenant_id=tid, title="Linked packet")
    i = _img(conn, tid, g["id"], "IMG_200.jpg", 1)
    _fav(conn, tid, g["id"], i)
    conn.commit()
    page = client.get(f"/galleries/{g['id']}").text
    assert f"/galleries/{g['id']}/selection-packet.txt" in page
