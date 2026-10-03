"""iPaymu payment gateway adapter (v2 API, "redirect payment" = the iPaymu payment page).

Environment:
  IPAYMU_VA       merchant VA number (Integrasi menu of the iPaymu dashboard)
  IPAYMU_API_KEY  merchant API key (same place) -- a secret: set it only in the environment
  IPAYMU_MODE     "sandbox" (default) or "production"; the two modes have different VA / API keys
  IPAYMU_PROXY_URL  optional: send iPaymu calls through a fixed-IP proxy (see shared/egress.py)

Until IPAYMU_VA and IPAYMU_API_KEY are both set, every entry point answers HTTP 501 instead of faking a payment,
and the webhook is never trusted.

Security model of the webhook: a notify POST is unauthenticated input. We never act on its content. Instead we
take the transaction id from it and ask iPaymu itself (signed "check transaction" call) what the transaction is,
and only the answer of that call decides whether an order is paid. The call also returns the reference id and the
amount, so a real but unrelated transaction cannot be used to pay for another order.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from fastapi import HTTPException, status

from shared.egress import proxies_for

logger = logging.getLogger(__name__)

# "Not available yet" is answered with 501, never 503: on DigitalOcean App Platform the edge replaces
# an application 503 with its own HTML 504 page, so the client never sees our JSON message.

_BASE_URLS = {
    "sandbox": "https://sandbox.ipaymu.com/api/v2",
    "production": "https://my.ipaymu.com/api/v2",
}
_TIMEOUT = 20
# Transaction Status values iPaymu documents as "paid": 1 = berhasil, 6 = settled/berhasil (unsettled), 7 = escrow.
_STATUS_LUNAS = {1, 6, 7}


class IpaymuNotReady(HTTPException):
    def __init__(self, detail: str = "Pembayaran (iPaymu) belum aktif. Menunggu verifikasi merchant dan integrasi API."):
        super().__init__(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=detail)


@dataclass
class ItemBayar:
    nama: str
    qty: int
    harga: str


@dataclass
class PembayaranResult:
    checkout_url: str
    gateway_ref: str


@dataclass
class NotifTerverifikasi:
    """What iPaymu itself says about a transaction (never what the notify POST claimed)."""

    reference_id: str
    transaction_id: str
    jumlah: Decimal
    lunas: bool


def is_configured() -> bool:
    return bool(os.getenv("IPAYMU_VA") and os.getenv("IPAYMU_API_KEY"))


def _mode() -> str:
    mode = (os.getenv("IPAYMU_MODE") or "sandbox").strip().lower()
    return mode if mode in _BASE_URLS else "sandbox"


def _credentials() -> tuple[str, str]:
    va, key = os.getenv("IPAYMU_VA"), os.getenv("IPAYMU_API_KEY")
    if not va or not key:
        raise IpaymuNotReady()
    return va.strip(), key.strip()


def buat_signature(va: str, api_key: str, body_json: str, method: str = "POST") -> str:
    """v2 signature: HMAC_SHA256(apiKey, "METHOD:VA:sha256(body):apiKey"), hex, lower case."""
    body_hash = hashlib.sha256(body_json.encode()).hexdigest()
    to_sign = f"{method}:{va}:{body_hash}:{api_key}"
    return hmac.new(api_key.encode(), to_sign.encode(), hashlib.sha256).hexdigest().lower()


def _post_sync(path: str, body: dict[str, Any]) -> dict[str, Any]:
    va, key = _credentials()
    body_json = json.dumps(body, separators=(",", ":"))  # the signature is over exactly these bytes
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "va": va,
        "signature": buat_signature(va, key, body_json),
        "timestamp": datetime.now().strftime("%Y%m%d%H%M%S"),
    }
    resp = requests.post(
        f"{_BASE_URLS[_mode()]}{path}", data=body_json, headers=headers, timeout=_TIMEOUT, proxies=proxies_for("IPAYMU_PROXY_URL")
    )
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200 or not isinstance(data, dict) or data.get("Status") not in (200, None):
        # The cause goes to the log (no secrets in it); the client only gets a calm message.
        logger.warning("iPaymu %s failed: http=%s status=%s message=%s", path, resp.status_code, data.get("Status") if isinstance(data, dict) else None, data.get("Message") if isinstance(data, dict) else None)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Pembayaran belum bisa dibuat. Coba lagi sebentar lagi.",
        )
    return data


async def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_post_sync, path, body)


def _pick(data: dict[str, Any], *names: str) -> Any:
    """First present key, matched case-insensitively (the API mixes CamelCase and snake_case)."""
    lowered = {str(k).lower(): v for k, v in data.items()}
    for n in names:
        if n.lower() in lowered and lowered[n.lower()] not in (None, ""):
            return lowered[n.lower()]
    return None


async def create_payment(
    *,
    pesanan_id: str,
    items: list[ItemBayar],
    total: str,
    nama_pembeli: str,
    email_pembeli: str,
    telepon_pembeli: str = "",
    notify_url: str,
    return_url: str,
    cancel_url: str,
) -> PembayaranResult:
    """Create an iPaymu payment-page session for one order; the buyer is sent to ``checkout_url``."""
    _credentials()  # 501 before doing any work when not configured
    produk = [i.nama for i in items]
    qty = [str(i.qty) for i in items]
    harga = [str(Decimal(i.harga).quantize(Decimal("1"))) for i in items]
    jumlah_item = sum(Decimal(h) * int(q) for h, q in zip(harga, qty, strict=True))
    selisih = Decimal(total) - jumlah_item
    if selisih > 0:  # e.g. shipping cost: shown as its own line so the lines add up to the amount charged
        produk.append("Ongkos kirim")
        qty.append("1")
        harga.append(str(selisih.quantize(Decimal("1"))))
    body: dict[str, Any] = {
        "product": produk,
        "qty": qty,
        "price": harga,
        "amount": str(Decimal(total).quantize(Decimal("1"))),
        "returnUrl": return_url,
        "cancelUrl": cancel_url,
        "notifyUrl": notify_url,
        "referenceId": pesanan_id,
        "buyerName": nama_pembeli,
        "buyerEmail": email_pembeli,
    }
    if telepon_pembeli:
        body["buyerPhone"] = telepon_pembeli
    data = await _post("/payment", body)
    inner = data.get("Data") if isinstance(data.get("Data"), dict) else {}
    url, sid = _pick(inner, "Url"), _pick(inner, "SessionID", "SessionId")
    if not url or not sid:
        logger.warning("iPaymu /payment answered without Url/SessionID")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Pembayaran belum bisa dibuat. Coba lagi sebentar lagi.")
    return PembayaranResult(checkout_url=str(url), gateway_ref=str(sid))


async def verify_webhook(payload: dict[str, Any]) -> NotifTerverifikasi | None:
    """Ask iPaymu about the transaction named in a notify POST. None = cannot be verified (never trust it)."""
    if not is_configured():
        return None
    trx = _pick(payload, "trx_id", "transaction_id", "transactionId")
    if trx is None:
        return None
    try:
        data = await _post("/transaction", {"transactionId": str(trx)})
    except HTTPException:
        return None
    inner = data.get("Data") if isinstance(data.get("Data"), dict) else None
    if not inner:
        return None
    reference = _pick(inner, "ReferenceId", "reference_id")
    status_code = _pick(inner, "Status", "StatusCode")
    jumlah_raw = _pick(inner, "SubTotal", "sub_total", "Amount", "Total")
    if reference is None or jumlah_raw is None:
        return None
    try:
        jumlah = Decimal(str(jumlah_raw))
        lunas = int(status_code) in _STATUS_LUNAS
    except (InvalidOperation, TypeError, ValueError):
        return None
    return NotifTerverifikasi(reference_id=str(reference), transaction_id=str(trx), jumlah=jumlah, lunas=lunas)
