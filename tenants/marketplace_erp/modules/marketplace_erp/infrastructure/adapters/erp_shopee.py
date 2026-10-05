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
import logging
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException, status

from shared.egress import proxies_for

_log = logging.getLogger(__name__)

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
        resp = requests.post(url, json=body, timeout=timeout, proxies=proxies_for("SHOPEE_PROXY_URL"))
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


_PATH_PUSH_SET = "/api/v2/push/set_app_push_config"
_PATH_PUSH_GET = "/api/v2/push/get_app_push_config"


async def _public_post(path: str, body: dict) -> dict:
    if not partner_configured():
        raise ShopeeNotConfigured()
    ts = int(time.time())
    sign = sign_request(path, ts)
    url = f"{_host()}{path}?partner_id={_partner_id_int()}&timestamp={ts}&sign={sign}"
    data = await _http_post_json(url, body)
    if data.get("error"):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Shopee push config gagal: {data.get('error')} {data.get('message', '')}".strip())
    return data


async def atur_push(*, callback_url: str, nyala: list[int]) -> dict:
    """Save the verified callback URL and turn on the push types the ERP handles (v2.push.set_app_push_config)."""
    return await _public_post(_PATH_PUSH_SET, {"callback_url": callback_url, "set_push_config_on": nyala})


async def baca_push() -> dict:
    if not partner_configured():
        raise ShopeeNotConfigured()
    ts = int(time.time())
    sign = sign_request(_PATH_PUSH_GET, ts)
    url = f"{_host()}{_PATH_PUSH_GET}?partner_id={_partner_id_int()}&timestamp={ts}&sign={sign}"
    import asyncio
    import requests

    def _do() -> dict:
        resp = requests.get(url, timeout=25, proxies=proxies_for("SHOPEE_PROXY_URL"))
        data = resp.json()
        if resp.status_code >= 400 or data.get("error"):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Shopee push config: {data}")
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
    raw: bool = False,
    timeout: float = 25.0,
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
        raw=raw,
        timeout=timeout,
    )



async def iklan_saran(session: Any, akun: Any, item_id: int, kata: str | None = None, bidding: str = "auto") -> dict:
    """Shopee's own recommendations before creating an item-level ad: ROAS target, daily budget, and keywords with
    search volume and suggested bid. Each part is best effort: one that fails becomes a note, not an error."""
    hasil: dict = {"roas": None, "anggaran": None, "kata_kunci": [], "catatan": []}

    async def coba(nama: str, kerja):
        try:
            return await kerja
        except HTTPException as exc:
            hasil["catatan"].append(f"{nama} tidak tersedia: {exc.detail}")
            return None

    roi = await coba(
        "Saran target ROAS",
        signed_shop_request(session, akun, "/api/v2/ads/get_product_recommended_roi_target", params={"reference_id": str(uuid.uuid4()), "item_id": item_id}),
    )
    if roi:
        r = roi.get("response") or {}
        hasil["roas"] = {
            nama: {"nilai": (r.get(kunci) or {}).get("value"), "persentil": (r.get(kunci) or {}).get("percentile")}
            for nama, kunci in (("rendah", "lower_bound"), ("sedang", "exact"), ("tinggi", "upper_bound"))
            if r.get(kunci)
        }
    uang = await coba(
        "Saran anggaran",
        signed_shop_request(
            session, akun, "/api/v2/ads/get_create_product_ad_budget_suggestion",
            method="POST",
            body={
                "reference_id": str(uuid.uuid4()), "product_selection": "manual", "campaign_placement": "all",
                "bidding_method": bidding, "item_id": item_id, **({"enhanced_cpc": "false"} if bidding == "manual" else {}),
            },
        ),
    )
    if uang:
        b = (uang.get("response") or {}).get("budget") or {}
        hasil["anggaran"] = {"min": b.get("min_budget"), "rekomendasi": b.get("recommended_budget"), "maks": b.get("max_budget")}
    params = {"item_id": item_id, **({"input_keyword": kata} if kata else {})}
    kunci = await coba("Saran kata kunci", signed_shop_request(session, akun, "/api/v2/ads/get_recommended_keyword_list", params=params))
    if kunci:
        hasil["kata_kunci"] = [
            {"kata": k.get("keyword"), "skor": k.get("quality_score"), "volume": k.get("search_volume"), "bid": k.get("suggested_bid")}
            for k in (kunci.get("response") or {}).get("suggested_keywords") or []
            if k.get("keyword")
        ][:50]
    return hasil


async def _call_shop_api(
    *,
    access_token: str,
    shop_id: str,
    api_path: str,
    method: str = "GET",
    body: dict | None = None,
    params: dict | None = None,
    raw: bool = False,
    timeout: float = 25.0,
) -> dict:
    """Signed shop-level call with an explicit token (no AkunMarketplace row needed).

    ``raw=True`` is for file downloads: a non-JSON 2xx body comes back as {"_bytes": ...}."""
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
    url = f"{_host()}{api_path}?{urlencode(query, doseq=True)}"

    def _do() -> dict:
        if method.upper() == "GET":
            resp = requests.get(url, timeout=timeout, proxies=proxies_for("SHOPEE_PROXY_URL"))
        else:
            resp = requests.post(url, json=body or {}, timeout=timeout, proxies=proxies_for("SHOPEE_PROXY_URL"))
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            if raw and resp.ok and resp.content:
                return {"_bytes": resp.content}
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Shopee response bukan JSON (HTTP {resp.status_code})",
            ) from exc

    data = await asyncio.to_thread(_do)
    if "_bytes" in data:
        return data
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


# --- Products ------------------------------------------------------------------

_PATH_ITEM_LIST = "/api/v2/product/get_item_list"
_PATH_ITEM_BASE = "/api/v2/product/get_item_base_info"
_PATH_MODEL_LIST = "/api/v2/product/get_model_list"
_PATH_UPDATE_STOCK = "/api/v2/product/update_stock"
_PATH_UPDATE_PRICE = "/api/v2/product/update_price"
_ITEM_LIST_PAGE_SIZE = 100
_ITEM_LIST_MAX_PAGES = 10
_ITEM_BASE_BATCH = 50
_MODEL_CONCURRENCY = 5
# Shopee item_status values pulled into the catalogue (NORMAL = Aktif, UNLIST = Tidak aktif, BANNED = Diblokir,
# REVIEWING = Sedang ditinjau); deleted items (SELLER_DELETE, SHOPEE_DELETE) are not shown in Seller Centre either.
STATUS_KATALOG = ("NORMAL", "UNLIST", "BANNED", "REVIEWING")
_PUSH_BATCH = 50


def kunci_listing(item_id: Any, model_id: Any = None) -> str:
    """ProdukListing.id_eksternal for a Shopee listing: "item_id", or "item_id:model_id" for a variant."""
    return f"{item_id}:{model_id}" if model_id else str(item_id)


def pecah_kunci_listing(kunci: str) -> tuple[int, int]:
    """Inverse of kunci_listing -> (item_id, model_id); model_id 0 means the item has no variants."""
    item, _, model = str(kunci).partition(":")
    return int(item), int(model or 0)


def _harga(price_info: Any) -> Decimal | None:
    info = (price_info or [{}])[0] if isinstance(price_info, list) else (price_info or {})
    value = info.get("current_price") if info.get("current_price") is not None else info.get("original_price")
    return Decimal(str(value)) if value is not None else None


def _stok(stock_info_v2: Any) -> int | None:
    summary = ((stock_info_v2 or {}).get("summary_info")) or {}
    value = summary.get("total_available_stock")
    return int(value) if value is not None else None


def _nama_model(model: dict, tier_variation: list[dict]) -> str:
    names = []
    for tier, idx in zip(tier_variation, model.get("tier_index") or []):
        options = tier.get("option_list") or []
        if 0 <= idx < len(options):
            names.append(str(options[idx].get("option") or ""))
    return " / ".join(n for n in names if n) or f"model {model.get('model_id')}"


def normalisasi_item(item: dict, model_resp: dict | None = None) -> list[dict]:
    """get_item_base_info entry (+ get_model_list response for variant items) -> listing dicts.

    One dict per sellable unit: the item itself, or each of its models. Keys: id_eksternal,
    nama_produk, sku, harga, stok, aktif.
    """
    item_id = item["item_id"]
    nama = str(item.get("item_name") or "").strip() or f"item {item_id}"
    aktif = str(item.get("item_status") or "NORMAL").upper() == "NORMAL"
    if item.get("has_model") and model_resp:
        out = []
        for model in model_resp.get("model") or []:
            if not model.get("model_id"):
                continue
            out.append(
                {
                    "id_eksternal": kunci_listing(item_id, model["model_id"]),
                    "nama_produk": f"{nama} - {_nama_model(model, model_resp.get('tier_variation') or [])}"[:255],
                    "sku": str(model.get("model_sku") or "").strip(),
                    "harga": _harga(model.get("price_info")),
                    "stok": _stok(model.get("stock_info_v2")),
                    "aktif": aktif and str(model.get("model_status") or "MODEL_NORMAL") == "MODEL_NORMAL",
                }
            )
        return out
    return [
        {
            "id_eksternal": kunci_listing(item_id),
            "nama_produk": nama[:255],
            "sku": str(item.get("item_sku") or "").strip(),
            "harga": _harga(item.get("price_info")),
            "stok": _stok(item.get("stock_info_v2")),
            "aktif": aktif,
        }
    ]


def _angka(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal("0")
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _deskripsi(item: dict) -> str:
    """Plain-text description: the ``description`` field, or the text parts of an extended description."""
    teks = str(item.get("description") or "").strip()
    if teks:
        return teks
    fields = (((item.get("description_info") or {}).get("extended_description")) or {}).get("field_list") or []
    return "\n".join(str(f.get("text") or "").strip() for f in fields if f.get("field_type") == "text" and f.get("text"))



def _detail_item(item: dict) -> dict:
    """Field get_item_base_info yang namanya dipakai apa adanya, supaya tidak tertukar."""
    merek = item.get("brand") or {}
    pre = item.get("pre_order") or {}
    atribut = []
    for a in item.get("attribute_list") or []:
        nama = a.get("original_attribute_name") or ""
        nilai = ", ".join(str(v.get("original_value_name") or "") for v in a.get("attribute_value_list") or [] if v.get("original_value_name"))
        if nama:
            atribut.append(f"{nama}: {nilai}" if nilai else nama)
    kurir = [str(x.get("logistic_name") or "") for x in item.get("logistic_info") or [] if x.get("enabled") and x.get("logistic_name")]
    grosir = [f"{w.get('min_count')}-{w.get('max_count')}: {w.get('unit_price')}" for w in item.get("wholesales") or []]
    return {
        "category_id": item.get("category_id"),
        "brand": merek.get("original_brand_name") or "",
        "attribute_list": "; ".join(atribut),
        "create_time": item.get("create_time"),
        "update_time": item.get("update_time"),
        "condition": item.get("condition") or "",
        "is_pre_order": bool(pre.get("is_pre_order")),
        "days_to_ship": pre.get("days_to_ship"),
        "logistic_info": ", ".join(kurir),
        "has_model": bool(item.get("has_model")),
        "has_promotion": bool(item.get("has_promotion")),
        "deboost": bool(item.get("deboost")),
        "item_dangerous": item.get("item_dangerous"),
        "wholesales": "; ".join(grosir),
        "video_info": bool(item.get("video_info")),
        "size_chart": item.get("size_chart") or "",
        "gtin_code": item.get("gtin_code") or "",
    }


def _varian_katalog(model: dict, tier_variation: list[dict], sumbu: str) -> dict:
    """One model (variant) of get_model_list as stored in the catalogue: its place in each tier (tier name + option),
    price (current and original), stock, weight, package size and pre-order. A field Shopee did not send stays None, so
    "not set on this variant" is told apart from zero; Shopee says an unset weight/size falls back to the item's."""
    opsi = []
    foto = None
    for tier, idx in zip(tier_variation, model.get("tier_index") or []):
        pilihan = tier.get("option_list") or []
        if 0 <= idx < len(pilihan):
            opsi.append({"tier": str(tier.get("name") or "").strip(), "opsi": str(pilihan[idx].get("option") or "").strip()})
            if foto is None:
                foto = (pilihan[idx].get("image") or {}).get("image_url") or None
    info = (model.get("price_info") or [{}])[0] if isinstance(model.get("price_info"), list) else (model.get("price_info") or {})
    sekarang, asli = _harga(model.get("price_info")), None
    if info.get("original_price") is not None and Decimal(str(info["original_price"])) > 0:
        asli = Decimal(str(info["original_price"]))
    berat = _angka_atau_none(model.get("weight"))
    dim = model.get("dimension") or {}
    pre = model.get("pre_order")
    return {
        "model_id": str(model.get("model_id")),
        "nama": _nama_model(model, tier_variation),
        "sumbu": sumbu,
        "opsi": opsi,
        "sku": str(model.get("model_sku") or "").strip(),
        "harga": str(_harga(model.get("price_info")) or ""),
        "harga_asli": str(asli) if asli is not None and sekarang is not None and asli > sekarang else None,
        "promo": bool(model.get("has_promotion")),
        "stok": _stok(model.get("stock_info_v2")),
        "berat_gram": int(round(berat * 1000)) if berat else None,
        "panjang_cm": _angka_atau_none(dim.get("package_length")),
        "lebar_cm": _angka_atau_none(dim.get("package_width")),
        "tinggi_cm": _angka_atau_none(dim.get("package_height")),
        "preorder": bool(pre.get("is_pre_order")) if isinstance(pre, dict) else None,
        "hari_kirim": pre.get("days_to_ship") if isinstance(pre, dict) else None,
        "status": str(model.get("model_status") or "") or None,
        "foto": foto,
    }


def _angka_atau_none(v: Any) -> float | None:
    """A positive number, or None for missing / empty / zero (Shopee sends 0 or "" for a value that is not set)."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def normalisasi_katalog(item: dict, model_resp: dict | None = None) -> dict:
    """get_item_base_info entry (+ models) -> one catalogue dict: text, photos, size and variants.

    Shopee reports weight in kg (as a string); it is stored in grams.
    """
    tier = model_resp.get("tier_variation") or model_resp.get("standardise_tier_variation") or [] if model_resp else []
    sumbu = [str(x.get("name") or x.get("variation_name") or "").strip() for x in tier]
    sumbu = [x for x in sumbu if x]
    varian = []
    if item.get("has_model") and model_resp:
        tier_asli = model_resp.get("tier_variation") or []
        for model in model_resp.get("model") or []:
            if model.get("model_id"):
                varian.append(_varian_katalog(model, tier_asli, ", ".join(sumbu)))
    harga_semua = [Decimal(v["harga"]) for v in varian if v["harga"]] or [h for h in [_harga(item.get("price_info"))] if h is not None]
    stok = sum(v["stok"] or 0 for v in varian) if varian else _stok(item.get("stock_info_v2"))
    dim = item.get("dimension") or {}
    return {
        "item_id": str(item["item_id"]),
        "nama": (str(item.get("item_name") or "").strip() or f"item {item['item_id']}")[:255],
        "sku": str(item.get("item_sku") or "").strip(),
        "deskripsi": _deskripsi(item),
        "foto": list(((item.get("image") or {}).get("image_url_list")) or []),
        "varian": varian,
        "sumbu": ", ".join(sumbu),
        "harga_min": min(harga_semua) if harga_semua else None,
        "harga_max": max(harga_semua) if harga_semua else None,
        "stok_shopee": stok,
        "berat_gram": int(_angka(item.get("weight")) * 1000),
        "panjang_cm": _angka(dim.get("package_length")),
        "lebar_cm": _angka(dim.get("package_width")),
        "tinggi_cm": _angka(dim.get("package_height")),
        "status": str(item.get("item_status") or "NORMAL").upper(),
        "detail": _detail_item(item),
    }


async def ambil_item_mentah(session: Any, akun: Any) -> list[tuple[dict, dict | None]]:
    """Pull the shop catalogue (active, unlisted, banned and under-review items, as Seller Centre shows them) as
    (get_item_base_info entry, get_model_list response)."""
    if not live_sync_enabled() or not _akun_configured(akun):
        raise ShopeeNotConfigured(
            "Shopee live sync nonaktif atau akun belum terhubung. "
            "Set SHOPEE_LIVE_SYNC=true + partner key, selesaikan OAuth dulu."
        )
    import asyncio

    item_ids: list[int] = []
    offset = 0
    for _ in range(_ITEM_LIST_MAX_PAGES):
        data = await signed_shop_request(
            session,
            akun,
            _PATH_ITEM_LIST,
            params={
                "offset": offset,
                "page_size": _ITEM_LIST_PAGE_SIZE,
                "item_status": list(STATUS_KATALOG),
            },
        )
        resp = data.get("response") or {}
        item_ids += [int(i["item_id"]) for i in resp.get("item") or []]
        if not resp.get("has_next_page"):
            break
        offset = int(resp.get("next_offset") or 0)

    items: list[dict] = []
    for i in range(0, len(item_ids), _ITEM_BASE_BATCH):
        batch = item_ids[i : i + _ITEM_BASE_BATCH]
        data = await signed_shop_request(
            session, akun, _PATH_ITEM_BASE, params={"item_id_list": ",".join(map(str, batch))}
        )
        items += (data.get("response") or {}).get("item_list") or []

    gate = asyncio.Semaphore(_MODEL_CONCURRENCY)

    async def _models(item: dict) -> dict | None:
        if not item.get("has_model"):
            return None
        async with gate:
            try:
                data = await signed_shop_request(session, akun, _PATH_MODEL_LIST, params={"item_id": item["item_id"]})
            except HTTPException:
                if str(item.get("item_status") or "NORMAL").upper() in ("NORMAL", "UNLIST"):
                    raise
                return None  # a banned / under-review item may have no readable variants: keep the item itself
        return data.get("response") or {}

    model_resps = await asyncio.gather(*(_models(it) for it in items))
    return list(zip(items, model_resps))


async def sync_produk(session: Any, akun: Any) -> list[dict]:
    """The shop catalogue as listing dicts (variants expanded)."""
    return [row for it, mr in await ambil_item_mentah(session, akun) for row in normalisasi_item(it, mr)]


async def kirim_stok_harga(session: Any, akun: Any, rows: list[dict]) -> dict:
    """Push stock and price for listings. ``rows``: dicts with id_eksternal, stok (int), harga (Decimal).

    Overwrites what Shopee currently holds, so callers must only trigger it deliberately.
    One failing item does not stop the rest; failures are reported per listing.
    """
    if not live_sync_enabled() or not _akun_configured(akun):
        raise ShopeeNotConfigured("Shopee live sync nonaktif atau akun belum terhubung.")
    hasil: dict[str, Any] = {"stok_ok": 0, "harga_ok": 0, "gagal": []}
    per_item: dict[int, list[tuple[int, dict]]] = {}
    for row in rows:
        try:
            item_id, model_id = pecah_kunci_listing(row["id_eksternal"])
        except ValueError:
            hasil["gagal"].append({"id_eksternal": row["id_eksternal"], "alasan": "id_eksternal bukan format Shopee"})
            continue
        per_item.setdefault(item_id, []).append((model_id, row))

    def _catat(item_id: int, models: list[tuple[int, dict]], failure_list: list[dict], alasan: str) -> None:
        by_model = {m: r for m, r in models}
        for f in failure_list:
            row = by_model.get(int(f.get("model_id") or 0))
            hasil["gagal"].append(
                {"id_eksternal": (row or {}).get("id_eksternal", kunci_listing(item_id, f.get("model_id"))),
                 "alasan": f"{alasan}: {f.get('failed_reason')}"}
            )

    for item_id, models in per_item.items():
        for i in range(0, len(models), _PUSH_BATCH):
            chunk = models[i : i + _PUSH_BATCH]
            for jalur, body, kunci_ok, alasan in (
                (
                    _PATH_UPDATE_STOCK,
                    {
                        "item_id": item_id,
                        "stock_list": [
                            {"model_id": m, "seller_stock": [{"stock": max(0, int(r["stok"]))}]} for m, r in chunk
                        ],
                    },
                    "stok_ok",
                    "stok",
                ),
                (
                    _PATH_UPDATE_PRICE,
                    {
                        "item_id": item_id,
                        "price_list": [{"model_id": m, "original_price": float(r["harga"])} for m, r in chunk],
                    },
                    "harga_ok",
                    "harga",
                ),
            ):
                try:
                    data = await signed_shop_request(session, akun, jalur, method="POST", body=body)
                except HTTPException as exc:
                    for _m, r in chunk:
                        hasil["gagal"].append({"id_eksternal": r["id_eksternal"], "alasan": f"{alasan}: {exc.detail}"})
                    continue
                resp = data.get("response") or {}
                hasil[kunci_ok] += len(resp.get("success_list") or [])
                _catat(item_id, chunk, resp.get("failure_list") or [], alasan)
    return hasil


_PATH_ORDER_LIST = "/api/v2/order/get_order_list"
_PATH_ORDER_DETAIL = "/api/v2/order/get_order_detail"
# item_list / buyer_username / total_amount are not returned unless asked for.
_ORDER_DETAIL_FIELDS = "buyer_username,item_list,total_amount,shipping_carrier,payment_method,estimated_shipping_fee,actual_shipping_fee,note,pay_time,cancel_by,cancel_reason,buyer_cancel_reason,package_list,recipient_address,cod,ship_by_date"
# Shopee rejects a time_from..time_to span over 15 days; stay a minute under.
_ORDER_WINDOW_SECONDS = 15 * 24 * 3600 - 60
# An incremental pull starts this much before the previous one, so a change that landed while it ran is not missed.
_ORDER_OVERLAP_SECONDS = 10 * 60


def jendela_sinkron_pesanan(dari: datetime | None, sekarang: int) -> tuple[int, int]:
    """(time_from, time_to) for get_order_list: everything changed since ``dari`` (minus the overlap),
    never further back than Shopee's 15-day limit; the whole window when ``dari`` is None."""
    paling_awal = sekarang - _ORDER_WINDOW_SECONDS
    if dari is None:
        return paling_awal, sekarang
    if dari.tzinfo is None:
        dari = dari.replace(tzinfo=timezone.utc)
    return max(int(dari.timestamp()) - _ORDER_OVERLAP_SECONDS, paling_awal), sekarang
_ORDER_LIST_PAGE_SIZE = 100
_ORDER_DETAIL_BATCH = 50
_ORDER_LIST_MAX_PAGES = 20


def _waktu_epoch(value: Any) -> datetime | None:
    """Shopee unix seconds -> aware UTC datetime (None when absent or not a number)."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc) if value else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


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
                "nama_produk": nama[:255] or "(tanpa nama)",
                "model_name": model[:255],
                "item_sku": str(it.get("item_sku") or "")[:128],
                "model_sku": str(it.get("model_sku") or "")[:128],
                "foto": str((it.get("image_info") or {}).get("image_url") or "")[:1024] or None,
                "item_id": str(it["item_id"]) if it.get("item_id") else None,
                "model_id": str(it["model_id"]) if it.get("model_id") else None,
                "harga_satuan": Decimal(str(price)),
                "qty": qty,
                "id_eksternal_kandidat": [
                    k
                    for k in (
                        kunci_listing(it.get("item_id"), it.get("model_id")) if it.get("item_id") else None,
                        str(it["item_id"]) if it.get("item_id") else None,
                    )
                    if k
                ],
            }
        )
    return {
        "id_eksternal": str(order["order_sn"]),
        "status": SHOPEE_STATUS_MAP.get(raw),
        "status_mentah": raw,
        "nama_pembeli": str(order.get("buyer_username") or ""),
        "total": Decimal(str(order.get("total_amount") or 0)),
        "kurir": str(order.get("shipping_carrier") or "").strip() or None,
        "dipesan_at": _waktu_epoch(order.get("create_time")),
        "items": items,
        "detail": {
            "payment_method": order.get("payment_method") or "",
            "currency": order.get("currency") or "",
            "cod": bool(order.get("cod")),
            "days_to_ship": order.get("days_to_ship"),
            "ship_by_date": order.get("ship_by_date"),
            "estimated_shipping_fee": order.get("estimated_shipping_fee"),
            "actual_shipping_fee": order.get("actual_shipping_fee"),
            "note": order.get("note") or "",
            "pay_time": order.get("pay_time"),
            "cancel_by": order.get("cancel_by") or "",
            "cancel_reason": order.get("cancel_reason") or order.get("buyer_cancel_reason") or "",
            "penerima": ((order.get("recipient_address") or {}).get("name") or ""),
            "kota": ((order.get("recipient_address") or {}).get("city") or ""),
            "order_chargeable_weight_gram": order.get("order_chargeable_weight_gram"),
        },
    }


async def sync_pesanan(
    session: Any,
    akun: Any,
    lewati_resi: frozenset[str] | set[str] = frozenset(),
    dari: datetime | None = None,
    lengkapi: list[str] | None = None,
) -> list[dict]:
    """Pull orders updated since ``dari`` (default: the last ~15 days) via get_order_list, then get_order_detail in batches.

    Orders already arranged for shipping get their tracking number (get_tracking_number) too, except
    those in ``lewati_resi`` (already stored), so repeated pulls do not re-ask Shopee for every order.

    Idempotent by design: the caller upserts on (platform, order_sn), so re-pulling the same window
    is harmless. Returns normalised dicts, see normalisasi_pesanan().

    ``lengkapi``: order numbers stored earlier without their real order time that fell out of the 15-day
    window; their detail is fetched by number so the real ``create_time`` (and current status) is filled in.
    """
    if not live_sync_enabled() or not _akun_configured(akun):
        raise ShopeeNotConfigured(
            "Shopee live sync nonaktif atau akun belum terhubung."
        )
    time_from, time_to = jendela_sinkron_pesanan(dari, int(time.time()))

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

    for sn in lengkapi or []:
        if sn not in order_sn_list:
            order_sn_list.append(sn)

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

    import asyncio

    gate = asyncio.Semaphore(_MODEL_CONCURRENCY)

    async def _resi(row: dict) -> None:
        async with gate:
            row["nomor_resi"] = await ambil_nomor_resi(session, akun, row["id_eksternal"])

    await asyncio.gather(
        *(
            _resi(r)
            for r in rows
            if r["status_mentah"] in STATUS_SUDAH_DIPROSES and r["id_eksternal"] not in lewati_resi
        )
    )
    return rows


# --- Payment: what Shopee released (settlement) -------------------------------------

_PATH_ESCROW_LIST = "/api/v2/payment/get_escrow_list"
_PATH_ESCROW_DETAIL = "/api/v2/payment/get_escrow_detail"
_ESCROW_PAGE_SIZE = 100
_ESCROW_MAX_PAGES = 50
# The docs state no limit for release_time_from/to; windows of ~15 days (like the order list) keep each call narrow.
ESCROW_WINDOW_SECONDS = 15 * 24 * 3600 - 60


def _uang(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else 0))
    except Exception:  # noqa: BLE001 - a malformed number from Shopee must not stop a whole sync
        return Decimal("0")


def normalisasi_escrow(order_sn: str, dirilis: datetime | None, payout: Any, detail: dict | None) -> dict:
    """get_escrow_list entry + get_escrow_detail response -> neutral dict for services.simpan_settlement_pesanan.

    ``detail`` is the ``response`` object of get_escrow_detail (``order_income`` inside). Fees are costs
    (positive); ``ongkir`` is final_shipping_fee as Shopee reports it (negative = borne by the seller)."""
    import json

    inc = dict((detail or {}).get("order_income") or {})
    inc.pop("items", None)
    ringkas = {k: v for k, v in inc.items() if not isinstance(v, (list, dict))}
    escrow = inc.get("escrow_amount_after_adjustment", inc.get("escrow_amount"))
    return {
        "order_sn": str(order_sn),
        "dirilis_at": dirilis,
        "jumlah_cair": _uang(payout if payout is not None else escrow),
        "penjualan": _uang(inc.get("order_original_price", inc.get("original_price"))),
        "voucher_penjual": _uang(inc.get("voucher_from_seller")),
        "komisi": _uang(inc.get("commission_fee")),
        "layanan": _uang(inc.get("service_fee")),
        "transaksi": _uang(inc.get("seller_transaction_fee")),
        "ongkir": _uang(inc.get("final_shipping_fee")),
        "subsidi_ongkir": _uang(inc.get("shopee_shipping_rebate")),
        "penyesuaian": _uang(inc.get("total_adjustment_amount")),
        "escrow": _uang(escrow),
        "rincian": json.dumps(ringkas, ensure_ascii=False, default=str),
    }


async def daftar_escrow(session: Any, akun: Any, dari: int, sampai: int) -> list[dict]:
    """Orders whose money was released between two unix times (get_escrow_list, all pages, windows of ~15 days)."""
    keluar: list[dict] = []
    awal = dari
    while awal < sampai:
        akhir = min(awal + ESCROW_WINDOW_SECONDS, sampai)
        for halaman in range(1, _ESCROW_MAX_PAGES + 1):
            data = await signed_shop_request(
                session,
                akun,
                _PATH_ESCROW_LIST,
                params={"release_time_from": awal, "release_time_to": akhir, "page_size": _ESCROW_PAGE_SIZE, "page_no": halaman},
            )
            resp = data.get("response") or {}
            for e in resp.get("escrow_list") or []:
                keluar.append(
                    {"order_sn": str(e["order_sn"]), "payout": e.get("payout_amount"), "dirilis": _waktu_epoch(e.get("escrow_release_time"))}
                )
            if not resp.get("more"):
                break
        awal = akhir
    return keluar


async def sync_settlement(
    session: Any,
    akun: Any,
    dari: int,
    sampai: int,
    sudah: frozenset[str] | set[str] = frozenset(),
    *,
    maks_detail: int = 150,
    batas_detik: float = 40.0,
) -> dict:
    """Pull what Shopee released in [dari, sampai]. Orders in ``sudah`` (already stored) are not read again;
    at most ``maks_detail`` new ones are fetched within ``batas_detik`` seconds, the rest is reported as ``sisa``
    so the next pull continues. Returns {"rows": [...], "ditemukan": n, "sisa": n}."""
    import asyncio

    if not live_sync_enabled() or not _akun_configured(akun):
        raise ShopeeNotConfigured("Shopee live sync nonaktif atau akun belum terhubung.")
    daftar = await daftar_escrow(session, akun, dari, sampai)
    baru = [e for e in {e["order_sn"]: e for e in daftar}.values() if e["order_sn"] not in sudah]
    mulai = time.monotonic()
    gate = asyncio.Semaphore(_MODEL_CONCURRENCY)
    rows: list[dict] = []

    async def _satu(e: dict) -> None:
        async with gate:
            if time.monotonic() - mulai > batas_detik:
                return
            data = await signed_shop_request(session, akun, _PATH_ESCROW_DETAIL, params={"order_sn": e["order_sn"]})
            rows.append(normalisasi_escrow(e["order_sn"], e["dirilis"], e["payout"], data.get("response")))

    await asyncio.gather(*(_satu(e) for e in baru[:maks_detail]))
    return {"rows": rows, "ditemukan": len(daftar), "sisa": len(baru) - len(rows)}


# --- Ads: shop-level performance and balance (v2.ads.*) ---------------------------------

_PATH_ADS_HARIAN = "/api/v2/ads/get_all_cpc_ads_daily_performance"
_PATH_ADS_SALDO = "/api/v2/ads/get_total_balance"
# Shopee: a range may not be longer than 1 month, start may not equal end, and nothing older than 6 months.
ADS_RENTANG_MAKS_HARI = 28
ADS_HARI_MAKS = 180


def _tanggal_ads(nilai: Any) -> "date | None":
    """'17-03-2021' (the format Shopee Ads uses) -> date."""
    try:
        return datetime.strptime(str(nilai), "%d-%m-%Y").date()
    except ValueError:
        return None


def normalisasi_iklan_harian(entry: dict) -> dict | None:
    """One day of get_all_cpc_ads_daily_performance -> neutral dict (None when the date is unreadable)."""
    tanggal = _tanggal_ads(entry.get("date"))
    if tanggal is None:
        return None
    return {
        "tanggal": tanggal,
        "impression": int(entry.get("impression") or 0),
        "clicks": int(entry.get("clicks") or 0),
        "direct_order": int(entry.get("direct_order") or 0),
        "broad_order": int(entry.get("broad_order") or 0),
        "direct_item_sold": int(entry.get("direct_item_sold") or 0),
        "broad_item_sold": int(entry.get("broad_item_sold") or 0),
        "direct_gmv": _uang(entry.get("direct_gmv")),
        "broad_gmv": _uang(entry.get("broad_gmv")),
        "expense": _uang(entry.get("expense")),
    }


def potong_rentang_iklan(dari: "date", sampai: "date") -> list[tuple["date", "date"]]:
    """Windows Shopee accepts: at most ADS_RENTANG_MAKS_HARI days each and never a single day (start != end, so a
    one-day request is widened to the day before)."""
    jendela: list[tuple[date, date]] = []
    awal = dari
    while awal <= sampai:
        akhir = min(awal + timedelta(days=ADS_RENTANG_MAKS_HARI - 1), sampai)
        if akhir == awal:
            awal = awal - timedelta(days=1)
        jendela.append((awal, akhir))
        awal = akhir + timedelta(days=1)
    return jendela


async def sync_iklan_toko(session: Any, akun: Any, dari: "date", sampai: "date") -> dict:
    """Ads performance per day for [dari, sampai] plus the current ads balance. A shop without Shopee Ads (or whose
    token lacks the Ads permission) raises the API error; the caller reports it per shop."""
    if not live_sync_enabled() or not _akun_configured(akun):
        raise ShopeeNotConfigured("Shopee live sync nonaktif atau akun belum terhubung.")
    hari: dict[date, dict] = {}
    for awal, akhir in potong_rentang_iklan(dari, sampai):
        data = await signed_shop_request(
            session,
            akun,
            _PATH_ADS_HARIAN,
            params={"start_date": awal.strftime("%d-%m-%Y"), "end_date": akhir.strftime("%d-%m-%Y")},
        )
        for entry in data.get("response") or []:
            baris = normalisasi_iklan_harian(entry)
            if baris is not None and dari <= baris["tanggal"] <= sampai:
                hari[baris["tanggal"]] = baris
    saldo = None
    data = await signed_shop_request(session, akun, _PATH_ADS_SALDO)
    resp = data.get("response") or {}
    if resp.get("total_balance") is not None:
        saldo = {"saldo": _uang(resp.get("total_balance")), "data_at": _waktu_epoch(resp.get("data_timestamp"))}
    return {"hari": [hari[k] for k in sorted(hari)], "saldo": saldo}


# --- Ads: campaigns of one shop (list, settings, performance) and the validated write actions ---------------

_PATH_ADS_KAMPANYE = "/api/v2/ads/get_product_level_campaign_id_list"
_PATH_ADS_PENGATURAN = "/api/v2/ads/get_product_level_campaign_setting_info"
_PATH_ADS_KINERJA = "/api/v2/ads/get_product_campaign_daily_performance"
_PATH_ADS_UBAH = "/api/v2/ads/edit_manual_product_ads"
_PATH_ADS_KATA_KUNCI = "/api/v2/ads/edit_manual_product_ad_keywords"
_PATH_ADS_BUAT = "/api/v2/ads/create_manual_product_ads"
ADS_KAMPANYE_MAKS = 500
ADS_BATCH = 100  # campaign ids per setting/performance call (Shopee maximum)
AKSI_KAMPANYE = frozenset({"pause", "resume", "stop", "delete", "change_budget", "change_roas_target"})
AKSI_KATA_KUNCI = frozenset({"add", "delete", "restore", "change_bid_price", "change_match_type"})
# A typo guard, not a Shopee rule: one extra zero in a daily budget costs real money.
ADS_ANGGARAN_MAKS = Decimal(os.getenv("ADS_ANGGARAN_HARIAN_MAKS", "1000000"))


def _bagi(daftar: list, ukuran: int) -> list[list]:
    return [daftar[i : i + ukuran] for i in range(0, len(daftar), ukuran)]


def _desimal(nilai: Any) -> Decimal | None:
    try:
        return Decimal(str(nilai))
    except Exception:  # noqa: BLE001 -- anything unreadable is "unknown"
        return None


def normalisasi_kampanye(pengaturan: dict, kinerja: dict | None) -> dict:
    """One campaign: its settings (get_product_level_campaign_setting_info) joined with the summed daily performance
    of the period (get_product_campaign_daily_performance). Missing pieces become None / empty, never an error."""
    info = pengaturan.get("common_info") or {}
    durasi = info.get("campaign_duration") or {}
    manual = pengaturan.get("manual_bidding_info") or {}
    auto = pengaturan.get("auto_bidding_info") or {}
    kata_kunci = [
        {
            "kata": k.get("keyword"),
            "status": k.get("status"),
            "tipe": k.get("match_type"),
            "bid": _desimal(k.get("bid_price_per_click")),
        }
        for k in (manual.get("selected_keywords") or [])
        if k.get("keyword") and k.get("status") != "deleted"
    ]
    jumlah = {"impression": 0, "clicks": 0, "expense": Decimal(0), "direct_order": 0, "direct_gmv": Decimal(0)}
    ada_kinerja = False
    for hari in (kinerja or {}).get("metrics_list") or []:
        ada_kinerja = True
        jumlah["impression"] += int(hari.get("impression") or 0)
        jumlah["clicks"] += int(hari.get("clicks") or 0)
        jumlah["expense"] += _uang(hari.get("expense"))
        jumlah["direct_order"] += int(hari.get("direct_order") or 0)
        jumlah["direct_gmv"] += _uang(hari.get("direct_gmv"))
    anggaran = _desimal(info.get("campaign_budget"))
    return {
        "campaign_id": str(pengaturan.get("campaign_id")),
        "nama": info.get("ad_name") or f"Kampanye {pengaturan.get('campaign_id')}",
        "jenis": info.get("ad_type"),
        "status": info.get("campaign_status"),
        "bidding": info.get("bidding_method"),
        "penempatan": info.get("campaign_placement"),
        "anggaran": anggaran,  # 0 = unlimited (Shopee)
        "mulai": _waktu_epoch(durasi.get("start_time")),
        "selesai": _waktu_epoch(durasi.get("end_time")) if durasi.get("end_time") else None,
        "item_id": [str(i) for i in (info.get("item_id_list") or [])],
        "roas_target": _desimal(auto.get("roas_target")),
        "kata_kunci": kata_kunci,
        "kinerja": (
            {
                "impression": jumlah["impression"],
                "clicks": jumlah["clicks"],
                "expense": jumlah["expense"],
                "direct_order": jumlah["direct_order"],
                "direct_gmv": jumlah["direct_gmv"],
                "roas": (jumlah["direct_gmv"] / jumlah["expense"]) if jumlah["expense"] > 0 else None,
                "ctr": (Decimal(jumlah["clicks"]) / jumlah["impression"]) if jumlah["impression"] else None,
            }
            if ada_kinerja
            else None
        ),
    }


async def daftar_kampanye_iklan(session: Any, akun: Any, hari: int = 7) -> dict:
    """Every product-level campaign of the shop with settings and performance of the last ``hari`` days, plus the
    ads balance. Performance and balance are best effort (a failure becomes a note in ``catatan``)."""
    if not live_sync_enabled() or not _akun_configured(akun):
        raise ShopeeNotConfigured("Shopee live sync nonaktif atau akun belum terhubung.")
    ids: list[int] = []
    awal = 0
    while len(ids) < ADS_KAMPANYE_MAKS:
        data = await signed_shop_request(
            session, akun, _PATH_ADS_KAMPANYE, params={"ad_type": "all", "offset": awal, "limit": 100}
        )
        resp = data.get("response") or {}
        halaman = [int(c["campaign_id"]) for c in resp.get("campaign_list") or [] if c.get("campaign_id")]
        ids.extend(halaman)
        if not resp.get("has_next_page") or not halaman:
            break
        awal += len(halaman)
    ids = ids[:ADS_KAMPANYE_MAKS]
    catatan: list[str] = []
    pengaturan: dict[int, dict] = {}
    for kelompok in _bagi(ids, ADS_BATCH):
        data = await signed_shop_request(
            session,
            akun,
            _PATH_ADS_PENGATURAN,
            params={"info_type_list": "1,2,3", "campaign_id_list": ",".join(str(i) for i in kelompok)},
        )
        for c in (data.get("response") or {}).get("campaign_list") or []:
            pengaturan[int(c["campaign_id"])] = c
    kinerja: dict[int, dict] = {}
    sampai = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=7))).date()
    dari = sampai - timedelta(days=max(2, min(hari, ADS_RENTANG_MAKS_HARI)) - 1)
    try:
        for kelompok in _bagi(ids, ADS_BATCH):
            data = await signed_shop_request(
                session,
                akun,
                _PATH_ADS_KINERJA,
                params={
                    "start_date": dari.strftime("%d-%m-%Y"),
                    "end_date": sampai.strftime("%d-%m-%Y"),
                    "campaign_id_list": ",".join(str(i) for i in kelompok),
                },
            )
            respon = data.get("response")
            for toko in respon if isinstance(respon, list) else [respon or {}]:
                for c in toko.get("campaign_list") or []:
                    kinerja[int(c["campaign_id"])] = c
    except HTTPException as exc:
        catatan.append(f"Performa kampanye gagal dimuat: {exc.detail}")
    saldo = None
    try:
        data = await signed_shop_request(session, akun, _PATH_ADS_SALDO)
        resp = data.get("response") or {}
        if resp.get("total_balance") is not None:
            saldo = _uang(resp.get("total_balance"))
    except HTTPException as exc:
        catatan.append(f"Saldo iklan gagal dimuat: {exc.detail}")
    kampanye = [normalisasi_kampanye(pengaturan[i], kinerja.get(i)) for i in ids if i in pengaturan]
    return {"saldo": saldo, "hari": (sampai - dari).days + 1, "kampanye": kampanye, "catatan": catatan}


def _aman_angka(nilai: Any, nama: str, *, maks: Decimal | None = None) -> float:
    angka = _desimal(nilai)
    if angka is None or angka <= 0:
        raise HTTPException(status_code=422, detail=f"{nama} harus angka lebih dari 0")
    if maks is not None and angka > maks:
        raise HTTPException(status_code=422, detail=f"{nama} melebihi batas Rp {maks:,.0f}".replace(",", "."))
    return float(angka)


def susun_aksi_kampanye(campaign_id: int, payload: dict) -> dict:
    """Validated body of edit_manual_product_ads. The browser sends only the action and its value; the reference id is
    made here so a double click cannot run the same change twice."""
    aksi = str(payload.get("aksi") or "")
    if aksi not in AKSI_KAMPANYE:
        raise HTTPException(status_code=422, detail=f"Aksi tidak dikenal: {aksi or '(kosong)'}")
    body: dict = {"reference_id": str(payload.get("reference_id") or uuid.uuid4()), "campaign_id": campaign_id, "edit_action": aksi}
    if aksi == "change_budget":
        body["budget"] = _aman_angka(payload.get("budget"), "Anggaran", maks=ADS_ANGGARAN_MAKS)
    if aksi == "change_roas_target":
        body["roas_target"] = _aman_angka(payload.get("roas_target"), "Target ROAS", maks=Decimal(100))
    return body


def susun_kata_kunci(campaign_id: int, payload: dict) -> dict:
    """Validated body of edit_manual_product_ad_keywords."""
    daftar = payload.get("kata_kunci")
    if not isinstance(daftar, list) or not daftar:
        raise HTTPException(status_code=422, detail="kata_kunci wajib berisi minimal satu kata")
    pilihan = []
    for k in daftar[:50]:
        aksi = str(k.get("aksi") or "")
        kata = str(k.get("kata") or "").strip()
        if aksi not in AKSI_KATA_KUNCI or not kata:
            raise HTTPException(status_code=422, detail="Setiap kata kunci butuh aksi yang sah dan kata")
        item: dict = {"edit_action": aksi, "keyword": kata}
        if aksi in {"add", "change_bid_price"}:
            item["bid_price_per_click"] = _aman_angka(k.get("bid"), f"Bid untuk '{kata}'", maks=Decimal(100000))
        if aksi in {"add", "change_match_type"}:
            if k.get("tipe") not in {"exact", "broad"}:
                raise HTTPException(status_code=422, detail=f"Tipe untuk '{kata}' harus exact atau broad")
            item["match_type"] = k["tipe"]
        pilihan.append(item)
    return {"reference_id": str(payload.get("reference_id") or uuid.uuid4()), "campaign_id": campaign_id, "selected_keywords": pilihan}


def susun_iklan_baru(payload: dict) -> dict:
    """Validated body of create_manual_product_ads for item-level auto bidding (GMV Max) or manual bidding."""
    try:
        item_id = int(payload.get("item_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="item_id harus angka") from None
    metode = str(payload.get("bidding") or "auto")
    if metode not in {"auto", "manual"}:
        raise HTTPException(status_code=422, detail="bidding harus auto atau manual")
    mulai = _tanggal_ads(payload.get("mulai")) or datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=7))).date()
    body: dict = {
        "reference_id": str(payload.get("reference_id") or uuid.uuid4()),
        "item_id": item_id,
        "budget": _aman_angka(payload.get("budget"), "Anggaran", maks=ADS_ANGGARAN_MAKS),
        "start_date": mulai.strftime("%d-%m-%Y"),
        "end_date": "",
        "bidding_method": metode,
    }
    if metode == "auto" and payload.get("roas_target"):
        body["roas_target"] = _aman_angka(payload.get("roas_target"), "Target ROAS", maks=Decimal(100))
    if metode == "manual":
        kata = [k for k in payload.get("kata_kunci") or [] if str(k.get("kata") or "").strip()]
        if not kata:
            raise HTTPException(status_code=422, detail="Iklan manual butuh minimal satu kata kunci")
        body["selected_keywords"] = [
            {
                "keyword": str(k["kata"]).strip(),
                "match_type": k.get("tipe") if k.get("tipe") in {"exact", "broad"} else "broad",
                "bid_price_per_click": _aman_angka(k.get("bid"), f"Bid untuk '{k['kata']}'", maks=Decimal(100000)),
            }
            for k in kata[:50]
        ]
    return body


# --- Logistics: arrange shipment + shipping label ---------------------------------

_PATH_SHIP_PARAM = "/api/v2/logistics/get_shipping_parameter"
_PATH_SHIP_ORDER = "/api/v2/logistics/ship_order"
_PATH_TRACKING = "/api/v2/logistics/get_tracking_number"
_PATH_DOC_PARAM = "/api/v2/logistics/get_shipping_document_parameter"
_PATH_DOC_CREATE = "/api/v2/logistics/create_shipping_document"
_PATH_DOC_RESULT = "/api/v2/logistics/get_shipping_document_result"
_PATH_DOC_DOWNLOAD = "/api/v2/logistics/download_shipping_document"
# Shopee statuses after "arrange shipment": a tracking number exists from here on.
STATUS_SUDAH_DIPROSES = frozenset({"PROCESSED", "SHIPPED", "TO_CONFIRM_RECEIVE", "COMPLETED"})
_DOC_POLL_TRIES = 100  # bounded by _DOC_BATAS_DETIK, not by the count
_DOC_POLL_DELAY = 0.6
# The whole label flow must answer well inside the proxy limit (~100 s, after which the user only sees a 504).
_DOC_BATAS_DETIK = 60.0
_DOC_TIMEOUT = 15.0


def pilih_parameter_kirim(param: dict, nama_toko: str) -> dict:
    """get_shipping_parameter response -> the pickup/dropoff/non_integrated part of ship_order's body.

    Prefers courier pickup (seller does nothing), then drop-off. Modes that need a tracking number
    assigned by a 3PL cannot be automated, so those raise a clear 409 pointing at Seller Centre.
    """
    info = param.get("info_needed") or {}
    pickup = param.get("pickup") or {}
    addresses = pickup.get("address_list") or []
    if addresses:
        needed = info.get("pickup") or []
        if "tracking_number" in needed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Kurir pesanan ini butuh nomor resi dari kurir; proses lewat Seller Centre Shopee.",
            )
        def _rank(a: dict) -> int:
            flags = a.get("address_flag") or []
            return 0 if "pickup_address" in flags else 1 if "default_address" in flags else 2

        address = sorted(addresses, key=_rank)[0]
        body: dict[str, Any] = {"address_id": address["address_id"]}
        if "pickup_time_id" in needed:
            slots = address.get("time_slot_list") or []
            if not slots:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="Shopee tidak mengembalikan jadwal pickup untuk pesanan ini."
                )
            slot = next((x for x in slots if "recommended" in (x.get("flags") or [])), slots[0])
            body["pickup_time_id"] = slot["pickup_time_id"]
        return {"pickup": body}

    dropoff = param.get("dropoff") or {}
    needed = info.get("dropoff") or []
    if dropoff.get("branch_list") or needed:
        if "tracking_no" in needed or "tracking_number" in needed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Kurir pesanan ini butuh nomor resi dari kurir; proses lewat Seller Centre Shopee.",
            )
        body = {}
        if "branch_id" in needed:
            branches = dropoff.get("branch_list") or []
            if not branches:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Shopee tidak mengembalikan cabang drop-off.")
            body["branch_id"] = branches[0]["branch_id"]
        if "sender_real_name" in needed:
            body["sender_real_name"] = nama_toko
        return {"dropoff": body}

    if info.get("non_integrated") is not None and "non_integrated" in info:
        if "tracking_no" in (info.get("non_integrated") or []):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Kurir non-integrasi butuh nomor resi manual; proses lewat Seller Centre Shopee.",
            )
        return {"non_integrated": {}}
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Pesanan belum bisa diproses di Shopee (belum siap kirim atau sudah diproses).",
    )


async def ambil_nomor_resi(session: Any, akun: Any, order_sn: str) -> str | None:
    """Best-effort tracking number; None when Shopee has none yet or the call fails."""
    try:
        data = await signed_shop_request(session, akun, _PATH_TRACKING, params={"order_sn": order_sn})
    except HTTPException:
        return None
    return str((data.get("response") or {}).get("tracking_number") or "").strip() or None


async def proses_pengiriman(session: Any, akun: Any, order_sn: str) -> dict:
    """Arrange shipment on Shopee (get_shipping_parameter -> ship_order) and fetch the tracking number."""
    if not live_sync_enabled() or not _akun_configured(akun):
        raise ShopeeNotConfigured("Shopee live sync nonaktif atau akun belum terhubung.")
    data = await signed_shop_request(session, akun, _PATH_SHIP_PARAM, params={"order_sn": order_sn})
    mode = pilih_parameter_kirim(data.get("response") or {}, akun.nama_toko)
    await signed_shop_request(session, akun, _PATH_SHIP_ORDER, method="POST", body={"order_sn": order_sn, **mode})
    return {"status_marketplace": "PROCESSED", "nomor_resi": await ambil_nomor_resi(session, akun, order_sn)}


# Label templates: thermal is Shopee's 100x150 mm label (about A6), normal is an A4 sheet with a small label.
TEMPLATE_RESI = ("THERMAL_AIR_WAYBILL", "NORMAL_AIR_WAYBILL")


def pilih_template_resi(hasil: dict, tipe: str | None) -> str:
    """Template to print: the requested one, else thermal (A6) when offered, else Shopee's suggestion."""
    bisa = list(hasil.get("selectable_shipping_document_type") or [])
    if tipe:
        if bisa and tipe not in bisa:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Template resi {tipe} tidak tersedia untuk pesanan ini. Tersedia: {', '.join(bisa)}.",
            )
        return tipe
    if "THERMAL_AIR_WAYBILL" in bisa:
        return "THERMAL_AIR_WAYBILL"
    pilihan = hasil.get("suggest_shipping_document_type") or next(iter(bisa), None)
    if not pilihan:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Shopee tidak menyediakan template resi untuk pesanan ini.")
    return pilihan


# Shopee prints at most this many labels per download, all from one courier.
MAKS_RESI_MASSAL = 50


def _gagal(result_list: list[dict]) -> str | None:
    """'SN: error message' for every order in a result_list that Shopee flagged, or None."""
    teks = [
        f"{r.get('order_sn')}: {r.get('fail_error')} {r.get('fail_message', '')}".strip()
        for r in result_list
        if r.get("fail_error")
    ]
    return "; ".join(teks) or None


def _template_bersama(result_list: list[dict], tipe: str | None) -> str:
    """One template for the whole batch: it must be offered for every order."""
    bisa = [set(r.get("selectable_shipping_document_type") or []) for r in result_list]
    bersama = set.intersection(*bisa) if bisa else set()
    saran = result_list[0].get("suggest_shipping_document_type") if result_list else None
    return pilih_template_resi(
        {
            "selectable_shipping_document_type": sorted(bersama),
            "suggest_shipping_document_type": saran if saran in bersama else None,
        },
        tipe,
    )


async def unduh_resi_banyak(
    session: Any, akun: Any, pesanan: list[tuple[str, str | None]], tipe: str | None = None
) -> bytes:
    """Shopee's own shipping labels as one PDF for several arranged orders of one shop and courier.

    ``pesanan``: (order_sn, tracking number or None) pairs. get_shipping_document_parameter (template
    choice) -> create_shipping_document -> poll until every order is READY -> download_shipping_document.
    Defaults to the thermal (A6-sized) template. If any order cannot get a label the whole call fails,
    so a parcel is never silently left without one.
    """
    if not live_sync_enabled() or not _akun_configured(akun):
        raise ShopeeNotConfigured("Shopee live sync nonaktif atau akun belum terhubung.")
    if not pesanan or len(pesanan) > MAKS_RESI_MASSAL:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Pilih 1 sampai {MAKS_RESI_MASSAL} pesanan.")
    import asyncio

    mulai = time.monotonic()
    daftar = [{"order_sn": sn} for sn, _ in pesanan]
    data = await signed_shop_request(session, akun, _PATH_DOC_PARAM, method="POST", body={"order_list": daftar}, timeout=_DOC_TIMEOUT)
    hasil = (data.get("response") or {}).get("result_list") or []
    if gagal := _gagal(hasil):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Resi belum bisa dibuat: {gagal}")
    if len(hasil) != len(pesanan):
        raise HTTPException(status_code=status.HTTP_424_FAILED_DEPENDENCY, detail="Shopee tidak menjawab untuk semua pesanan.")
    tipe = _template_bersama(hasil, tipe)
    _log.info("resi: %d pesanan, template %s, parameter ok setelah %.1fs", len(pesanan), tipe, time.monotonic() - mulai)

    item_buat = []
    for sn, resi in pesanan:
        resi = resi or await ambil_nomor_resi(session, akun, sn)
        item_buat.append({"order_sn": sn, "shipping_document_type": tipe, **({"tracking_number": resi} if resi else {})})
    data = await signed_shop_request(session, akun, _PATH_DOC_CREATE, method="POST", body={"order_list": item_buat}, timeout=_DOC_TIMEOUT)
    if gagal := _gagal((data.get("response") or {}).get("result_list") or []):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Resi gagal dibuat: {gagal}")

    cek = {"order_list": [{"order_sn": sn, "shipping_document_type": tipe} for sn, _ in pesanan]}
    for _ in range(_DOC_POLL_TRIES):
        data = await signed_shop_request(session, akun, _PATH_DOC_RESULT, method="POST", body=cek, timeout=_DOC_TIMEOUT)
        hasil = (data.get("response") or {}).get("result_list") or []
        _log.info("resi: status dokumen %s setelah %.1fs", [r.get("status") for r in hasil], time.monotonic() - mulai)
        if any(r.get("status") == "FAILED" for r in hasil):
            gagal = _gagal([r for r in hasil if r.get("status") == "FAILED"]) or "FAILED"
            raise HTTPException(status_code=status.HTTP_424_FAILED_DEPENDENCY, detail=f"Shopee gagal membuat resi: {gagal}")
        if len(hasil) == len(pesanan) and all(r.get("status") == "READY" for r in hasil):
            break
        if time.monotonic() - mulai >= _DOC_BATAS_DETIK:
            break
        await asyncio.sleep(_DOC_POLL_DELAY)
    else:
        hasil = []
    if not (len(hasil) == len(pesanan) and all(r.get("status") == "READY" for r in hasil)):
        # 409 (not 5xx): the proxy turns upstream-looking 5xx into an opaque 504 page; this is simply "try again".
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Resi {tipe} belum siap di Shopee setelah {_DOC_BATAS_DETIK:.0f} detik. Coba lagi sebentar, atau pakai template lain (A4).",
        )

    # The documented top-level template, plus the per-item one the A4 flow already worked with.
    _log.info("resi: %d pesanan siap (%s), unduh setelah %.1fs", len(pesanan), tipe, time.monotonic() - mulai)
    data = await signed_shop_request(
        session,
        akun,
        _PATH_DOC_DOWNLOAD,
        method="POST",
        body={"shipping_document_type": tipe, "order_list": cek["order_list"]},
        raw=True,
        timeout=_DOC_TIMEOUT * 2,
    )
    pdf = data.get("_bytes")
    if not pdf:
        raise HTTPException(status_code=status.HTTP_424_FAILED_DEPENDENCY, detail="Shopee tidak mengirim file resi.")
    return pdf


async def unduh_resi(session: Any, akun: Any, order_sn: str, nomor_resi: str | None = None, tipe: str | None = None) -> bytes:
    """Shopee's own shipping label (PDF) for one arranged order."""
    return await unduh_resi_banyak(session, akun, [(order_sn, nomor_resi)], tipe)


# --- Cancel ---------------------------------------------------------------------------

_PATH_CANCEL = "/api/v2/order/cancel_order"
# Seller-side reasons Shopee accepts for every region (UNDELIVERABLE_AREA is TW/MY only).
ALASAN_BATAL = ("CUSTOMER_REQUEST", "OUT_OF_STOCK", "COD_NOT_SUPPORTED")
# Raw Shopee statuses from which a seller can still cancel ("before the order has been shipped").
STATUS_BISA_DIBATALKAN = frozenset({"UNPAID", "READY_TO_SHIP", "PROCESSED", "RETRY_SHIP"})


async def batalkan_pesanan(session: Any, akun: Any, order_sn: str, alasan: str) -> None:
    """Cancel an order on Shopee (cancel_order). OUT_OF_STOCK also needs the order's item/model ids,
    which come from get_order_detail so they never depend on listings being linked in the ERP."""
    if not live_sync_enabled() or not _akun_configured(akun):
        raise ShopeeNotConfigured("Shopee live sync nonaktif atau akun belum terhubung.")
    if alasan not in ALASAN_BATAL:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Alasan pembatalan tidak dikenal")
    body: dict[str, Any] = {"order_sn": order_sn, "cancel_reason": alasan}
    if alasan == "OUT_OF_STOCK":
        data = await signed_shop_request(
            session, akun, _PATH_ORDER_DETAIL, params={"order_sn_list": order_sn, "response_optional_fields": "item_list"}
        )
        (order,) = (data.get("response") or {}).get("order_list") or [{}]
        body["item_list"] = [
            {"item_id": it["item_id"], "model_id": it.get("model_id") or 0} for it in order.get("item_list") or [] if it.get("item_id")
        ]
        if not body["item_list"]:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Shopee tidak mengembalikan item pesanan ini.")
    await signed_shop_request(session, akun, _PATH_CANCEL, method="POST", body=body)


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
