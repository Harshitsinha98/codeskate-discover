"""Encryption for user-supplied LLM API keys.

Users bring their own key (BYOK), which is the right default at launch: their
usage bills their account, not yours, so a single enthusiastic user cannot
generate a surprise invoice. It also means the platform is holding a credential
that can spend real money, so it is encrypted at rest and never sent back to the
browser — the UI only ever sees the last four characters.

Fernet gives authenticated encryption (AES-128-CBC with an HMAC), so ciphertext
that has been tampered with fails to decrypt rather than decrypting to garbage.

APP_SECRET is the only thing standing between a database dump and usable keys.
It belongs in the platform's environment variables, never in the repository, and
rotating it invalidates every stored key — which is the correct behaviour if it
ever leaks.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


class CryptoError(RuntimeError):
    pass


def _fernet() -> Fernet:
    secret = os.getenv("APP_SECRET", "").strip()
    if len(secret) < 32:
        raise CryptoError(
            "APP_SECRET must be set to at least 32 characters. Generate one with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"`."
        )
    # Fernet needs exactly 32 url-safe base64 bytes; derive them so the operator
    # can use any sufficiently long secret string.
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise CryptoError(
            "Stored key could not be decrypted. APP_SECRET has changed — users "
            "must re-enter their API keys."
        ) from e


def hint(api_key: str) -> str:
    """The only part of a key ever shown back to the user."""
    tail = api_key.strip()[-4:]
    return f"...{tail}" if tail else "..."
