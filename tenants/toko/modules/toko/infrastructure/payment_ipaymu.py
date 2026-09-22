"""iPaymu payment gateway adapter -- PLACEHOLDER.

STATUS: not wired to the real iPaymu API yet. The merchant account behind
IPAYMU_VA/IPAYMU_API_KEY was only just registered and has not completed
merchant verification, so there is no working key to build or test
against. Every call below fails loudly (HTTP 503) instead of pretending
to succeed, because silently faking a "payment created" response in a
finance-adjacent system is worse than an honest "not configured yet".

TODO once you have a sandbox or production API key from the iPaymu
dashboard:
  1. Fill IPAYMU_VA / IPAYMU_API_KEY / IPAYMU_MODE in your .env (see
     .env.example).
  2. Confirm the exact request/response shape against the current iPaymu
     API v2 docs (https://ipaymu.com/en/developer-api-payment-gateway/ --
     unreachable from this session's network, must be checked from
     wherever you read this). The implementation below follows iPaymu's
     documented v2 signature scheme as of this module's authoring, but it
     has NOT been exercised against a live endpoint:
       - signature = HMAC_SHA256(key=apiKey, msg=stringToSign), hex,
         lowercase
       - stringToSign = f"{METHOD}:{va}:{sha256(body_json).hexdigest()}:{apiKey}"
       - headers: va, signature, timestamp (YmdHis)
       - POST {base_url}/payment -> response Data.Url is the checkout
         redirect URL, Data.SessionID / Data.TransactionId identify the
         transaction
     Verify every field name against the real docs/Postman collection
     before removing this warning.
  3. Confirm the webhook payload shape iPaymu actually POSTs to your
     notifyUrl (commonly form-encoded: trx_id, status, status_code,
     sid, reference_id, amount) and adjust parse_webhook() accordingly.
  4. Remove the NotConfigured guards once verified end-to-end in
     sandbox mode.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, Request, status

IPAYMU_VA = os.getenv("IPAYMU_VA")
IPAYMU_API_KEY = os.getenv("IPAYMU_API_KEY")
IPAYMU_MODE = os.getenv("IPAYMU_MODE", "sandbox")  # "sandbox" or "production"

_BASE_URLS = {
    "sandbox": "https://sandbox.ipaymu.com/api/v2",
    "production": "https://my.ipaymu.com/api/v2",
}


class IpaymuNotConfigured(HTTPException):
    def __init__(self, detail: str = "Payment gateway (iPaymu) belum dikonfigurasi. Isi IPAYMU_VA dan IPAYMU_API_KEY setelah verifikasi merchant selesai."):
        super().__init__(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


@dataclass
class PembayaranResult:
    checkout_url: str
    gateway_ref: str


def _is_configured() -> bool:
    return bool(IPAYMU_VA and IPAYMU_API_KEY)


def _sign(method: str, body: dict) -> tuple[str, str]:
    """Returns (signature, timestamp). See module docstring -- NOT verified
    against a live iPaymu account yet."""
    body_json = json.dumps(body, separators=(",", ":"))
    body_hash = hashlib.sha256(body_json.encode()).hexdigest()
    string_to_sign = f"{method}:{IPAYMU_VA}:{body_hash}:{IPAYMU_API_KEY}"
    signature = hmac.new(IPAYMU_API_KEY.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return signature, timestamp


async def create_payment(
    *,
    pesanan_id: str,
    total: str,
    nama_pembeli: str,
    email_pembeli: str,
    notify_url: str,
    return_url: str,
) -> PembayaranResult:
    """Create a redirect-payment session for an order.

    NOT IMPLEMENTED against the real API yet -- raises IpaymuNotConfigured
    until IPAYMU_VA/IPAYMU_API_KEY are set. See module docstring for what's
    left to verify before wiring the actual HTTP call.
    """
    if not _is_configured():
        raise IpaymuNotConfigured()

    # Deliberately not making the real HTTP call yet -- the request/response
    # shape below is unverified (see module docstring). Wire this up with
    # httpx.AsyncClient().post(f"{_BASE_URLS[IPAYMU_MODE]}/payment", ...)
    # once you have a sandbox key to test against.
    raise NotImplementedError(
        "iPaymu create_payment() has a signature helper but the actual HTTP call is "
        "not wired up -- verify the request shape against current docs first."
    )


def verify_webhook_signature(request: Request, raw_body: bytes) -> bool:
    """Placeholder: iPaymu's notify callback does not sign its POST body the
    same way as outgoing requests (per public docs, notifyUrl payloads are
    typically unsigned form posts validated by re-querying the transaction
    status via the API instead). Confirm this against current docs before
    trusting any webhook payload in production -- until then, treat this as
    NOT a security boundary."""
    return False


def parse_webhook(form_data: dict) -> dict:
    """Best-effort mapping of the commonly documented iPaymu notify fields.
    UNVERIFIED -- confirm field names against a real sandbox transaction
    before relying on this."""
    return {
        "gateway_ref": form_data.get("trx_id") or form_data.get("reference_id"),
        "status_mentah": form_data.get("status"),
        "amount": form_data.get("amount"),
    }
