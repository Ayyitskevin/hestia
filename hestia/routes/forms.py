"""Public questionnaire routes — the client-facing intake form."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..questionnaires import get_questionnaire_by_token, submit_questionnaire
from ..ratelimit import enforce
from ..tenants import get_tenant
from .deps import db_conn, render

router = APIRouter()


@router.get("/q/{token}")
def fill_page(request: Request, token: str):
    with db_conn(request) as conn:
        q = get_questionnaire_by_token(conn, token)
        # A draft isn't public yet; a void questionnaire is gone.
        if not q or q["status"] in ("draft", "void"):
            return render(request, "offer_missing.html", auth=None, status_code=404)
        tenant = get_tenant(conn, q["tenant_id"])
    return render(request, "questionnaires/fill.html", auth=None, q=q, tenant=tenant)


@router.post("/q/{token}")
async def fill_submit(request: Request, token: str):
    enforce(request, "checkout")
    form = await request.form()
    # Answers arrive as item_<id> fields; remap to {item_id: answer}.
    answers = {k[len("item_"):]: v for k, v in form.items()
               if k.startswith("item_") and isinstance(v, str)}
    with db_conn(request) as conn:
        q = get_questionnaire_by_token(conn, token)
        if not q or q["status"] in ("draft", "void"):
            return render(request, "offer_missing.html", auth=None, status_code=404)
        # Already completed (or a double submit) → idempotent: show the result.
        if q["status"] == "completed":
            return RedirectResponse(f"/q/{token}", status_code=303)
        submit_questionnaire(conn, token=token, answers=answers)
    return RedirectResponse(f"/q/{token}", status_code=303)
