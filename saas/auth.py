"""Sessions.

Passwords are gone — Google is the only sign-in method, which removed hashing,
reset tokens, reset email, and the enumeration and credential-stuffing surfaces
that came with them. What remains is session handling.

Session tokens are random and only their SHA-256 is stored, so a database dump
yields no usable sessions.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

COOKIE_NAME = "cs_session"
STATE_COOKIE = "cs_oauth_state"


@dataclass(frozen=True)
class NewSession:
    token: str       # goes to the browser cookie
    token_hash: str  # goes to the database


def new_session() -> NewSession:
    token = secrets.token_urlsafe(32)
    return NewSession(token=token, token_hash=hash_token(token))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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


def state_cookie_kwargs(secure: bool) -> dict:
    """Short-lived cookie holding the OAuth state for the round trip to Google.

    SameSite must be lax rather than strict: the browser arrives back at the
    callback from accounts.google.com, and a strict cookie would not be sent.
    """
    return {
        "key": STATE_COOKIE,
        "httponly": True,
        "samesite": "lax",
        "secure": secure,
        "path": "/",
        "max_age": 600,
    }
