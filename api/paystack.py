"""Thin wrapper around the Paystack HTTP API (https://paystack.com/docs/api/)."""
import hashlib
import hmac
from decimal import Decimal, ROUND_HALF_UP

import requests
from django.conf import settings

BASE_URL = "https://api.paystack.co"
CURRENCY = "GHS"
TIMEOUT = 15  # seconds


class PaystackError(Exception):
    """Paystack was unreachable or rejected a request."""


def to_pesewas(amount):
    """GHS amount -> pesewas (Paystack expects the smallest currency unit)."""
    return int((Decimal(amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _headers():
    return {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def _call(method, path, **kwargs):
    try:
        response = requests.request(
            method, f"{BASE_URL}{path}", headers=_headers(), timeout=TIMEOUT, **kwargs
        )
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise PaystackError("Could not reach Paystack") from exc

    if not response.ok or not body.get("status"):
        raise PaystackError(body.get("message") or "Paystack rejected the request")
    return body["data"]


def initialize_transaction(*, email, amount, reference, metadata):
    """Start a payment. `amount` is a Decimal in GHS."""
    payload = {
        "email": email,
        "amount": to_pesewas(amount),
        "currency": CURRENCY,
        "reference": reference,
        "metadata": metadata,
    }
    callback_url = getattr(settings, "PAYSTACK_CALLBACK_URL", "")
    if callback_url:
        payload["callback_url"] = callback_url
    return _call("POST", "/transaction/initialize", json=payload)


def verify_transaction(reference):
    """Ask Paystack for the true state of a transaction."""
    return _call("GET", f"/transaction/verify/{reference}")


def valid_signature(raw_body: bytes, signature: str) -> bool:
    """Check the x-paystack-signature header (HMAC-SHA512 of the raw body)."""
    expected = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode(), raw_body, hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")
