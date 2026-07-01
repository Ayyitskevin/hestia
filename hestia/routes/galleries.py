"""Gallery routes — the native product surface: create, upload, process, offer."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from .. import messaging
from ..albums import get_album_for_gallery
from ..auth import context_from_session
from ..campaigns import (
    end_campaign,
    gallery_sales_opportunity,
    get_active_campaign,
    launch_gallery_sales_campaign,
)
from ..crm import assign_gallery_to_project, get_client, get_project, list_projects
from ..db import audit
from ..delivery import delivery_url, enable_delivery, regenerate_delivery_token, set_delivery_expiry
from ..email import notify
from ..fulfillment import list_fulfillments
from ..galleries import (
    add_image,
    apply_cull,
    apply_quality_cull,
    create_gallery,
    get_gallery,
    list_galleries,
    list_images,
    publish_gallery,
    set_cover_image,
    set_image_hidden,
)
from ..jobs import drain, enqueue
from ..orders import list_orders
from ..pipeline import start_run
from ..products import get_set_for_gallery
from ..proofing import comments_for_gallery, favorite_image_ids, list_favorites
from ..sales import get_offer_for_gallery, offer_public_url
from ..tenants import get_tenant, tenant_flags
from ..vision import cull_summary, gallery_analysis_map, hero_suggestions
from .deps import db_conn, render, settings_of, storage_of

router = APIRouter(prefix="/galleries")


def _require_user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


def _schedule(request: Request, background_tasks: BackgroundTasks) -> None:
    """Kick an inline drain so work starts now; the worker thread is the backstop."""
    settings = settings_of(request)
    background_tasks.add_task(drain, settings.db_path, settings)


@router.get("")
def gallery_list(request: Request):
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        galleries = list_galleries(conn, auth.tenant["id"])
    return render(request, "galleries.html", auth=auth, galleries=galleries,
                  storage=storage_of(request))


@router.get("/new")
def gallery_new(request: Request, project_id: int | None = None):
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        projects = list_projects(conn, auth.tenant["id"])
    return render(request, "gallery_new.html", auth=auth, projects=projects,
                  preselect_project=project_id)


@router.post("")
def gallery_create(
    request: Request,
    title: str = Form(...),
    client_name: str = Form(""),
    pin: str = Form(""),
    project_id: str = Form(""),
):
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        gallery = create_gallery(conn, tenant_id=auth.tenant["id"], title=title,
                                 client_name=client_name, pin=pin.strip() or None)
        if project_id.strip().isdigit():
            assign_gallery_to_project(conn, auth.tenant["id"], gallery["id"], int(project_id))
    return RedirectResponse(f"/galleries/{gallery['id']}", status_code=303)


@router.get("/{gallery_id}")
def gallery_detail(request: Request, gallery_id: int):
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        gallery = get_gallery(conn, auth.tenant["id"], gallery_id)
        if not gallery:
            return RedirectResponse("/galleries", status_code=303)
        images = list_images(conn, gallery_id)
        offer = get_offer_for_gallery(conn, auth.tenant["id"], gallery_id)
        run = conn.execute(
            "SELECT id, status FROM pipeline_runs WHERE tenant_id = ? AND source='gallery' AND source_id = ?",
            (auth.tenant["id"], str(gallery_id)),
        ).fetchone()
        flags = tenant_flags(get_tenant(conn, auth.tenant["id"]))
        project = get_project(conn, auth.tenant["id"], gallery["project_id"]) if gallery.get("project_id") else None
        album = get_album_for_gallery(conn, auth.tenant["id"], gallery_id)
        product_set = get_set_for_gallery(conn, auth.tenant["id"], gallery_id)
        favorites = favorite_image_ids(conn, gallery_id, tenant_id=auth.tenant["id"])
        comments = comments_for_gallery(conn, auth.tenant["id"], gallery_id)
        campaign = get_active_campaign(conn, gallery_id, tenant_id=auth.tenant["id"])
        sales_opportunity = gallery_sales_opportunity(conn, auth.tenant["id"], gallery_id)
        orders = list_orders(conn, auth.tenant["id"], gallery_id=gallery_id)
        fulfillments = list_fulfillments(conn, auth.tenant["id"],
                                         order_ids=[o["id"] for o in orders])
        cull = cull_summary(conn, auth.tenant["id"], gallery_id)
        analysis = gallery_analysis_map(conn, gallery_id)
        hero_ids = hero_suggestions(conn, auth.tenant["id"], gallery_id)
    culled_ids = cull.get("culled_ids") or set()
    # How many flagged frames are still visible (the "apply" button only matters if > 0),
    # and how many are currently hidden (so the owner can see/undo the cull state).
    cull_pending = sum(1 for im in images if im["id"] in culled_ids and not im["hidden"])
    # Frames flagged as a likely technical reject (soft/dark/bright) that are still visible.
    flagged_pending = sum(1 for im in images
                          if (analysis.get(im["id"]) or {}).get("flags") and not im["hidden"])
    hidden_count = sum(1 for im in images if im["hidden"])
    settings = settings_of(request)
    offer_url = offer_public_url(settings, auth.tenant["slug"], offer["token"]) if offer else None
    delivery_link = delivery_url(settings, gallery["delivery_token"]) if gallery.get("delivery_token") else None
    return render(request, "gallery_detail.html", auth=auth, gallery=gallery, images=images,
                  offer=offer, offer_url=offer_url, run=dict(run) if run else None,
                  storage=storage_of(request), flags=flags, project=project, album=album,
                  product_set=product_set, favorites=favorites, comments=comments, campaign=campaign,
                  sales_opportunity=sales_opportunity, orders=orders, fulfillments=fulfillments,
                  cull=cull, cull_pending=cull_pending, hidden_count=hidden_count, analysis=analysis, hero_ids=hero_ids,
                  flagged_pending=flagged_pending,
                  cover_id=gallery.get("cover_image_id"), delivery_link=delivery_link)


@router.get("/{gallery_id}/selects.txt")
def gallery_selects(request: Request, gallery_id: int):
    """Download the client's picks as a plain filename list — drop straight into
    Lightroom/your editor to pull the album & print selects. Owner-only, tenant-scoped."""
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        if not get_gallery(conn, auth.tenant["id"], gallery_id):
            return RedirectResponse("/galleries", status_code=303)
        favs = list_favorites(conn, auth.tenant["id"], gallery_id)
    body = "".join(f"{f['filename']}\n" for f in favs)
    return Response(content=body, media_type="text/plain",
                    headers={"Content-Disposition": f'attachment; filename="selects-{gallery_id}.txt"'})


@router.post("/{gallery_id}/delivery")
def gallery_delivery_enable(request: Request, gallery_id: int):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        gallery = get_gallery(conn, auth.tenant["id"], gallery_id)
        if not gallery:
            return RedirectResponse("/galleries", status_code=303)
        was_new = not gallery.get("delivery_token")
        token = enable_delivery(conn, auth.tenant["id"], gallery_id)
        if token:
            audit(conn, actor="owner", action="gallery.delivery_enabled",
                  tenant_id=auth.tenant["id"], detail=f"gallery #{gallery_id}")
            # On first enable, email the client their private download link (if on file).
            if was_new:
                project = get_project(conn, auth.tenant["id"], gallery["project_id"]) if gallery.get("project_id") else None
                client = get_client(conn, auth.tenant["id"], project["client_id"]) if project and project.get("client_id") else None
                if client and client.get("email"):
                    studio = auth.tenant.get("name", "your photographer")
                    ctx = {"client": client["name"], "studio": studio,
                           "download_url": delivery_url(settings, token)}
                    msg = messaging.render(conn, auth.tenant["id"], "gallery_ready", ctx)
                    notify(conn, settings, to=client["email"], tenant_id=auth.tenant["id"],
                           subject=msg["subject"], body=msg["body"])
    return RedirectResponse(f"/galleries/{gallery_id}", status_code=303)


@router.post("/{gallery_id}/delivery/regenerate")
def gallery_delivery_regenerate(request: Request, gallery_id: int):
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        token = regenerate_delivery_token(conn, auth.tenant["id"], gallery_id)
        if token:
            audit(conn, actor="owner", action="gallery.delivery_rotated",
                  tenant_id=auth.tenant["id"], detail=f"gallery #{gallery_id}")
    return RedirectResponse(f"/galleries/{gallery_id}", status_code=303)


@router.post("/{gallery_id}/delivery/expiry")
def gallery_delivery_expiry(request: Request, gallery_id: int, expires_at: str = Form("")):
    """Set or clear the download link's expiry date — clients can download through it."""
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_delivery_expiry(conn, auth.tenant["id"], gallery_id, expires_at)
        audit(conn, actor="owner", action="gallery.delivery_expiry_set",
              tenant_id=auth.tenant["id"], detail=f"gallery #{gallery_id} · {expires_at or 'cleared'}")
    return RedirectResponse(f"/galleries/{gallery_id}", status_code=303)


@router.post("/{gallery_id}/campaign")
def gallery_campaign_launch(request: Request, gallery_id: int, headline: str = Form(""),
                            discount_pct: str = Form("10"), days: str = Form("7")):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        gallery = get_gallery(conn, auth.tenant["id"], gallery_id)
        if not gallery:
            return RedirectResponse("/galleries", status_code=303)
        try:
            pct = int(discount_pct)
        except (ValueError, TypeError):
            pct = 0
        try:
            span = int(days)
        except (ValueError, TypeError):
            span = 7
        launch_gallery_sales_campaign(
            conn,
            settings,
            tenant=auth.tenant,
            gallery_id=gallery_id,
            headline=headline,
            discount_pct=pct,
            days=span,
            source="manual",
        )
        conn.commit()
    return RedirectResponse(f"/galleries/{gallery_id}", status_code=303)


@router.post("/{gallery_id}/campaign/end")
def gallery_campaign_end(request: Request, gallery_id: int):
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        if not get_gallery(conn, auth.tenant["id"], gallery_id):
            return RedirectResponse("/galleries", status_code=303)
        end_campaign(conn, auth.tenant["id"], gallery_id)
    return RedirectResponse(f"/galleries/{gallery_id}", status_code=303)


@router.post("/{gallery_id}/images")
async def gallery_upload(request: Request, gallery_id: int, files: list[UploadFile] = File(...)):
    storage = storage_of(request)
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        gallery = get_gallery(conn, auth.tenant["id"], gallery_id)
        if not gallery:
            return RedirectResponse("/galleries", status_code=303)
        for up in files:
            if not up.filename:
                continue
            add_image(conn, storage, tenant_id=auth.tenant["id"], gallery_id=gallery_id,
                      filename=up.filename, fileobj=up.file,
                      content_type=up.content_type or "application/octet-stream")
    return RedirectResponse(f"/galleries/{gallery_id}", status_code=303)


@router.post("/{gallery_id}/publish")
def gallery_publish(request: Request, gallery_id: int):
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        gallery = get_gallery(conn, auth.tenant["id"], gallery_id)
        if not gallery:
            return RedirectResponse("/galleries", status_code=303)
        publish_gallery(conn, auth.tenant["id"], gallery_id)
        audit(conn, actor="owner", action="gallery.published",
              tenant_id=auth.tenant["id"], detail=gallery["title"])
    return RedirectResponse(f"/galleries/{gallery_id}", status_code=303)


@router.post("/{gallery_id}/process")
def gallery_process(request: Request, gallery_id: int, background_tasks: BackgroundTasks):
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        gallery = get_gallery(conn, auth.tenant["id"], gallery_id)
        if not gallery:
            return RedirectResponse("/galleries", status_code=303)
        tenant = get_tenant(conn, auth.tenant["id"])
        run = start_run(conn, tenant=tenant, gallery_id=gallery_id)
        enqueue(conn, kind="pipeline.run", payload={"run_id": run["id"]}, tenant_id=tenant["id"])
    _schedule(request, background_tasks)
    return RedirectResponse(f"/pipeline/{run['id']}", status_code=303)


@router.post("/{gallery_id}/cull/apply")
def gallery_cull_apply(request: Request, gallery_id: int):
    """Apply the vision pass's cull suggestions: hide every flagged near-duplicate and
    likely blink in one click. Reversible (each frame can be restored) — nothing is deleted."""
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        if not get_gallery(conn, auth.tenant["id"], gallery_id):
            return RedirectResponse("/galleries", status_code=303)
        hidden = apply_cull(conn, auth.tenant["id"], gallery_id)
        if hidden:
            audit(conn, actor="owner", action="gallery.cull_applied",
                  tenant_id=auth.tenant["id"], detail=f"gallery #{gallery_id} · {hidden} hidden")
    return RedirectResponse(f"/galleries/{gallery_id}", status_code=303)


@router.post("/{gallery_id}/quality-cull/apply")
def gallery_quality_cull_apply(request: Request, gallery_id: int):
    """Hide every frame the vision pass flags as a likely technical reject (soft / under- or
    over-exposed) in one click. Reversible (each frame can be restored) — nothing is deleted."""
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        if not get_gallery(conn, auth.tenant["id"], gallery_id):
            return RedirectResponse("/galleries", status_code=303)
        hidden = apply_quality_cull(conn, auth.tenant["id"], gallery_id)
        if hidden:
            audit(conn, actor="owner", action="gallery.quality_cull_applied",
                  tenant_id=auth.tenant["id"], detail=f"gallery #{gallery_id} · {hidden} hidden")
    return RedirectResponse(f"/galleries/{gallery_id}", status_code=303)


@router.post("/{gallery_id}/images/{image_id}/hide")
def gallery_image_hide(request: Request, gallery_id: int, image_id: int):
    """Hide (cull) a single frame from the client gallery and delivery — reversible."""
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        if not get_gallery(conn, auth.tenant["id"], gallery_id):
            return RedirectResponse("/galleries", status_code=303)
        set_image_hidden(conn, auth.tenant["id"], image_id, True, gallery_id=gallery_id)
    return RedirectResponse(f"/galleries/{gallery_id}", status_code=303)


@router.post("/{gallery_id}/images/{image_id}/unhide")
def gallery_image_unhide(request: Request, gallery_id: int, image_id: int):
    """Restore a previously hidden frame back into the client gallery and delivery."""
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        if not get_gallery(conn, auth.tenant["id"], gallery_id):
            return RedirectResponse("/galleries", status_code=303)
        set_image_hidden(conn, auth.tenant["id"], image_id, False, gallery_id=gallery_id)
    return RedirectResponse(f"/galleries/{gallery_id}", status_code=303)


@router.post("/{gallery_id}/cover/{image_id}")
def gallery_set_cover(request: Request, gallery_id: int, image_id: int):
    """Set the gallery cover to a frame — typically the AI's top hero pick, one click."""
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        if not get_gallery(conn, auth.tenant["id"], gallery_id):
            return RedirectResponse("/galleries", status_code=303)
        set_cover_image(conn, auth.tenant["id"], gallery_id, image_id)
    return RedirectResponse(f"/galleries/{gallery_id}", status_code=303)
