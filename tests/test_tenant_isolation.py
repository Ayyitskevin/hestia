"""Cross-tenant isolation — a studio entity that (wrongly) references another studio's
client must never surface that studio's client name through a join. Regression for the
codebase-wide unscoped-JOIN class the audit surfaced on invoices, now closed everywhere.

Each getter joins clients by global id; before the fix a B-owned entity pointing at an
A-owned client_id leaked A's client name. The joins are now tenant-matched."""

from hestia.contracts import create_contract, get_contract
from hestia.crm import create_client, create_project, get_project, list_projects
from hestia.payment_plans import (
    create_payment_plan,
    deposit_balance_installments,
    get_payment_plan,
)
from hestia.questionnaires import create_questionnaire, get_questionnaire
from hestia.scheduler import create_appointment, get_appointment
from hestia.tenants import create_tenant


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