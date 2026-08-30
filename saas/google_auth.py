"""Google Sign-In, authorization code flow.

Google is the only sign-in method, which deletes a whole category of work:
password hashing, reset tokens, reset email, and the account-enumeration and
credential-stuffing surfaces that come with them. It is also a lower bar to clear
than "create a password" for someone who just wants to try the product.

Implemented against Google's HTTP endpoints directly rather than pulling in an
OAuth library. The code flow is short, and one fewer dependency in a serverless
bundle is worth more here than the abstraction.

The ID token is verified by asking Google's tokeninfo endpoint rather than by
checking the JWT signature locally, which would require fetching and caching
Google's JWKS. Since the token was just received over TLS from Google's own token
endpoint in exchange for a code we issued, this is a confirmation rather than the
primary trust boundary. The aud check below is the part that matters.
"""

from __future__ import annotations

import os
import secrets

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
TIMEOUT = httpx.Timeout(15.0)


class GoogleAuthError(RuntimeError):
    pass


def configured() -> bool:
    return bool(
        os.getenv("GOOGLE_CLIENT_ID", "").strip()
        and os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    )


def _client_id() -> str:
    value = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    if not value:
        raise GoogleAuthError("GOOGLE_CLIENT_ID is not set")
    return value


def redirect_uri() -> str:
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        raise GoogleAuthError("PUBLIC_BASE_URL is not set — needed for the OAuth callback")
    return f"{base}/api/auth/google/callback"


def new_state() -> str:
    """CSRF token. Held in a short-lived cookie and compared on the way back."""
    return secrets.token_urlsafe(24)


def authorize_url(state: str) -> str:
    from urllib.parse import urlencode

    params = {
        "client_id": _client_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        # Consent is not forced: a returning user should not have to approve again.
        "prompt": "select_account",
        "access_type": "online",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict:
    """Swap the authorization code for tokens, then read the verified identity."""
    with httpx.Client(timeout=TIMEOUT) as client:
        token_response = client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": _client_id(),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", "").strip(),
                "redirect_uri": redirect_uri(),
                "grant_type": "authorization_code",
            },
        )
        if token_response.status_code >= 400:
            raise GoogleAuthError(f"Google rejected the code: {token_response.text[:200]}")

        id_token = token_response.json().get("id_token")
        if not id_token:
            raise GoogleAuthError("Google did not return an id_token")

        info_response = client.get(TOKENINFO_URL, params={"id_token": id_token})
        if info_response.status_code >= 400:
            raise GoogleAuthError("Could not verify the Google token")
        info = info_response.json()

    # The audience check is the one that must not be skipped: without it, a token
    # minted for a different application would be accepted here.
    if info.get("aud") != _client_id():
        raise GoogleAuthError("Google token was issued for a different application")

    email = (info.get("email") or "").strip().lower()
    if not email:
        raise GoogleAuthError("Google account has no email address")
    if info.get("email_verified") not in (True, "true"):
        raise GoogleAuthError("Please verify your email address with Google first")

    return {
        "sub": info["sub"],
        "email": email,
        "name": info.get("name") or email.split("@")[0],
        "picture": info.get("picture"),
    }
