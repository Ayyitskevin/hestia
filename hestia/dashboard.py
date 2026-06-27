"""The owner's 'today' view — what needs attention across the studio, gathered into
one home screen so nothing slips: new leads to answer, invoices to chase, sessions
coming up, and finished galleries still waiting to be delivered. Pure read-side
aggregation over the modules that already own each thing."""

from __future__ import annotations

import sqlite3

from .invoices import money


def needs_attention(conn: sqlite3.Connection, tenant_id: str, *, limit: int = 8) -> dict:
    """Actionable items for the dashboard, each scoped to the tenant."""
    leads = [dict(r) for r in conn.execute(
        "SELECT p.id, p.name, p.created_at, p.shoot_type, c.name AS client_name "
        "FROM projects p LEFT JOIN clients c ON c.id = p.client_id AND c.tenant_id = p.tenant_id "
        "WHERE p.tenant_id = ? AND p.status = 'lead' "
        "ORDER BY p.created_at ASC LIMIT ?",  # oldest unanswered first
        (tenant_id, limit))]

    unpaid = [dict(r) for r in conn.execute(
        "SELECT i.id, i.title, i.amount_cents, i.currency, i.status, c.name AS client_name, "
        # flag the overdue ones (sent, past a parseable due_date) and float them up
        "  CASE WHEN i.status = 'sent' AND date(i.due_date) IS NOT NULL "
        "       AND date(i.due_date) < date('now') THEN 1 ELSE 0 END AS is_overdue "
        "FROM invoices i LEFT JOIN clients c ON c.id = i.client_id AND c.tenant_id = i.tenant_id "
        # plan_id IS NULL: installments live under their payment plan, not this list,
        # so they don't get double-counted here and under /payment-plans
        "WHERE i.tenant_id = ? AND i.status IN ('draft', 'sent') AND i.plan_id IS NULL "
        "ORDER BY is_overdue DESC, i.id DESC LIMIT ?",
        (tenant_id, limit))]
    for inv in unpaid:
        inv["amount_display"] = money(inv["amount_cents"], inv.get("currency") or "usd")

    # starts_at is free-text (owners type it), so parse via datetime(): a real
    # timestamp compares chronologically; unparseable text yields NULL and is excluded
    # rather than mis-sorted by a lexicographic string compare.
    upcoming = [dict(r) for r in conn.execute(
        "SELECT a.id, a.title, a.starts_at, a.status, c.name AS client_name "
        "FROM appointments a LEFT JOIN clients c ON c.id = a.client_id AND c.tenant_id = a.tenant_id "
        "WHERE a.tenant_id = ? AND a.status != 'canceled' "
        "AND datetime(a.starts_at) IS NOT NULL AND datetime(a.starts_at) >= datetime('now') "
        "ORDER BY datetime(a.starts_at) ASC LIMIT ?",
        (tenant_id, limit))]

    # Published galleries the client can see but can't yet download — finish the job.
    to_deliver = [dict(r) for r in conn.execute(
        "SELECT id, title FROM galleries "
        "WHERE tenant_id = ? AND status = 'published' "
        "AND (delivery_token IS NULL OR delivery_token = '') "
        "ORDER BY id DESC LIMIT ?",
        (tenant_id, limit))]

    # Contracts sent but not yet signed — the booking can't proceed until they are.
    # Client join tenant-matched so a stray cross-tenant client_id can't surface a name.
    awaiting_contract = [dict(r) for r in conn.execute(
        "SELECT ct.id, ct.title, c.name AS client_name FROM contracts ct "
        "LEFT JOIN clients c ON c.id = ct.client_id AND c.tenant_id = ct.tenant_id "
        "WHERE ct.tenant_id = ? AND ct.status = 'sent' "
        "ORDER BY ct.created_at ASC LIMIT ?",  # oldest unsigned first
        (tenant_id, limit))]

    # Questionnaires sent but not yet completed — chase the details you need to shoot.
    awaiting_questionnaire = [dict(r) for r in conn.execute(
        "SELECT q.id, q.title, c.name AS client_name FROM questionnaires q "
        "LEFT JOIN clients c ON c.id = q.client_id AND c.tenant_id = q.tenant_id "
        "WHERE q.tenant_id = ? AND q.status = 'sent' "
        "ORDER BY q.created_at ASC LIMIT ?",
        (tenant_id, limit))]

    return {
        "leads": leads,
        "unpaid": unpaid,
        "upcoming": upcoming,
        "to_deliver": to_deliver,
        "awaiting_contract": awaiting_contract,
        "awaiting_questionnaire": awaiting_questionnaire,
        "total": (len(leads) + len(unpaid) + len(upcoming) + len(to_deliver)
                  + len(awaiting_contract) + len(awaiting_questionnaire)),
    }
