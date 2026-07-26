"""Real Chromium acceptance for the public proofing gallery."""

from __future__ import annotations

import re
from io import BytesIO
from urllib.parse import urlsplit

from PIL import Image
from playwright.sync_api import Browser, BrowserContext, Locator, Page, Route, expect

_STUDIO_NAME = "Browser Studio"
_OWNER_EMAIL = "browser-owner@example.test"
_OWNER_PASSWORD = "browser-owner-password"
_GALLERY_TITLE = "Browser Proofs"
_NOTE = (
    "Use this frame in the album, keep the warm crop, and preserve enough edge room "
    "for a full-bleed spread without trimming the client details."
)


def _jpeg_payload(name: str, color: tuple[int, int, int]) -> dict:
    image = BytesIO()
    Image.new("RGB", (960, 640), color).save(image, format="JPEG", quality=88)
    return {"name": name, "mimeType": "image/jpeg", "buffer": image.getvalue()}


def _context(
    browser: Browser,
    base_url: str,
    blocked_external: list[str],
    *,
    width: int,
    height: int,
    mobile: bool = False,
) -> BrowserContext:
    origin = urlsplit(base_url)
    context = browser.new_context(
        viewport={"width": width, "height": height},
        screen={"width": width, "height": height},
        is_mobile=mobile,
        has_touch=mobile,
        device_scale_factor=1,
    )
    context.set_default_timeout(10_000)
    context.set_default_navigation_timeout(15_000)

    def guard(route: Route) -> None:
        target = urlsplit(route.request.url)
        same_origin = (
            target.scheme == origin.scheme
            and target.hostname == origin.hostname
            and target.port == origin.port
        )
        if same_origin or target.scheme in {"about", "blob", "data"}:
            route.continue_()
            return
        blocked_external.append(route.request.url)
        route.abort("blockedbyclient")

    context.route("**/*", guard)
    return context


def _instrument(page: Page, base_url: str, browser_issues: list[str]) -> None:
    page.on("pageerror", lambda error: browser_issues.append(f"page error: {error}"))
    page.on(
        "console",
        lambda message: browser_issues.append(f"console error: {message.text}")
        if message.type == "error"
        else None,
    )
    page.on(
        "response",
        lambda response: browser_issues.append(
            f"HTTP {response.status}: {response.url}"
        )
        if response.url.startswith(base_url) and response.status >= 400
        else None,
    )


def _assert_no_horizontal_overflow(page: Page) -> None:
    overflow = page.evaluate(
        """() => ({
            html: document.documentElement.scrollWidth - document.documentElement.clientWidth,
            body: document.body.scrollWidth - document.body.clientWidth,
        })"""
    )
    assert overflow["html"] <= 1, overflow
    assert overflow["body"] <= 1, overflow


def _assert_no_internal_horizontal_overflow(locator: Locator) -> None:
    dimensions = locator.evaluate(
        """element => ({
            clientWidth: element.clientWidth,
            scrollWidth: element.scrollWidth,
        })"""
    )
    assert dimensions["scrollWidth"] <= dimensions["clientWidth"] + 1, dimensions


def _bounding_box(locator: Locator) -> dict[str, float]:
    box = locator.bounding_box()
    assert box is not None
    return box


def _assert_within_viewport(page: Page, locator: Locator) -> None:
    box = _bounding_box(locator)
    viewport = page.viewport_size
    assert viewport is not None
    assert box["x"] >= -1, box
    assert box["y"] >= -1, box
    assert box["x"] + box["width"] <= viewport["width"] + 1, box
    assert box["y"] + box["height"] <= viewport["height"] + 1, box


def _assert_minimum_target(locator: Locator, size: int = 44) -> None:
    box = _bounding_box(locator)
    assert box["width"] >= size, box
    assert box["height"] >= size, box


def _assert_image_loaded(page: Page, image: Locator) -> None:
    image.scroll_into_view_if_needed()
    handle = image.element_handle()
    assert handle is not None
    page.wait_for_function(
        "(element) => element.complete && element.naturalWidth > 0",
        arg=handle,
    )


def _swipe_left(context: BrowserContext, page: Page) -> None:
    stage = page.locator(".lightbox-stage")
    box = stage.bounding_box()
    assert box is not None
    start_x = box["x"] + box["width"] * 0.78
    end_x = box["x"] + box["width"] * 0.22
    y = box["y"] + box["height"] * 0.5
    session = context.new_cdp_session(page)
    try:
        session.send(
            "Input.dispatchTouchEvent",
            {
                "type": "touchStart",
                "touchPoints": [
                    {
                        "x": start_x,
                        "y": y,
                        "radiusX": 1,
                        "radiusY": 1,
                        "force": 1,
                        "id": 1,
                    }
                ],
            },
        )
        session.send(
            "Input.dispatchTouchEvent",
            {
                "type": "touchMove",
                "touchPoints": [
                    {
                        "x": end_x,
                        "y": y,
                        "radiusX": 1,
                        "radiusY": 1,
                        "force": 1,
                        "id": 1,
                    }
                ],
            },
        )
        session.send(
            "Input.dispatchTouchEvent",
            {"type": "touchEnd", "touchPoints": []},
        )
    finally:
        session.detach()


def test_gallery_proofing_journey(browser: Browser, live_hestia: str) -> None:
    """Owner setup through client proofing stays usable at desktop and 320px."""
    contexts: list[BrowserContext] = []
    blocked_external: list[str] = []
    browser_issues: list[str] = []

    def make_context(*, width: int, height: int, mobile: bool = False) -> BrowserContext:
        context = _context(
            browser,
            live_hestia,
            blocked_external,
            width=width,
            height=height,
            mobile=mobile,
        )
        contexts.append(context)
        return context

    try:
        admin_context = make_context(width=1280, height=900)
        admin_page = admin_context.new_page()
        _instrument(admin_page, live_hestia, browser_issues)
        admin_page.goto(f"{live_hestia}/admin")
        admin_page.get_by_label("Admin token").fill("browser-admin-token")
        admin_page.get_by_role("button", name="Sign in", exact=True).click()
        expect(admin_page).to_have_url(re.compile(r"/admin/tenants$"))
        admin_page.get_by_role("link", name=re.compile("Onboard")).first.click()
        admin_page.get_by_label("Studio name").fill(_STUDIO_NAME)
        admin_page.get_by_label("Primary shoot type").select_option("wedding")
        admin_page.get_by_label("Owner email").fill(_OWNER_EMAIL)
        admin_page.get_by_label("Owner password").fill(_OWNER_PASSWORD)
        admin_page.get_by_role("button", name="Create studio", exact=True).click()
        expect(admin_page.get_by_role("heading", name=_STUDIO_NAME, exact=True)).to_be_visible()
        tenant_slug = admin_page.locator(".page-title .muted.mono").inner_text().strip()
        assert tenant_slug == "browser-studio"

        owner_context = make_context(width=1280, height=900)
        owner_page = owner_context.new_page()
        _instrument(owner_page, live_hestia, browser_issues)
        owner_page.goto(f"{live_hestia}/login")
        owner_page.get_by_label("Email").fill(_OWNER_EMAIL)
        owner_page.get_by_label("Password").fill(_OWNER_PASSWORD)
        owner_page.get_by_role("button", name="Sign in", exact=True).click()
        expect(owner_page).to_have_url(re.compile(r"/onboarding$"))
        owner_page.goto(f"{live_hestia}/galleries/new")
        owner_page.get_by_label("Gallery title").fill(_GALLERY_TITLE)
        owner_page.get_by_label("Client name").fill("Pat Client")
        owner_page.get_by_role("button", name="Create gallery", exact=True).click()
        gallery_match = re.fullmatch(r"/galleries/(\d+)", urlsplit(owner_page.url).path)
        assert gallery_match is not None, owner_page.url
        gallery_id = gallery_match.group(1)
        owner_detail_url = f"{live_hestia}/galleries/{gallery_id}"

        owner_page.locator('input[type="file"][name="files"]').set_input_files(
            [
                _jpeg_payload("first-frame.jpg", (50, 95, 145)),
                _jpeg_payload(
                    "second-frame-with-a-deliberately-long-client-proofing-name.jpg",
                    (175, 105, 65),
                ),
            ]
        )
        owner_page.get_by_role("button", name="Upload", exact=True).click()
        expect(owner_page).to_have_url(owner_detail_url)

        publish_prompts: list[str] = []

        def accept_publish(dialog) -> None:
            publish_prompts.append(dialog.message)
            if dialog.type == "confirm" and dialog.message.startswith("Publish this gallery"):
                dialog.accept()
            else:
                dialog.dismiss()

        owner_page.on("dialog", accept_publish)
        owner_page.get_by_role("button", name=re.compile(r"^Publish")).click()
        owner_page.remove_listener("dialog", accept_publish)
        expect(
            owner_page.locator("span.pill.on").filter(
                has_text=re.compile(r"^published$")
            )
        ).to_be_visible()
        assert not publish_prompts or publish_prompts == [
            "Publish this gallery permanently? Existing enabled Gallery published automations "
            "will be queued once."
        ]

        client_url = f"{live_hestia}/g/{tenant_slug}/browser-proofs"

        desktop_context = make_context(width=1280, height=900)
        desktop_page = desktop_context.new_page()
        _instrument(desktop_page, live_hestia, browser_issues)
        desktop_page.goto(client_url)
        desktop_items = desktop_page.locator("[data-gallery-item]")
        expect(desktop_items).to_have_count(2)
        for index in range(2):
            _assert_image_loaded(
                desktop_page,
                desktop_items.nth(index).locator(".proof-image-link img"),
            )
        _assert_no_horizontal_overflow(desktop_page)

        desktop_items.nth(0).locator("[data-lightbox-open]").click()
        desktop_dialog = desktop_page.locator("#gallery-lightbox")
        expect(desktop_dialog).to_be_visible()
        assert desktop_dialog.evaluate("(element) => element.open") is True
        expect(desktop_page.locator("[data-lightbox-position]")).to_have_text("1 of 2")
        desktop_close = desktop_page.locator("[data-lightbox-close]")
        desktop_previous = desktop_page.locator("[data-lightbox-prev]")
        desktop_next = desktop_page.locator("[data-lightbox-next]")
        desktop_layout = desktop_page.locator(".lightbox-layout")
        desktop_stage = desktop_page.locator(".lightbox-stage")
        desktop_proofing = desktop_page.locator(".lightbox-proofing")
        expect(desktop_close).to_be_focused()
        _assert_image_loaded(desktop_page, desktop_page.locator("[data-lightbox-image]"))
        _assert_within_viewport(desktop_page, desktop_dialog)
        _assert_no_internal_horizontal_overflow(desktop_dialog)
        _assert_no_internal_horizontal_overflow(desktop_layout)
        for control in (desktop_close, desktop_previous, desktop_next):
            _assert_minimum_target(control)
        desktop_stage_box = _bounding_box(desktop_stage)
        desktop_proofing_box = _bounding_box(desktop_proofing)
        assert (
            desktop_proofing_box["x"]
            >= desktop_stage_box["x"] + desktop_stage_box["width"] - 1
        )
        assert desktop_page.locator("[data-lightbox-image]").evaluate(
            "element => getComputedStyle(element).objectFit"
        ) == "contain"
        _assert_no_horizontal_overflow(desktop_page)

        desktop_page.keyboard.press("ArrowRight")
        desktop_position = desktop_page.locator("[data-lightbox-position]")
        expect(desktop_position).to_have_text("2 of 2")
        desktop_page.locator("#lightbox-comment").focus()
        desktop_page.keyboard.press("ArrowLeft")
        expect(desktop_position).to_have_text("2 of 2")
        desktop_close.focus()
        desktop_page.keyboard.press("ArrowLeft")
        expect(desktop_position).to_have_text("1 of 2")
        desktop_page.keyboard.press("ArrowRight")
        expect(desktop_position).to_have_text("2 of 2")
        desktop_page.keyboard.press("Escape")
        expect(desktop_dialog).not_to_be_visible()
        expect(desktop_items.nth(1).locator("[data-lightbox-open]")).to_be_focused()
        assert "lightbox=" not in desktop_page.url
        assert urlsplit(desktop_page.url).fragment.startswith("img-")
        _assert_no_horizontal_overflow(desktop_page)

        mobile_context = make_context(width=320, height=568, mobile=True)
        mobile_page = mobile_context.new_page()
        _instrument(mobile_page, live_hestia, browser_issues)
        mobile_page.goto(client_url)
        mobile_items = mobile_page.locator("[data-gallery-item]")
        expect(mobile_items).to_have_count(2)
        _assert_no_horizontal_overflow(mobile_page)
        mobile_items.nth(0).locator("[data-lightbox-open]").click()
        mobile_dialog = mobile_page.locator("#gallery-lightbox")
        mobile_layout = mobile_page.locator(".lightbox-layout")
        mobile_stage = mobile_page.locator(".lightbox-stage")
        mobile_proofing = mobile_page.locator(".lightbox-proofing")
        mobile_close = mobile_page.locator("[data-lightbox-close]")
        mobile_previous = mobile_page.locator("[data-lightbox-prev]")
        mobile_next = mobile_page.locator("[data-lightbox-next]")
        expect(mobile_dialog).to_be_visible()
        expect(mobile_page.locator("[data-lightbox-position]")).to_have_text("1 of 2")
        _assert_within_viewport(mobile_page, mobile_dialog)
        mobile_dialog_box = _bounding_box(mobile_dialog)
        assert abs(mobile_dialog_box["x"]) <= 1, mobile_dialog_box
        assert abs(mobile_dialog_box["y"]) <= 1, mobile_dialog_box
        assert abs(mobile_dialog_box["width"] - 320) <= 1, mobile_dialog_box
        assert abs(mobile_dialog_box["height"] - 568) <= 1, mobile_dialog_box
        _assert_no_internal_horizontal_overflow(mobile_dialog)
        _assert_no_internal_horizontal_overflow(mobile_layout)
        for control in (mobile_close, mobile_previous, mobile_next):
            _assert_minimum_target(control)
        mobile_stage_box = _bounding_box(mobile_stage)
        mobile_proofing_box = _bounding_box(mobile_proofing)
        assert (
            mobile_proofing_box["y"]
            >= mobile_stage_box["y"] + mobile_stage_box["height"] - 1
        )
        scroll_metrics = mobile_layout.evaluate(
            """element => ({
                overflowY: getComputedStyle(element).overflowY,
                clientHeight: element.clientHeight,
                scrollHeight: element.scrollHeight,
            })"""
        )
        assert scroll_metrics["overflowY"] in {"auto", "scroll"}, scroll_metrics
        assert scroll_metrics["scrollHeight"] > scroll_metrics["clientHeight"], scroll_metrics
        mobile_layout.evaluate("element => { element.scrollTop = element.scrollHeight; }")
        for reachable in (
            mobile_page.locator(".lightbox-header"),
            mobile_close,
            mobile_page.locator("#lightbox-comment"),
            mobile_page.get_by_role("button", name="Add note", exact=True),
            mobile_page.locator("[data-lightbox-review]"),
        ):
            _assert_within_viewport(mobile_page, reachable)
        _assert_no_internal_horizontal_overflow(mobile_layout)
        mobile_layout.evaluate("element => { element.scrollTop = 0; }")
        _swipe_left(mobile_context, mobile_page)
        expect(mobile_page.locator("[data-lightbox-position]")).to_have_text("2 of 2")
        _assert_no_internal_horizontal_overflow(mobile_dialog)
        _assert_no_internal_horizontal_overflow(mobile_layout)
        _assert_no_horizontal_overflow(mobile_page)

        mobile_page.locator("[data-lightbox-favorite]").click()
        expect(mobile_page.locator("#gallery-lightbox")).to_be_visible()
        expect(mobile_page.locator("[data-lightbox-position]")).to_have_text("2 of 2")
        favorite = mobile_page.locator("[data-lightbox-favorite]")
        expect(favorite).to_have_attribute("aria-pressed", "true")
        expect(mobile_page.locator("[data-lightbox-favorite-label]")).to_have_text(
            "Remove favorite"
        )
        expect(mobile_page.locator("[data-gallery-item]").nth(1)).to_have_class(
            re.compile(r"\bis-fav\b")
        )

        note_form = mobile_page.locator("form[data-lightbox-comment-form]")
        note_form.get_by_label("Your name").fill("Pat")
        note_form.get_by_label("Note about this photo").fill(_NOTE)
        note_form.get_by_role("button", name="Add note", exact=True).click()
        expect(mobile_page.locator("#gallery-lightbox")).to_be_visible()
        expect(mobile_page.locator("[data-lightbox-position]")).to_have_text("2 of 2")
        expect(mobile_page.locator("[data-lightbox-comments]")).to_contain_text(_NOTE)
        _assert_no_internal_horizontal_overflow(mobile_dialog)
        _assert_no_internal_horizontal_overflow(mobile_layout)
        _assert_no_horizontal_overflow(mobile_page)

        mobile_page.locator("[data-lightbox-review]").click()
        expect(mobile_page.locator("#gallery-lightbox")).not_to_be_visible()
        proofing_actions = mobile_page.locator("#proofing-actions")
        expect(proofing_actions).to_be_focused()
        _assert_no_horizontal_overflow(mobile_page)

        submit_button = proofing_actions.get_by_role(
            "button", name=re.compile(r"I'm done")
        )
        dismissed: list[tuple[str, str]] = []

        def dismiss_submit(dialog) -> None:
            dismissed.append((dialog.type, dialog.message))
            dialog.dismiss()

        mobile_page.once("dialog", dismiss_submit)
        submit_button.click()
        assert dismissed == [
            ("confirm", f"Send your 1 favorite to {_STUDIO_NAME}?")
        ]
        expect(submit_button).to_be_visible()

        accepted: list[tuple[str, str]] = []

        def accept_submit(dialog) -> None:
            accepted.append((dialog.type, dialog.message))
            dialog.accept()

        mobile_page.once("dialog", accept_submit)
        submit_button.click()
        expect(mobile_page).to_have_url(client_url)
        assert accepted == [
            ("confirm", f"Send your 1 favorite to {_STUDIO_NAME}?")
        ]
        expect(proofing_actions).to_contain_text(
            f"You've sent 1 favorite to {_STUDIO_NAME}"
        )
        expect(proofing_actions.locator('form[action$="/submit"]')).to_have_count(0)
        mobile_page.reload()
        expect(mobile_page).to_have_url(client_url)
        expect(proofing_actions).to_contain_text(
            f"You've sent 1 favorite to {_STUDIO_NAME}"
        )
        expect(proofing_actions.locator('form[action$="/submit"]')).to_have_count(0)

        owner_page.goto(owner_detail_url)
        proofing_heading = owner_page.get_by_role(
            "heading", name="Proofing activity", exact=True
        )
        expect(proofing_heading).to_be_visible()
        proofing_card = owner_page.locator(".card", has=proofing_heading)
        expected_kpis = {
            "Favorites": "1",
            "Notes": "1",
            "Status": "Selections submitted",
        }
        for label, value in expected_kpis.items():
            kpi = proofing_card.locator(".kpi-card").filter(
                has_text=re.compile(
                    rf"^{re.escape(label)}\s*{re.escape(value)}$"
                )
            )
            expect(kpi).to_have_count(1)
            expect(kpi.locator(".kpi-label")).to_have_text(label)
            expect(kpi.locator(".kpi-value")).to_have_text(value)
        expect(proofing_card).to_contain_text(_NOTE)
        expect(proofing_card).to_contain_text("Pat")

        assert blocked_external == [], blocked_external
        assert browser_issues == [], browser_issues
    finally:
        for context in reversed(contexts):
            context.close()
