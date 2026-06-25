"""CSRF protection for session-authenticated form POSTs.

The UI rides on a session cookie, so its form POSTs are the CSRF-exposed surface.
The JSON API authenticates with a bearer token (an attacker can't read it, so it's
immune), and the public ``/pay``, ``/g`` and ``/webhooks`` routes are either
unauthenticated or signature-verified — none of those are session-cookie driven.

We bind a **stateless** token to the session::

    token = HMAC(session_secret, session_token)

An attacker can make a victim's browser *send* the session cookie, but can neither
read it (it's ``httponly``) nor read our cross-origin HTML to lift the matching
hidden field — so they cannot produce the (cookie, token) pair this requires.
No server-side storage, no extra column: the session cookie already is the secret.
"""

from __future__ import annotations

import hmac
from hashlib import sha256

from fastapi import Form, HTTPException, Request

from .auth import SESSION_COOKIE

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def issue_token(session_token: str, secret: str) -> str:
    """Derive the CSRF token for a session (empty string when not logged in)."""
    if not session_token:
        return ""
    return hmac.new(secret.encode(), session_token.encode(), sha256).hexdigest()


def valid_token(submitted: str, session_token: str, secret: str) -> bool:
    expected = issue_token(session_token, secret)
    if not expected or not submitted:
        return False
    return hmac.compare_digest(submitted, expected)


def csrf_token_for(request: Request) -> str:
    """Token to embed in this request's forms — '' when there's no session."""
    session_token = request.cookies.get(SESSION_COOKIE, "")
    return issue_token(session_token, request.app.state.settings.session_secret)


def csrf_protect(request: Request, csrf_token: str = Form("")) -> None:
    """Dependency: reject unsafe *session-authenticated* requests without a valid token.

    Safe methods pass through, and so do requests with no session cookie — those
    are unauthenticated public POSTs (login, studio inquiry, invoice checkout)
    where there's no session to ride and nothing for an attacker to forge.
    """
    if request.method in SAFE_METHODS:
        return
    session_token = request.cookies.get(SESSION_COOKIE, "")
    if not session_token:
        return
    secret = request.app.state.settings.session_secret
    if not valid_token(csrf_token, session_token, secret):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
