"""Canonical path policy for private, client-facing surfaces.

Every prefix here identifies a URL whose path can contain a bearer credential,
private tenant identifier, or revocable client content.  Response hardening,
access-log redaction, robots output, hosted preflight, and CI all consume this
registry so a new private surface cannot be protected in only one layer.
"""

from __future__ import annotations

PRIVATE_SURFACE_PREFIXES = (
    "/portal/",
    "/d/",
    "/pay/",
    "/a/",
    "/sign/",
    "/g/",
    "/s/",
    "/book/",
    "/q/",
    "/t/",
    "/invite/",
    "/verify/",
    "/reset/",
    "/calendar/",
    "/media/",
    "/proposal/",
)

PRIVATE_SURFACE_SEGMENTS = frozenset(
    prefix.removeprefix("/").removesuffix("/") for prefix in PRIVATE_SURFACE_PREFIXES
)
