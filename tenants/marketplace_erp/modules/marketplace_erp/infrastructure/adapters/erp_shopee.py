"""Shopee Open Platform v2 adapter for marketplace_erp.

Real structure shipped in Tahap 2:
  - OAuth authorize URL generation (HMAC-SHA256 sign)
  - Access-token exchange + refresh helpers (signed request)
  - Signed request helper for future pull/push calls
  - Token persistence onto AkunMarketplace (shop-level)

Live catalog/order sync stays behind ``SHOPEE_LIVE_SYNC=true`` plus
``SHOPEE_PARTNER_ID`` / ``SHOPEE_PARTNER_KEY``. Without those, sync_* and
proses_pesanan raise 501 (honest, not fake-success). Partner key never
lives in the repo or DB -- env only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException, status

SHOPEE_PARTNER_ID = os.getenv("SHOPEE_PARTNER_ID", "").strip()
SHOPEE_PARTNER_KEY = os.getenv("SHOPEE_PARTNER_KEY", "").strip()
SHOPEE_REDIRECT_URI = os.getenv("SHOPEE_REDIRECT_URI", "").strip()
# "sandbox" -> Open Platform v2 sandbox host (Test Account-Sandbox v2); else production host
SHOPEE_ENV = (os.getenv("SHOPEE_ENV", "sandbox") or "sandbox").strip().lower()
# Gate for live pull/push (still needs partner + shop tokens).
SHOPEE_LIVE_SYNC = (os.getenv("SHOPEE_LIVE_SYNC", "") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

_PATH_AUTH_PARTNER = "/api/v2/shop/auth_partner"
_PATH_TOKEN_GET = "/api/v2/auth/token/get"
_PATH_TOKEN_REFRESH = "/api/v2/auth/access_token/get"


class ShopeeNotConfigured(HTTPException):
    def __init__(
        self,
        detail: str = (
            "Integrasi Shopee belum dikonfigurasi. "
            "Isi SHOPEE_PARTNER_ID/SHOPEE_PARTNER_KEY setelah aplikasi partner disetujui."
        ),
    ):
        super().__init__(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=detail)


def partner_configured() -> bool:
    return bool(SHOPEE_PARTNER_ID and SHOPEE_PARTNER_KEY)


def live_sync_enabled() -> bool:
    return SHOPEE_LIVE_SYNC and partner_configured()


def _host() -> str:
    if SHOPEE_ENV in {"live", "production", "prod"}:
        return "https://partner.shopeemobile.com"
    return "https://openplatform.sandbox.test-stable.shopee.sg"


def _partner_id_int() -> int:
    try:
        return int(SHOPEE_PARTNER_ID)
    except (TypeError, ValueError) as exc:
        raise ShopeeNotConfigured("SHOPEE_PARTNER_ID harus angka") from exc


def sign_request(api_path: str, timestamp: int, *, access_token: str | None = None, shop_id: str | None = None) -> str:
    """HMAC-SHA256 base string: partner_id + path + timestamp [+ access_token + shop_id].

    Token get/refresh omit access_token/shop_id per Shopee v2 docs.
    """
    if not partner_configured():
        raise ShopeeNotConfigured()
    base = f"{SHOPEE_PARTNER_ID}{api_path}{timestamp}"
    if access_token is not None and shop_id is not None:
        base = f"{base}{access_token}{shop_id}"
    return hmac.new(
        SHOPEE_PARTNER_KEY.encode("utf-8"),
        base.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_authorize_url(*, redirect_uri: str | None = None) -> str:
    """Build the shop authorization URL (valid ~5 minutes via timestamp)."""
    if not partner_configured():
        raise ShopeeNotConfigured()
    redirect = (redirect_uri or SHOPEE_REDIRECT_URI or "").strip()
    if not redirect:
        raise ShopeeNotConfigured(
            "SHOPEE_REDIRECT_URI belum diisi (atau kirim redirect_uri pada start OAuth)."
        )
    ts = int(time.time())
    sign = sign_request(_PATH_AUTH_PARTNER, ts)
    query = urlencode(
        {
            "partner_id": _partner_id_int(),
            "timestamp": ts,
            "sign": sign,
            "redirect": redirect,
        }
    )
    return f"{_host()}{_PATH_AUTH_PARTNER}?{query}"


def _akun_configured(akun: Any) -> bool:
    return bool(getattr(akun, "access_token", None) and getattr(akun, "id_toko_eksternal", None))


async def _http_post_json(url: str, body: dict, *, timeout: float = 25.0) -> dict:
    """Minimal async-friendly POST via thread offload (no new HTTP dep)."""
    import asyncio

    import requests

    def _do() -> dict:
        resp = requests.post(url, json=body, timeout=timeout)
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Shopee response bukan JSON (HTTP {resp.status_code})",
            ) from exc
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Shopee HTTP {resp.status_code}: {data}",
            )
        return data

    return await asyncio.to_thread(_do)


async def exchange_token(
    *, code: str, shop_id: str | None = None, main_account_id: str | None = None
) -> dict[str, Any]:
    """POST /api/v2/auth/token/get -- exchange OAuth code for tokens.

    Pass exactly one of shop_id (authorised from a shop account) or main_account_id (authorised from
    a main account; the response then carries shop_id_list). Returns dict with access_token,
    refresh_token, expire_in, ... Caller persists onto AkunMarketplace.
    """
    if bool(shop_id) == bool(main_account_id):
        raise ValueError("exchange_token needs exactly one of shop_id / main_account_id")
    if not partner_configured():
        raise ShopeeNotConfigured()
    ts = int(time.time())
    sign = sign_request(_PATH_TOKEN_GET, ts)
    url = (
        f"{_host()}{_PATH_TOKEN_GET}"
        f"?partner_id={_partner_id_int()}&timestamp={ts}&sign={sign}"
    )
    body: dict[str, Any] = {"code": code, "partner_id": _partner_id_int()}
    if main_account_id:
        body["main_account_id"] = int(main_account_id) if str(main_account_id).isdigit() else main_account_id
    else:
        body["shop_id"] = int(shop_id) if str(shop_id).isdigit() else shop_id
    data = await _http_post_json(url, body)
    # Shopee wraps errors as error/message even on HTTP 200.
    if data.get("error"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Shopee token exchange gagal: {data.get('error')} {data.get('message', '')}".strip(),
        )
    return data.get("response") or data


async def refresh_access_token(*, refresh_token: str, shop_id: str) -> dict[str, Any]:
    if not partner_configured():
        raise ShopeeNotConfigured()
    ts = int(time.time())
    sign = sign_request(_PATH_TOKEN_REFRESH, ts)
    url = (
        f"{_host()}{_PATH_TOKEN_REFRESH}"
        f"?partner_id={_partner_id_int()}&timestamp={ts}&sign={sign}"
    )
    body = {
        "refresh_token": refresh_token,
        "partner_id": _partner_id_int(),
        "shop_id": int(shop_id) if str(shop_id).isdigit() else shop_id,
    }
    data = await _http_post_json(url, body)
    if data.get("error"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Shopee refresh gagal: {data.get('error')} {data.get('message', '')}".strip(),
        )
    return data.get("response") or data


def apply_token_payload(akun: Any, payload: dict[str, Any], *, shop_id: str | None = None) -> None:
    """Write token fields onto an AkunMarketplace-like object (no commit)."""
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    expire_in = payload.get("expire_in") or payload.get("expireIn")
    if not access:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Shopee response tanpa access_token",
        )
    akun.access_token = access
    if refresh:
        akun.refresh_token = refresh
    sid = shop_id or payload.get("shop_id") or payload.get("shopid")
    if sid is not None:
        akun.id_toko_eksternal = str(sid)
    if expire_in is not None:
        try:
            akun.token_kedaluwarsa = datetime.now(timezone.utc) + timedelta(seconds=int(expire_in))
        except (TypeError, ValueError):
            pass
    akun.status = "terhubung"


# Access token lives 4h; refresh a bit early so a call never starts with a token about to die.
TOKEN_REFRESH_MARGIN = timedelta(minutes=10)


def token_perlu_refresh(akun: Any, *, now: datetime | None = None) -> bool:
    expiry = getattr(akun, "token_kedaluwarsa", None)
    if expiry is None:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry - (now or datetime.now(timezone.utc)) <= TOKEN_REFRESH_MARGIN


async def pastikan_token_segar(session: Any, akun: Any) -> None:
    """Refresh the shop token when it is (about to be) expired and commit it right away.

    Shopee refresh tokens are single-use: the response carries a *new* refresh_token and the old
    one dies. If the new pair were only saved when the surrounding request finishes, any later
    error would roll it back and leave the shop with a dead token, so it is committed here.
    The row is locked first so two concurrent requests cannot both spend the same refresh_token.
    """
    if not token_perlu_refresh(akun):
        return
    await session.refresh(akun, with_for_update=True)
    if not token_perlu_refresh(akun):
        return  # another request refreshed it while we waited for the lock
    if not getattr(akun, "refresh_token", None):
        akun.status = "token_kadaluarsa"
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Token Shopee kedaluwarsa dan tidak ada refresh token. Hubungkan ulang toko.",
        )
    try:
        payload = await refresh_access_token(
            refresh_token=akun.refresh_token, shop_id=str(akun.id_toko_eksternal)
        )
        apply_token_payload(akun, payload, shop_id=str(akun.id_toko_eksternal))
    except HTTPException:
        akun.status = "token_kadaluarsa"
        await session.commit()
        raise
    await session.commit()


async def signed_shop_request(
    session: Any,
    akun: Any,
    api_path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    params: dict | None = None,
) -> dict:
    """Generic signed shop call. Used by sync_* once LIVE_SYNC is on."""
    if not live_sync_enabled():
        raise ShopeeNotConfigured(
            "Shopee live sync nonaktif. Set SHOPEE_LIVE_SYNC=true dan partner credentials."
        )
    if not _akun_configured(akun):
        raise ShopeeNotConfigured("Akun Shopee belum punya access_token / id_toko_eksternal.")
    await pastikan_token_segar(session, akun)

    return await _call_shop_api(
        access_token=str(akun.access_token),
        shop_id=str(akun.id_toko_eksternal),
        api_path=api_path,
        method=method,
        body=body,
        params=params,
    )


async def _call_shop_api(
    *,
    access_token: str,
    shop_id: str,
    api_path: str,
    method: str = "GET",
    body: dict | None = None,
    params: dict | None = None,
) -> dict:
    """Signed shop-level call with an explicit token (no AkunMarketplace row needed)."""
    import asyncio

    import requests

    ts = int(time.time())
    sign = sign_request(api_path, ts, access_token=access_token, shop_id=shop_id)
    query = {
        "partner_id": _partner_id_int(),
        "timestamp": ts,
        "sign": sign,
        "access_token": access_token,
        "shop_id": int(shop_id) if shop_id.isdigit() else shop_id,
    }
    if params:
        query.update(params)
    url = f"{_host()}{api_path}?{urlencode(query)}"

    def _do() -> dict:
        if method.upper() == "GET":
            resp = requests.get(url, timeout=25)
        else:
            resp = requests.post(url, json=body or {}, timeout=25)
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Shopee response bukan JSON (HTTP {resp.status_code})",
            ) from exc

    data = await asyncio.to_thread(_do)
    # Shopee reports failures as error/message, often with HTTP 200.
    if data.get("error"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Shopee {api_path} gagal: {data.get('error')} {data.get('message', '')}".strip(),
        )
    return data


async def get_shop_name(access_token: str, shop_id: str) -> str | None:
    """Best-effort shop name (get_shop_info). Never raises: a missing name must not block connecting."""
    try:
        data = await _call_shop_api(access_token=access_token, shop_id=shop_id, api_path="/api/v2/shop/get_shop_info")
    except Exception:  # noqa: BLE001
        return None
    return (data.get("shop_name") or "").strip() or None


async def sync_produk(akun: Any) -> list[dict]:
    if not live_sync_enabled() or not _akun_configured(akun):
        raise ShopeeNotConfigured(
            "Shopee live sync nonaktif atau akun belum terhubung. "
            "Set SHOPEE_LIVE_SYNC=true + partner key, selesaikan OAuth dulu."
        )
    raise NotImplementedError(
        "Shopee sync_produk() menunggu mapping GetItemList -- hidupkan setelah sandbox verified."
    )


_PATH_ORDER_LIST = "/api/v2/order/get_order_list"
_PATH_ORDER_DETAIL = "/api/v2/order/get_order_detail"
# item_list / buyer_username / total_amount are not returned unless asked for.
_ORDER_DETAIL_FIELDS = "buyer_username,item_list,total_amount"
# Shopee rejects a time_from..time_to span over 15 days; stay a minute under.
_ORDER_WINDOW_SECONDS = 15 * 24 * 3600 - 60
_ORDER_LIST_PAGE_SIZE = 100
_ORDER_DETAIL_BATCH = 50
_ORDER_LIST_MAX_PAGES = 20


def normalisasi_pesanan(order: dict) -> dict:
    """Shopee get_order_detail entry -> neutral dict consumed by services.impor_pesanan_marketplace.

    ``status`` is None for a Shopee status we do not map, so the importer skips it instead of
    guessing (map_shopee_status would silently turn an unknown state into 'unpaid').
    """
    raw = str(order.get("order_status") or "").upper()
    items = []
    for it in order.get("item_list") or []:
        qty = int(it.get("model_quantity_purchased") or 0)
        if qty <= 0:
            continue
        nama = str(it.get("item_name") or "").strip()
        model = str(it.get("model_name") or "").strip()
        price = it.get("model_discounted_price") or it.get("model_original_price") or 0
        items.append(
            {
                "nama_produk": (f"{nama} - {model}" if model else nama)[:255] or "(tanpa nama)",
                "harga_satuan": Decimal(str(price)),
                "qty": qty,
                "id_eksternal_kandidat": [str(it[k]) for k in ("item_id", "model_id") if it.get(k)],
            }
        )
    return {
        "id_eksternal": str(order["order_sn"]),
        "status": SHOPEE_STATUS_MAP.get(raw),
        "status_mentah": raw,
        "nama_pembeli": str(order.get("buyer_username") or ""),
        "total": Decimal(str(order.get("total_amount") or 0)),
        "items": items,
    }


async def sync_pesanan(session: Any, akun: Any) -> list[dict]:
    """Pull orders updated in the last ~15 days (get_order_list, then get_order_detail in batches).

    Idempotent by design: the caller upserts on (platform, order_sn), so re-pulling the same window
    is harmless. Returns normalised dicts, see normalisasi_pesanan().
    """
    if not live_sync_enabled() or not _akun_configured(akun):
        raise ShopeeNotConfigured(
            "Shopee live sync nonaktif atau akun belum terhubung."
        )
    time_to = int(time.time())
    time_from = time_to - _ORDER_WINDOW_SECONDS

    order_sn_list: list[str] = []
    cursor = ""
    for _ in range(_ORDER_LIST_MAX_PAGES):
        params: dict[str, Any] = {
            "time_range_field": "update_time",
            "time_from": time_from,
            "time_to": time_to,
            "page_size": _ORDER_LIST_PAGE_SIZE,
        }
        if cursor:
            params["cursor"] = cursor
        data = await signed_shop_request(session, akun, _PATH_ORDER_LIST, params=params)
        resp = data.get("response") or {}
        order_sn_list += [o["order_sn"] for o in resp.get("order_list") or []]
        cursor = resp.get("next_cursor") or ""
        if not resp.get("more") or not cursor:
            break

    rows: list[dict] = []
    for i in range(0, len(order_sn_list), _ORDER_DETAIL_BATCH):
        batch = order_sn_list[i : i + _ORDER_DETAIL_BATCH]
        data = await signed_shop_request(
            session,
            akun,
            _PATH_ORDER_DETAIL,
            params={"order_sn_list": ",".join(batch), "response_optional_fields": _ORDER_DETAIL_FIELDS},
        )
        rows += [normalisasi_pesanan(o) for o in (data.get("response") or {}).get("order_list") or []]
    return rows


async def proses_pesanan(akun: Any, pesanan: Any) -> None:
    """Push local to_ship acknowledgement. Stub until live sync + SetOrderReadyToShip."""
    if not live_sync_enabled() or not _akun_configured(akun):
        raise ShopeeNotConfigured(
            "Shopee live sync nonaktif atau akun belum terhubung -- push to_ship ditunda."
        )
    raise NotImplementedError(
        "Shopee proses_pesanan() belum diimplementasikan -- verifikasi endpoint ship dulu."
    )


# Status map used when pull/webhook lands (documented for adapters).
SHOPEE_STATUS_MAP = {
    "UNPAID": "unpaid",
    "READY_TO_SHIP": "to_ship",
    "PROCESSED": "to_ship",
    "RETRY_SHIP": "to_ship",
    "SHIPPED": "shipped",
    "TO_CONFIRM_RECEIVE": "shipped",
    "COMPLETED": "completed",
    "CANCELLED": "cancelled",
    "IN_CANCEL": "cancelled",
}


def map_shopee_status(raw: str) -> str:
    return SHOPEE_STATUS_MAP.get((raw or "").upper(), "unpaid")


def debug_sign_fingerprint() -> str:
    """Non-secret fingerprint for health checks (never returns the key)."""
    if not partner_configured():
        return "unconfigured"
    digest = hashlib.sha256(f"{SHOPEE_PARTNER_ID}:{SHOPEE_PARTNER_KEY[:4]}".encode()).hexdigest()[:12]
    return f"configured:{digest}"


# Silence unused import warning for json in case future body dumps need it
_ = json
