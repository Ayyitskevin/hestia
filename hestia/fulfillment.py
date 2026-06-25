"""Print-fulfillment seam — settle a paid order to a print lab.

Same pluggable shape as the payments/storage/email seams:

- ``mock`` — record the lab order and simulate acceptance (``submitted``). The
  default: the whole purchase→fulfillment flow is testable with no lab account.
- ``lab`` — submit to a real print lab (WHCC / Bay Photo class) over HTTP. Only
  active with real config; a submit failure is captured as the row's status,
  never raised into the worker (the job stays done, the operator sees ``failed``).

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


class LabFulfillment:
    backend = "lab"

    def __init__(self, settings: Settings):
        self.settings = settings

    def submit(self, order: dict) -> FulfillmentResult:
        s = self.settings
        if not s.fulfillment_api_key or not s.fulfillment_endpoint:
            return FulfillmentResult(ref="", status="failed", detail="lab backend not configured")
        try:
            return self._post(order)
        except Exception as exc:  # noqa: BLE001 - a lab miss is recorded, never raised
            return FulfillmentResult(ref="", status="failed", detail=f"lab error: {exc}")

    def _post(self, order: dict) -> FulfillmentResult:
        import json
        import urllib.request

        s = self.settings
        body = json.dumps({
            "external_id": order["id"], "sku": order["sku"],
            "name": order["name"], "amount_cents": order["amount_cents"],
        }).encode()
        req = urllib.request.Request(
            s.fulfillment_endpoint.rstrip("/") + "/orders", data=body,
            headers={"Authorization": f"Bearer {s.fulfillment_api_key}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - operator-set endpoint
            payload = json.loads(resp.read() or "{}")
        return FulfillmentResult(ref=str(payload.get("id", "")), status="submitted",
                                 detail="submitted to lab")


def build_fulfillment(settings: Settings):
    if settings.fulfillment_backend == "lab":
        return LabFulfillment(settings)
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
