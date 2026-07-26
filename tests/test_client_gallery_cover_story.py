"""Cover-led client gallery opening without widening public media authority."""

from __future__ import annotations

import io

from conftest import login_owner, onboard_studio
from PIL import Image

from hestia.galleries import (
    add_image,
    create_gallery,
    gallery_story_cover_image,
    publish_gallery,
    set_cover_image,
    set_image_hidden,
)
from hestia.tenants import create_tenant


def _image(conn, storage, tenant_id, gallery_id, filename, color):
    image_bytes = io.BytesIO()
    Image.new("RGB", (1200, 800), color).save(image_bytes, format="JPEG", quality=90)
    image_bytes.seek(0)
    return add_image(
        conn,
        storage,
        tenant_id=tenant_id,
        gallery_id=gallery_id,
        filename=filename,
        fileobj=image_bytes,
        content_type="image/jpeg",
    )


def _alt(conn, tenant_id, gallery_id, image_id, text):
    conn.execute(
        "INSERT INTO image_analyses "
        "(image_id, gallery_id, tenant_id, keywords_json, alt_text) "
        "VALUES (?, ?, ?, '[]', ?)",
        (image_id, gallery_id, tenant_id, text),
    )


def _story_markup(page: str) -> str:
    start = page.index('<header class="client-gallery-story"')
    return page[start : page.index("</header>", start)]


def test_story_cover_selector_requires_an_authorized_visible_browse_thumbnail():
    first = {
        "id": 11,
        "filename": "first.jpg",
        "hidden": 0,
        "thumb_key": "first-thumb.jpg",
    }
    second = {
        "id": 12,
        "filename": "second.jpg",
        "hidden": 0,
        "thumb_key": "second-thumb.jpg",
    }
    hidden = {
        "id": 13,
        "filename": "hidden.jpg",
        "hidden": 1,
        "thumb_key": "hidden-thumb.jpg",
    }
    original_only = {
        "id": 14,
        "filename": "original-only.tiff",
        "hidden": 0,
        "thumb_key": None,
    }
    authorized = [hidden, original_only, first, second]

    assert gallery_story_cover_image(authorized, second["id"]) is second
    assert gallery_story_cover_image(authorized, 999_999) is first
    assert gallery_story_cover_image(authorized, hidden["id"]) is first
    assert gallery_story_cover_image(authorized, original_only["id"]) is first
    assert gallery_story_cover_image(authorized, None) is first
    assert gallery_story_cover_image([hidden, original_only], second["id"]) is None
    assert gallery_story_cover_image([], second["id"]) is None


def test_selected_visible_cover_opens_the_unlocked_client_gallery(client, conn, storage):
    studio = create_tenant(conn, name="North Star Studio", shoot_type="wedding")
    gallery = create_gallery(conn, tenant_id=studio["id"], title="Avery & Rowan")
    first = _image(
        conn, storage, studio["id"], gallery["id"], "01-arrival.jpg", (60, 80, 100)
    )
    cover = _image(
        conn, storage, studio["id"], gallery["id"], "02-ceremony.jpg", (120, 90, 70)
    )
    _alt(
        conn,
        studio["id"],
        gallery["id"],
        cover["id"],
        "A sunlit ceremony beneath old oak trees.",
    )
    assert set_cover_image(conn, studio["id"], gallery["id"], cover["id"]) is True
    assert publish_gallery(conn, studio["id"], gallery["id"]) is True
    conn.commit()

    page = client.get(f"/g/{studio['slug']}/{gallery['slug']}")

    assert page.status_code == 200
    assert '<link rel="stylesheet" href="/static/client-gallery-story.css">' in page.text
    story = _story_markup(page.text)
    assert 'data-client-gallery-story' in story
    assert f'src="/media/{cover["access_token"]}?s=t"' in story
    assert first["access_token"] not in story
    assert 'alt="A sunlit ceremony beneath old oak trees."' in story
    assert "North Star Studio" in story
    assert "Avery &amp; Rowan" in story
    assert "2 photographs" in story
    assert 'href="#gallery-photos"' in story
    assert "data-gallery-item" not in story
    assert "data-lightbox-open" not in story
    assert 'id="gallery-photos"' in page.text
    assert page.text.index("data-client-gallery-story") < page.text.index('id="proofing-actions"')
    assert page.text.count("data-gallery-item") == 2
    assert first["storage_key"] not in page.text
    assert cover["storage_key"] not in page.text

    assert set_cover_image(conn, studio["id"], gallery["id"], first["id"]) is True
    conn.commit()
    changed = client.get(f"/g/{studio['slug']}/{gallery['slug']}")
    changed_story = _story_markup(changed.text)
    assert f'src="/media/{first["access_token"]}?s=t"' in changed_story
    assert cover["access_token"] not in changed_story


def test_hidden_cover_falls_back_to_first_authorized_visible_image(client, conn, storage):
    studio = create_tenant(conn, name="Fallback Studio", shoot_type="portrait")
    gallery = create_gallery(conn, tenant_id=studio["id"], title="Family")
    visible = _image(
        conn, storage, studio["id"], gallery["id"], "visible.jpg", (30, 70, 110)
    )
    hidden = _image(
        conn, storage, studio["id"], gallery["id"], "hidden.jpg", (120, 20, 40)
    )
    assert set_cover_image(conn, studio["id"], gallery["id"], hidden["id"]) is True
    assert set_image_hidden(
        conn,
        studio["id"],
        hidden["id"],
        True,
        gallery_id=gallery["id"],
    ) is True
    assert publish_gallery(conn, studio["id"], gallery["id"]) is True
    conn.commit()

    page = client.get(f"/g/{studio['slug']}/{gallery['slug']}")

    assert page.status_code == 200
    story = _story_markup(page.text)
    assert f'src="/media/{visible["access_token"]}?s=t"' in story
    assert hidden["access_token"] not in page.text
    assert hidden["storage_key"] not in page.text
    assert hidden["filename"] not in page.text
    assert page.text.count("data-gallery-item") == 1


def test_gallery_without_a_browse_thumbnail_keeps_text_header_and_normal_grid(
    client, conn, storage
):
    studio = create_tenant(conn, name="Original Only Studio", shoot_type="portrait")
    gallery = create_gallery(conn, tenant_id=studio["id"], title="Original only")
    image = _image(
        conn,
        storage,
        studio["id"],
        gallery["id"],
        "large-original-only.jpg",
        (245, 245, 245),
    )
    conn.execute("UPDATE images SET thumb_key = NULL WHERE id = ?", (image["id"],))
    assert publish_gallery(conn, studio["id"], gallery["id"]) is True
    conn.commit()

    page = client.get(f"/g/{studio['slug']}/{gallery['slug']}")

    assert page.status_code == 200
    assert '<header class="client-header">' in page.text
    assert 'class="client-gallery-story"' not in page.text
    assert "data-client-gallery-story" not in page.text
    assert page.text.count("data-gallery-item") == 1
    assert f'src="/media/{image["access_token"]}"' in page.text
    assert f'/media/{image["access_token"]}?s=t' not in page.text
    assert image["storage_key"] not in page.text


def test_pin_locked_gallery_is_media_dark_until_successful_unlock(client, conn, storage):
    studio = create_tenant(conn, name="Private Studio", shoot_type="wedding")
    gallery = create_gallery(
        conn,
        tenant_id=studio["id"],
        title="Private gallery",
        pin="2468",
    )
    cover = _image(
        conn,
        storage,
        studio["id"],
        gallery["id"],
        "private-cover-never-preload.jpg",
        (80, 40, 100),
    )
    private_alt = "Private client standing beside a window."
    _alt(conn, studio["id"], gallery["id"], cover["id"], private_alt)
    assert publish_gallery(conn, studio["id"], gallery["id"]) is True
    conn.commit()

    url = f"/g/{studio['slug']}/{gallery['slug']}"
    locked = client.get(url)
    client.post(f"{url}/pin", data={"pin": "0000"})
    wrong_pin = client.get(url)

    for response in (locked, wrong_pin):
        assert response.status_code == 200
        assert 'class="client-gallery-story"' not in response.text
        assert "data-client-gallery-story" not in response.text
        assert cover["access_token"] not in response.text
        assert cover["storage_key"] not in response.text
        assert cover["filename"] not in response.text
        assert private_alt not in response.text
        assert "<img" not in response.text
        assert "?s=t" not in response.text
        assert "/static/client-gallery.js" not in response.text
        assert "preload" not in response.text
        assert "background-image" not in response.text
        assert "url(" not in response.text

    client.post(f"{url}/pin", data={"pin": "2468"})
    unlocked = client.get(url)
    assert f'src="/media/{cover["access_token"]}?s=t"' in _story_markup(unlocked.text)


def test_empty_published_gallery_keeps_the_text_header_and_no_client_script(
    client, conn
):
    studio = create_tenant(conn, name="Empty Studio", shoot_type="wedding")
    gallery = create_gallery(conn, tenant_id=studio["id"], title="Coming soon")
    assert publish_gallery(conn, studio["id"], gallery["id"]) is True
    conn.commit()

    page = client.get(f"/g/{studio['slug']}/{gallery['slug']}")

    assert page.status_code == 200
    assert '<header class="client-header">' in page.text
    assert 'class="client-gallery-story"' not in page.text
    assert 'id="gallery-photos"' not in page.text
    assert "/static/client-gallery.js" not in page.text
    assert "No photos are available in this gallery yet." in page.text


def test_owner_copy_explains_that_existing_cover_controls_client_opening(
    client, conn, storage
):
    creds = onboard_studio(
        client,
        email="client-cover@studio.test",
        name="Client Cover Studio",
    )
    login_owner(client, creds)
    tenant_id = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    gallery = create_gallery(conn, tenant_id=tenant_id, title="Wedding")
    _image(conn, storage, tenant_id, gallery["id"], "cover.jpg", (40, 80, 120))
    _image(conn, storage, tenant_id, gallery["id"], "alternate.jpg", (120, 80, 40))
    conn.commit()

    page = client.get(f"/galleries/{gallery['id']}")

    assert page.status_code == 200
    assert "This cover opens the client gallery" in page.text
    assert '<span class="pill ember">client cover</span>' in page.text
    assert "Set as cover" in page.text


def test_owner_and_client_agree_when_the_saved_cover_is_hidden(client, conn, storage):
    creds = onboard_studio(
        client,
        email="hidden-client-cover@studio.test",
        name="Hidden Cover Studio",
    )
    login_owner(client, creds)
    tenant = conn.execute("SELECT id, slug FROM tenants LIMIT 1").fetchone()
    tenant_id = tenant["id"]
    gallery = create_gallery(conn, tenant_id=tenant_id, title="Fallback wedding")
    visible = _image(
        conn,
        storage,
        tenant_id,
        gallery["id"],
        "visible-client-cover.jpg",
        (35, 75, 115),
    )
    saved_cover = _image(
        conn,
        storage,
        tenant_id,
        gallery["id"],
        "saved-cover-now-hidden.jpg",
        (125, 75, 35),
    )
    assert set_cover_image(conn, tenant_id, gallery["id"], saved_cover["id"]) is True
    assert set_image_hidden(
        conn,
        tenant_id,
        saved_cover["id"],
        True,
        gallery_id=gallery["id"],
    ) is True
    assert publish_gallery(conn, tenant_id, gallery["id"]) is True
    conn.commit()

    owner_page = client.get(f"/galleries/{gallery['id']}")
    client_page = client.get(f"/g/{tenant['slug']}/{gallery['slug']}")

    assert owner_page.status_code == 200
    assert (
        '<span class="name">visible-client-cover.jpg</span>'
        '<span class="pill ember">client cover</span>'
    ) in owner_page.text
    assert "the saved cover is hidden, unavailable, or has no browse thumbnail" in owner_page.text
    assert "Set as cover" in owner_page.text
    story = _story_markup(client_page.text)
    assert f'src="/media/{visible["access_token"]}?s=t"' in story
    assert saved_cover["access_token"] not in client_page.text
    assert saved_cover["storage_key"] not in client_page.text


def test_owner_discloses_text_header_when_no_visible_browse_thumbnail(
    client, conn, storage
):
    creds = onboard_studio(
        client,
        email="no-thumb-cover@studio.test",
        name="No Thumbnail Studio",
    )
    login_owner(client, creds)
    tenant_id = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
    gallery = create_gallery(conn, tenant_id=tenant_id, title="Legacy originals")
    image = _image(
        conn,
        storage,
        tenant_id,
        gallery["id"],
        "legacy-original.jpg",
        (225, 225, 225),
    )
    conn.execute("UPDATE images SET thumb_key = NULL WHERE id = ?", (image["id"],))
    conn.commit()

    page = client.get(f"/galleries/{gallery['id']}")

    assert page.status_code == 200
    assert "no visible frame has a browse thumbnail" in page.text
    assert '<span class="pill ember">client cover</span>' not in page.text


def test_dedicated_story_stylesheet_is_served(client):
    response = client.get("/static/client-gallery-story.css")

    assert response.status_code == 200
    assert ".client-gallery-story" in response.text
    assert ".client-gallery-story-copy" in response.text
    assert "background: #17130f" in response.text
    assert response.text.count("overflow-wrap: anywhere") >= 2
    assert "@media (max-width: 640px)" in response.text
    assert "overflow-x" not in response.text
