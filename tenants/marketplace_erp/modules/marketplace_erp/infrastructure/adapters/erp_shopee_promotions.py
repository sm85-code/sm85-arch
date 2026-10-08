"""Shopee Discount APIs. IDs are strings at the ERP boundary; integers on Shopee requests."""
import logging
import time
from decimal import Decimal

from fastapi import HTTPException
from pydantic import ValidationError

from ...application.schemas import PromosiOut, PromosiDetailOut
from .erp_shopee import ShopeeAPIError, signed_shop_request

BASE = "/api/v2/discount/"
logger = logging.getLogger(__name__)


def ident(value) -> int:
    if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
        raise ValueError("invalid identity")
    return int(value)


def amount(value):
    if value is None:
        return None
    d = Decimal(str(value))
    if not d.is_finite() or d < 0:
        raise ValueError("invalid amount")
    return str(d)


def project(row):
    # Validate read contracts before returning an apparently successful page.
    return PromosiOut.model_validate({
        "id": str(ident(row["discount_id"])), "nama": row["discount_name"], "status": row["status"],
        "mulai_at": row["start_time"], "selesai_at": row["end_time"],
    }).model_dump()


def incomplete(path, data, exc=None, *, status_filter=None, halaman=None):
    # Record contract metadata only; validation inputs can contain buyer data.
    fields = []
    reason = type(exc).__name__ if exc is not None else "unconfirmed_mutation"
    if isinstance(exc, ValidationError):
        fields = [{"field": list(e["loc"]), "type": e["type"]} for e in exc.errors(include_input=False, include_context=False, include_url=False)[:10]]
    elif isinstance(exc, KeyError):
        fields = [str(exc.args[0])]
    elif isinstance(exc, ValueError) and str(exc) in {
        "invalid page", "invalid pagination", "invalid identity", "invalid amount", "invalid detail", "empty continuation",
    }:
        reason = str(exc)
    response = data.get("response")
    expected = ("discount_list", "more", "discount_id", "status", "discount_name", "start_time", "end_time", "item_list")
    shape = {key: type(response[key]).__name__ if key in response else "missing" for key in expected} if isinstance(response, dict) else type(response).__name__
    logger.warning(
        "shopee_promotion phase=validation_failed path=%s request_id=%s status_filter=%s page=%s reason=%s fields=%s response_shape=%s",
        path, data.get("request_id"), status_filter, halaman, reason, fields, shape,
    )
    raise ShopeeAPIError(path, "unconfirmed_response", "Respons belum terkonfirmasi. Segarkan sebelum mengirim ulang.", data.get("request_id")) from exc


async def daftar(session, akun, status="all", halaman=1):
    path = BASE + "get_discount_list"
    data = await signed_shop_request(session, akun, path, params={"discount_status": status, "page_no": halaman, "page_size": 40}, timeout=15)
    try:
        r = data["response"]
        if not isinstance(r["discount_list"], list) or type(r["more"]) is not bool:
            raise ValueError("invalid page")
        rows = [project(row) for row in r["discount_list"]]
        if len({row["id"] for row in rows}) != len(rows) or (r["more"] and not rows):
            raise ValueError("invalid pagination")
        return {"items": rows, "halaman": halaman, "ada_lagi": r["more"]}
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        incomplete(path, data, exc, status_filter=status, halaman=halaman)


async def detail(session, akun, id, halaman=1):
    path = BASE + "get_discount"
    data = await signed_shop_request(session, akun, path, params={"discount_id": ident(id), "page_no": halaman, "page_size": 50})
    try:
        r = data["response"]
        out = project(r)
        if out["id"] != str(id) or not isinstance(r["item_list"], list) or type(r["more"]) is not bool:
            raise ValueError("invalid detail")
        rows = []
        for item in r["item_list"]:
            models = item.get("model_list") or []
            for model in models or [None]:
                rows.append({
                    "item_id": str(ident(item["item_id"])), "model_id": str(model["model_id"]) if model else None,
                    "nama": item["item_name"], "nama_varian": model.get("model_name") if model else None,
                    "harga_asli": amount(model.get("model_original_price") if model else item.get("item_original_price")),
                    "harga_promo": amount(model.get("model_promotion_price") if model else item.get("item_promotion_price")),
                    "stok_promo": model.get("model_promotion_stock") if model else item.get("item_promotion_stock"),
                    "batas_pembelian": item.get("purchase_limit"),
                })
        if r["more"] and not rows:
            raise ValueError("empty continuation")
        return PromosiDetailOut.model_validate({**out, "barang": rows, "halaman": halaman, "ada_lagi": r["more"]}).model_dump()
    except (KeyError, TypeError, ValueError, ValidationError, ArithmeticError) as exc:
        incomplete(path, data, exc)


async def buat(session, akun, payload):
    duration = payload.selesai_at - payload.mulai_at
    if payload.mulai_at < int(time.time()) + 3600 or duration < 3600 or duration >= 180 * 86400:
        raise HTTPException(status_code=422, detail="Mulai promosi minimal 1 jam dari sekarang; durasi minimal 1 jam dan kurang dari 180 hari.")
    path = BASE + "add_discount"
    data = await signed_shop_request(session, akun, path, method="POST", body={
        "discount_name": payload.nama, "start_time": payload.mulai_at, "end_time": payload.selesai_at,
    })
    try:
        id = str(ident(data["response"]["discount_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        incomplete(path, data, exc)
    return {"ok": True, "id": id, "request_id": data.get("request_id"), "warnings": [data["warning"]] if data.get("warning") else []}


async def akhiri(session, akun, id, hapus=False):
    # Fetch exact activity; Shopee decides status eligibility and permissions.
    await detail(session, akun, id)
    path = BASE + ("delete_discount" if hapus else "end_discount")
    data = await signed_shop_request(session, akun, path, method="POST", body={"discount_id": ident(id)})
    if str((data.get("response") or {}).get("discount_id")) != str(id):
        incomplete(path, data)
    return {"ok": True, "id": str(id), "request_id": data.get("request_id"), "warnings": [data["warning"]] if data.get("warning") else []}


async def barang(session, akun, id, item_id, model_id, payload):
    method = {"tambah": "add_discount_item", "ubah": "update_discount_item", "hapus": "delete_discount_item"}[payload.operasi]
    path = BASE + method
    if payload.operasi == "hapus":
        body = {"discount_id": ident(id), "item_id": ident(item_id)}
        if model_id is not None:
            body["model_id"] = int(model_id)
    else:
        item = {"item_id": ident(item_id), "purchase_limit": payload.batas_pembelian}
        if model_id is not None:
            item["model_list"] = [{"model_id": int(model_id), "model_promotion_price": float(payload.harga)}]
        else:
            item["item_promotion_price"] = float(payload.harga)
        body = {"discount_id": ident(id), "item_list": [item]}
    data = await signed_shop_request(session, akun, path, method="POST", body=body)
    response = data.get("response") or {}
    if str(response.get("discount_id")) != str(id) or not isinstance(response.get("error_list", []), list):
        incomplete(path, data)
    failures = response.get("error_list") or []
    if not all(isinstance(f, dict) for f in failures):
        incomplete(path, data)
    if not failures and payload.operasi != "hapus" and (type(response.get("count")) is not int or response["count"] != 1):
        incomplete(path, data)
    # Preserve per-item failures and confirmed partial outcomes; never suggest resending the whole batch.
    return {"ok": not failures, "id": str(id), "request_id": data.get("request_id"), "gagal": failures,
            "warnings": [data["warning"]] if data.get("warning") else []}
