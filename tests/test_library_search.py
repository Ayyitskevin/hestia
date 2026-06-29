"""Library — tenant-wide catalog search by the AI's per-image keywords.

The vision pass tags every analyzed frame; this surfaces those tags as a searchable
catalog across all of a studio's galleries. Read-only and strictly tenant-scoped.
"""

import io
import json

from conftest import login_owner, onboard_studio

from hestia.galleries import add_image, create_gallery
from hestia.tenants import create_tenant
from hestia.vision import (
    search_images,
    search_images_by_keyword,
    tenant_keyword_facets,
    tenant_shot_type_facets,
)


def _img(conn, storage, t_id, g_id, name, data=b"x" * 16):
    return add_image(conn, storage, tenant_id=t_id, gallery_id=g_id,
                     filename=name, fileobj=io.BytesIO(data))


def _analyze(conn, t_id, g_id, image_id, keywords, *, shot="candid", alt="", keeper=0.8):
    conn.execute(
        "INSERT INTO image_analyses (image_id, gallery_id, tenant_id, keywords_json, "
        "keeper_score, hero_potential, shot_type, alt_text) VALUES (?, ?, ?, ?, ?, 0.5, ?, ?)",
        (image_id, g_id, t_id, json.dumps(keywords), keeper, shot, alt),
    )


# ── module logic ──────────────────────────────────────────────────────────────


def test_facets_count_keywords_across_galleries(conn, storage):
    t = create_tenant(conn, name="Lib Studio", shoot_type="wedding")
    g1 = create_gallery(conn, tenant_id=t["id"], title="Wedding A")
    g2 = create_gallery(conn, tenant_id=t["id"], title="Wedding B")
    a = _img(conn, storage, t["id"], g1["id"], "a.jpg")
    b = _img(conn, storage, t["id"], g1["id"], "b.jpg")
    c = _img(conn, storage, t["id"], g2["id"], "c.jpg")
    _analyze(conn, t["id"], g1["id"], a["id"], ["candid", "golden-hour"])
    _analyze(conn, t["id"], g1["id"], b["id"], ["candid", "portrait"])
    _analyze(conn, t["id"], g2["id"], c["id"], ["candid"])
    conn.commit()
    facets = tenant_keyword_facets(conn, t["id"])
    counts = {f["keyword"]: f["count"] for f in facets}
    assert counts["candid"] == 3                                   # across both galleries
    assert counts["portrait"] == 1 and counts["golden-hour"] == 1
    assert facets[0]["keyword"] == "candid"                        # most common first


def test_search_returns_matching_images_with_gallery_context(conn, storage):
    t = create_tenant(conn, name="Lib Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="Beach Day")
    a = _img(conn, storage, t["id"], g["id"], "a.jpg")
    b = _img(conn, storage, t["id"], g["id"], "b.jpg")
    _analyze(conn, t["id"], g["id"], a["id"], ["candid", "golden-hour"],
             shot="portrait", alt="a sunset portrait")
    _analyze(conn, t["id"], g["id"], b["id"], ["portrait"])
    conn.commit()
    hits = search_images_by_keyword(conn, t["id"], "golden-hour")
    assert [h["id"] for h in hits] == [a["id"]]
    assert hits[0]["gallery_title"] == "Beach Day"
    assert hits[0]["alt_text"] == "a sunset portrait" and hits[0]["shot_type"] == "portrait"
    assert [h["id"] for h in search_images_by_keyword(conn, t["id"], "GOLDEN-HOUR")] == [a["id"]]


def test_search_and_facets_are_tenant_scoped(conn, storage):
    ta = create_tenant(conn, name="A Studio", shoot_type="wedding")
    tb = create_tenant(conn, name="B Studio", shoot_type="portrait")
    ga = create_gallery(conn, tenant_id=ta["id"], title="A Gallery")
    gb = create_gallery(conn, tenant_id=tb["id"], title="B Gallery")
    ia = _img(conn, storage, ta["id"], ga["id"], "a.jpg")
    ib = _img(conn, storage, tb["id"], gb["id"], "b.jpg")
    _analyze(conn, ta["id"], ga["id"], ia["id"], ["candid"])
    _analyze(conn, tb["id"], gb["id"], ib["id"], ["candid"])
    conn.commit()
    assert [h["id"] for h in search_images_by_keyword(conn, ta["id"], "candid")] == [ia["id"]]
    assert [h["id"] for h in search_images_by_keyword(conn, tb["id"], "candid")] == [ib["id"]]
    assert {f["keyword"]: f["count"] for f in tenant_keyword_facets(conn, ta["id"])}["candid"] == 1


def test_search_matches_exact_token_not_substring(conn, storage):
    t = create_tenant(conn, name="Tok Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    im = _img(conn, storage, t["id"], g["id"], "a.jpg")
    _analyze(conn, t["id"], g["id"], im["id"], ["close-up"])
    conn.commit()
    assert search_images_by_keyword(conn, t["id"], "close-up") != []
    assert search_images_by_keyword(conn, t["id"], "close") == []   # not a substring of the token


def test_search_escapes_like_wildcards_and_empty(conn, storage):
    t = create_tenant(conn, name="Esc Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    im = _img(conn, storage, t["id"], g["id"], "a.jpg")
    _analyze(conn, t["id"], g["id"], im["id"], ["portrait"])
    conn.commit()
    # % and _ must be literal, not LIKE wildcards (otherwise these would match "portrait")
    assert search_images_by_keyword(conn, t["id"], "p%t") == []
    assert search_images_by_keyword(conn, t["id"], "p_rtrait") == []
    assert search_images_by_keyword(conn, t["id"], "   ") == []     # blank query
    assert search_images_by_keyword(conn, t["id"], "portrait") != []


# ── route ─────────────────────────────────────────────────────────────────────


def test_library_redirects_anonymous_to_login(client):
    assert client.get("/library").url.path == "/login"


def test_library_shows_facets_and_search_results(client, conn, storage):
    creds = onboard_studio(client, email="lib@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    g = create_gallery(conn, tenant_id=tid, title="Sunset Shoot")
    im = _img(conn, storage, tid, g["id"], "a.jpg")
    _analyze(conn, tid, g["id"], im["id"], ["golden-hour", "candid"])
    conn.commit()
    page = client.get("/library").text
    assert "golden-hour" in page and "candid" in page              # the AI's keyword cloud
    hit = client.get("/library?q=golden-hour").text
    assert "Sunset Shoot" in hit and "1 photo tagged" in hit
    assert "No photos match" in client.get("/library?q=nonsense").text


def test_library_search_is_tenant_isolated_at_route(client, conn, storage):
    creds = onboard_studio(client, email="a@lib.test", name="A Studio")
    login_owner(client, creds)
    # a different studio has a frame tagged 'candid' — A must never see it
    tb = create_tenant(conn, name="B Studio", shoot_type="portrait")
    gb = create_gallery(conn, tenant_id=tb["id"], title="B Secret Gallery")
    ib = _img(conn, storage, tb["id"], gb["id"], "b.jpg")
    _analyze(conn, tb["id"], gb["id"], ib["id"], ["candid"])
    conn.commit()
    page = client.get("/library?q=candid").text
    assert "B Secret Gallery" not in page
    assert "0 photos tagged" in page


# ── shot-type axis ──────────────────────────────────────────────────────────────


def test_shot_type_facets_count_and_scope(conn, storage):
    ta = create_tenant(conn, name="A Shots", shoot_type="wedding")
    tb = create_tenant(conn, name="B Shots", shoot_type="portrait")
    ga = create_gallery(conn, tenant_id=ta["id"], title="GA")
    gb = create_gallery(conn, tenant_id=tb["id"], title="GB")
    a1 = _img(conn, storage, ta["id"], ga["id"], "a1.jpg")
    a2 = _img(conn, storage, ta["id"], ga["id"], "a2.jpg")
    a3 = _img(conn, storage, ta["id"], ga["id"], "a3.jpg")
    b1 = _img(conn, storage, tb["id"], gb["id"], "b1.jpg")
    _analyze(conn, ta["id"], ga["id"], a1["id"], ["x"], shot="portrait")
    _analyze(conn, ta["id"], ga["id"], a2["id"], ["x"], shot="portrait")
    _analyze(conn, ta["id"], ga["id"], a3["id"], ["x"], shot="candid")
    _analyze(conn, tb["id"], gb["id"], b1["id"], ["x"], shot="portrait")
    conn.commit()
    facets = tenant_shot_type_facets(conn, ta["id"])
    assert {f["shot_type"]: f["count"] for f in facets} == {"portrait": 2, "candid": 1}  # A only
    assert facets[0]["shot_type"] == "portrait"                    # most common first


def test_search_by_shot_type_and_combined(conn, storage):
    t = create_tenant(conn, name="Shot Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    a = _img(conn, storage, t["id"], g["id"], "a.jpg")
    b = _img(conn, storage, t["id"], g["id"], "b.jpg")
    _analyze(conn, t["id"], g["id"], a["id"], ["candid"], shot="portrait")
    _analyze(conn, t["id"], g["id"], b["id"], ["candid"], shot="wide")
    conn.commit()
    assert [h["id"] for h in search_images(conn, t["id"], shot_type="portrait")] == [a["id"]]
    assert [h["id"] for h in search_images(conn, t["id"], shot_type="PORTRAIT")] == [a["id"]]  # ci
    # keyword AND shot together narrow to the intersection
    assert [h["id"] for h in search_images(conn, t["id"], keyword="candid", shot_type="wide")] == [b["id"]]
    assert search_images(conn, t["id"], keyword="candid", shot_type="detail") == []   # AND, no match
    assert search_images(conn, t["id"]) == []                      # no filters → empty


def test_search_shot_type_is_tenant_scoped(conn, storage):
    ta = create_tenant(conn, name="A2", shoot_type="wedding")
    tb = create_tenant(conn, name="B2", shoot_type="portrait")
    ga = create_gallery(conn, tenant_id=ta["id"], title="GA")
    gb = create_gallery(conn, tenant_id=tb["id"], title="GB")
    ia = _img(conn, storage, ta["id"], ga["id"], "a.jpg")
    ib = _img(conn, storage, tb["id"], gb["id"], "b.jpg")
    _analyze(conn, ta["id"], ga["id"], ia["id"], ["x"], shot="portrait")
    _analyze(conn, tb["id"], gb["id"], ib["id"], ["x"], shot="portrait")
    conn.commit()
    assert [h["id"] for h in search_images(conn, ta["id"], shot_type="portrait")] == [ia["id"]]
    assert [h["id"] for h in search_images(conn, tb["id"], shot_type="portrait")] == [ib["id"]]


def test_library_shot_type_filter_route(client, conn, storage):
    creds = onboard_studio(client, email="shot@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    g = create_gallery(conn, tenant_id=tid, title="Reception")
    a = _img(conn, storage, tid, g["id"], "a.jpg")
    b = _img(conn, storage, tid, g["id"], "b.jpg")
    _analyze(conn, tid, g["id"], a["id"], ["candid"], shot="portrait")
    _analyze(conn, tid, g["id"], b["id"], ["candid"], shot="wide")
    conn.commit()
    page = client.get("/library").text
    assert "portrait" in page and "wide" in page                   # shot-type chips render
    res = client.get("/library?shot=portrait").text
    assert "Reception" in res and "1 photo" in res                 # filtered to the portrait
    combo = client.get("/library?q=candid&shot=wide").text
    assert "1 photo" in combo and "tagged" in combo                # keyword AND shot


# ── keepers-only axis ───────────────────────────────────────────────────────────


def test_keepers_only_filters_and_ranks(conn, storage):
    t = create_tenant(conn, name="Keep Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    hi = _img(conn, storage, t["id"], g["id"], "hi.jpg")
    mid = _img(conn, storage, t["id"], g["id"], "mid.jpg")
    lo = _img(conn, storage, t["id"], g["id"], "lo.jpg")
    _analyze(conn, t["id"], g["id"], hi["id"], ["candid"], keeper=0.95)
    _analyze(conn, t["id"], g["id"], mid["id"], ["candid"], keeper=0.72)
    _analyze(conn, t["id"], g["id"], lo["id"], ["candid"], keeper=0.40)
    conn.commit()
    res = search_images(conn, t["id"], keepers_only=True)
    assert [r["id"] for r in res] == [hi["id"], mid["id"]]         # below-threshold dropped, best first
    res2 = search_images(conn, t["id"], keyword="candid", keepers_only=True)
    assert [r["id"] for r in res2] == [hi["id"], mid["id"]]        # combines with keyword
    # without the keepers filter, the low-quality frame is included
    assert {r["id"] for r in search_images(conn, t["id"], keyword="candid")} == \
        {hi["id"], mid["id"], lo["id"]}


def test_keepers_only_alone_spans_galleries_and_is_scoped(conn, storage):
    ta = create_tenant(conn, name="KA", shoot_type="wedding")
    tb = create_tenant(conn, name="KB", shoot_type="portrait")
    ga = create_gallery(conn, tenant_id=ta["id"], title="GA")
    ga2 = create_gallery(conn, tenant_id=ta["id"], title="GA2")
    gb = create_gallery(conn, tenant_id=tb["id"], title="GB")
    a1 = _img(conn, storage, ta["id"], ga["id"], "a1.jpg")
    a2 = _img(conn, storage, ta["id"], ga2["id"], "a2.jpg")
    b1 = _img(conn, storage, tb["id"], gb["id"], "b1.jpg")
    _analyze(conn, ta["id"], ga["id"], a1["id"], ["x"], keeper=0.9)
    _analyze(conn, ta["id"], ga2["id"], a2["id"], ["x"], keeper=0.85)
    _analyze(conn, tb["id"], gb["id"], b1["id"], ["x"], keeper=0.99)
    conn.commit()
    # no keyword/shot — just "my best frames across every gallery", tenant-scoped
    res = search_images(conn, ta["id"], keepers_only=True)
    assert {r["id"] for r in res} == {a1["id"], a2["id"]}          # only A's, never B's


def test_library_keepers_filter_route(client, conn, storage):
    creds = onboard_studio(client, email="keep@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    g = create_gallery(conn, tenant_id=tid, title="Reception")
    hi = _img(conn, storage, tid, g["id"], "hi.jpg")
    lo = _img(conn, storage, tid, g["id"], "lo.jpg")
    _analyze(conn, tid, g["id"], hi["id"], ["candid"], keeper=0.95)
    _analyze(conn, tid, g["id"], lo["id"], ["candid"], keeper=0.30)
    conn.commit()
    assert "Keepers only" in client.get("/library").text          # the toggle renders
    res = client.get("/library?keepers=1").text
    assert "1 keeper photo" in res                                 # only the strong frame, labelled
