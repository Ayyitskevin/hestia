"""Studio-facing vision calibration packet: one safe, tenant-scoped CSV row per frame."""

from __future__ import annotations

import csv
import io
import json

import pytest
from conftest import login_owner, onboard_studio

from hestia.galleries import create_gallery
from hestia.tenants import create_tenant


def _tenant_id(conn) -> str:
    return conn.execute("SELECT id FROM tenants ORDER BY rowid DESC LIMIT 1").fetchone()["id"]


def _image(
    conn,
    tenant_id: str,
    gallery_id: int,
    *,
    filename: str,
    position: int,
    hidden: int = 0,
) -> int:
    cur = conn.execute(
        "INSERT INTO images "
        "(gallery_id, tenant_id, filename, storage_key, content_type, width, height, bytes, "
        "position, hidden) VALUES (?, ?, ?, ?, 'image/jpeg', 6000, 4000, 123456, ?, ?)",
        (gallery_id, tenant_id, filename, f"key-{position}", position, hidden),
    )
    return cur.lastrowid


def _analysis(
    conn,
    tenant_id: str,
    gallery_id: int,
    image_id: int,
    *,
    keywords: list[str],
    keeper: float,
    hero: float,
    shot_type: str,
    alt_text: str,
    eyes_closed: float,
    dup_key: str,
    exposure: float,
    sharpness: float,
) -> None:
    conn.execute(
        "INSERT INTO image_analyses "
        "(image_id, gallery_id, tenant_id, keywords_json, keeper_score, hero_potential, "
        "shot_type, alt_text, eyes_closed, dup_key, exposure, sharpness) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            image_id,
            gallery_id,
            tenant_id,
            json.dumps(keywords),
            keeper,
            hero,
            shot_type,
            alt_text,
            eyes_closed,
            dup_key,
            exposure,
            sharpness,
        ),
    )


def _pipeline_summary(
    conn,
    tenant_id: str,
    gallery_id: int,
    hero_id: int,
    *,
    vision_status: str = "done",
    hero_ids: list | None = None,
) -> None:
    steps = [
        {
            "name": "vision",
            "status": vision_status,
            "finished_at": "2026-07-17T20:00:00+00:00",
            "output": {
                "summary": {
                    "backend": "mock",
                    "fallback_from": "xai",
                    "fallback_scope": "whole_gallery",
                    "style_applied": True,
                    "hero_image_ids": [hero_id] if hero_ids is None else hero_ids,
                }
            },
        },
        {"name": "offer", "status": "done", "output": {}},
    ]
    conn.execute(
        "INSERT INTO pipeline_runs (tenant_id, source, source_id, status, steps_json) "
        "VALUES (?, 'gallery', ?, ?, ?)",
        (tenant_id, str(gallery_id), vision_status, json.dumps(steps)),
    )


def test_calibration_csv_has_every_frame_model_decisions_and_review_columns(client, conn):
    creds = onboard_studio(client, email="calibration@example.com")
    login_owner(client, creds)
    tenant_id = _tenant_id(conn)
    gallery = create_gallery(conn, tenant_id=tenant_id, title="Calibration Set")
    hero_id = _image(
        conn,
        tenant_id,
        gallery["id"],
        filename="=HYPERLINK(\"bad\")",
        position=1,
    )
    rejected_id = _image(
        conn,
        tenant_id,
        gallery["id"],
        filename="IMG_0002.jpg",
        position=2,
        hidden=1,
    )
    untouched_id = _image(
        conn,
        tenant_id,
        gallery["id"],
        filename="IMG_0003.jpg",
        position=3,
    )
    _analysis(
        conn,
        tenant_id,
        gallery["id"],
        hero_id,
        keywords=["=formula", "portrait"],
        keeper=0.92,
        hero=0.96,
        shot_type="portrait",
        alt_text="=HYPERLINK(\"bad\")",
        eyes_closed=0.05,
        dup_key="d_aaaaaaaaaaaaaaaa",
        exposure=0.5,
        sharpness=0.9,
    )
    _analysis(
        conn,
        tenant_id,
        gallery["id"],
        rejected_id,
        keywords=["candid"],
        keeper=0.65,
        hero=0.1,
        shot_type="candid",
        alt_text="A soft frame.",
        eyes_closed=0.95,
        dup_key="d_aaaaaaaaaaaaaaaa",
        exposure=0.2,
        sharpness=0.2,
    )
    conn.execute(
        "INSERT INTO image_favorites (tenant_id, gallery_id, image_id) VALUES (?, ?, ?)",
        (tenant_id, gallery["id"], rejected_id),
    )
    conn.execute(
        "UPDATE galleries SET cover_image_id = ? WHERE id = ? AND tenant_id = ?",
        (hero_id, gallery["id"], tenant_id),
    )
    _pipeline_summary(conn, tenant_id, gallery["id"], hero_id)
    conn.commit()

    response = client.get(f"/galleries/{gallery['id']}/vision-calibration.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == (
        f'attachment; filename="vision-calibration-{gallery["id"]}.csv"'
    )
    assert response.headers["cache-control"] == "private, no-store"
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert set(rows[0]).isdisjoint(
        {"storage_key", "access_token", "media_url", "url", "comments", "client_name"}
    )
    assert [row["image_id"] for row in rows] == [
        str(hero_id),
        str(rejected_id),
        str(untouched_id),
    ]

    hero, rejected, untouched = rows
    assert hero["gallery_title"] == "Calibration Set"
    assert hero["vision_backend"] == "mock"
    assert hero["fallback_from"] == "xai"
    assert hero["fallback_scope"] == "whole_gallery"
    assert hero["style_applied"] == "yes"
    assert hero["vision_completed_at"] == "2026-07-17T20:00:00+00:00"
    assert hero["filename"].startswith("'=")
    assert hero["keywords"].startswith("'=")
    assert hero["alt_text"].startswith("'=")
    assert hero["keeper_decision_at_0_70"] == "yes"
    assert hero["cull_apply_action"] == "no_change"
    assert hero["pipeline_hero"] == "yes"
    assert hero["cover_current"] == "yes"

    assert rejected["keeper_score"] == "0.65"
    assert rejected["keeper_decision_at_0_70"] == "no"
    assert rejected["blink_flag_at_0_85"] == "yes"
    assert rejected["duplicate_flag"] == "yes"
    assert rejected["quality_flags"] == "soft|dark"
    assert rejected["cull_apply_action"] == "hide"
    assert rejected["hidden_current"] == "yes"
    assert rejected["client_favorite_current"] == "yes"

    assert untouched["analysis_status"] == "not_analyzed"
    assert untouched["keeper_score"] == ""
    assert untouched["keeper_decision_at_0_70"] == ""
    assert untouched["pipeline_hero"] == ""
    assert untouched["cull_apply_action"] == ""
    assert untouched["hidden_current"] == "no"
    assert untouched["review_keep"] == ""
    assert untouched["review_reason"] == ""
    assert untouched["review_notes"] == ""


def test_calibration_csv_is_studio_authenticated_and_tenant_scoped(client, conn):
    anon = client.__class__(client.app)
    assert anon.get(
        "/galleries/999/vision-calibration.csv", follow_redirects=False
    ).headers["location"] == "/login"

    creds = onboard_studio(client, email="calibration-scope@example.com")
    login_owner(client, creds)
    owner_id = _tenant_id(conn)
    other = create_tenant(conn, name="Other Studio", shoot_type="wedding")
    foreign = create_gallery(conn, tenant_id=other["id"], title="Secret Calibration")
    _image(
        conn,
        other["id"],
        foreign["id"],
        filename="SECRET_FRAME.jpg",
        position=1,
    )
    conn.commit()

    response = client.get(
        f"/galleries/{foreign['id']}/vision-calibration.csv",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/galleries"
    assert "SECRET_FRAME.jpg" not in response.text
    assert owner_id != other["id"]


def test_calibration_csv_tolerates_malformed_legacy_analysis(client, conn):
    creds = onboard_studio(client, email="calibration-legacy@example.com")
    login_owner(client, creds)
    tenant_id = _tenant_id(conn)
    gallery = create_gallery(conn, tenant_id=tenant_id, title="Legacy Calibration")
    image_id = _image(
        conn,
        tenant_id,
        gallery["id"],
        filename="IMG_LEGACY.jpg",
        position=1,
    )
    conn.execute(
        "INSERT INTO image_analyses "
        "(image_id, gallery_id, tenant_id, keywords_json, keeper_score, hero_potential, "
        "shot_type, alt_text, eyes_closed, dup_key, exposure, sharpness) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            image_id,
            gallery["id"],
            tenant_id,
            json.dumps({"not": "a list"}),
            7.0,
            -0.5,
            "s" * 200,
            "a" * 1000,
            2.0,
            "d" * 200,
            -0.1,
            1.1,
        ),
    )
    conn.execute("UPDATE images SET hidden = 'false' WHERE id = ?", (image_id,))
    conn.commit()

    response = client.get(f"/galleries/{gallery['id']}/vision-calibration.csv")

    assert response.status_code == 200
    row = next(csv.DictReader(io.StringIO(response.text)))
    assert row["analysis_status"] == "analyzed"
    assert row["keywords"] == ""
    assert row["shot_type"] == ""
    assert len(row["alt_text"]) == 500
    assert row["keeper_score"] == ""
    assert row["keeper_decision_at_0_70"] == ""
    assert row["hero_potential"] == ""
    assert row["eyes_closed"] == ""
    assert row["blink_flag_at_0_85"] == ""
    assert row["exposure"] == ""
    assert row["sharpness"] == ""
    assert row["quality_flags"] == ""
    assert row["duplicate_flag"] == ""
    assert row["cull_apply_action"] == ""
    assert row["hidden_current"] == ""


def test_overlong_legacy_duplicate_keys_do_not_collapse_by_prefix(client, conn):
    creds = onboard_studio(client, email="calibration-dup-key@example.com")
    login_owner(client, creds)
    tenant_id = _tenant_id(conn)
    gallery = create_gallery(conn, tenant_id=tenant_id, title="Legacy Dup Keys")
    first = _image(
        conn,
        tenant_id,
        gallery["id"],
        filename="FIRST.jpg",
        position=1,
    )
    second = _image(
        conn,
        tenant_id,
        gallery["id"],
        filename="SECOND.jpg",
        position=2,
    )
    shared_prefix = "d_" + ("a" * 80)
    for image_id, suffix in ((first, "1"), (second, "2")):
        _analysis(
            conn,
            tenant_id,
            gallery["id"],
            image_id,
            keywords=[],
            keeper=0.8,
            hero=0.5,
            shot_type="candid",
            alt_text="Frame.",
            eyes_closed=0.0,
            dup_key=shared_prefix + suffix,
            exposure=0.5,
            sharpness=0.8,
        )
    conn.commit()

    response = client.get(f"/galleries/{gallery['id']}/vision-calibration.csv")

    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 2
    assert [row["duplicate_flag"] for row in rows] == ["", ""]
    assert [row["cull_apply_action"] for row in rows] == ["", ""]


def test_invalid_keeper_does_not_fabricate_a_duplicate_winner(client, conn):
    creds = onboard_studio(client, email="calibration-dup-score@example.com")
    login_owner(client, creds)
    tenant_id = _tenant_id(conn)
    gallery = create_gallery(conn, tenant_id=tenant_id, title="Corrupt Dup Scores")
    image_ids = [
        _image(
            conn,
            tenant_id,
            gallery["id"],
            filename=f"FRAME_{position}.jpg",
            position=position,
        )
        for position in (1, 2)
    ]
    for image_id, keeper in zip(image_ids, (0.8, 7.0), strict=True):
        _analysis(
            conn,
            tenant_id,
            gallery["id"],
            image_id,
            keywords=[],
            keeper=keeper,
            hero=0.5,
            shot_type="candid",
            alt_text="Frame.",
            eyes_closed=0.0,
            dup_key="d_cccccccccccccccc",
            exposure=0.5,
            sharpness=0.8,
        )
    conn.commit()

    response = client.get(f"/galleries/{gallery['id']}/vision-calibration.csv")

    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert [row["keeper_score"] for row in rows] == ["0.8", ""]
    assert [row["duplicate_flag"] for row in rows] == ["", ""]
    assert [row["cull_apply_action"] for row in rows] == ["", ""]


@pytest.mark.parametrize(
    "hero_ids",
    ([True], list(range(1, 1002))),
    ids=("malformed", "oversized"),
)
def test_invalid_hero_provenance_stays_unknown(client, conn, hero_ids):
    creds = onboard_studio(client, email="calibration-hero-state@example.com")
    login_owner(client, creds)
    tenant_id = _tenant_id(conn)
    gallery = create_gallery(conn, tenant_id=tenant_id, title="Corrupt Hero State")
    image_id = _image(
        conn,
        tenant_id,
        gallery["id"],
        filename="HERO.jpg",
        position=1,
    )
    _analysis(
        conn,
        tenant_id,
        gallery["id"],
        image_id,
        keywords=[],
        keeper=0.8,
        hero=0.9,
        shot_type="portrait",
        alt_text="Hero frame.",
        eyes_closed=0.0,
        dup_key="d_dddddddddddddddd",
        exposure=0.5,
        sharpness=0.8,
    )
    _pipeline_summary(
        conn,
        tenant_id,
        gallery["id"],
        image_id,
        hero_ids=hero_ids,
    )
    conn.commit()

    response = client.get(f"/galleries/{gallery['id']}/vision-calibration.csv")

    row = next(csv.DictReader(io.StringIO(response.text)))
    assert row["pipeline_hero"] == ""


def test_running_reprocess_does_not_mix_old_run_provenance_with_current_rows(client, conn):
    creds = onboard_studio(client, email="calibration-running@example.com")
    login_owner(client, creds)
    tenant_id = _tenant_id(conn)
    gallery = create_gallery(conn, tenant_id=tenant_id, title="Running Calibration")
    image_id = _image(
        conn,
        tenant_id,
        gallery["id"],
        filename="CURRENT.jpg",
        position=1,
    )
    _analysis(
        conn,
        tenant_id,
        gallery["id"],
        image_id,
        keywords=["current"],
        keeper=0.9,
        hero=0.8,
        shot_type="portrait",
        alt_text="Current frame.",
        eyes_closed=0.0,
        dup_key="d_bbbbbbbbbbbbbbbb",
        exposure=0.5,
        sharpness=0.8,
    )
    _pipeline_summary(
        conn,
        tenant_id,
        gallery["id"],
        image_id,
        vision_status="running",
    )
    conn.commit()

    response = client.get(f"/galleries/{gallery['id']}/vision-calibration.csv")

    row = next(csv.DictReader(io.StringIO(response.text)))
    assert row["analysis_status"] == "analyzed"
    assert row["keeper_score"] == "0.9"
    assert row["vision_backend"] == ""
    assert row["fallback_from"] == ""
    assert row["fallback_scope"] == ""
    assert row["style_applied"] == ""
    assert row["vision_completed_at"] == ""
    assert row["pipeline_hero"] == ""


def test_gallery_links_the_studio_calibration_packet(client, conn):
    creds = onboard_studio(client, email="calibration-link@example.com")
    login_owner(client, creds)
    tenant_id = _tenant_id(conn)
    gallery = create_gallery(conn, tenant_id=tenant_id, title="Linked Calibration")
    _image(
        conn,
        tenant_id,
        gallery["id"],
        filename="IMG_1000.jpg",
        position=1,
    )
    conn.commit()

    page = client.get(f"/galleries/{gallery['id']}")

    assert page.status_code == 200
    assert f"/galleries/{gallery['id']}/vision-calibration.csv" in page.text
    assert "Export AI review" in page.text


def test_calibration_route_reads_one_database_snapshot(client, conn, monkeypatch):
    creds = onboard_studio(client, email="calibration-snapshot@example.com")
    login_owner(client, creds)
    tenant_id = _tenant_id(conn)
    gallery = create_gallery(conn, tenant_id=tenant_id, title="Snapshot Calibration")
    conn.commit()
    observed: dict[str, bool] = {}

    def inspect_transaction(route_conn, actual_tenant_id, actual_gallery_id):
        observed["in_transaction"] = route_conn.in_transaction
        assert actual_tenant_id == tenant_id
        assert actual_gallery_id == gallery["id"]
        return []

    monkeypatch.setattr(
        "hestia.routes.galleries.gallery_calibration_rows",
        inspect_transaction,
    )

    response = client.get(f"/galleries/{gallery['id']}/vision-calibration.csv")

    assert response.status_code == 200
    assert observed == {"in_transaction": True}
