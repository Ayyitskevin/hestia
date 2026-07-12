"""In-process rate limiting for sensitive endpoints (brute-force / abuse guard).

A sliding-window counter keyed by ``(bucket, client-ip)``. In-memory — fine for a
single-process deploy; swap the store for Redis when Hestia runs multi-worker.
The limiter lives on ``app.state.limiter`` so tests get a fresh one per app.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from fastapi import HTTPException, Request

# bucket -> (max_requests, window_seconds)
LIMITS: dict[str, tuple[int, float]] = {
    "login": (10, 60),
    "admin_login": (10, 60),
    "inquiry": (5, 60),
    "checkout": (15, 60),
    "download": (10, 60),
    "password_reset": (5, 60),
    "signup": (5, 60),
}
_DEFAULT = (30, 60)
MAX_TRACKED_KEYS = 10_000


class RateLimiter:
    def __init__(self, *, max_keys: int = MAX_TRACKED_KEYS) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._windows: dict[tuple[str, str], float] = {}
        self._max_keys = max(1, int(max_keys))
        self._lock = threading.Lock()

    def _prune_expired(self, now: float) -> None:
        for tracked, hits in list(self._hits.items()):
            cutoff = now - self._windows[tracked]
            while hits and hits[0] < cutoff:
                hits.popleft()
            if not hits:
                self._hits.pop(tracked, None)
                self._windows.pop(tracked, None)

    def check(self, bucket: str, key: str, *, limit: int, window: float,
              now: float | None = None) -> bool:
        """Record a hit; return True if it's within the limit, False if over."""
        now = time.monotonic() if now is None else now
        cutoff = now - window
        tracked = (bucket, key)
        with self._lock:
            if tracked not in self._hits:
                if len(self._hits) >= self._max_keys:
                    self._prune_expired(now)
                if len(self._hits) >= self._max_keys:
                    # A high-cardinality identity flood must not turn the limiter
                    # itself into an unbounded memory sink. Fail closed until an
                    # existing window expires.
                    return False
                self._hits[tracked] = deque()
            self._windows[tracked] = window
            dq = self._hits[tracked]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._windows.clear()


def client_ip(request: Request, *, trusted_proxies: int = 0) -> str:
    """The identity to rate-limit on. ``X-Forwarded-For`` is set by the client, so a
    naive "first hop" read lets an attacker rotate the header and dodge the login /
    reset / checkout limits entirely. We only trust XFF when the app sits behind
    ``trusted_proxies`` reverse proxies we control (``HESTIA_TRUSTED_PROXIES``), and then
    read the hop our OUTERMOST trusted proxy recorded — the ``trusted_proxies``-th entry
    from the RIGHT — so prepended fake hops on the left are ignored. With 0 (the default,
    not behind a trusted proxy) XFF is discarded and the real peer address is used."""
    peer = request.client.host if request.client else "unknown"
    if trusted_proxies <= 0:
        return peer
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    if len(hops) >= trusted_proxies:
        return hops[-trusted_proxies]
    return peer   # fewer hops than expected → header is spoofed/absent, trust the peer


def enforce(request: Request, bucket: str) -> None:
    """Raise 429 when the caller has exceeded ``bucket``'s limit."""
    limiter: RateLimiter | None = getattr(request.app.state, "limiter", None)
    if limiter is None:
        return
    settings = getattr(request.app.state, "settings", None)
    trusted = getattr(settings, "trusted_proxies", 0) if settings is not None else 0
    limit, window = LIMITS.get(bucket, _DEFAULT)
    if not limiter.check(bucket, client_ip(request, trusted_proxies=trusted), limit=limit, window=window):
        raise HTTPException(status_code=429,
                            detail="Too many requests — slow down and try again shortly.")
