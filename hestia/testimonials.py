"""Testimonials — capture a client's review through an unguessable link, then
feature the best on the public studio site.

The studio *requests* a testimonial (one row, ``status='requested'``, carrying a
token); the client submits a rating + a few words at ``/t/{token}``
(``status='submitted'``); the studio *features* the ones it loves
(``status='featured'``) and they render on the public site. Hiding is
non-destructive (``status='hidden'``). The public link is the same
unguessable-token model as offers and portals — no client login, no new password
surface. One submission per link: a returned 'requested' row is the only thing a
submit can move, so a refreshed or re-shared link can't overwrite a review.
"""

from __future__ import annotations

import sqlite3

from .config import Settings
from .crypto import new_session_token

REQUESTED, SUBMITTED, FEATURED, HIDDEN = "requested", "submitted", "featured", "hidden"
# the transitions an owner may apply — never back to 'requested', and only to a
# review that has actually come back.
_OWNER_STATUSES = frozenset({FEATURED, HIDDEN, SUBMITTED})
_MOVABLE = (SUBMITTED, FEATURED, HIDDEN)


def request_testimonial(conn: sqlite3.Connection, *, tenant_id: str,
                        client_id: int | None = None, author_name: str = "") -> dict:
    """Open a pending testimonial request and return it (including its public token)."""
    token = new_session_token()
    cur = conn.execute(
        "INSERT INTO testimonials (tenant_id, client_id, token, author_name) "
        "VALUES (?, ?, ?, ?)",
        (tenant_id, client_id, token, author_name.strip()),
    )
    return get_testimonial(conn, tenant_id, cur.lastrowid)


def get_testimonial(conn: sqlite3.Connection, tenant_id: str, testimonial_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM testimonials WHERE tenant_id = ? AND id = ?",
        (tenant_id, testimonial_id),
    ).fetchone()
    return dict(row) if row else None


def get_by_token(conn: sqlite3.Connection, token: str) -> dict | None:
    if not token:
        return None
    row = conn.execute("SELECT * FROM testimonials WHERE token = ?", (token,)).fetchone()
    return dict(row) if row else None


def submit_testimonial(conn: sqlite3.Connection, token: str, *, rating, body: str,
                       author_name: str = "") -> bool:
    """The client's submission. Idempotent on the link: only a 'requested' row is
    accepted, so a second POST (a refresh, a link shared twice) is a no-op and can't
    overwrite the review. Rating is clamped to 1..5; a blank name keeps any we
    pre-filled from the CRM. Returns True iff the review was recorded."""
    try:
        stars = max(1, min(5, int(rating)))
    except (TypeError, ValueError):
        stars = 5
    name = author_name.strip()
    cur = conn.execute(
        "UPDATE testimonials SET status = 'submitted', rating = ?, body = ?, "
        "author_name = CASE WHEN ? <> '' THEN ? ELSE author_name END, "
        "submitted_at = datetime('now') "
        "WHERE token = ? AND status = 'requested'",
        (stars, body.strip(), name, name, token),
    )
    return cur.rowcount > 0


def set_status(conn: sqlite3.Connection, tenant_id: str, testimonial_id: int, status: str) -> bool:
    """Owner action: feature / hide / un-feature. Only a review that has come back
    can be moved (you can't feature an unanswered request), and only among the owner
    statuses. Returns True iff a row changed."""
    if status not in _OWNER_STATUSES:
        return False
    cur = conn.execute(
        "UPDATE testimonials SET status = ? WHERE tenant_id = ? AND id = ? "
        "AND status IN (?, ?, ?)",
        (status, tenant_id, testimonial_id, *_MOVABLE),
    )
    return cur.rowcount > 0


def featured_testimonials(conn: sqlite3.Connection, tenant_id: str, *, limit: int = 12) -> list[dict]:
    """The reviews to show on the public site, most-recent first."""
    rows = conn.execute(
        "SELECT * FROM testimonials WHERE tenant_id = ? AND status = 'featured' "
        "ORDER BY submitted_at DESC LIMIT ?",
        (tenant_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def list_testimonials(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    """Every testimonial for the owner hub, freshest activity first."""
    rows = conn.execute(
        "SELECT * FROM testimonials WHERE tenant_id = ? "
        "ORDER BY COALESCE(submitted_at, created_at) DESC, id DESC",
        (tenant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def testimonial_public_url(settings: Settings, token: str) -> str:
    return f"{settings.public_url.rstrip('/')}/t/{token}"
