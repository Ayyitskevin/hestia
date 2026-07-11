"""The real print-lab fulfillment seam — structured payload + response mapping.

Exercised with an injectable HTTP transport (no lab account needed). Proves the
payload a lab receives is well-shaped and that the lab's responses map correctly
to submitted/failed, including idempotent re-submit handling.
"""

import dataclasses

from hestia.fulfillment import (
    FulfillmentResult,
    LabFulfillment,
    build_fulfillment,
    lab_order_payload,
    lab_result_from_response,
)


class _FakeResp:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text

    def json(self):
        return self._body


def _fake_client(status_code, body=None, text="", *, raise_exc=None):
    """Returns a callable matching LabFulfillment's client seam."""
    def client(method, url, json, headers):
        if raise_exc is not None:
            raise raise_exc
        assert method == "POST"
        assert url.endswith("/orders")
        assert headers["Authorization"].startswith("Bearer ")
        assert headers["Content-Type"] == "application/json"
        # stash the last payload for assertions
        client.last_payload = json
        client.last_url = url
        return _FakeResp(status_code, body, text)
    client.last_payload = None
    return client


def _lab_settings(settings, **overrides):
    return dataclasses.replace(
        settings,
        fulfillment_backend="lab",
        fulfillment_endpoint="https://lab.example/api",
        fulfillment_api_key="lab-partner-42",
        **overrides,
    )


def _order(**overrides):
    base = {"id": 77, "tenant_id": "t1", "offer_id": 5, "gallery_id": 9,
            "invoice_id": 12, "sku": "print_set", "name": "Signature Print Set",
            "amount_cents": 9900, "currency": "usd"}
    base.update(overrides)
    return base


# ── payload shape ────────────────────────────────────────────────────────────


def test_lab_payload_has_line_items_refs_and_partner(settings):
    s = _lab_settings(settings)
    payload = lab_order_payload(_order(), s)
    assert payload["external_id"] == "77"
    assert payload["partner_id"] == "lab-partner-42"
    assert payload["currency"] == "usd"
    assert payload["line_items"] == [{
        "sku": "print_set", "name": "Signature Print Set",
        "quantity": 1, "unit_amount_cents": 9900,
    }]
    assert payload["image_refs"] == {"gallery_id": 9, "offer_id": 5}
    assert payload["shipping"] is None


def test_lab_payload_respects_quantity_and_shipping(settings):
    s = _lab_settings(settings)
    payload = lab_order_payload(_order(quantity=4, shipping={"name": "Jane", "line1": "1 Main"}), s)
    assert payload["line_items"][0]["quantity"] == 4
    assert payload["shipping"]["name"] == "Jane"


# ── response mapping ─────────────────────────────────────────────────────────


def test_submit_success_maps_to_submitted(settings):
    s = _lab_settings(settings)
    client = _fake_client(200, body={"id": "WHCC-1001", "status": "accepted"})
    res = LabFulfillment(s, client=client).submit(_order())
    assert res.status == "submitted"
    assert res.ref == "WHCC-1001"
    assert isinstance(res, FulfillmentResult)


def test_submit_409_is_idempotent_submitted(settings):
    s = _lab_settings(settings)
    client = _fake_client(409, body={"id": "WHCC-1001"})
    res = LabFulfillment(s, client=client).submit(_order())
    assert res.status == "submitted"
    assert res.ref == "WHCC-1001"
    assert "idempotent" in res.detail.lower()


def test_submit_500_maps_to_failed_no_raise(settings):
    s = _lab_settings(settings)
    client = _fake_client(500, body={"error": "lab down"}, text="boom")
    res = LabFulfillment(s, client=client).submit(_order())
    assert res.status == "failed"
    assert "HTTP 500" in res.detail
    assert res.ref == ""


def test_submit_rejected_status_maps_to_failed(settings):
    s = _lab_settings(settings)
    client = _fake_client(200, body={"id": "X1", "status": "rejected"})
    res = LabFulfillment(s, client=client).submit(_order())
    assert res.status == "failed"
    assert res.ref == "X1"
    assert "rejected" in res.detail


def test_submit_falls_back_to_external_id_when_lab_omits_id(settings):
    s = _lab_settings(settings)
    client = _fake_client(200, body={"status": "queued"})
    res = LabFulfillment(s, client=client).submit(_order())
    assert res.status == "submitted"
    assert res.ref == "77"  # external_id fallback


def test_not_configured_returns_failed(settings):
    s = dataclasses.replace(settings, fulfillment_backend="lab",
                            fulfillment_endpoint="", fulfillment_api_key="")
    res = LabFulfillment(s, client=_fake_client(200)).submit(_order())
    assert res.status == "failed"
    assert "not configured" in res.detail


def test_network_error_does_not_raise(settings):
    s = _lab_settings(settings)
    client = _fake_client(200, raise_exc=ConnectionError("dns failure"))
    res = LabFulfillment(s, client=client).submit(_order())
    assert res.status == "failed"
    assert "lab error" in res.detail


# ── build_fulfillment wiring ─────────────────────────────────────────────────


def test_build_fulfillment_lab_returns_lab_provider(settings):
    s = _lab_settings(settings)
    provider = build_fulfillment(s, client=_fake_client(200, body={"id": "L1"}))
    assert provider.backend == "lab"
    assert provider.submit(_order()).status == "submitted"


def test_build_fulfillment_mock_default(settings):
    assert build_fulfillment(settings).backend == "mock"


# ── lab_result_from_response unit (no provider) ──────────────────────────────


def test_result_unit_2xx_no_body():
    payload = {"external_id": "42"}
    res = lab_result_from_response(_FakeResp(201), payload)
    assert res.status == "submitted"
    assert res.ref == "42"
