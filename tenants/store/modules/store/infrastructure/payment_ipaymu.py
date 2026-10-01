"""iPaymu payment gateway adapter -- PLACEHOLDER (fails closed).

The merchant account is not verified yet, so there is no key to build or
test the real HTTP call against. Every entry point raises HTTP 503 instead
of faking a successful payment, and the webhook is NEVER trusted: an
unauthenticated POST must not be able to mark an order as paid.

TODO once an iPaymu sandbox/production key exists (IPAYMU_VA,
IPAYMU_API_KEY, IPAYMU_MODE in the environment):
  1. Implement create_payment() against POST {base}/payment (v2 signature:
     HMAC_SHA256(apiKey, "POST:va:sha256(body):apiKey"), headers va /
     signature / timestamp) -- verify every field name against the docs.
  2. Implement verify_webhook() (typically by re-querying the transaction
     status via the API, since notify posts are unsigned) and only then let
     the /payment/callback route mark orders as paid.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import HTTPException, status

_BASE_URLS = {
    "sandbox": "https://sandbox.ipaymu.com/api/v2",
    "production": "https://my.ipaymu.com/api/v2",
}


class IpaymuNotReady(HTTPException):
    def __init__(self, detail: str = "Pembayaran (iPaymu) belum aktif. Menunggu verifikasi merchant dan integrasi API."):
        super().__init__(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


@dataclass
class PembayaranResult:
    checkout_url: str
    gateway_ref: str


def is_configured() -> bool:
    return bool(os.getenv("IPAYMU_VA") and os.getenv("IPAYMU_API_KEY"))


async def create_payment(
    *,
    pesanan_id: str,
    total: str,
    nama_pembeli: str,
    email_pembeli: str,
    notify_url: str,
    return_url: str,
) -> PembayaranResult:
    raise IpaymuNotReady()


async def verify_webhook(payload: dict) -> str | None:
    """Return the verified gateway_ref for a webhook payload, or None when
    it cannot be verified. Placeholder: nothing is ever verified."""
    return None
