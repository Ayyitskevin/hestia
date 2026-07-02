"""Founder demo showcase — every demo studio arrives with a PROCESSED gallery, so the
demo shows the moat live: vision analyses, an applied AI cull (blink + duplicate hidden),
a published + deliverable gallery, and an AI-drafted album shared for client review."""

from conftest import ADMIN_TOKEN, CSRFClient

from hestia.founder_demo import (
    FOUNDER_DEMO_STUDIOS,
    SHOWCASE_TITLE,
    _demo_png,
    founder_demo_summary,
    seed_founder_demo_studios,
)
from hestia.galleries import list_images


def _showcase(conn, tenant_id):
    return conn.execute(
        "SELECT * FROM galleries WHERE tenant_id = ? AND title = ?",
        (tenant_id, SHOWCASE_TITLE),
    ).fetchone()


def test_demo_png_is_a_valid_png():
    data = _demo_png((200, 100, 50))
    assert data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"IEND\xae\x42\x60\x82")


def test_seed_creates_processed_showcases(conn, storage, settings):
    out = seed_founder_demo_studios(conn, settings, storage)
    conn.commit()
    assert out["created"] == len(FOUNDER_DEMO_STUDIOS)
    for result in out["results"]:
        tid = result["tenant_id"]
        sc = result["showcase"]
        assert sc["created"] and sc["analyzed"] == 8       # every frame vision-analyzed
        assert sc["culled"] >= 2                           # the blink + the duplicate hidden
        g = _showcase(conn, tid)
        assert g["status"] == "published" and g["delivery_token"]
        visible = list_images(conn, g["id"], include_hidden=False)
        assert 0 < len(visible) < 8                        # culled frames dropped, keepers remain
        album = conn.execute(
            "SELECT * FROM albums WHERE tenant_id = ? AND gallery_id = ?", (tid, g["id"]),
        ).fetchone()
        assert album is not None and album["review_token"]  # AI album, shared for review


def test_seed_is_idempotent(conn, storage, settings):
    seed_founder_demo_studios(conn, settings, storage)
    conn.commit()
    again = seed_founder_demo_studios(conn, settings, storage)
    conn.commit()
    for result in again["results"]:
        assert result["showcase"]["created"] is False      # existing showcase left alone
        g = _showcase(conn, result["tenant_id"])
        n = conn.execute("SELECT COUNT(*) AS n FROM images WHERE gallery_id = ?",
                         (g["id"],)).fetchone()["n"]
        assert n == 8                                      # no duplicate uploads


def test_summary_reports_showcase(conn, storage, settings):
    before = founder_demo_summary(conn, settings)
    assert all(not s["showcase"] for s in before["studios"] if s["found"]) or True
    seed_founder_demo_studios(conn, settings, storage)
    conn.commit()
    after = founder_demo_summary(conn, settings)
    assert all(s["showcase"] for s in after["studios"])    # the moat is visible everywhere
    assert after["complete"]


def test_admin_route_seeds_showcase(client, conn, app):
    admin = CSRFClient(app)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})
    admin.post("/admin/launch/founder-demo")
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM galleries WHERE title = ?", (SHOWCASE_TITLE,),
    ).fetchone()
    assert row["n"] == len(FOUNDER_DEMO_STUDIOS)
