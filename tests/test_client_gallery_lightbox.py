"""Client gallery lightbox: authorized navigation, proofing continuity, and a11y markup."""

import io

from PIL import Image

from hestia.galleries import (
    add_image,
    create_gallery,
    get_gallery,
    publish_gallery,
    set_image_hidden,
    submit_selections,
)
from hestia.proofing import (
    add_comment,
    comments_by_image,
    favorite_count,
    selection_packet,
    set_favorite,
)
from hestia.tenants import create_tenant


def _image(conn, storage, tenant_id, gallery_id, filename):
    image_bytes = io.BytesIO()
    Image.new("RGB", (1200, 800), (90, 120, 200)).save(
        image_bytes, format="JPEG", quality=90
    )
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


def test_lightbox_navigation_serializes_only_visible_resolved_gallery_images(
    client, conn, storage
):
    studio = create_tenant(conn, name="Visible Studio", shoot_type="wedding")
    other_studio = create_tenant(conn, name="Other Studio", shoot_type="wedding")
    gallery = create_gallery(conn, tenant_id=studio["id"], title="Finals")
    other_gallery = create_gallery(conn, tenant_id=studio["id"], title="Other")
    cross_tenant_gallery = create_gallery(
        conn, tenant_id=other_studio["id"], title="Finals"
    )
    first = _image(conn, storage, studio["id"], gallery["id"], "01-visible.jpg")
    hidden = _image(conn, storage, studio["id"], gallery["id"], "02-hidden.jpg")
    second = _image(conn, storage, studio["id"], gallery["id"], "03-visible.jpg")
    foreign = _image(conn, storage, studio["id"], other_gallery["id"], "foreign.jpg")
    cross_tenant = _image(
        conn,
        storage,
        other_studio["id"],
        cross_tenant_gallery["id"],
        "cross-tenant.jpg",
    )
    assert set_favorite(
        conn,
        tenant_id=studio["id"],
        gallery_id=gallery["id"],
        image_id=hidden["id"],
        favorited=True,
    ) is True
    add_comment(
        conn,
        tenant_id=studio["id"],
        gallery_id=gallery["id"],
        image_id=hidden["id"],
        body="hidden note must not render",
    )
    _caption(conn, studio["id"], gallery["id"], hidden["id"], "hidden alt must not render")
    _caption(
        conn,
        other_studio["id"],
        gallery["id"],
        first["id"],
        "cross-tenant alt must not render",
    )
    set_image_hidden(conn, studio["id"], hidden["id"], True)
    publish_gallery(conn, studio["id"], gallery["id"])
    publish_gallery(conn, studio["id"], other_gallery["id"])
    publish_gallery(conn, other_studio["id"], cross_tenant_gallery["id"])
    conn.commit()

    page = client.get(f"/g/{studio['slug']}/{gallery['slug']}")

    assert page.status_code == 200
    assert page.text.count("data-gallery-item") == 2
    assert page.text.index(f'id="img-{first["id"]}"') < page.text.index(
        f'id="img-{second["id"]}"'
    )
    for visible in (first, second):
        assert f"/media/{visible['access_token']}" in page.text
        assert f'id="img-{visible["id"]}"' in page.text
    for excluded in (hidden, foreign, cross_tenant):
        assert excluded["access_token"] not in page.text
        assert excluded["filename"] not in page.text
        assert f'id="img-{excluded["id"]}"' not in page.text
    assert "hidden note must not render" not in page.text
    assert "hidden alt must not render" not in page.text
    assert "cross-tenant alt must not render" not in page.text
    # Aggregate counts preserve the existing owner packet; no hidden frame identity or
    # content enters the viewer's authorized navigation set.
    assert "1 favorited so far" in page.text
    assert "1 note" in page.text
    assert 'type="application/json"' not in page.text
    assert "data-lightbox-src" not in page.text
    assert 'rel="preload"' not in page.text

    submit = client.post(
        f"/g/{studio['slug']}/{gallery['slug']}/submit", follow_redirects=False
    )
    assert submit.status_code == 303
    assert favorite_count(conn, gallery["id"], tenant_id=studio["id"]) == 1
    packet = selection_packet(conn, studio["id"], gallery["id"])
    assert packet["favorite_count"] == 1
    assert packet["comment_count"] == 1
    assert packet["submitted_at"]


def test_lightbox_has_native_dialog_labels_and_no_javascript_fallbacks(
    client, conn, storage
):
    studio = create_tenant(conn, name="Proof Studio", shoot_type="wedding")
    gallery = create_gallery(conn, tenant_id=studio["id"], title="Wedding")
    image = _image(conn, storage, studio["id"], gallery["id"], "portrait.jpg")
    publish_gallery(conn, studio["id"], gallery["id"])
    conn.commit()

    page = client.get(f"/g/{studio['slug']}/{gallery['slug']}").text

    assert '<dialog class="gallery-lightbox" id="gallery-lightbox"' in page
    assert 'aria-modal="true"' in page
    assert 'aria-labelledby="lightbox-title"' in page
    assert 'aria-describedby="lightbox-help"' in page
    assert 'aria-label="Close image viewer"' in page
    assert 'aria-label="Previous image"' in page
    assert 'aria-label="Next image"' in page
    assert 'aria-live="polite"' in page
    assert "1 of 1" in page
    assert 'data-lightbox-prev disabled' in page
    assert 'data-lightbox-next disabled' in page
    assert 'aria-pressed="false"' in page
    assert '<script src="/static/client-gallery.js" defer></script>' in page

    # The enhancement is optional: ordinary media and proofing forms remain usable.
    assert f'href="/media/{image["access_token"]}"' in page
    assert f'src="/media/{image["access_token"]}?s=t"' in page
    assert (
        f'action="/g/{studio["slug"]}/{gallery["slug"]}/favorite/{image["id"]}"'
        in page
    )
    assert (
        f'action="/g/{studio["slug"]}/{gallery["slug"]}/comment/{image["id"]}"'
        in page
    )
    assert 'name="favorite" value="1"' in page
    assert f'action="/g/{studio["slug"]}/{gallery["slug"]}/submit"' in page


def test_locked_invalid_draft_empty_and_single_image_gallery_states(
    client, conn, storage
):
    studio = create_tenant(conn, name="Access Studio", shoot_type="wedding")
    locked = create_gallery(conn, tenant_id=studio["id"], title="Locked", pin="2468")
    locked_image = _image(conn, storage, studio["id"], locked["id"], "locked.jpg")
    publish_gallery(conn, studio["id"], locked["id"])
    draft = create_gallery(conn, tenant_id=studio["id"], title="Draft")
    _image(conn, storage, studio["id"], draft["id"], "draft.jpg")
    empty = create_gallery(conn, tenant_id=studio["id"], title="Empty")
    publish_gallery(conn, studio["id"], empty["id"])
    conn.commit()

    locked_url = f"/g/{studio['slug']}/{locked['slug']}"
    locked_page = client.get(locked_url)
    assert locked_page.status_code == 200
    assert 'id="gallery-lightbox"' not in locked_page.text
    assert locked_image["access_token"] not in locked_page.text
    assert "data-gallery-item" not in locked_page.text

    client.post(f"{locked_url}/pin", data={"pin": "0000"})
    assert locked_image["access_token"] not in client.get(locked_url).text
    client.post(f"{locked_url}/pin", data={"pin": "2468"})
    unlocked_page = client.get(locked_url).text
    assert 'id="gallery-lightbox"' in unlocked_page
    assert unlocked_page.count("data-gallery-item") == 1

    assert client.get(f"/g/{studio['slug']}/{draft['slug']}").status_code == 404
    assert client.get(f"/g/{studio['slug']}/does-not-exist").status_code == 404
    empty_page = client.get(f"/g/{studio['slug']}/{empty['slug']}").text
    assert "No photos are available in this gallery yet." in empty_page
    assert 'id="gallery-lightbox"' not in empty_page
    assert "data-gallery-item" not in empty_page

    for invalid_lightbox in ("9" * 5000, "١", "1<script>"):
        invalid_page = client.get(
            locked_url,
            params={"lightbox": invalid_lightbox},
        )
        assert invalid_page.status_code == 200
        assert 'data-reopen-image=""' in invalid_page.text


def test_desired_favorite_and_comment_return_only_to_authorized_lightbox_image(
    client, conn, storage
):
    studio = create_tenant(conn, name="Continuity Studio", shoot_type="wedding")
    gallery = create_gallery(conn, tenant_id=studio["id"], title="Wedding")
    visible = _image(conn, storage, studio["id"], gallery["id"], "visible.jpg")
    hidden = _image(conn, storage, studio["id"], gallery["id"], "hidden.jpg")
    set_image_hidden(conn, studio["id"], hidden["id"], True)
    publish_gallery(conn, studio["id"], gallery["id"])
    conn.commit()
    base = f"/g/{studio['slug']}/{gallery['slug']}"

    first = client.post(
        f"{base}/favorite/{visible['id']}",
        data={"favorite": "1", "return_to": "lightbox"},
        follow_redirects=False,
    )
    repeat = client.post(
        f"{base}/favorite/{visible['id']}",
        data={"favorite": "1", "return_to": "lightbox"},
        follow_redirects=False,
    )
    assert first.headers["location"] == f"{base}?lightbox={visible['id']}#img-{visible['id']}"
    assert repeat.headers["location"] == first.headers["location"]
    assert favorite_count(conn, gallery["id"], tenant_id=studio["id"]) == 1
    reopened = client.get(first.headers["location"])
    assert reopened.status_code == 200
    assert f'data-reopen-image="{visible["id"]}"' in reopened.text
    favorited_page = client.get(base).text
    assert 'name="favorite" value="0"' in favorited_page
    assert 'aria-pressed="true"' in favorited_page

    assert submit_selections(
        conn, tenant_id=studio["id"], gallery_id=gallery["id"]
    ) is True
    conn.commit()
    submitted_at = get_gallery(conn, studio["id"], gallery["id"])[
        "selections_submitted_at"
    ]
    repeat_after_submit = client.post(
        f"{base}/favorite/{visible['id']}",
        data={"favorite": "1", "return_to": "lightbox"},
        follow_redirects=False,
    )
    assert repeat_after_submit.headers["location"] == first.headers["location"]
    assert get_gallery(conn, studio["id"], gallery["id"])[
        "selections_submitted_at"
    ] == submitted_at

    note = client.post(
        f"{base}/comment/{visible['id']}",
        data={
            "author_name": "Pat",
            "body": "Use this in the album",
            "return_to": "lightbox",
        },
        follow_redirects=False,
    )
    assert note.headers["location"] == first.headers["location"]
    assert get_gallery(conn, studio["id"], gallery["id"])[
        "selections_submitted_at"
    ] is None

    hidden_favorite = client.post(
        f"{base}/favorite/{hidden['id']}",
        data={"favorite": "1", "return_to": "lightbox"},
        follow_redirects=False,
    )
    hidden_note = client.post(
        f"{base}/comment/{hidden['id']}",
        data={"body": "stale", "return_to": "lightbox"},
        follow_redirects=False,
    )
    assert hidden_favorite.headers["location"] == base
    assert hidden_note.headers["location"] == base
    assert favorite_count(conn, gallery["id"], tenant_id=studio["id"]) == 1
    assert hidden["id"] not in comments_by_image(
        conn, gallery["id"], tenant_id=studio["id"]
    )

    assert submit_selections(
        conn, tenant_id=studio["id"], gallery_id=gallery["id"]
    ) is True
    conn.commit()
    remove = client.post(
        f"{base}/favorite/{visible['id']}",
        data={"favorite": "0", "return_to": "lightbox"},
        follow_redirects=False,
    )
    assert get_gallery(conn, studio["id"], gallery["id"])[
        "selections_submitted_at"
    ] is None
    assert submit_selections(
        conn, tenant_id=studio["id"], gallery_id=gallery["id"]
    ) is True
    conn.commit()
    resubmitted_at = get_gallery(conn, studio["id"], gallery["id"])[
        "selections_submitted_at"
    ]
    repeat_remove = client.post(
        f"{base}/favorite/{visible['id']}",
        data={"favorite": "0", "return_to": "lightbox"},
        follow_redirects=False,
    )
    assert remove.headers["location"] == first.headers["location"]
    assert repeat_remove.headers["location"] == first.headers["location"]
    assert favorite_count(conn, gallery["id"], tenant_id=studio["id"]) == 0
    assert get_gallery(conn, studio["id"], gallery["id"])[
        "selections_submitted_at"
    ] == resubmitted_at

    invalid_intent = client.post(
        f"{base}/favorite/{visible['id']}",
        data={"favorite": "toggle", "return_to": "lightbox"},
        follow_redirects=False,
    )
    assert invalid_intent.headers["location"] == base
    assert favorite_count(conn, gallery["id"], tenant_id=studio["id"]) == 0

    invalid_query = client.get(f"{base}?lightbox={hidden['id']}").text
    assert f'data-reopen-image="{hidden["id"]}"' not in invalid_query
    assert 'data-reopen-image=""' in invalid_query


def test_rejected_foreign_and_locked_mutations_do_not_echo_image_navigation_state(
    client, conn, storage
):
    studio = create_tenant(conn, name="Boundary Studio", shoot_type="wedding")
    other_studio = create_tenant(conn, name="Other Boundary", shoot_type="wedding")
    gallery = create_gallery(conn, tenant_id=studio["id"], title="Published")
    sibling = create_gallery(conn, tenant_id=studio["id"], title="Sibling")
    cross_gallery = create_gallery(
        conn, tenant_id=other_studio["id"], title="Cross tenant"
    )
    locked = create_gallery(
        conn, tenant_id=studio["id"], title="Locked", pin="2468"
    )
    sibling_image = _image(
        conn, storage, studio["id"], sibling["id"], "sibling.jpg"
    )
    cross_image = _image(
        conn, storage, other_studio["id"], cross_gallery["id"], "cross.jpg"
    )
    locked_image = _image(
        conn, storage, studio["id"], locked["id"], "locked.jpg"
    )
    publish_gallery(conn, studio["id"], gallery["id"])
    publish_gallery(conn, studio["id"], locked["id"])
    conn.commit()

    base = f"/g/{studio['slug']}/{gallery['slug']}"
    for image_id in (sibling_image["id"], cross_image["id"]):
        favorite = client.post(
            f"{base}/favorite/{image_id}",
            data={"favorite": "1", "return_to": "lightbox"},
            follow_redirects=False,
        )
        note = client.post(
            f"{base}/comment/{image_id}",
            data={"body": "stale", "return_to": "lightbox"},
            follow_redirects=False,
        )
        assert favorite.headers["location"] == base
        assert note.headers["location"] == base

    locked_base = f"/g/{studio['slug']}/{locked['slug']}"
    locked_favorite = client.post(
        f"{locked_base}/favorite/{locked_image['id']}",
        data={"favorite": "1", "return_to": "lightbox"},
        follow_redirects=False,
    )
    locked_note = client.post(
        f"{locked_base}/comment/{locked_image['id']}",
        data={"body": "stale", "return_to": "lightbox"},
        follow_redirects=False,
    )
    assert locked_favorite.headers["location"] == locked_base
    assert locked_note.headers["location"] == locked_base

    for invalid_id in ("0", "-1", str(1 << 63)):
        invalid_favorite = client.post(
            f"{base}/favorite/{invalid_id}",
            data={"favorite": "1", "return_to": "lightbox"},
            follow_redirects=False,
        )
        invalid_note = client.post(
            f"{base}/comment/{invalid_id}",
            data={"body": "stale", "return_to": "lightbox"},
            follow_redirects=False,
        )
        assert invalid_favorite.headers["location"] == base
        assert invalid_note.headers["location"] == base
    assert favorite_count(conn, gallery["id"], tenant_id=studio["id"]) == 0
    assert comments_by_image(conn, gallery["id"], tenant_id=studio["id"]) == {}


def test_hostile_existing_metadata_is_escaped_without_script_serialization(
    client, conn, storage
):
    studio = create_tenant(conn, name="Escape Studio", shoot_type="wedding")
    gallery = create_gallery(conn, tenant_id=studio["id"], title="Escapes")
    image = _image(
        conn,
        storage,
        studio["id"],
        gallery["id"],
        'frame"><script>window.filenamePwned=1</script>.jpg',
    )
    _caption(
        conn,
        studio["id"],
        gallery["id"],
        image["id"],
        'portrait"><script>window.altPwned=1</script>',
    )
    add_comment(
        conn,
        tenant_id=studio["id"],
        gallery_id=gallery["id"],
        image_id=image["id"],
        author_name='Pat"><img src=x onerror=alert(1)>',
        body="</textarea><script>window.notePwned=1</script>",
    )
    publish_gallery(conn, studio["id"], gallery["id"])
    conn.commit()

    page = client.get(f"/g/{studio['slug']}/{gallery['slug']}").text

    for executable in (
        "<script>window.filenamePwned=1</script>",
        "<script>window.altPwned=1</script>",
        "<script>window.notePwned=1</script>",
        "<img src=x onerror=alert(1)>",
    ):
        assert executable not in page
    assert "&lt;script&gt;window.altPwned=1&lt;/script&gt;" in page
    assert "&lt;script&gt;window.notePwned=1&lt;/script&gt;" in page
    assert "data-lightbox-src" not in page
    assert 'type="application/json"' not in page


def test_lightbox_assets_encode_keyboard_focus_swipe_and_small_screen_behavior(client):
    script = client.get("/static/client-gallery.js")
    css = client.get("/static/hestia.css")

    assert script.status_code == 200
    for behavior in (
        "showModal()",
        'event.key === "ArrowLeft"',
        'event.key === "ArrowRight"',
        "isTypingTarget",
        "pointerdown",
        "pointerup",
        "stage.setPointerCapture",
        "event.altKey",
        "event.defaultPrevented",
        "scrollIntoView",
        "preventScroll",
        "history.replaceState",
        "cloneNode(true)",
        'var currentOpener = item.querySelector("[data-lightbox-open]")',
    ):
        assert behavior in script.text
    assert "innerHTML" not in script.text
    assert "fetch(" not in script.text

    assert css.status_code == 200
    for rule in (
        ".gallery-lightbox",
        ".gallery-lightbox:not([open])",
        "100dvh",
        "100vh",
        "pinch-zoom",
        "min-height: 44px",
        ":focus-visible",
        "@media (max-width: 420px)",
    ):
        assert rule in css.text
