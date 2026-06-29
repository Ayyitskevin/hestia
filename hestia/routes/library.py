"""Library — search the studio's whole catalog by what the AI sees in each frame.

The vision pass tags every analyzed image with keywords, a shot type and alt text. This
surfaces that understanding as a tenant-wide, content-based search across all galleries —
an AI-native capability a Lightroom-export-to-gallery workflow doesn't offer. Read-only.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..vision import search_images, tenant_keyword_facets, tenant_shot_type_facets
from .deps import db_conn, render, storage_of

router = APIRouter()


@router.get("/library")
def library(request: Request, q: str = "", shot: str = ""):
    query = (q or "").strip()
    shot_type = (shot or "").strip()
    with db_conn(request) as conn:
        auth = context_from_session(conn, request)
        if not auth or not auth.tenant:
            return RedirectResponse("/login", status_code=303)
        facets = tenant_keyword_facets(conn, auth.tenant["id"])
        shots = tenant_shot_type_facets(conn, auth.tenant["id"])
        results = (search_images(conn, auth.tenant["id"], keyword=query, shot_type=shot_type)
                   if (query or shot_type) else [])
    return render(request, "library.html", auth=auth, facets=facets, shots=shots,
                  results=results, q=query, shot=shot_type, storage=storage_of(request))
