"""Vision deepening — per-frame exposure & sharpness sub-scores and advisory flags.

The pass gains two technical sub-scores (exposure, sharpness) that drive owner-facing
flags (soft / dark / bright). They're advisory only — existing cull and keeper behaviour
is unchanged.
"""

import io

from conftest import login_owner, onboard_studio

from hestia.galleries import add_image, create_gallery
from hestia.tenants import create_tenant
from hestia.vision import (
    MockVisionProvider,
    _result_from_parsed,
    analyze_gallery,
    gallery_analysis_map,
)


def _img(conn, storage, t_id, g_id, name, data=b"x" * 16):
    return add_image(conn, storage, tenant_id=t_id, gallery_id=g_id,
                     filename=name, fileobj=io.BytesIO(data))


def _row(conn, t_id, g_id, image_id, *, exposure=0.6, sharpness=0.6):
    conn.execute(
        "INSERT INTO image_analyses (image_id, gallery_id, tenant_id, keywords_json, "
        "keeper_score, hero_potential, shot_type, alt_text, exposure, sharpness) "
        "VALUES (?, ?, ?, '[]', 0.8, 0.5, 'candid', '', ?, ?)",
        (image_id, g_id, t_id, exposure, sharpness),
    )


def test_mock_provides_sub_scores_deterministically():
    p = MockVisionProvider()
    a = p.analyze(filename="f.jpg", data=b"one")
    b = p.analyze(filename="f.jpg", data=b"two")
    assert 0.0 <= a.exposure <= 1.0 and 0.0 <= a.sharpness <= 1.0
    assert a.exposure == b.exposure and a.sharpness == b.sharpness     # keyed on filename
    assert "exposure" in a.as_dict() and "sharpness" in a.as_dict()


def test_parser_coerces_sub_scores():
    good = _result_from_parsed({"exposure": 0.2, "sharpness": 0.95})
    assert good.exposure == 0.2 and good.sharpness == 0.95
    missing = _result_from_parsed({})                                 # omitted → neutral, not worst-case
    assert missing.exposure == 0.5 and missing.sharpness == 0.5
    junk = _result_from_parsed({"exposure": "bright", "sharpness": None})
    assert junk.exposure == 0.5 and junk.sharpness == 0.5
    clamp = _result_from_parsed({"exposure": 1.8, "sharpness": -0.4})
    assert clamp.exposure == 1.0 and clamp.sharpness == 0.0           # clamped to 0..1


def test_analyze_gallery_persists_sub_scores(conn, storage, settings):
    t = create_tenant(conn, name="Q Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    for i in range(3):
        _img(conn, storage, t["id"], g["id"], f"f{i}.jpg", data=bytes([i + 1]) * 20)
    conn.commit()
    analyze_gallery(conn, storage, settings, tenant_id=t["id"], gallery_id=g["id"])
    rows = conn.execute(
        "SELECT exposure, sharpness FROM image_analyses WHERE gallery_id = ?", (g["id"],)
    ).fetchall()
    assert len(rows) == 3
    assert all(r["exposure"] is not None and r["sharpness"] is not None for r in rows)
    assert all(0.0 <= r["exposure"] <= 1.0 and 0.0 <= r["sharpness"] <= 1.0 for r in rows)


def test_quality_flags_from_sub_scores(conn, storage):
    t = create_tenant(conn, name="Flag Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    soft = _img(conn, storage, t["id"], g["id"], "soft.jpg")
    dark = _img(conn, storage, t["id"], g["id"], "dark.jpg")
    bright = _img(conn, storage, t["id"], g["id"], "bright.jpg")
    fine = _img(conn, storage, t["id"], g["id"], "fine.jpg")
    _row(conn, t["id"], g["id"], soft["id"], sharpness=0.20, exposure=0.60)
    _row(conn, t["id"], g["id"], dark["id"], sharpness=0.80, exposure=0.20)
    _row(conn, t["id"], g["id"], bright["id"], sharpness=0.80, exposure=0.97)
    _row(conn, t["id"], g["id"], fine["id"], sharpness=0.80, exposure=0.60)
    conn.commit()
    m = gallery_analysis_map(conn, g["id"])
    assert m[soft["id"]]["flags"] == ["soft"]
    assert m[dark["id"]]["flags"] == ["dark"]
    assert m[bright["id"]]["flags"] == ["bright"]
    assert m[fine["id"]]["flags"] == []


def test_null_sub_scores_yield_no_flag(conn, storage):
    t = create_tenant(conn, name="Null Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    im = _img(conn, storage, t["id"], g["id"], "a.jpg")
    # a frame analysed before the sub-scores existed — NULL exposure/sharpness
    conn.execute(
        "INSERT INTO image_analyses (image_id, gallery_id, tenant_id, keywords_json, "
        "keeper_score, hero_potential, shot_type, alt_text) "
        "VALUES (?, ?, ?, '[]', 0.8, 0.5, 'candid', '')",
        (im["id"], g["id"], t["id"]),
    )
    conn.commit()
    assert gallery_analysis_map(conn, g["id"])[im["id"]]["flags"] == []


def test_owner_gallery_shows_quality_flag(client, conn, storage):
    creds = onboard_studio(client, email="flag@studio.test")
    login_owner(client, creds)
    tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    g = create_gallery(conn, tenant_id=tid, title="Wedding")
    im = _img(conn, storage, tid, g["id"], "a.jpg")
    _row(conn, tid, g["id"], im["id"], sharpness=0.15, exposure=0.60)   # soft
    conn.commit()
    assert "⚠ soft" in client.get(f"/galleries/{g['id']}").text
