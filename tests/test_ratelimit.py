"""Rate limiting + security headers (public-surface hardening)."""

from hestia.ratelimit import RateLimiter, client_ip


def test_limiter_allows_then_blocks():
    rl = RateLimiter()
    for i in range(3):
        assert rl.check("b", "ip", limit=3, window=60, now=100 + i) is True
    assert rl.check("b", "ip", limit=3, window=60, now=103) is False


def test_limiter_window_slides():
    rl = RateLimiter()
    assert rl.check("b", "ip", limit=1, window=10, now=0) is True
    assert rl.check("b", "ip", limit=1, window=10, now=5) is False   # still in window
    assert rl.check("b", "ip", limit=1, window=10, now=11) is True   # old hit expired


def test_limiter_keys_are_independent():
    rl = RateLimiter()
    assert rl.check("b", "ip1", limit=1, window=60, now=0) is True
    assert rl.check("b", "ip2", limit=1, window=60, now=0) is True   # different ip
    assert rl.check("other", "ip1", limit=1, window=60, now=0) is True  # different bucket
    assert rl.check("b", "ip1", limit=1, window=60, now=1) is False


def test_limiter_bounds_unique_identity_state():
    rl = RateLimiter(max_keys=2)
    assert rl.check("b", "ip1", limit=1, window=60, now=0) is True
    assert rl.check("b", "ip2", limit=1, window=60, now=0) is True
    assert rl.check("b", "ip3", limit=1, window=60, now=0) is False
    assert len(rl._hits) == 2


def test_limiter_prunes_expired_identities_before_rejecting_new_ones():
    rl = RateLimiter(max_keys=2)
    assert rl.check("b", "ip1", limit=1, window=10, now=0) is True
    assert rl.check("b", "ip2", limit=1, window=10, now=0) is True
    assert rl.check("b", "ip3", limit=1, window=10, now=11) is True
    assert len(rl._hits) == 1


class _Req:
    def __init__(self, xff=None, host="1.2.3.4"):
        self.headers = {"x-forwarded-for": xff} if xff else {}
        self.client = type("C", (), {"host": host})()


def test_client_ip():
    assert client_ip(_Req(xff="9.9.9.9, 10.0.0.1")) == "9.9.9.9"  # first hop
    assert client_ip(_Req(host="5.6.7.8")) == "5.6.7.8"


def test_login_is_rate_limited(client):
    # 10 attempts allowed (bad creds re-render the form), the 11th is blocked
    for _ in range(10):
        r = client.post("/login", data={"email": "x@y.com", "password": "nope"})
        assert r.status_code == 200
    blocked = client.post("/login", data={"email": "x@y.com", "password": "nope"})
    assert blocked.status_code == 429


def test_admin_login_is_rate_limited(client):
    for _ in range(10):
        assert client.post("/admin/login", data={"token": "wrong"}).status_code == 200
    assert client.post("/admin/login", data={"token": "wrong"}).status_code == 429


def test_security_headers_present(client):
    r = client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert "referrer-policy" in r.headers
