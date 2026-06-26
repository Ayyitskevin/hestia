"""Payment-plan installments carry the studio's sales tax too — completing tax
coverage across every invoice type (standalone, orders, now plan installments)."""

from hestia.payment_plans import (
    create_payment_plan,
    deposit_balance_installments,
    installments_for_plan,
)
from hestia.tenants import create_tenant, set_tax_rate


def _plan(conn, settings, tenant_id, *, total, deposit):
    return create_payment_plan(conn, settings, tenant_id=tenant_id, title="Wedding", client_id=None,
                               installments=deposit_balance_installments(total_cents=total,
                                                                         deposit_cents=deposit))


def test_installments_carry_studio_tax(conn, settings):
    t = create_tenant(conn, name="Plan", shoot_type="wedding")
    set_tax_rate(conn, t["id"], 1000)                              # 10%
    plan = _plan(conn, settings, t["id"], total=400000, deposit=100000)
    conn.commit()
    by = {i["amount_cents"]: i for i in installments_for_plan(conn, t["id"], plan["id"])}
    assert by[100000]["tax_cents"] == 10000 and by[100000]["total_cents"] == 110000   # deposit + 10%
    assert by[300000]["tax_cents"] == 30000 and by[300000]["total_cents"] == 330000   # balance + 10%
    assert by[100000]["total_display"] == "$1,100.00" and by[100000]["amount_display"] == "$1,000.00"


def test_installments_untaxed_without_rate(conn, settings):
    t = create_tenant(conn, name="NoTax", shoot_type="wedding")    # default rate 0
    plan = _plan(conn, settings, t["id"], total=200000, deposit=50000)
    conn.commit()
    insts = installments_for_plan(conn, t["id"], plan["id"])
    assert insts and all(i["tax_cents"] == 0 and i["total_cents"] == i["amount_cents"] for i in insts)
