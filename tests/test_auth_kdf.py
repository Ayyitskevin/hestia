"""Password KDF hardening (Security Slice 3): the work factor meets OWASP-current
guidance, existing hashes still verify, and a successful login transparently upgrades
a weaker hash in place — so the fleet migrates with zero resets."""

from hestia.auth import authenticate_user
from hestia.crypto import (
    _PBKDF2_ITERATIONS,
    hash_password,
    needs_rehash,
    verify_password,
)
from hestia.tenants import create_tenant, create_user, get_user_by_email

_LEGACY = 240_000   # the previous cost, still deployed on existing beta tenants


def test_kdf_meets_owasp_current():
    assert _PBKDF2_ITERATIONS >= 600_000
    assert hash_password("pw12345678").split("$")[1] == str(_PBKDF2_ITERATIONS)


def test_needs_rehash_flags_only_weaker_hashes():
    assert needs_rehash(hash_password("x", iterations=_LEGACY)) is True
    assert needs_rehash(hash_password("x")) is False          # current cost — no upgrade
    assert needs_rehash("bcrypt$12$whatever") is True          # foreign algorithm
    assert needs_rehash("not-a-hash") is True                  # malformed → upgrade


def test_legacy_hash_still_verifies():
    legacy = hash_password("correct horse battery", iterations=_LEGACY)
    assert verify_password("correct horse battery", legacy) is True
    assert verify_password("wrong", legacy) is False           # backward-compatible verify


def test_login_upgrades_a_legacy_hash_in_place(conn):
    t = create_tenant(conn, name="Auth Studio", shoot_type="wedding")
    create_user(conn, tenant_id=t["id"], email="owner@x.test", password="pw12345678",
                role="owner", verified=1)
    # Backdate to a legacy-cost hash, as an existing beta tenant would have.
    conn.execute("UPDATE users SET password_hash = ? WHERE lower(email) = ?",
                 (hash_password("pw12345678", iterations=_LEGACY), "owner@x.test"))
    conn.commit()
    assert get_user_by_email(conn, "owner@x.test")["password_hash"].split("$")[1] == str(_LEGACY)

    # A successful login re-hashes at the current cost, transparently.
    assert authenticate_user(conn, "owner@x.test", "pw12345678") is not None
    upgraded = get_user_by_email(conn, "owner@x.test")["password_hash"]
    assert upgraded.split("$")[1] == str(_PBKDF2_ITERATIONS)
    assert verify_password("pw12345678", upgraded) is True

    # A FAILED login never touches the stored hash.
    assert authenticate_user(conn, "owner@x.test", "wrong-password") is None
    assert get_user_by_email(conn, "owner@x.test")["password_hash"] == upgraded
