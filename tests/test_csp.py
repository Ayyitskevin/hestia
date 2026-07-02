"""Content-Security-Policy + strict script-src (Security Slice 5).

Every response carries a CSP with a per-request nonce; script-src is nonce-only (no
'unsafe-inline'), so an injected inline script won't execute. The app's inline scripts
(the confirm-on-submit delegator and the pipeline poller) carry the nonce, and no
inline event-handler attribute remains anywhere (those can't be nonce'd).
"""

import re
from pathlib import Path


def test_hardened_response_headers_present(client):
    h = client.get("/").headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "SAMEORIGIN"
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "max-age=31536000" in h["strict-transport-security"]   # HSTS, pin to HTTPS


def test_csp_header_is_present_and_locked_down(client):
    csp = client.get("/").headers["content-security-policy"]
    for directive in ("default-src 'self'", "object-src 'none'", "frame-ancestors 'none'",
                      "base-uri 'self'", "form-action 'self'", "img-src 'self' data:"):
        assert directive in csp, csp
    assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]


def test_script_src_is_nonce_scoped_and_matches_the_page(client):
    resp = client.get("/")
    m = re.search(r"script-src 'self' 'nonce-([\w-]+)'", resp.headers["content-security-policy"])
    assert m, resp.headers["content-security-policy"]
    # the confirm delegator (in _confirm.html, included by base) carries the same nonce
    assert f'<script nonce="{m.group(1)}">' in resp.text


def test_nonce_is_fresh_per_request(client):
    a = client.get("/").headers["content-security-policy"]
    b = client.get("/").headers["content-security-policy"]
    assert a != b                          # a replayed nonce would defeat the point


def test_no_inline_event_handlers_remain_in_templates():
    """Inline on*="…" handlers are blocked by a nonce-only script-src, so none may
    survive in markup — they were refactored to <form data-confirm> + the delegator."""
    offenders = []
    for p in Path("hestia/templates").rglob("*.html"):
        if p.name == "_confirm.html":      # the delegator itself names the pattern in a comment
            continue
        for m in re.finditer(r'\bon(submit|click|change|input|load|error|focus)\s*=\s*["\']', p.read_text()):
            offenders.append(f"{p}: {m.group(0)!r}")
    assert offenders == [], offenders
