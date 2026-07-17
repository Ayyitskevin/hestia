"""Pipeline — gallery → vision → offer, in one process.

The state machine (persisted, resumable, idempotent) survives from the
orchestration design, but the steps now call in-app modules
(:mod:`hestia.vision`, :mod:`hestia.sales`) instead of HTTP services. That is the
payoff of consolidating the suite: the "magic moment" is a function call, and the
idempotency the real Plutus lacked is guaranteed here.

Idempotency: runs are keyed by ``(tenant_id, gallery_id)``. Re-running reuses the
single offer/token for that gallery — never a duplicate client link.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .db import audit, connect
from .features import FeatureFlags, flags_for
from .galleries import get_gallery
from .jobs import register
from .sales import create_or_update_offer, offer_public_url
from .storage import build_storage
from .tenants import get_tenant
from .vision import MockVisionProvider, VisionError, VisionProviderError, analyze_gallery

STEP_DEFS = [
    {"name": "vision", "label": "Vision — understand every frame"},
    {"name": "offer", "label": "Offer — print & album collection"},
]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _initial_steps() -> list[dict]:
    return [
        {"name": d["name"], "label": d["label"], "status": "pending",
         "detail": "", "output": {}, "started_at": None, "finished_at": None}
        for d in STEP_DEFS
    ]


# ── Run persistence ─────────────────────────────────────────────────────────


def _row_to_run(row: sqlite3.Row) -> dict:
    run = dict(row)
    run["steps"] = json.loads(run.pop("steps_json") or "[]")
    return run


def load_run(conn: sqlite3.Connection, run_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)).fetchone()
    return _row_to_run(row) if row else None


def list_runs(conn: sqlite3.Connection, tenant_id: str, *, limit: int = 25) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM pipeline_runs WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?",
        (tenant_id, limit),
    ).fetchall()
    return [_row_to_run(r) for r in rows]


def _save_run(conn: sqlite3.Connection, run: dict) -> None:
    conn.execute(
        """
        UPDATE pipeline_runs SET status = ?, steps_json = ?, offer_url = ?, error = ?,
               updated_at = datetime('now')
         WHERE id = ?
        """,
        (run["status"], json.dumps(run["steps"]), run.get("offer_url"),
         run.get("error"), run["id"]),
    )
    conn.commit()


def start_run(conn: sqlite3.Connection, *, tenant: dict, gallery_id: int) -> dict:
    """Create or re-arm the run for a gallery (idempotent on (tenant, gallery))."""
    source_id = str(gallery_id)
    existing = conn.execute(
        "SELECT id FROM pipeline_runs WHERE tenant_id = ? AND source = 'gallery' AND source_id = ?",
        (tenant["id"], source_id),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE pipeline_runs SET status='queued', error=NULL, updated_at=datetime('now') WHERE id = ?",
            (existing["id"],),
        )
        conn.commit()
        return load_run(conn, existing["id"])
    cur = conn.execute(
        "INSERT INTO pipeline_runs (tenant_id, source, source_id, status, steps_json) "
        "VALUES (?, 'gallery', ?, 'queued', ?)",
        (tenant["id"], source_id, json.dumps(_initial_steps())),
    )
    audit(conn, actor=f"tenant:{tenant['slug']}", action="pipeline.start",
          tenant_id=tenant["id"], detail=f"gallery:{gallery_id}")
    conn.commit()
    return load_run(conn, cur.lastrowid)


def _find_step(run: dict, name: str) -> dict:
    for step in run["steps"]:
        if step["name"] == name:
            return step
    raise KeyError(name)


def _begin(step: dict) -> None:
    step["status"] = "running"
    step["started_at"] = step["started_at"] or _now()


def _finish(step: dict, *, status: str, detail: str = "", output: dict | None = None) -> None:
    step["status"] = status
    step["detail"] = detail
    if output is not None:
        step["output"] = output
    step["finished_at"] = _now()


# ── Executor ────────────────────────────────────────────────────────────────


def execute_run(
    db_path: str | Path,
    settings: Settings,
    run_id: int,
    *,
    storage=None,
    provider=None,
    flags: FeatureFlags | None = None,
) -> dict:
    """Run (or resume) the gallery→vision→offer pipeline to completion."""
    conn = connect(db_path)
    try:
        run = load_run(conn, run_id)
        if run is None:
            raise ValueError(f"run {run_id} not found")
        tenant = get_tenant(conn, run["tenant_id"])
        gallery = get_gallery(conn, run["tenant_id"], int(run["source_id"]))
        if not tenant or not gallery:
            return _fail(conn, run, "tenant or gallery missing")

        if flags is None:
            flags = flags_for(tenant.get("shoot_type"))
        if storage is None:
            storage = build_storage(settings)

        run["status"] = "running"
        run["error"] = None
        _save_run(conn, run)

        # 1. vision -----------------------------------------------------------
        vision = _find_step(run, "vision")
        summary = (vision.get("output") or {}).get("summary")
        retry_live_after_fallback = (
            isinstance(summary, dict) and summary.get("fallback_from") == "xai"
        )
        if vision["status"] != "done" or not summary or retry_live_after_fallback:
            _begin(vision)
            _save_run(conn, run)
            subsidy_note = None
            fallback_note = None
            try:
                from .ai_usage import resolve_vision_provider
                provider, subsidy_note = resolve_vision_provider(
                    conn, settings, tenant_id=tenant["id"], gallery_id=gallery["id"],
                    provider=provider,
                )
                summary = analyze_gallery(
                    conn, storage, settings, tenant_id=tenant["id"],
                    gallery_id=gallery["id"], hero_count=flags.hero_count, provider=provider,
                )
            except VisionProviderError:
                # A live provider can fail after some frame upserts. Roll the entire
                # attempt back, then recompute every frame with one deterministic
                # provider so persisted analyses never mix live and mock results.
                conn.rollback()
                try:
                    summary = analyze_gallery(
                        conn,
                        storage,
                        settings,
                        tenant_id=tenant["id"],
                        gallery_id=gallery["id"],
                        hero_count=flags.hero_count,
                        provider=MockVisionProvider(),
                    )
                except Exception as fallback_exc:  # noqa: BLE001 - persist a terminal run
                    conn.rollback()
                    failure_type = type(fallback_exc).__name__
                    _finish(
                        vision,
                        status="error",
                        detail=f"deterministic mock fallback failed ({failure_type})",
                    )
                    return _fail(conn, run, f"vision fallback failed ({failure_type})")
                summary["fallback_from"] = "xai"
                summary["fallback_scope"] = "whole_gallery"
                fallback_note = "xAI unavailable · deterministic mock used for the whole gallery"
                audit(
                    conn,
                    actor="pipeline",
                    action="pipeline.vision_fallback",
                    tenant_id=tenant["id"],
                    detail=f"run {run_id} gallery {gallery['id']}: xai -> mock whole_gallery",
                )
                conn.commit()
            except VisionError as exc:
                conn.rollback()
                _finish(vision, status="error", detail=str(exc))
                return _fail(conn, run, f"vision failed: {exc}")
            detail = (
                f"{summary['analyzed']} analyzed · {summary['keeper_count']} keepers · "
                f"{len(summary['hero_image_ids'])} heroes"
            )
            if subsidy_note:
                detail = f"{detail} · {subsidy_note}"
            if fallback_note:
                detail = f"{detail} · {fallback_note}"
            _finish(vision, status="done", detail=detail, output={"summary": summary})
            _save_run(conn, run)

        # 2. offer (idempotent) ----------------------------------------------
        offer_step = _find_step(run, "offer")
        _begin(offer_step)
        _save_run(conn, run)
        try:
            offer = create_or_update_offer(
                conn, tenant=tenant, gallery=gallery, run_id=run["id"],
                vision_summary=summary, flags=flags,
            )
        except Exception as exc:  # noqa: BLE001
            _finish(offer_step, status="error", detail=str(exc))
            return _fail(conn, run, f"offer failed: {exc}")
        run["offer_url"] = offer_public_url(settings, tenant["slug"], offer["token"])
        _finish(offer_step, status="done",
                detail=f"{len(offer['bundles'])} bundles · {offer['total_cents'] / 100:,.0f} total value",
                output={"token": offer["token"], "bundles": len(offer["bundles"])})

        run["status"] = "done"
        run["error"] = None
        _save_run(conn, run)
        audit(conn, actor="pipeline", action="pipeline.done",
              tenant_id=tenant["id"], detail=f"run {run_id} → {run['offer_url']}")
        conn.commit()
        return run
    finally:
        conn.close()


def _fail(conn: sqlite3.Connection, run: dict, message: str) -> dict:
    run["status"] = "error"
    run["error"] = message
    _save_run(conn, run)
    audit(conn, actor="pipeline", action="pipeline.error",
          tenant_id=run["tenant_id"], detail=message)
    conn.commit()
    return run


@register("pipeline.run")
def _job_run_pipeline(settings: Settings, payload: dict) -> None:
    """Job handler: run the gallery→vision→offer pipeline for a persisted run."""
    execute_run(settings.db_path, settings, int(payload["run_id"]))


# ── Presentation ────────────────────────────────────────────────────────────


def run_public_dict(run: dict) -> dict:
    return {
        "id": run["id"],
        "tenant_id": run["tenant_id"],
        "gallery_id": int(run["source_id"]) if run["source_id"].isdigit() else run["source_id"],
        "status": run["status"],
        "offer_url": run.get("offer_url"),
        "error": run.get("error"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "steps": [
            {"name": s["name"], "label": s["label"], "status": s["status"],
             "detail": s.get("detail", "")}
            for s in run["steps"]
        ],
    }
