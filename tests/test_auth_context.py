from starlette.requests import Request

from hestia.auth import (
    SESSION_COOKIE,
    context_from_bearer,
    context_from_session,
    create_session,
    get_valid_session,
)
from hestia.tenants import create_tenant, create_user


def _request(*, cookie: str = "", authorization: str = "") -> Request:
    headers = []
    if cookie:
        headers.append((b"cookie", f"{SESSION_COOKIE}={cookie}".encode()))
    if authorization:
        headers.append((b"authorization", authorization.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/galleries",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def test_valid_user_session_requires_matching_user_tenant_and_role(conn):
    tenant = create_tenant(conn, name="Session Studio", shoot_type="wedding")
    user = create_user(
        conn,
        tenant_id=tenant["id"],
        email="owner@session.test",
        password="pw12345678",
        role="owner",
    )
    token = create_session(
        conn,
        role="owner",
        user_id=user["id"],
        tenant_id=tenant["id"],
    )

    auth = context_from_session(conn, _request(cookie=token))

    assert auth is not None
    assert auth.tenant_id == tenant["id"]
    assert auth.user["id"] == user["id"]


def test_cross_tenant_user_session_is_rejected_and_revoked(conn):
    tenant_a = create_tenant(conn, name="Studio A", shoot_type="wedding")
    tenant_b = create_tenant(conn, name="Studio B", shoot_type="portrait")
    user_b = create_user(
        conn,
        tenant_id=tenant_b["id"],
        email="owner@b.test",
        password="pw12345678",
    )
    token = create_session(
        conn,
        role="owner",
        user_id=user_b["id"],
        tenant_id=tenant_a["id"],
    )

    assert context_from_session(conn, _request(cookie=token)) is None
    assert get_valid_session(conn, token) is None


def test_tenant_user_cannot_be_promoted_by_a_malformed_session_row(conn):
    tenant = create_tenant(conn, name="No Escalation", shoot_type="wedding")
    user = create_user(
        conn,
        tenant_id=tenant["id"],
        email="owner@no-escalation.test",
        password="pw12345678",
    )
    token = create_session(
        conn,
        role="admin",
        user_id=user["id"],
        tenant_id=tenant["id"],
    )

    assert context_from_session(conn, _request(cookie=token)) is None
    assert get_valid_session(conn, token) is None


def test_admin_bearer_uses_constant_time_comparison(conn, settings, monkeypatch):
    comparisons = []

    def _compare(candidate, expected):
        comparisons.append((candidate, expected))
        return candidate == expected

    monkeypatch.setattr("hestia.auth.hmac.compare_digest", _compare)
    auth = context_from_bearer(
        conn,
        settings,
        _request(authorization=f"Bearer {settings.api_token}"),
    )

    assert auth is not None and auth.is_admin
    assert comparisons == [(settings.api_token, settings.api_token)]
