"""Album designer — the 'code validates' placement guarantee + idempotency."""

import dataclasses
import io

from conftest import login_owner, onboard_studio

from hestia.albums import (
    MockArranger,
    XaiArranger,
    build_arranger,
    generate_album,
    get_album_for_gallery,
    validate_and_repair,
)
from hestia.galleries import add_image, create_gallery
from hestia.tenants import create_tenant


def test_validate_and_repair_guarantees_permutation():
    all_ids = [1, 2, 3, 4, 5]
    # proposal with a duplicate (2), a foreign id (99), and a missing one (5)
    out = validate_and_repair([3, 2, 2, 99, 1, 4], all_ids)
    assert sorted(out) == all_ids          # every photo exactly once
    assert len(out) == len(set(out))       # no duplicates
    assert out[:4] == [3, 2, 1, 4]         # honors valid proposal order
    assert out[-1] == 5                    # backfills the dropped one


def test_build_arranger_selection(settings):
    assert isinstance(build_arranger(settings), MockArranger)
    assert isinstance(build_arranger(dataclasses.replace(settings, album_backend="xai")), XaiArranger)


def _gallery_with_images(conn, storage, n=10, heroes=None):
    t = create_tenant(conn, name="Album Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="Big Day")
    ids = []
    for i in range(n):
        img = add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"],
                        filename=f"f{i}.jpg", fileobj=io.BytesIO(bytes([i]) * 16))
        ids.append(img["id"])
    # optional controlled hero scores
    if heroes:
        for iid, score in zip(ids, heroes, strict=False):
            conn.execute(
                "INSERT INTO image_analyses (image_id, gallery_id, tenant_id, hero_potential) "
                "VALUES (?, ?, ?, ?)", (iid, g["id"], t["id"], score))
    conn.commit()
    return t, g, ids


def test_every_photo_placed_exactly_once(conn, storage, settings):
    t, g, ids = _gallery_with_images(conn, storage, n=10)
    album = generate_album(conn, settings, tenant=t, gallery=g, per_spread=4)
    placed = [pid for sp in album["spreads"] for pid in sp["photo_ids"]]
    assert sorted(placed) == sorted(ids)      # no photo dropped or duplicated
    assert album["photo_count"] == 10
    assert len(album["spreads"]) == 3         # ceil(10/4)


def test_hero_is_top_scored_per_spread(conn, storage, settings):
    # 8 photos, per_spread 4 → two spreads; make photo 3 and 7 the clear heroes
    heroes = [0.1, 0.1, 0.1, 0.9, 0.1, 0.1, 0.1, 0.95]
    t, g, ids = _gallery_with_images(conn, storage, n=8, heroes=heroes)
    album = generate_album(conn, settings, tenant=t, gallery=g, per_spread=4)
    spread_heroes = {sp["hero_image_id"] for sp in album["spreads"]}
    assert ids[3] in spread_heroes and ids[7] in spread_heroes


def test_album_is_idempotent_per_gallery(conn, storage, settings):
    t, g, _ = _gallery_with_images(conn, storage, n=6)
    a1 = generate_album(conn, settings, tenant=t, gallery=g)
    a2 = generate_album(conn, settings, tenant=t, gallery=g)
    assert a1["id"] == a2["id"]
    assert conn.execute("SELECT COUNT(*) AS n FROM albums").fetchone()["n"] == 1


def test_album_works_without_vision(conn, storage, settings):
    # no image_analyses rows → still places every photo, heroes default to first
    t, g, ids = _gallery_with_images(conn, storage, n=5)
    album = generate_album(conn, settings, tenant=t, gallery=g)
    placed = [pid for sp in album["spreads"] for pid in sp["photo_ids"]]
    assert sorted(placed) == sorted(ids)


def test_tenant_isolation(conn, storage, settings):
    t1, g1, _ = _gallery_with_images(conn, storage, n=4)
    generate_album(conn, settings, tenant=t1, gallery=g1)
    t2 = create_tenant(conn, name="Other", shoot_type="wedding")
    conn.commit()
    assert get_album_for_gallery(conn, t2["id"], g1["id"]) is None


def test_http_generate_and_view(client):
    creds = onboard_studio(client, email="album@example.com")
    login_owner(client, creds)
    gid = client.post("/galleries", data={"title": "Wedding"}).url.path.rstrip("/").split("/")[-1]
    files = [("files", (f"f{i}.jpg", bytes([i]) * 32, "image/jpeg")) for i in range(6)]
    client.post(f"/galleries/{gid}/images", files=files)
    r = client.post(f"/galleries/{gid}/album")
    assert "/albums/" in str(r.url)
    page = client.get(str(r.url).replace("http://testserver", ""))
    assert page.status_code == 200 and "Spread" in page.text
