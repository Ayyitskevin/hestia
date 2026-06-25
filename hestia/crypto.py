"""Cryptographic helpers: password hashing, tenant API keys, token encryption.

Design goals for Phase 0:
- No plaintext secrets at rest. User passwords are PBKDF2-SHA256; per-tenant
  service tokens are encrypted with Fernet; tenant API keys are stored hashed.
- Stdlib-only password hashing (no passlib/bcrypt dependency).
- The Fernet key is derived deterministically from ``HESTIA_SESSION_SECRET`` so
  the same deployment can always decrypt what it wrote, with no extra key file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

# ── Password hashing (PBKDF2-SHA256) ────────────────────────────────────────

_PBKDF2_ITERATIONS = 240_000


def hash_password(password: str, *, iterations: int = _PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iter_s, salt_hex, hash_hex = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(digest, expected)


# ── Tenant API keys (hestia_tk_<slug>_<secret>) ─────────────────────────────


def generate_tenant_api_key(slug: str) -> str:
    """Mint a fresh tenant API key. Shown to the admin once; only the hash is stored."""
    return f"hestia_tk_{slug}_{secrets.token_urlsafe(24)}"


def parse_tenant_slug(api_key: str) -> str | None:
    """Extract the tenant slug embedded in a ``hestia_tk_<slug>_<secret>`` key."""
    if not api_key or not api_key.startswith("hestia_tk_"):
        return None
    rest = api_key[len("hestia_tk_") :]
    # slug may itself contain no underscores by construction (slugs are hyphenated);
    # the secret is the final underscore-delimited segment.
    if "_" not in rest:
        return None
    slug, _, _secret = rest.rpartition("_")
    return slug or None


def hash_api_key(api_key: str, pepper: str) -> str:
    """Deterministic keyed hash of an API key for storage/lookup."""
    return hmac.new(pepper.encode(), api_key.encode(), hashlib.sha256).hexdigest()


def verify_api_key(api_key: str, stored_hash: str, pepper: str) -> bool:
    return hmac.compare_digest(hash_api_key(api_key, pepper), stored_hash)


# ── Service-token encryption at rest (Fernet) ───────────────────────────────


def _fernet(session_secret: str) -> Fernet:
    # Derive a 32-byte urlsafe-base64 key from the session secret.
    key = hashlib.sha256(("hestia-token-enc:" + session_secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_token(plaintext: str, session_secret: str) -> str:
    if not plaintext:
        return ""
    return _fernet(session_secret).encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str, session_secret: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet(session_secret).decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return ""


# ── Misc ────────────────────────────────────────────────────────────────────


def new_session_token() -> str:
    return secrets.token_urlsafe(32)
