"""Client gallery selected-photo projection and safe navigation continuity."""

import io
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from hestia.galleries import (
    add_image,
    create_gallery,
    publish_gallery,
    set_cover_image,
    set_image_hidden,
)
from hestia.proofing import add_comment, favorite_count, set_favorite
from hestia.tenants import create_tenant


def _image(conn, storage, tenant_id, gallery_id, filename, color=(90, 120, 200)):
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


def _caption(conn, tenant_id, gallery_id, image_id, alt_text):
    conn.execute(
        "INSERT INTO image_analyses "
        "(image_id, gallery_id, tenant_id, keywords_json, alt_text) "
        "VALUES (?, ?, ?, '[]', ?)",
        (image_id, gallery_id, tenant_id, alt_text),
    )


def test_selected_view_projects_only_visible_favorites_in_gallery_order(client, conn, storage):
    studio = create_tenant(conn, name="Selected Studio", shoot_type="wedding")
    other = create_tenant(conn, name="Other Studio", shoot_type="wedding")
    gallery = create_gallery(conn, tenant_id=studio["id"], title="Wedding")
    sibling = create_gallery(conn, tenant_id=studio["id"], title="Sibling")
    other_gallery = create_gallery(conn, tenant_id=other["id"], title="Other")
    first = _image(conn, storage, studio["id"], gallery["id"], "01-first.jpg")
    second = _image(conn, storage, studio["id"], gallery["id"], "02-selected.jpg")
    third = _image(conn, storage, studio["id"], gallery["id"], "03-selected.jpg")
    hidden = _image(conn, storage, studio["id"], gallery["id"], "04-hidden.jpg")
    sibling_image = _image(conn, storage, studio["id"], sibling["id"], "sibling-secret.jpg")
    other_image = _image(conn, storage, other["id"], other_gallery["id"], "other-secret.jpg")
    for image in (second, third, hidden):
        assert set_favorite(
            conn,
            tenant_id=studio["id"],
            gallery_id=gallery["id"],
            image_id=image["id"],
            favorited=True,
        )
    set_image_hidden(conn, studio["id"], hidden["id"], True)
    add_comment(
        conn,
        tenant_id=studio["id"],
        gallery_id=gallery["id"],
        image_id=second["id"],
        body="selected note",
    )
    _caption(
        conn,
        studio["id"],
        gallery["id"],
        second["id"],
        "selected frame description",
    )
    conn.execute(
        "INSERT INTO image_favorites (tenant_id, gallery_id, image_id) VALUES (?, ?, ?)",
        (other["id"], gallery["id"], first["id"]),
    )
    for malformed_target in (sibling_image, other_image):
        conn.execute(
            "INSERT INTO image_favorites (tenant_id, gallery_id, image_id) "
            "VALUES (?, ?, ?)",
            (studio["id"], gallery["id"], malformed_target["id"]),
        )
    conn.execute(
        "INSERT INTO image_comments (tenant_id, gallery_id, image_id, body) "
        "VALUES (?, ?, ?, ?)",
        (studio["id"], gallery["id"], other_image["id"], "foreign image note"),
    )
    _caption(
        conn,
        studio["id"],
        gallery["id"],
        other_image["id"],
        "foreign image alt",
    )
    conn.execute(
        "INSERT INTO image_comments (tenant_id, gallery_id, image_id, body) VALUES (?, ?, ?, ?)",
        (other["id"], gallery["id"], third["id"], "foreign note"),
    )
    _caption(
        conn,
        other["id"],
        gallery["id"],
        third["id"],
        "foreign alt",
    )
    assert set_cover_image(conn, studio["id"], gallery["id"], second["id"]) is True
    publish_gallery(conn, studio["id"], gallery["id"])
    conn.commit()
    base = f"/g/{studio['slug']}/{gallery['slug']}"

    for suffix in ("", "?view=all", "?view=favorites", "?view=hostile%0d%0aX-Test%3Ayes"):
        page = client.get(f"{base}{suffix}")
        assert page.status_code == 200
        assert page.text.count("data-gallery-item") == 3
        assert (
            page.text.index(f'id="img-{first["id"]}"')
            < page.text.index(f'id="img-{second["id"]}"')
            < page.text.index(f'id="img-{third["id"]}"')
        )
        assert "hostile" not in page.text

    selected = client.get(f"{base}?view=selected")
    assert selected.status_code == 200
    assert selected.text.count("data-gallery-item") == 2
    assert selected.text.index(f'id="img-{second["id"]}"') < selected.text.index(
        f'id="img-{third["id"]}"'
    )
    assert f'id="img-{first["id"]}"' not in selected.text
    assert first["access_token"] not in selected.text
    assert "01-first.jpg" not in selected.text
    assert hidden["access_token"] not in selected.text
    assert sibling_image["access_token"] not in selected.text
    assert other_image["access_token"] not in selected.text
    assert "04-hidden.jpg" not in selected.text
    assert "sibling-secret.jpg" not in selected.text
    assert "other-secret.jpg" not in selected.text
    assert "foreign note" not in selected.text
    assert "foreign alt" not in selected.text
    assert "foreign image note" not in selected.text
    assert "foreign image alt" not in selected.text
    assert "selected note" in selected.text
    assert "selected frame description" in selected.text
    assert 'aria-label="Gallery view"' in selected.text
    assert f'href="{base}#gallery-view-heading"' in selected.text
    assert f'href="{base}?view=selected#gallery-view-heading"' in selected.text
    assert "All photos" in selected.text
    assert selected.text.count(
        '<span class="gallery-view-count">3</span>'
    ) == 1
    assert "Selected photos" in selected.text
    assert selected.text.count(
        '<span class="gallery-view-count">2</span>'
    ) == 1
    assert "3 photographs · in this gallery" in selected.text
    assert "Some photos selected earlier are no longer available." in selected.text

    for rejected in (
        first["id"],
        hidden["id"],
        sibling_image["id"],
        other_image["id"],
        1 << 63,
    ):
        page = client.get(
            base,
            params={"view": "selected", "lightbox": str(rejected)},
        )
        assert 'data-reopen-image=""' in page.text
        assert f'data-reopen-image="{rejected}"' not in page.text

    accepted = client.get(
        base,
        params={"view": "selected", "lightbox": str(second["id"])},
    )
    assert f'data-reopen-image="{second["id"]}"' in accepted.text


def test_selected_empty_state_keeps_gallery_story_but_emits_no_grid_media_or_script(
    client, conn, storage
):
    studio = create_tenant(conn, name="Empty Selection", shoot_type="wedding")
    gallery = create_gallery(conn, tenant_id=studio["id"], title="Portraits")
    cover = _image(conn, storage, studio["id"], gallery["id"], "cover.jpg")
    _image(conn, storage, studio["id"], gallery["id"], "second.jpg")
    set_cover_image(conn, studio["id"], gallery["id"], cover["id"])
    publish_gallery(conn, studio["id"], gallery["id"])
    conn.commit()
    base = f"/g/{studio['slug']}/{gallery['slug']}"

    page = client.get(f"{base}?view=selected")
    assert page.status_code == 200
    assert "2 photographs · in this gallery" in page.text
    assert "No selected photos yet." in page.text
    assert "Show all photos" in page.text
    assert f'href="{base}#gallery-view-heading"' in page.text
    assert "No photos are available in this gallery yet." not in page.text
    assert f'src="/media/{cover["access_token"]}?s=t"' in page.text
    assert "data-gallery-item" not in page.text
    assert 'id="gallery-lightbox"' not in page.text
    assert '<script src="/static/client-gallery.js" defer></script>' not in page.text
    assert f'id="img-{cover["id"]}"' not in page.text


def test_locked_selected_view_is_media_dark_and_pin_preserves_normalized_view(
    client, conn, storage
):
    studio = create_tenant(conn, name="Locked Selection", shoot_type="wedding")
    gallery = create_gallery(conn, tenant_id=studio["id"], title="Protected", pin="2468")
    image = _image(conn, storage, studio["id"], gallery["id"], "private.jpg")
    assert set_favorite(
        conn,
        tenant_id=studio["id"],
        gallery_id=gallery["id"],
        image_id=image["id"],
        favorited=True,
    )
    publish_gallery(conn, studio["id"], gallery["id"])
    conn.commit()
    base = f"/g/{studio['slug']}/{gallery['slug']}"

    locked = client.get(
        base,
        params={"view": "selected", "lightbox": str(image["id"])},
    )
    assert locked.status_code == 200
    assert image["access_token"] not in locked.text
    assert image["filename"] not in locked.text
    assert "Selected photos" not in locked.text
    assert "data-gallery-item" not in locked.text
    assert 'id="gallery-lightbox"' not in locked.text
    assert '<script src="/static/client-gallery.js" defer></script>' not in locked.text
    assert 'name="view" value="selected"' in locked.text

    wrong = client.post(
        f"{base}/pin",
        data={"pin": "0000", "view": "selected"},
        follow_redirects=False,
    )
    assert wrong.headers["location"] == f"{base}?view=selected"
    assert image["access_token"] not in client.get(wrong.headers["location"]).text

    unlocked = client.post(
        f"{base}/pin",
        data={"pin": "2468", "view": "selected"},
        follow_redirects=False,
    )
    assert unlocked.headers["location"] == f"{base}?view=selected"
    assert f'id="img-{image["id"]}"' in client.get(unlocked.headers["location"]).text

    hostile = client.post(
        f"{base}/pin",
        data={"pin": "2468", "view": "//evil.example/%0d%0aX-Test:yes"},
        follow_redirects=False,
    )
    assert hostile.headers["location"] == base
    assert "evil" not in hostile.headers["location"]


def test_selected_mode_post_redirects_preserve_only_safe_presentation_state(client, conn, storage):
    studio = create_tenant(conn, name="Continuity Studio", shoot_type="wedding")
    other = create_tenant(conn, name="Other Continuity", shoot_type="wedding")
    gallery = create_gallery(conn, tenant_id=studio["id"], title="Wedding")
    foreign_gallery = create_gallery(conn, tenant_id=other["id"], title="Foreign")
    first = _image(conn, storage, studio["id"], gallery["id"], "first.jpg")
    second = _image(conn, storage, studio["id"], gallery["id"], "second.jpg")
    hidden = _image(conn, storage, studio["id"], gallery["id"], "hidden.jpg")
    foreign = _image(conn, storage, other["id"], foreign_gallery["id"], "foreign.jpg")
    for image in (first, second, hidden):
        assert set_favorite(
            conn,
            tenant_id=studio["id"],
            gallery_id=gallery["id"],
            image_id=image["id"],
            favorited=True,
        )
    set_image_hidden(conn, studio["id"], hidden["id"], True)
    publish_gallery(conn, studio["id"], gallery["id"])
    conn.commit()
    base = f"/g/{studio['slug']}/{gallery['slug']}"

    note = client.post(
        f"{base}/comment/{second['id']}",
        data={
            "body": "Keep this",
            "return_to": "lightbox",
            "view": "selected",
        },
        follow_redirects=False,
    )
    assert note.headers["location"] == (
        f"{base}?view=selected&lightbox={second['id']}#img-{second['id']}"
    )

    remove = client.post(
        f"{base}/favorite/{second['id']}",
        data={
            "favorite": "0",
            "return_to": "lightbox",
            "view": "selected",
        },
        follow_redirects=False,
    )
    assert remove.headers["location"] == (f"{base}?view=selected#gallery-view-heading")
    assert str(second["id"]) not in remove.headers["location"]
    assert favorite_count(conn, gallery["id"], tenant_id=studio["id"]) == 2

    unselected_note = client.post(
        f"{base}/comment/{second['id']}",
        data={
            "body": "Valid note without selected navigation authority",
            "return_to": "lightbox",
            "view": "selected",
        },
        follow_redirects=False,
    )
    assert unselected_note.headers["location"] == (f"{base}?view=selected#gallery-view-heading")
    assert "lightbox=" not in unselected_note.headers["location"]
    assert "#img-" not in unselected_note.headers["location"]

    submit = client.post(
        f"{base}/submit",
        data={"view": "selected"},
        follow_redirects=False,
    )
    assert submit.headers["location"] == f"{base}?view=selected#proofing-actions"

    for image_id in (hidden["id"], foreign["id"], 1 << 63):
        rejected = client.post(
            f"{base}/favorite/{image_id}",
            data={
                "favorite": "1",
                "return_to": "//evil.example/",
                "view": "selected",
            },
            follow_redirects=False,
        )
        assert rejected.headers["location"] == f"{base}?view=selected"
        assert str(image_id) not in rejected.headers["location"]
        assert "evil" not in rejected.headers["location"]

    hostile = client.post(
        f"{base}/comment/{first['id']}",
        data={
            "body": "Still safe",
            "return_to": "lightbox",
            "view": "//evil.example/?view=selected",
        },
        follow_redirects=False,
    )
    location = hostile.headers["location"]
    assert location == f"{base}?lightbox={first['id']}#img-{first['id']}"
    assert urlsplit(location).netloc == ""
    assert parse_qs(urlsplit(location).query) == {"lightbox": [str(first["id"])]}
