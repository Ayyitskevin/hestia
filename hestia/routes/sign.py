"""Public sign routes — the client-facing contract review + e-signature."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..contracts import get_contract_by_token, sign_contract
from ..ratelimit import enforce
from ..tenants import get_tenant
from .deps import db_conn, render

router = APIRouter()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


@router.get("/sign/{token}")
def sign_page(request: Request, token: str):
    with db_conn(request) as conn:
        contract = get_contract_by_token(conn, token)
        # A draft isn't public yet; a void contract is gone.
        if not contract or contract["status"] in ("draft", "void"):
            return render(request, "offer_missing.html", auth=None, status_code=404)
        tenant = get_tenant(conn, contract["tenant_id"])
    return render(request, "contracts/sign.html", auth=None, contract=contract, tenant=tenant)


@router.post("/sign/{token}")
def sign_submit(request: Request, token: str, signature_name: str = Form(""),
                agree: str = Form("")):
    enforce(request, "checkout")
    with db_conn(request) as conn:
        contract = get_contract_by_token(conn, token)
        if not contract or contract["status"] in ("draft", "void"):
            return render(request, "offer_missing.html", auth=None, status_code=404)
        # Already signed (or a double submit) → idempotent: just show the result.
        if contract["status"] == "signed":
            return RedirectResponse(f"/sign/{token}", status_code=303)
        if not signature_name.strip() or not agree:
            tenant = get_tenant(conn, contract["tenant_id"])
            return render(request, "contracts/sign.html", auth=None, contract=contract,
                          tenant=tenant, error="Type your full name and check the box to sign.",
                          status_code=400)
        sign_contract(conn, token=token, signature_name=signature_name,
                      signer_ip=_client_ip(request))
    return RedirectResponse(f"/sign/{token}", status_code=303)
