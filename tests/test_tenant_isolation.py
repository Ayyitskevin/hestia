"""Cross-tenant isolation — a studio entity that (wrongly) references another studio's
client must never surface that studio's client name through a join. Regression for the
codebase-wide unscoped-JOIN class the audit surfaced on invoices, now closed everywhere.

Each getter joins clients by global id; before the fix a B-owned entity pointing at an
A-owned client_id leaked A's client name. The joins are now tenant-matched."""

import io

import pytest

from hestia.contracts import create_contract, get_contract
from hestia.crm import create_client, create_project, get_project, list_projects
from hestia.galleries import add_image, create_gallery
from hestia.payment_plans import (
    create_payment_plan,
    deposit_balance_installments,
    get_payment_plan,
)
from hestia.questionnaires import create_questionnaire, get_questionnaire
from hestia.scheduler import create_appointment, get_appointment
from hestia.storage import LocalStorage
from hestia.tenants import create_tenant
from hestia.vision import search_images


def _two_tenants_and_foreign_client(conn):
    a = create_tenant(conn, name="StudioA", shoot_type="wedding")
    b = create_tenant(conn, name="StudioB", shoot_type="wedding")
    ca = create_client(conn, tenant_id=a["id"], name="SECRET-A", email="a@example.com")
    conn.commit()
    return a, b, ca


def test_contract_join_no_cross_tenant_client_leak(conn):
    _a, b, ca = _two_tenants_and_foreign_client(conn)
    ct = create_contract(conn, tenant_id=b["id"], title="Deal", client_id=ca["id"])
    conn.commit()
    assert get_contract(conn, b["id"], ct["id"])["client_name"] is None


def test_appointment_join_no_cross_tenant_client_leak(conn):
    _a, b, ca = _two_tenants_and_foreign_client(conn)
    ap = create_appointment(conn, tenant_id=b["id"], title="Call",
                            options=["2026-07-01 10:00"], client_id=ca["id"])
    conn.commit()
    assert get_appointment(conn, b["id"], ap["id"])["client_name"] is None


def test_questionnaire_join_no_cross_tenant_client_leak(conn):
    _a, b, ca = _two_tenants_and_foreign_client(conn)
    q = create_questionnaire(conn, tenant_id=b["id"], title="Intake", prompts=["Q1"],
                             client_id=ca["id"])
    conn.commit()
    assert get_questionnaire(conn, b["id"], q["id"])["client_name"] is None


def test_payment_plan_join_no_cross_tenant_client_leak(conn, settings):
    _a, b, ca = _two_tenants_and_foreign_client(conn)
    pp = create_payment_plan(conn, settings, tenant_id=b["id"], title="Plan", client_id=ca["id"],
                             installments=deposit_balance_installments(total_cents=200000,
                                                                       deposit_cents=50000))
    conn.commit()
    assert get_payment_plan(conn, b["id"], pp["id"])["client_name"] is None


def test_project_join_no_cross_tenant_client_leak(conn):
    _a, b, ca = _two_tenants_and_foreign_client(conn)
    p = create_project(conn, tenant_id=b["id"], name="Shoot", client_id=ca["id"])
    conn.commit()
    assert get_project(conn, b["id"], p["id"])["client_name"] is None
    listed = {pr["id"]: pr for pr in list_projects(conn, b["id"])}
    assert listed[p["id"]]["client_name"] is None                 # list path too


# ── Image catalog search + storage isolation (Slice 2) ──────────────────────
# The FK-join tests above cover client-name leakage; these cover the AI catalog
# search and the media blob store — the other surfaces where two studios' data sits
# side by side (shared keyword, sequential image ids, one media root).


def _tenant_with_analyzed_image(conn, storage, *, name, keyword):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="Finals")
    img = add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"],
                    filename="a.jpg", fileobj=io.BytesIO(b"x" * 32), content_type="image/jpeg")
    conn.execute(
        "INSERT INTO image_analyses (image_id, gallery_id, tenant_id, keywords_json, "
        "keeper_score, shot_type) VALUES (?, ?, ?, ?, ?, ?)",
        (img["id"], g["id"], t["id"], f'["{keyword}"]', 0.9, "candid"),
    )
    conn.commit()
    return t, g, img


def test_catalog_search_never_crosses_tenants(conn, storage):
    a, _, a_img = _tenant_with_analyzed_image(conn, storage, name="Studio A", keyword="beach")
    b, _, b_img = _tenant_with_analyzed_image(conn, storage, name="Studio B", keyword="beach")

    a_hits = {r["id"] for r in search_images(conn, a["id"], keyword="beach")}
    b_hits = {r["id"] for r in search_images(conn, b["id"], keyword="beach")}

    assert a_hits == {a_img["id"]}                 # A sees only A's frame...
    assert b_hits == {b_img["id"]}                 # ...B only B's, despite the shared keyword
    assert a_img["id"] not in b_hits and b_img["id"] not in a_hits


def test_search_by_shot_type_is_tenant_scoped(conn, storage):
    a, _, a_img = _tenant_with_analyzed_image(conn, storage, name="Studio A", keyword="x")
    _tenant_with_analyzed_image(conn, storage, name="Studio B", keyword="y")   # also 'candid'
    hits = {r["id"] for r in search_images(conn, a["id"], shot_type="candid")}
    assert hits == {a_img["id"]}                   # both have 'candid'; A sees only its own


def test_storage_keys_are_tenant_prefixed(conn, storage):
    a, _, a_img = _tenant_with_analyzed_image(conn, storage, name="Studio A", keyword="x")
    row = conn.execute("SELECT storage_key FROM images WHERE id = ?", (a_img["id"],)).fetchone()
    assert row["storage_key"].startswith(f"{a['id']}/")   # blobs are namespaced by tenant


def test_localstorage_rejects_path_traversal(tmp_path):
    """A crafted key must not escape the media root — one traversal would cross studios."""
    store = LocalStorage(tmp_path)
    for bad in ("../../etc/passwd", "a/../../escape.txt"):
        with pytest.raises(ValueError):
            store.open(bad)
        with pytest.raises(ValueError):
            store.put(bad, io.BytesIO(b"x"))