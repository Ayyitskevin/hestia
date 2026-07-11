"""Print-fulfillment seam — settle a paid order to a print lab.

Same pluggable shape as the payments/storage/email seams:

- ``mock`` — record the lab order and simulate acceptance (``submitted``). The
  default: the whole purchase→fulfillment flow is testable with no lab account.
- ``lab`` — submit a structured print order to a real print lab (WHCC / Bay Photo
  class) over HTTP. Only active with real config; a submit failure is captured as
  the row's status, never raised into the worker (the job stays done, the
  operator sees ``failed``).

The lab provider builds a real print-order payload (line items, currency, image
refs, partner id) and maps the lab's response to ``submitted``/``failed``
including idempotent re-submit handling (HTTP 409 = already accepted). The HTTP
transport is an injectable seam (``client=``) so the payload and response
handling are fully testable with no lab account; production uses ``httpx``.
Pointing at a specific lab is configuring ``fulfillment_endpoint`` +
``fulfillment_api_key`` and adapting the request/response shape to that lab's
API — the seam makes that a small adapter, not a rewrite.

Submission runs on the durable job queue (``fulfillment.submit``), enqueued when
an order's invoice is paid, so a slow/failing lab never blocks the pay request.
Every attempt is recorded in ``fulfillment_orders``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import Settings
from .crypto import new_session_token
from .db import audit, get_db
from .invoices import money
from .jobs import register


@dataclass
class FulfillmentResult:
    ref: str
    status: str          # submitted | failed
    detail: str = ""


class MockFulfillment:
    backend = "mock"

    def submit(self, order: dict) -> FulfillmentResult:
        return FulfillmentResult(ref=f"mock_{order['id']}_{new_session_token()[:8]}",
                                 status="submitted", detail="simulated lab acceptance")


def lab_order_payload(order: dict, settings: Settings) -> dict:
    """Build the structured print order a lab receives. One line item per order
    bundle (the order row carries the resolved bundle sku/name/price); image
    refs let the lab pull the gallery's frames, and ``shipping`` is the address
    captured at checkout (None until that product step ships)."""
    return {
        "external_id": str(order["id"]),
        "partner_id": settings.fulfillment_api_key,
        "currency": order.get("currency") or settings.currency,
        "line_items": [
            {
                "sku": order.get("sku", ""),
                "name": order.get("name", ""),
                "quantity": int(order.get("quantity") or 1),
                "unit_amount_cents": int(order.get("amount_cents", 0)),
            }
        ],
        "image_refs": {
            "gallery_id": order.get("gallery_id"),
            "offer_id": order.get("offer_id"),
        },
        "shipping": order.get("shipping"),
        "notes": order.get("notes"),
    }


def _safe_json(resp) -> dict:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - a non-JSON lab response is "no body"
        return {}
    return body if isinstance(body, dict) else {}


def _body_text(resp) -> str:
    try:
        return resp.text or ""
    except Exception:  # noqa: BLE001
        return ""


# Lab-reported statuses that mean the order did NOT enter the queue.
_LAB_FAILED_STATUSES = frozenset({"failed", "rejected", "error", "cancelled", "canceled"})


def lab_result_from_response(resp, payload: dict) -> FulfillmentResult:
    """Map a lab HTTP response to a FulfillmentResult. Idempotent: a 409 (already
    accepted) is treated as submitted with the lab's existing id, so a re-submit
    after a crash never creates a duplicate physical order."""
    status_code = getattr(resp, "status_code", 0) or 0
    body = _safe_json(resp)
    external_id = payload["external_id"]
    if status_code == 409:
        ref = str(body.get("id") or body.get("order_id") or external_id)
        return FulfillmentResult(ref=ref, status="submitted",
                                 detail="lab: already accepted (idempotent re-submit)")
    if not 200 <= status_code < 300:
        snippet = (body.get("error") or body.get("message") or _body_text(resp)).strip()[:200]
        return FulfillmentResult(ref="", status="failed",
                                 detail=f"lab HTTP {status_code}: {snippet}" if snippet
                                 else f"lab HTTP {status_code}")
    ref = str(body.get("id") or body.get("order_id") or external_id)
    lab_status = str(body.get("status") or "submitted").lower()
    if lab_status in _LAB_FAILED_STATUSES:
        return FulfillmentResult(ref=ref, status="failed", detail=f"lab status: {lab_status}")
    return FulfillmentResult(ref=ref, status="submitted", detail="submitted to lab")


class LabFulfillment:
    backend = "lab"

    def __init__(self, settings: Settings, *, client=None):
        self.settings = settings
        # Injectable transport for tests: a callable (method, url, json, headers)
        # -> response, or an httpx.Client-like object. None → real httpx at runtime.
        self._client = client

    def submit(self, order: dict) -> FulfillmentResult:
        s = self.settings
        if not s.fulfillment_api_key or not s.fulfillment_endpoint:
            return FulfillmentResult(ref="", status="failed", detail="lab backend not configured")
        payload = lab_order_payload(order, s)
        try:
            return self._post(payload)
        except Exception as exc:  # noqa: BLE001 - a lab miss is recorded, never raised
            return FulfillmentResult(ref="", status="failed", detail=f"lab error: {exc}")

    def _post(self, payload: dict) -> FulfillmentResult:
        s = self.settings
        url = s.fulfillment_endpoint.rstrip("/") + "/orders"
        headers = {
            "Authorization": f"Bearer {s.fulfillment_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._client is not None:
            resp = self._client("POST", url, json=payload, headers=headers)
        else:
            import httpx

            with httpx.Client(timeout=30) as c:  # noqa: S310 - operator-set lab endpoint
                resp = c.post(url, json=payload, headers=headers)
        return lab_result_from_response(resp, payload)


def build_fulfillment(settings: Settings, *, client=None):
    if settings.fulfillment_backend == "lab":
        return LabFulfillment(settings, client=client)
    return MockFulfillment()


@register("fulfillment.submit")
def _submit_fulfillment(settings: Settings, payload: dict) -> None:
    """Job handler: submit one paid order to the fulfillment seam and record it.

    The queue is at-least-once and a real-lab submit is irreversible, so we DURABLY
    PRE-CLAIM the order before submitting: a committed ``pending`` row on the
    UNIQUE ``order_id`` latch. If the pre-claim INSERT loses the race (or a prior
    attempt already claimed it — including a crash after the lab POST but before
    the status update), this attempt stops without re-submitting. No duplicate
    physical order, ever; a crashed-mid-submit order is simply left ``pending``
    for the operator to see.
    """
    order_id = int(payload["order_id"])
    with get_db(settings.db_path) as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return
        order = dict(row)
        try:
            conn.execute(
                "INSERT INTO fulfillment_orders (tenant_id, order_id, backend, status, detail) "
                "VALUES (?, ?, ?, 'pending', 'claimed')",
                (order["tenant_id"], order_id, settings.fulfillment_backend),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return  # already claimed (UNIQUE order_id) — never submit twice
        provider = build_fulfillment(settings)
        result = provider.submit(order)
        conn.execute(
            "UPDATE fulfillment_orders SET backend = ?, provider_ref = ?, status = ?, "
            "detail = ?, updated_at = datetime('now') WHERE order_id = ?",
            (provider.backend, result.ref, result.status, result.detail, order_id),
        )
        audit(conn, actor=f"fulfillment:{provider.backend}", action="fulfillment.submitted",
              tenant_id=order["tenant_id"],
              detail=f"{order['name']} · {money(order['amount_cents'], order['currency'])} · {result.status}")


def list_fulfillments(conn, tenant_id: str, *, order_ids: list[int] | None = None) -> dict[int, dict]:
    """Latest fulfillment row per order (for the owner view), keyed by order_id."""
    rows = conn.execute(
        "SELECT * FROM fulfillment_orders WHERE tenant_id = ? ORDER BY id", (tenant_id,)
    ).fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        if order_ids is None or r["order_id"] in order_ids:
            out[r["order_id"]] = dict(r)  # later row wins → latest attempt
    return out
