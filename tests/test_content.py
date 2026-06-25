"""Marketing content — recipes, generation, keyword harvest, persistence."""

import dataclasses
import io
import json

from conftest import login_owner, onboard_studio

from hestia.content import (
    MockContent,
    XaiContent,
    build_content,
    generate_pack,
    list_packs,
    project_keywords,
    recipes_for,
)
from hestia.crm import create_project
from hestia.galleries import add_image, create_gallery
from hestia.tenants import create_tenant


def test_recipes_gated_by_shoot_type():
    food = {r["slug"] for r in recipes_for("food")}
    commercial = {r["slug"] for r in recipes_for("commercial")}
    wedding = {r["slug"] for r in recipes_for("wedding")}
    assert "menu-launch" in food and "menu-launch" not in wedding
    assert "brand-campaign" in commercial and "brand-campaign" not in food
    assert {"social-set", "shot-list"} <= wedding  # universal recipes everywhere


def test_build_content_selection(settings):
    assert isinstance(build_content(settings), MockContent)
    assert isinstance(build_content(dataclasses.replace(settings, content_backend="xai")), XaiContent)


def test_mock_generate_shape_and_shoot_type():
    project = {"name": "Bistro Launch", "shoot_type": "food"}
    body = MockContent().generate(project=project, recipe="menu-launch",
                                  keywords=["plating", "steam"])
    assert body["headline"] and body["strategy"]
    assert len(body["shot_list"]) >= 3
    assert any("flat-lay" in s.lower() for s in body["shot_list"])  # food-specific
    assert len(body["captions"]) >= 3
    assert "plating" in " ".join(body["captions"]).lower()  # seeded by keywords


def _project_with_keywords(conn, storage):
    t = create_tenant(conn, name="Content Studio", shoot_type="food")
    p = create_project(conn, tenant_id=t["id"], name="Menu Shoot", shoot_type="food")
    g = create_gallery(conn, tenant_id=t["id"], title="Dishes")
    conn.execute("UPDATE galleries SET project_id = ? WHERE id = ?", (p["id"], g["id"]))
    img = add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"],
                    filename="dish.jpg", fileobj=io.BytesIO(b"x" * 16))
    conn.execute(
        "INSERT INTO image_analyses (image_id, gallery_id, tenant_id, keywords_json) VALUES (?, ?, ?, ?)",
        (img["id"], g["id"], t["id"], json.dumps(["plating", "macro", "plating"])))
    conn.commit()
    return t, p


def test_project_keywords_harvest(conn, storage):
    t, p = _project_with_keywords(conn, storage)
    kws = project_keywords(conn, t["id"], p["id"])
    assert "plating" in kws and "macro" in kws


def test_generate_pack_persists(conn, storage, settings):
    t, p = _project_with_keywords(conn, storage)
    pack = generate_pack(conn, settings, tenant=t, project=p, recipe="menu-launch")
    assert pack["recipe"] == "menu-launch"
    assert set(pack["body"]) == {"headline", "strategy", "shot_list", "captions"}
    assert list_packs(conn, t["id"], project_id=p["id"])[0]["id"] == pack["id"]


def test_unknown_recipe_falls_back(conn, storage, settings):
    t, p = _project_with_keywords(conn, storage)
    pack = generate_pack(conn, settings, tenant=t, project=p, recipe="bogus")
    assert pack["recipe"] == "social-set"


def test_tenant_isolation(conn, storage, settings):
    t1, p1 = _project_with_keywords(conn, storage)
    generate_pack(conn, settings, tenant=t1, project=p1)
    t2 = create_tenant(conn, name="Other", shoot_type="food")
    conn.commit()
    assert list_packs(conn, t2["id"]) == []


def test_http_generate_and_view(client):
    creds = onboard_studio(client, shoot_type="food", email="content@example.com")
    login_owner(client, creds)
    pid = client.post("/projects", data={"name": "Bistro", "shoot_type": "food"}).url.path.split("/")[-1]
    r = client.post(f"/projects/{pid}/content", data={"recipe": "menu-launch"})
    assert "/content/" in str(r.url)
    page = client.get(str(r.url).replace("http://testserver", ""))
    assert page.status_code == 200 and "Captions" in page.text
