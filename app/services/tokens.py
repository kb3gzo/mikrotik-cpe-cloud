"""Token hashing + generation helpers.

Router telemetry tokens and admin fetch tokens both use the same scheme:
- Raw token is 32 URL-safe bytes (~43 chars base64url-encoded)
- We store sha256(raw) as `token_hash`
- First 8 chars of the raw token are stored as `token_prefix` for lookup

Argon2 would be overkill — these tokens are long random strings, so sha256
is enough to make reversing them computationally pointless.
"""
from __future__ import annotations

import hashlib
import secrets


TOKEN_BYTES = 32
PREFIX_LEN = 8


def mint_token() -> str:
    """Return a fresh URL-safe token string."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def prefix_of(raw: str) -> str:
    return raw[:PREFIX_LEN]


def verify_token(raw: str, expected_hash: str) -> bool:
    """Constant-time comparison of a candidate token against a stored hash."""
    return secrets.compare_digest(hash_token(raw), expected_hash)
