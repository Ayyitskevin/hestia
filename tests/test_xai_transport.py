"""Characterize the xAI HTTP boundary before consolidating its transport plumbing."""

from __future__ import annotations

import base64
import dataclasses
import io
import json

import httpx
import pytest
from PIL import Image

from hestia.albums import XaiArranger
from hestia.config import Settings
from hestia.content import MockContent, XaiContent
from hestia.products import PRESETS, XaiRenderer
from hestia.vision import (
    MAX_VISION_RESPONSE_BYTES,
    VisionError,
    VisionProviderError,
    XaiVisionProvider,
)
from hestia.xai import XaiTransport


class _Response:
    def __init__(
        self,
        payload: dict,
        error: Exception | None = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ):
        self.payload = payload
        self.error = error
        self.status_code = status_code
        self.status_checked = False
        self.headers = httpx.Headers(headers or {})
        self.request = httpx.Request("POST", "https://xai.example.test/v1/test")
        self.extensions = {}

    def raise_for_status(self) -> None:
        self.status_checked = True
        if self.error:
            raise self.error

    def json(self) -> dict:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def iter_bytes(self):
        body = json.dumps(self.payload).encode()
        midpoint = max(1, len(body) // 2)
        yield body[:midpoint]
        yield body[midpoint:]


def _capture_client(
    monkeypatch,
    payload: dict,
    error: Exception | None = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
):
    calls: list[tuple] = []
    response = _Response(payload, error, status_code, headers)

    class Client:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, path: str, **kwargs):
            calls.append(("post", path, kwargs))
            return response

        def stream(self, method: str, path: str, **kwargs):
            assert method == "POST"
            calls.append(("stream", path, kwargs))
            return response

    monkeypatch.setattr(httpx, "Client", Client)
    return calls, response


def _live(settings):
    return dataclasses.replace(
        settings,
        xai_api_key="xai-test-secret",
        xai_base_url="https://xai.example.test/v1",
        xai_model="grok-test",
        xai_image_model="grok-image-test",
    )


def _assert_request(calls, response, *, path: str, timeout: int, method: str = "post") -> dict:
    assert calls[0] == ("init", {"base_url": "https://xai.example.test/v1", "timeout": timeout})
    actual_method, actual_path, kwargs = calls[1]
    assert actual_method == method
    assert actual_path == path
    assert kwargs["headers"] == {"Authorization": "Bearer xai-test-secret"}
    assert response.status_checked is True
    return kwargs


def test_album_xai_contract(monkeypatch, settings):
    calls, response = _capture_client(monkeypatch, {
        "choices": [{"message": {"content": "Here is the order: [2, 1]"}}],
    })

    order = XaiArranger(_live(settings)).propose([
        {"id": 1, "shot_type": "wide", "hero_potential": 0.4},
        {"id": 2, "shot_type": "portrait", "hero_potential": 0.9},
    ])

    assert order == [2, 1]
    kwargs = _assert_request(calls, response, path="/chat/completions", timeout=60)
    assert kwargs["json"]["model"] == "grok-test"
    assert kwargs["json"]["temperature"] == 0.3


def test_content_xai_contract(monkeypatch, settings):
    calls, response = _capture_client(monkeypatch, {
        "choices": [{"message": {"content": (
            '{"headline":"Launch","strategy":"Lead","shot_list":["Hero"],'
            '"captions":["Caption"]}'
        )}}],
    })

    body = XaiContent(_live(settings)).generate(
        project={"name": "Bistro", "shoot_type": "food"},
        recipe="menu-launch",
        keywords=["steam"],
    )

    assert body == {
        "headline": "Launch",
        "strategy": "Lead",
        "shot_list": ["Hero"],
        "captions": ["Caption"],
    }
    kwargs = _assert_request(calls, response, path="/chat/completions", timeout=60)
    assert kwargs["json"]["model"] == "grok-test"
    assert kwargs["json"]["temperature"] == 0.7


def test_content_xai_malformed_fields_use_per_field_fallback(monkeypatch, settings):
    _capture_client(monkeypatch, {
        "choices": [{"message": {"content": (
            '{"headline":7,"strategy":"Lead","shot_list":"Hero portrait",'
            '"captions":["Caption",null]}'
        )}}],
    })
    project = {"name": "Bistro", "shoot_type": "food"}
    fallback = MockContent().generate(
        project=project,
        recipe="menu-launch",
        keywords=["steam"],
    )

    body = XaiContent(_live(settings)).generate(
        project=project,
        recipe="menu-launch",
        keywords=["steam"],
    )

    assert body == {
        "headline": fallback["headline"],
        "strategy": "Lead",
        "shot_list": fallback["shot_list"],
        "captions": fallback["captions"],
    }


def test_vision_xai_contract(monkeypatch, settings):
    calls, response = _capture_client(monkeypatch, {
        "choices": [{"message": {"content": (
            '{"keywords":["couple"],"shot_type":"portrait","keeper_score":0.8,'
            '"hero_potential":0.9,"alt_text":"A couple.","eyes_closed":0.1,'
            '"exposure":0.5,"sharpness":0.8}'
        )}}],
    })

    result = XaiVisionProvider(_live(settings)).analyze(filename="frame.jpg", data=b"jpeg")

    assert result.keywords == ["couple"]
    assert result.keeper_score == 0.8
    kwargs = _assert_request(
        calls,
        response,
        path="/chat/completions",
        timeout=60,
        method="stream",
    )
    assert kwargs["json"]["model"] == "grok-test"
    image = kwargs["json"]["messages"][0]["content"][1]["image_url"]["url"]
    assert image == f"data:image/jpeg;base64,{base64.b64encode(b'jpeg').decode()}"


def test_vision_xai_normalizes_and_bounds_malformed_fields(monkeypatch, settings, conn):
    _capture_client(monkeypatch, {
        "choices": [{"message": {"content": json.dumps({
            "keywords": [
                "  GOLDEN   HOUR  ",
                "golden hour",
                7,
                "",
                "x" * 100,
                " Detail ",
                "CANDID",
                "extra",
            ],
            "shot_type": "not-a-shot",
            "alt_text": "  A   product \ud800 " + (" description " * 100),
            "keeper_score": True,
            "hero_potential": "1.5",
            "eyes_closed": "-0.2",
            "exposure": "NaN",
            "sharpness": "Infinity",
        })}}],
    })

    result = XaiVisionProvider(_live(settings)).analyze(filename="frame.jpg", data=b"jpeg")

    assert result.keywords == [
        "golden hour",
        "x" * 64,
        "detail",
        "candid",
        "extra",
    ]
    assert result.shot_type == "candid"
    assert len(result.alt_text) == 500
    assert "  " not in result.alt_text
    assert "\ud800" not in result.alt_text
    assert "\ufffd" in result.alt_text
    assert conn.execute("SELECT ?", (result.alt_text,)).fetchone()[0] == result.alt_text
    assert result.keeper_score == 0.0
    assert result.hero_potential == 1.0
    assert result.eyes_closed == 0.0
    assert result.exposure == 0.5
    assert result.sharpness == 0.5


@pytest.mark.parametrize("content", ["[]", "null", '"text"'])
def test_vision_xai_non_object_json_is_typed_provider_failure(
    monkeypatch, settings, content
):
    _capture_client(monkeypatch, {
        "choices": [{"message": {"content": content}}],
    })

    with pytest.raises(VisionProviderError, match="JSON object"):
        XaiVisionProvider(_live(settings)).analyze(filename="frame.jpg", data=b"jpeg")


@pytest.mark.parametrize(
    "headers, content",
    [
        ({"Content-Length": str(MAX_VISION_RESPONSE_BYTES + 1)}, "{}"),
        ({}, "x" * MAX_VISION_RESPONSE_BYTES),
    ],
)
def test_vision_xai_bounds_response_before_json_parsing(
    monkeypatch, settings, headers, content
):
    _capture_client(
        monkeypatch,
        {"choices": [{"message": {"content": content}}]},
        headers=headers,
    )

    with pytest.raises(VisionProviderError, match="transport size limit"):
        XaiVisionProvider(_live(settings)).analyze(filename="frame.jpg", data=b"jpeg")


def _image_bytes(
    image_format: str = "JPEG",
    size: tuple[int, int] = (4, 3),
    *,
    transparent: bool = False,
) -> bytes:
    out = io.BytesIO()
    mode = "RGBA" if transparent else "RGB"
    color = (255, 255, 255, 0) if transparent else "white"
    Image.new(mode, size, color).save(out, format=image_format)
    return out.getvalue()


class _Storage:
    def __init__(self, source: bytes | None = None):
        self.source = source if source is not None else _image_bytes()
        self.saved = None
        self.opened = 0

    def open(self, key: str) -> bytes:
        assert key == "tenant/gallery/source.jpg"
        self.opened += 1
        return self.source

    def put(self, key: str, data: io.BytesIO, content_type: str) -> str:
        self.saved = (key, data.read(), content_type)
        return key


def test_image_edit_xai_contract(monkeypatch, settings):
    source = _image_bytes(size=(8, 6))
    rendered = _image_bytes(size=(1024, 768))
    calls, response = _capture_client(monkeypatch, {
        "data": [{"b64_json": base64.b64encode(rendered).decode()}],
    })
    storage = _Storage(source)
    preset = PRESETS[0]

    result = XaiRenderer(_live(settings)).render(
        image={"storage_key": "tenant/gallery/source.jpg", "filename": "source.jpg"},
        preset=preset,
        storage=storage,
    )

    assert result["status"] == "rendered"
    kwargs = _assert_request(
        calls, response, path="/images/edits", timeout=120, method="stream"
    )
    assert "data" not in kwargs
    assert "files" not in kwargs
    assert kwargs["json"]["model"] == "grok-image-test"
    assert kwargs["json"]["resolution"] == "2k"
    assert kwargs["json"]["response_format"] == "b64_json"
    assert kwargs["json"]["image"] == {
        "type": "image_url",
        "url": f"data:image/jpeg;base64,{base64.b64encode(source).decode()}",
    }
    key, saved, content_type = storage.saved
    assert key == "tenant/gallery/source.jpg.catalog_square.jpg"
    assert content_type == "image/jpeg"
    with Image.open(io.BytesIO(saved)) as image:
        assert image.format == "JPEG"
        assert image.size == (2000, 2000)


@pytest.mark.parametrize(
    "encoded", [
        base64.b64encode(b"pixels").decode() + "%%%",
        base64.b64encode(b"not an image").decode(),
        "",
    ]
)
def test_image_edit_xai_invalid_pixels_keep_planned_variant(monkeypatch, settings, encoded):
    _capture_client(monkeypatch, {
        "data": [{"b64_json": encoded}],
    })
    storage = _Storage()

    result = XaiRenderer(_live(settings)).render(
        image={"storage_key": "tenant/gallery/source.jpg", "filename": "source.jpg"},
        preset=PRESETS[0],
        storage=storage,
    )

    assert result["status"] == "planned"
    assert result["output_ref"] == "tenant/gallery/source.jpg"
    assert "xai render failed" in result["note"]
    assert storage.saved is None


def test_image_edit_xai_supported_output_format_is_canonicalized(monkeypatch, settings):
    rendered = _image_bytes("PNG", size=(16, 9), transparent=True)
    _capture_client(monkeypatch, {
        "data": [{"b64_json": base64.b64encode(rendered).decode()}],
    })
    storage = _Storage()

    preset = {**PRESETS[0], "width": 12, "height": 12}
    result = XaiRenderer(_live(settings)).render(
        image={"storage_key": "tenant/gallery/source.jpg", "filename": "source.jpg"},
        preset=preset,
        storage=storage,
    )

    assert result["status"] == "rendered"
    _, saved, content_type = storage.saved
    assert content_type == "image/jpeg"
    with Image.open(io.BytesIO(saved)) as image:
        assert image.format == "JPEG"
        assert image.size == (12, 12)


@pytest.mark.parametrize(("transparent", "expected_status"), [(True, "rendered"), (False, "planned")])
def test_image_edit_xai_transparent_preset_requires_real_alpha(
    monkeypatch, settings, transparent, expected_status
):
    rendered = _image_bytes("PNG", transparent=transparent)
    _capture_client(monkeypatch, {
        "data": [{"b64_json": base64.b64encode(rendered).decode()}],
    })
    storage = _Storage()
    preset = {
        **next(p for p in PRESETS if p["key"] == "transparent_cutout"),
        "width": 4,
        "height": 3,
    }

    result = XaiRenderer(_live(settings)).render(
        image={"storage_key": "tenant/gallery/source.jpg", "filename": "source.jpg"},
        preset=preset,
        storage=storage,
    )

    assert result["status"] == expected_status
    if transparent:
        key, saved, content_type = storage.saved
        assert key == "tenant/gallery/source.jpg.transparent_cutout.png"
        assert content_type == "image/png"
        with Image.open(io.BytesIO(saved)) as image:
            assert image.format == "PNG"
            assert image.size == (4, 3)
            assert image.convert("RGBA").getchannel("A").getextrema()[0] < 255
    else:
        assert storage.saved is None


def test_image_edit_xai_transparency_must_survive_final_crop(monkeypatch, settings):
    provider_image = Image.new("RGBA", (8, 4), (255, 255, 255, 255))
    for y in range(provider_image.height):
        provider_image.putpixel((0, y), (255, 255, 255, 0))
    out = io.BytesIO()
    provider_image.save(out, format="PNG")
    _capture_client(monkeypatch, {
        "data": [{"b64_json": base64.b64encode(out.getvalue()).decode()}],
    })
    storage = _Storage()
    preset = {
        **next(p for p in PRESETS if p["key"] == "transparent_cutout"),
        "width": 4,
        "height": 4,
    }

    result = XaiRenderer(_live(settings)).render(
        image={"storage_key": "tenant/gallery/source.jpg", "filename": "source.jpg"},
        preset=preset,
        storage=storage,
    )

    assert result["status"] == "planned"
    assert "no retained transparent pixels" in result["note"]
    assert storage.saved is None


def test_image_edit_xai_oversized_output_keeps_planned_variant(monkeypatch, settings):
    rendered = _image_bytes()
    _capture_client(monkeypatch, {
        "data": [{"b64_json": base64.b64encode(rendered).decode()}],
    })
    monkeypatch.setattr("hestia.products._MAX_RENDER_BYTES", len(rendered) - 1)
    storage = _Storage()

    result = XaiRenderer(_live(settings)).render(
        image={"storage_key": "tenant/gallery/source.jpg", "filename": "source.jpg"},
        preset=PRESETS[0],
        storage=storage,
    )

    assert result["status"] == "planned"
    assert storage.saved is None


def test_image_edit_xai_excessive_pixels_keep_planned_variant(monkeypatch, settings):
    rendered = _image_bytes(size=(4, 3))
    _capture_client(monkeypatch, {
        "data": [{"b64_json": base64.b64encode(rendered).decode()}],
    })
    monkeypatch.setattr("hestia.products._MAX_RENDER_PIXELS", 11)
    storage = _Storage()

    result = XaiRenderer(_live(settings)).render(
        image={"storage_key": "tenant/gallery/source.jpg", "filename": "source.jpg"},
        preset=PRESETS[0],
        storage=storage,
    )

    assert result["status"] == "planned"
    assert storage.saved is None


def test_image_edit_xai_provider_dimensions_are_cropped_and_resized(monkeypatch, settings):
    rendered = _image_bytes(size=(16, 9))
    _capture_client(monkeypatch, {
        "data": [{"b64_json": base64.b64encode(rendered).decode()}],
    })
    storage = _Storage()
    preset = {**PRESETS[0], "width": 5, "height": 4}

    result = XaiRenderer(_live(settings)).render(
        image={"storage_key": "tenant/gallery/source.jpg", "filename": "source.jpg"},
        preset=preset,
        storage=storage,
    )

    assert result["status"] == "rendered"
    _, saved, _ = storage.saved
    with Image.open(io.BytesIO(saved)) as image:
        assert image.size == (5, 4)


@pytest.mark.parametrize("preset", PRESETS, ids=lambda preset: preset["key"])
def test_image_edit_xai_normal_provider_raster_fulfills_every_real_preset(
    monkeypatch, settings, preset
):
    transparent = preset["background"] == "transparent"
    rendered = _image_bytes(
        "PNG" if transparent else "JPEG",
        size=(32, 24),
        transparent=transparent,
    )
    _capture_client(monkeypatch, {
        "data": [{"b64_json": base64.b64encode(rendered).decode()}],
    })
    storage = _Storage()

    result = XaiRenderer(_live(settings)).render(
        image={"storage_key": "tenant/gallery/source.jpg", "filename": "source.jpg"},
        preset=preset,
        storage=storage,
    )

    assert result["status"] == "rendered"
    key, saved, content_type = storage.saved
    expected_format = "PNG" if preset["format"] == "png" else "JPEG"
    assert key.endswith(f".{preset['key']}.{preset['format']}")
    assert content_type == ("image/png" if expected_format == "PNG" else "image/jpeg")
    with Image.open(io.BytesIO(saved)) as image:
        assert image.format == expected_format
        assert image.size == (preset["width"], preset["height"])
        if transparent:
            assert image.convert("RGBA").getchannel("A").getextrema()[0] < 255


def test_image_edit_xai_reuses_one_validated_source_for_a_preset_set(monkeypatch, settings):
    rendered = _image_bytes(size=(16, 12))
    _capture_client(monkeypatch, {
        "data": [{"b64_json": base64.b64encode(rendered).decode()}],
    })
    storage = _Storage()
    renderer = XaiRenderer(_live(settings))

    for preset in (
        {**PRESETS[0], "width": 8, "height": 8},
        {**PRESETS[1], "width": 8, "height": 8},
    ):
        assert renderer.render(
            image={"storage_key": "tenant/gallery/source.jpg", "filename": "source.jpg"},
            preset=preset,
            storage=storage,
        )["status"] == "rendered"

    assert storage.opened == 1


def test_image_edit_xai_oversized_source_metadata_never_reads_or_calls(
    monkeypatch, settings
):
    calls, _ = _capture_client(monkeypatch, {
        "data": [{"b64_json": base64.b64encode(_image_bytes()).decode()}],
    })
    monkeypatch.setattr("hestia.products._MAX_SOURCE_BYTES", 1)
    storage = _Storage()

    result = XaiRenderer(_live(settings)).render(
        image={
            "storage_key": "tenant/gallery/source.jpg",
            "filename": "source.jpg",
            "bytes": 2,
        },
        preset=PRESETS[0],
        storage=storage,
    )

    assert result["status"] == "planned"
    assert storage.opened == 0
    assert calls == []
    assert storage.saved is None


def test_image_edit_xai_invalid_source_never_calls_provider(monkeypatch, settings):
    calls, _ = _capture_client(monkeypatch, {
        "data": [{"b64_json": base64.b64encode(_image_bytes()).decode()}],
    })
    storage = _Storage(b"not an image")

    result = XaiRenderer(_live(settings)).render(
        image={"storage_key": "tenant/gallery/source.jpg", "filename": "source.jpg"},
        preset=PRESETS[0],
        storage=storage,
    )

    assert result["status"] == "planned"
    assert calls == []
    assert storage.saved is None


def test_image_model_setting_reads_environment(monkeypatch):
    monkeypatch.setenv("HESTIA_XAI_IMAGE_MODEL", "grok-image-env-test")

    assert Settings.from_env().xai_image_model == "grok-image-env-test"


def test_album_xai_transport_failure_keeps_gallery_order(monkeypatch, settings):
    _capture_client(monkeypatch, {}, RuntimeError("upstream unavailable"))
    images = [{"id": 1}, {"id": 2}]

    assert XaiArranger(_live(settings)).propose(images) == [1, 2]


def test_content_xai_transport_failure_keeps_template_fallback(monkeypatch, settings):
    _capture_client(monkeypatch, {}, RuntimeError("upstream unavailable"))

    body = XaiContent(_live(settings)).generate(
        project={"name": "Bistro", "shoot_type": "food"},
        recipe="menu-launch",
        keywords=["steam"],
    )

    assert body["headline"] == "Introducing the new menu at Bistro"
    assert body["shot_list"]


def test_image_edit_xai_transport_failure_keeps_planned_variant(monkeypatch, settings):
    _capture_client(monkeypatch, {}, RuntimeError("upstream unavailable"))

    result = XaiRenderer(_live(settings)).render(
        image={"storage_key": "tenant/gallery/source.jpg", "filename": "source.jpg"},
        preset=PRESETS[0],
        storage=_Storage(),
    )

    assert result["status"] == "planned"
    assert result["output_ref"] == "tenant/gallery/source.jpg"
    assert "upstream unavailable" in result["note"]


def test_vision_xai_transport_failure_remains_explicit(monkeypatch, settings):
    _capture_client(monkeypatch, {}, RuntimeError("upstream unavailable"))

    with pytest.raises(VisionError, match="xai vision failed: upstream unavailable"):
        XaiVisionProvider(_live(settings)).analyze(filename="frame.jpg", data=b"jpeg")


class _LogRecorder:
    def __init__(self):
        self.records = []

    def info(self, message: str, *, extra: dict) -> None:
        self.records.append(("info", message, extra))

    def warning(self, message: str, *, extra: dict) -> None:
        self.records.append(("warning", message, extra))


def test_transport_logs_metadata_only_on_success(monkeypatch, settings):
    _capture_client(monkeypatch, {"ok": True}, status_code=201)
    recorder = _LogRecorder()
    monkeypatch.setattr("hestia.xai.log", recorder)
    ticks = iter([10.0, 10.125])
    monkeypatch.setattr("hestia.xai.time.monotonic", lambda: next(ticks))

    XaiTransport(_live(settings)).post("/safe-operation", timeout=30, json={"secret": "payload"})

    assert recorder.records == [(
        "info",
        "xai request completed",
        {"action": "xai.request", "path": "/safe-operation", "status": 201,
         "duration_ms": 125},
    )]
    assert "xai-test-secret" not in repr(recorder.records)
    assert "payload" not in repr(recorder.records)


def test_transport_logs_metadata_only_on_failure(monkeypatch, settings):
    _capture_client(
        monkeypatch,
        {},
        RuntimeError("failure contains sensitive upstream detail"),
        status_code=503,
    )
    recorder = _LogRecorder()
    monkeypatch.setattr("hestia.xai.log", recorder)
    ticks = iter([20.0, 20.25])
    monkeypatch.setattr("hestia.xai.time.monotonic", lambda: next(ticks))

    with pytest.raises(RuntimeError, match="sensitive upstream detail"):
        XaiTransport(_live(settings)).post("/safe-operation", timeout=30)

    assert recorder.records == [(
        "warning",
        "xai request failed",
        {"action": "xai.request", "path": "/safe-operation", "status": 503,
         "duration_ms": 250},
    )]
    assert "sensitive upstream detail" not in repr(recorder.records)
    assert "xai-test-secret" not in repr(recorder.records)


@pytest.mark.parametrize(
    "headers,max_bytes",
    [
        ({"content-length": "999"}, 100),
        ({}, 20),
    ],
    ids=["content-length", "chunk-accounting"],
)
def test_transport_bounded_response_rejects_oversized_body(
    monkeypatch, settings, headers, max_bytes
):
    calls, response = _capture_client(
        monkeypatch,
        {"data": "x" * 200},
        headers=headers,
    )

    with pytest.raises(ValueError, match="transport size limit"):
        XaiTransport(_live(settings)).post(
            "/images/edits",
            timeout=120,
            max_response_bytes=max_bytes,
            json={"safe": True},
        )

    _assert_request(
        calls, response, path="/images/edits", timeout=120, method="stream"
    )


def test_transport_bounded_response_rebuilds_decoded_gzip_safely(monkeypatch, settings):
    payload = {"data": [{"b64_json": "safe"}]}
    calls, response = _capture_client(
        monkeypatch,
        payload,
        headers={
            "content-encoding": "gzip",
            "content-length": "64",
            "transfer-encoding": "chunked",
        },
    )

    result = XaiTransport(_live(settings)).post(
        "/images/edits",
        timeout=120,
        max_response_bytes=1_000,
        json={"safe": True},
    )

    assert result.json() == payload
    assert "content-encoding" not in result.headers
    assert "transfer-encoding" not in result.headers
    assert result.headers["content-length"] == str(len(json.dumps(payload).encode()))
    _assert_request(
        calls, response, path="/images/edits", timeout=120, method="stream"
    )
