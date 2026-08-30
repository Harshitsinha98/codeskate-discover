"""Outbound email, with a provider that can be absent.

Password reset is the feature that makes accounts survivable — without it, a
forgotten password locks someone out permanently and the only recovery is the
owner editing the database.

The provider is pluggable and degrades deliberately: with no credentials
configured, the message is written to the server log instead of being sent. That
keeps the whole flow testable before an email account exists, and it means a
misconfigured deployment fails visibly in the logs rather than silently dropping
resets.

It never returns the reset link to the browser. Doing so would let anyone trigger
a reset for any address and read the token straight out of the response.
"""

from __future__ import annotations

import os

import httpx

RESEND_URL = "https://api.resend.com/emails"


class MailError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.getenv("RESEND_API_KEY", "").strip())


def _from_address() -> str:
    return os.getenv("MAIL_FROM", "CodeSkate <onboarding@resend.dev>").strip()


def send(to: str, subject: str, text: str) -> bool:
    """Returns True if actually sent, False if only logged.

    Never raises on a provider failure: a signup or reset request must not appear
    broken to the user because an email provider had a bad minute. Failures are
    logged for the operator.
    """
    api_key = os.getenv("RESEND_API_KEY", "").strip()

    if not api_key:
        print(
            "\n=== EMAIL NOT SENT (no RESEND_API_KEY configured) ===\n"
            f"To:      {to}\nSubject: {subject}\n\n{text}\n"
            "=== set RESEND_API_KEY to deliver this for real ===\n",
            flush=True,
        )
        return False

    try:
        r = httpx.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": _from_address(), "to": [to], "subject": subject, "text": text},
            timeout=httpx.Timeout(15.0),
        )
        if r.status_code >= 400:
            print(f"EMAIL SEND FAILED {r.status_code}: {r.text[:300]}", flush=True)
            return False
    except Exception as e:  # noqa: BLE001
        print(f"EMAIL SEND ERROR: {type(e).__name__}: {e}", flush=True)
        return False

    return True


def send_password_reset(to: str, link: str) -> bool:
    return send(
        to,
        "Reset your CodeSkate password",
        "Someone asked to reset the password for this CodeSkate account.\n\n"
        f"{link}\n\n"
        "The link works once and expires in 60 minutes.\n\n"
        "If this wasn't you, ignore this email — nothing has changed, and your "
        "existing password still works.\n",
    )
