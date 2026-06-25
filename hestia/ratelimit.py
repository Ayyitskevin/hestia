"""In-process rate limiting for sensitive endpoints (brute-force / abuse guard).

A sliding-window counter keyed by ``(bucket, client-ip)``. In-memory — fine for a
single-process deploy; swap the store for Redis when Hestia runs multi-worker.
The limiter lives on ``app.state.limiter`` so tests get a fresh one per app.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

# bucket -> (max_requests, window_seconds)
LIMITS: dict[str, tuple[int, float]] = {
    "login": (10, 60),
    "admin_login": (10, 60),
    "inquiry": (5, 60),
    "checkout": (15, 60),
    "password_reset": (5, 60),
    "signup": (5, 60),
}
_DEFAULT = (30, 60)


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, bucket: str, key: str, *, limit: int, window: float,
              now: float | None = None) -> bool:
        """Record a hit; return True if it's within the limit, False if over."""
        now = time.monotonic() if now is None else now
        cutoff = now - window
        with self._lock:
            dq = self._hits[(bucket, key)]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def client_ip(request: Request) -> str:
    """Best-effort client IP — first X-Forwarded-For hop, else the peer."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def enforce(request: Request, bucket: str) -> None:
    """Raise 429 when the caller has exceeded ``bucket``'s limit."""
    limiter: RateLimiter | None = getattr(request.app.state, "limiter", None)
    if limiter is None:
        return
    limit, window = LIMITS.get(bucket, _DEFAULT)
    if not limiter.check(bucket, client_ip(request), limit=limit, window=window):
        raise HTTPException(status_code=429,
                            detail="Too many requests — slow down and try again shortly.")
