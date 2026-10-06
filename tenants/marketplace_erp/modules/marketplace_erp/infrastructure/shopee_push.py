"""Shopee Push Mechanism (webhook): Shopee POSTs an event to our URL instead of us asking for changes.

Facts from the Open Platform docs (push category + each push's page): every push is JSON
``{"data": {...}, "shop_id": N, "code": C, "timestamp": T}``; Shopee waits only 3 seconds for the answer and retries
after 5 min, 30 min and 3 h when it does not get a success; order_status_push is code 3, order_trackingno_push 4,
shop authorization 1, authorization cancelled 2, authorization expiry 12, return updates 29.

The Authorization header is the HMAC-SHA256 (hex) of ``<callback url>|<raw body>`` with the App partner key
(Open Platform developer guide 18). A separate SHOPEE_PUSH_KEY is accepted too when set. The raw body is used
as received; it is not re-serialized. Every attempt is logged (see ``diagnosis``).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ShopeePushReceipt
from typing import Any

# URL yang lolos Verify di Open Platform. Env SHOPEE_PUSH_URL menimpa ini bila diisi.
CALLBACK_URL = "https://api.ampelkuning.com/api/marketplace-erp/shopee/push"
# Jenis yang ERP terima: otorisasi, batal otorisasi, status pesanan, resi, masa berlaku otorisasi.
PUSH_AKTIF = [1, 2, 3, 4, 12]

JENIS_PUSH = {
    1: "shop_authorization",
    2: "shop_authorization_canceled",
    3: "order_status",
    4: "order_trackingno",
    12: "open_api_authorization_expiry",
    29: "return_updates",
}
# Pushes that mean "an order changed": the ERP pulls that shop's changes right away.
KODE_PESANAN = frozenset({3, 4})


def push_key() -> str:
    return os.getenv("SHOPEE_PUSH_KEY", "").strip()


def tanda_tangan(key: str, url: str, body: bytes) -> str:
    return hmac.new(key.encode(), url.encode() + b"|" + body, hashlib.sha256).hexdigest()


def kandidat_url(url_terlihat: str, header: dict[str, str]) -> list[str]:
    """The callback URL as Shopee has it: the configured one first, then what the request looked like
    (behind the platform's proxy the scheme/host may differ from what was typed into Open Platform)."""
    return [os.getenv("SHOPEE_PUSH_URL", "").strip() or CALLBACK_URL]


def sidik_jari_kunci(key: str) -> dict[str, Any]:
    return {"sha256": hashlib.sha256(key.encode()).hexdigest()[:12]}


def verifikasi(
    key: str, urls: list[str], body: bytes, authorization: str | None, kunci_lain: dict[str, str] | None = None
) -> tuple[bool, dict[str, Any]]:
    """Only provider HMAC-SHA256 of exact configured callback URL + '|' + raw body.

    Prefer the explicit push key; use the partner key only when no push key is
    configured. Never accept body-only/no-delimiter/hex-decoded key variants.
    """
    chosen = key or (kunci_lain or {}).get("partner_key", "")
    diagnosis: dict[str, Any] = {"ada_key": bool(chosen), "ada_authorization": bool(authorization), "url_dicoba": urls[:1]}
    if chosen:
        diagnosis["kunci_server"] = sidik_jari_kunci(chosen)
    if not chosen or not authorization or not urls:
        return False, diagnosis
    received = authorization.strip().lower()
    diagnosis["diterima"] = received[:8]
    if not re.fullmatch(r"[0-9a-f]{64}", received):
        return False, diagnosis
    expected = tanda_tangan(chosen, urls[0], body)
    diagnosis["hitung"] = {urls[0]: expected[:8]}
    valid = hmac.compare_digest(expected, received)
    if valid:
        diagnosis["cocok"] = f"url|badan  [{urls[0]}]"
    return valid, diagnosis


# Accommodates the provider's documented 5 min / 30 min / 3 h retry sequence.
REPLAY_WINDOW = timedelta(hours=4)
FUTURE_SKEW = timedelta(minutes=5)


async def claim_push(session, body: bytes, push: dict) -> bool:
    """Reject stale events; acknowledge identical retries without a second pull.

    The receipt's unique primary key handles concurrent arrivals and survives
    restarts independently from the bounded diagnostic log.
    """
    stamp = push.get("timestamp")
    now = datetime.now(timezone.utc)
    if type(stamp) is not int:
        raise ValueError("Missing push timestamp")
    try:
        sent = datetime.fromtimestamp(stamp, timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("Invalid push timestamp") from exc
    if sent < now - REPLAY_WINDOW or sent > now + FUTURE_SKEW:
        raise ValueError("Push outside replay window")
    await session.execute(delete(ShopeePushReceipt).where(ShopeePushReceipt.expires_at < now))
    try:
        async with session.begin_nested():
            session.add(ShopeePushReceipt(
                fingerprint=hashlib.sha256(body).hexdigest(),
                expires_at=sent + REPLAY_WINDOW,
            ))
            await session.flush()
    except IntegrityError:
        return False
    return True


def urai(body: bytes) -> dict[str, Any] | None:
    """The push as a dict with at least ``code``; None when it is not a JSON object."""
    try:
        data = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def ringkas(push: dict[str, Any]) -> dict[str, Any]:
    """What the log shows of one push: the kind, the shop and (for orders) the order number and status."""
    data = push.get("data") if isinstance(push.get("data"), dict) else {}
    kode = push.get("code")
    return {
        "kode": kode if isinstance(kode, int) else None,
        "jenis": JENIS_PUSH.get(kode) if isinstance(kode, int) else None,
        "shop_id": str(push["shop_id"]) if push.get("shop_id") is not None else None,
        "order_sn": str(data.get("ordersn") or data.get("order_sn") or "") or None,
        "status": str(data.get("status") or "") or None,
    }
