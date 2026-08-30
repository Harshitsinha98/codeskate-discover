"""Passwords and sessions.

Hashing uses hashlib.scrypt from the standard library rather than bcrypt or
argon2. It is memory-hard, it is what the stdlib offers, and it avoids a compiled
dependency in a serverless bundle — one fewer thing that can fail to build.

Session tokens are random, and only their SHA-256 is stored. A leaked database
therefore does not hand over usable sessions.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass

# scrypt parameters. n=2**15 with r=8 needs roughly 32MB and tens of milliseconds
# — expensive enough to make offline cracking painful, cheap enough that a login
# does not stall a serverless function.
#
# maxmem must be passed explicitly. OpenSSL defaults it to 32MB, and this
# configuration needs 128 * n * r = exactly 32MB, so the default rejects it with
# "memory limit exceeded". The headroom below also allows raising _N later without
# tripping over the same edge.
_N = 2 ** 15
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16
_MAXMEM = 96 * 1024 * 1024

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
MIN_PASSWORD = 10


class AuthError(ValueError):
    pass


def validate_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 320:
        raise AuthError("Enter a valid email address")
    return email


def validate_password(password: str) -> str:
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(f"Password must be at least {MIN_PASSWORD} characters")
    if len(password) > 1024:
        # scrypt over an unbounded input is a cheap denial-of-service.
        raise AuthError("Password is too long")
    return password


def hash_password(password: str) -> str:
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM
    )
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(hash_hex)),
            maxmem=_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, bytes.fromhex(hash_hex))


@dataclass(frozen=True)
class NewSession:
    token: str       # goes to the browser cookie
    token_hash: str  # goes to the database


def new_session() -> NewSession:
    token = secrets.token_urlsafe(32)
    return NewSession(token=token, token_hash=hash_token(token))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


COOKIE_NAME = "cs_session"


def cookie_kwargs(secure: bool) -> dict:
    """HttpOnly so JavaScript cannot read it; SameSite=Lax to blunt CSRF."""
    return {
        "key": COOKIE_NAME,
        "httponly": True,
        "samesite": "lax",
        "secure": secure,
        "path": "/",
        "max_age": 30 * 24 * 3600,
    }
