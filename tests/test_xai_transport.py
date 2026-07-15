"""Characterize the xAI HTTP boundary before consolidating its transport plumbing."""

from __future__ import annotations

import base64
import dataclasses
import io

import httpx
import pytest

from hestia.albums import XaiArranger
from hestia.content import MockContent, XaiContent
from hestia.products import PRESETS, XaiRenderer
from hestia.vision import VisionError, XaiVisionProvider
from hestia.xai import XaiTransport


class _Response:
    def __init__(self, payload: dict, error: Exception | None = None, status_code: int = 200):
        self.payload = payload
        self.error = error
        self.status_code = status_code
        self.status_checked = False

    def raise_for_status(self) -> None:
        self.status_checked = True
        if self.error:
            raise self.error

    def json(self) -> dict:
        return self.payload


def _capture_client(
    monkeypatch,
    payload: dict,
    error: Exception | None = None,
    status_code: int = 200,
):
    calls: list[tuple] = []
    response = _Response(payload, error, status_code)

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

    monkeypatch.setattr(httpx, "Client", Client)
    return calls, response


def _live(settings):
    return dataclasses.replace(
        settings,
        xai_api_key="xai-test-secret",
        xai_base_url="https://xai.example.test/v1",
        xai_model="grok-test",
    )


def _assert_request(calls, response, *, path: str, timeout: int) -> dict:
    assert calls[0] == ("init", {"base_url": "https://xai.example.test/v1", "timeout": timeout})
    _, actual_path, kwargs = calls[1]
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
    kwargs = _assert_request(calls, response, path="/chat/completions", timeout=60)
    assert kwargs["json"]["model"] == "grok-test"
    image = kwargs["json"]["messages"][0]["content"][1]["image_url"]["url"]
    assert image == f"data:image/jpeg;base64,{base64.b64encode(b'jpeg').decode()}"


class _Storage:
    def __init__(self):
        self.saved = None

    def open(self, key: str) -> bytes:
        assert key == "tenant/gallery/source.jpg"
        return b"source"

    def put(self, key: str, data: io.BytesIO, content_type: str) -> str:
        self.saved = (key, data.read(), content_type)
        return key


def test_image_edit_xai_contract(monkeypatch, settings):
    rendered = b"rendered-pixels"
    calls, response = _capture_client(monkeypatch, {
        "data": [{"b64_json": base64.b64encode(rendered).decode()}],
    })
    storage = _Storage()

    result = XaiRenderer(_live(settings)).render(
        image={"storage_key": "tenant/gallery/source.jpg", "filename": "source.jpg"},
        preset=PRESETS[0],
        storage=storage,
    )

    assert result["status"] == "rendered"
    kwargs = _assert_request(calls, response, path="/images/edits", timeout=120)
    assert kwargs["data"]["response_format"] == "b64_json"
    assert kwargs["files"]["image"] == ("source.jpg", b"source", "application/octet-stream")
    assert storage.saved == (
        "tenant/gallery/source.jpg.catalog_square.jpg",
        rendered,
        "image/jpg",
    )


@pytest.mark.parametrize(
    "encoded", [base64.b64encode(b"pixels").decode() + "%%%", ""]
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
