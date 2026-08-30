"""Razorpay payments.

Razorpay rather than Stripe because the users are in India: UPI and netbanking are
what people actually pay with, pricing is native INR, and onboarding an Indian
entity is far less friction than Stripe's India requirements.

This uses Orders and Checkout — the user buys a month (or several) and the plan
expiry extends. It is not auto-debit. That is a deliberate v1 choice: order
verification is a single well-defined HMAC check that is hard to get wrong, while
Razorpay Subscriptions need mandate handling and webhook-driven state that is very
easy to get subtly wrong and cannot be verified without a live merchant account.
Auto-renewal is the natural next step once real payments are flowing.

Two independent paths mark a payment good: the browser returning from Checkout, and
the server-to-server webhook. Both are idempotent through a unique constraint on
the provider payment id, because a user closing the tab mid-redirect must not lose
a month they paid for.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

import httpx

from .plans import PLANS, plan_for

API_BASE = "https://api.razorpay.com/v1"
TIMEOUT = httpx.Timeout(20.0)


class BillingError(RuntimeError):
    pass


def configured() -> bool:
    return bool(
        os.getenv("RAZORPAY_KEY_ID", "").strip()
        and os.getenv("RAZORPAY_KEY_SECRET", "").strip()
    )


def key_id() -> str:
    value = os.getenv("RAZORPAY_KEY_ID", "").strip()
    if not value:
        raise BillingError("RAZORPAY_KEY_ID is not set")
    return value


def _secret() -> str:
    value = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
    if not value:
        raise BillingError("RAZORPAY_KEY_SECRET is not set")
    return value


def create_order(user_id: int, plan_key: str, months: int = 1) -> dict:
    plan = plan_for(plan_key)
    if plan.price_inr <= 0:
        raise BillingError("The free plan does not require payment")
    if months < 1 or months > 12:
        raise BillingError("Choose between 1 and 12 months")

    amount_paise = plan.price_inr * 100 * months

    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.post(
            f"{API_BASE}/orders",
            auth=(key_id(), _secret()),
            json={
                "amount": amount_paise,
                "currency": "INR",
                # Razorpay rejects receipts over 40 characters.
                "receipt": f"cs-{user_id}-{plan.key}-{months}"[:40],
                "notes": {"user_id": str(user_id), "plan": plan.key, "months": str(months)},
            },
        )
    if r.status_code >= 400:
        raise BillingError(f"Razorpay rejected the order: {r.text[:200]}")

    order = r.json()
    return {
        "order_id": order["id"],
        "amount_paise": amount_paise,
        "currency": "INR",
        "key_id": key_id(),
        "plan": plan.key,
        "months": months,
    }


def verify_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """Razorpay signs `order_id|payment_id` with the key secret.

    compare_digest rather than == so a mismatch cannot be narrowed down by timing.
    """
    expected = hmac.new(
        _secret().encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, (signature or "").strip())


def verify_webhook(body: bytes, signature: str) -> bool:
    """Webhooks are signed with a separate secret from the API key."""
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()
    if not secret:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (signature or "").strip())


def fetch_payment(payment_id: str) -> dict:
    """Confirm with Razorpay rather than trusting the browser's word.

    A valid signature proves the values came from Razorpay, but fetching the
    payment is what proves it was actually captured and for how much.
    """
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.get(f"{API_BASE}/payments/{payment_id}", auth=(key_id(), _secret()))
    if r.status_code >= 400:
        raise BillingError(f"Could not fetch payment {payment_id}: {r.text[:200]}")
    return r.json()


def parse_webhook_payment(body: bytes) -> dict | None:
    """Pull the fields we act on out of a payment.captured event."""
    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        return None
    if event.get("event") not in ("payment.captured", "order.paid"):
        return None

    payment = (
        event.get("payload", {}).get("payment", {}).get("entity")
        or event.get("payload", {}).get("order", {}).get("entity")
    )
    if not payment:
        return None

    notes = payment.get("notes") or {}
    return {
        "payment_id": payment.get("id"),
        "order_id": payment.get("order_id") or payment.get("id"),
        "amount_paise": int(payment.get("amount") or 0),
        "status": payment.get("status") or "captured",
        "user_id": int(notes["user_id"]) if str(notes.get("user_id", "")).isdigit() else None,
        "plan": notes.get("plan") if notes.get("plan") in PLANS else None,
        "months": int(notes["months"]) if str(notes.get("months", "")).isdigit() else 1,
        "raw": payment,
    }
