"""Vision deepening — blink signal, duplicate culling, and AI style profiles."""

import io

from conftest import login_owner, onboard_studio

from hestia.db import connect
from hestia.galleries import add_image, create_gallery
from hestia.tenants import (
    can_use_style_profile,
    create_tenant,
    get_tenant,
    set_vision_style,
)
from hestia.vision import (
    BLINK_THRESHOLD,
    MockVisionProvider,
    analyze_gallery,
    content_dup_key,
    cull_summary,
)


def _tenant(conn, name="Vision Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def _img(conn, storage, t, g, name, data):
    return add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"],
                     filename=name, fileobj=io.BytesIO(data))


def test_content_dup_key():
    assert content_dup_key(b"same") == content_dup_key(b"same")
    assert content_dup_key(b"a") != content_dup_key(b"b")


def test_mock_has_blink_signal():
    r = MockVisionProvider().analyze(filename="x.jpg", data=b"bytes")
    assert 0.0 <= r.eyes_closed <= 1.0
    assert "eyes_closed" in r.as_dict()
    # deterministic on filename (matches the mock contract)
    assert MockVisionProvider().analyze(filename="x.jpg", data=b"other").eyes_closed == r.eyes_closed


def test_style_biases_hero_ranking():
    p = MockVisionProvider()
    base = p.analyze(filename="frame.jpg", data=b"x").hero_potential
    styled = p.analyze(filename="frame.jpg", data=b"x", style="moody film").hero_potential
    assert base != styled  # the style profile re-weights the hero score


def test_analyze_gallery_culls_duplicates(conn, storage, settings):
    t = _tenant(conn)
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    # two byte-identical frames + one distinct
    _img(conn, storage, t, g, "a.jpg", b"IDENTICAL-CONTENT")
    _img(conn, storage, t, g, "b.jpg", b"IDENTICAL-CONTENT")
    _img(conn, storage, t, g, "c.jpg", b"different")
    conn.commit()
    summary = analyze_gallery(conn, storage, settings, tenant_id=t["id"], gallery_id=g["id"])
    assert summary["duplicate_count"] == 1           # one of the pair is culled
    assert summary["culled_count"] >= 1
    # the culled frame is never a hero
    assert set(summary["hero_image_ids"]).isdisjoint(summary["culled_image_ids"])


def test_cull_summary_flags_blinks_and_dups(conn, storage, settings):
    t = _tenant(conn)
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    a = _img(conn, storage, t, g, "a.jpg", b"X")
    b = _img(conn, storage, t, g, "b.jpg", b"X")          # dup of a
    blink = _img(conn, storage, t, g, "c.jpg", b"Y")
    # hand-set analyses: a/b share a dup_key; c is a blink
    for img, dk, ec in [(a, "dk1", 0.1), (b, "dk1", 0.1), (blink, "dk2", 0.95)]:
        conn.execute(
            "INSERT INTO image_analyses (image_id, gallery_id, tenant_id, keeper_score, "
            "eyes_closed, dup_key) VALUES (?, ?, ?, ?, ?, ?)",
            (img["id"], g["id"], t["id"], 0.8, ec, dk))
    conn.commit()
    cull = cull_summary(conn, t["id"], g["id"])
    assert len(cull["duplicate_ids"]) == 1 and blink["id"] in cull["blink_ids"]
    assert cull["culled_ids"] == cull["duplicate_ids"] | cull["blink_ids"]
    assert 0.85 == BLINK_THRESHOLD  # the documented blink cutoff


def test_style_profile_tier_gate(conn):
    assert can_use_style_profile({"plan": "studio_pro"}) is True
    assert can_use_style_profile({"plan": "beta"}) is True
    assert can_use_style_profile({"plan": "studio"}) is False
    t = _tenant(conn)
    set_vision_style(conn, t["id"], "bright and airy")
    assert get_tenant(conn, t["id"])["vision_style"] == "bright and airy"


def test_analyze_gallery_applies_style(conn, storage, settings):
    t = _tenant(conn)
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    for i in range(5):
        _img(conn, storage, t, g, f"f{i}.jpg", bytes([i]) * 8)
    conn.commit()
    plain = analyze_gallery(conn, storage, settings, tenant_id=t["id"], gallery_id=g["id"])
    assert plain["style_applied"] is False
    set_vision_style(conn, t["id"], "moody documentary")
    conn.commit()
    styled = analyze_gallery(conn, storage, settings, tenant_id=t["id"], gallery_id=g["id"])
    assert styled["style_applied"] is True
    # the style re-weights hero ranking → the ordering generally changes
    assert styled["hero_image_ids"] != plain["hero_image_ids"]


def test_http_style_setting_is_tier_gated(client, app):
    creds = onboard_studio(client, email="style@example.com")  # default plan = beta
    login_owner(client, creds)
    page = client.get("/settings/site")
    assert "AI style profile" in page.text and 'name="vision_style"' in page.text  # beta → form
    client.post("/settings/vision-style", data={"vision_style": "true-to-life skin tones"})

    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        assert get_tenant(conn, tid)["vision_style"] == "true-to-life skin tones"
        # downgrade to studio (no style profile) → server-side gate blocks the save
        conn.execute("UPDATE tenants SET plan = 'studio' WHERE id = ?", (tid,))
        conn.commit()
    finally:
        conn.close()
    gated = client.get("/settings/site")
    assert "Studio Pro" in gated.text and 'name="vision_style"' not in gated.text
    client.post("/settings/vision-style", data={"vision_style": "HACKED"})
    conn = connect(app.state.settings.db_path)
    try:
        assert get_tenant(conn, tid)["vision_style"] == "true-to-life skin tones"  # unchanged
    finally:
        conn.close()
