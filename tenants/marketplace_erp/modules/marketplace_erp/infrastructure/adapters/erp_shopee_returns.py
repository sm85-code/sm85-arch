"""Read-only Shopee returns projection; no stock or payout mutations."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException

from .erp_shopee import ShopeeAPIError, signed_shop_request

PATH_LIST = "/api/v2/returns/get_return_list"
PATH_DETAIL = "/api/v2/returns/get_return_detail"
WIB = timezone(timedelta(hours=7))


def rentang_retur(dari: date, sampai: date) -> tuple[int, int]:
    if sampai < dari or (sampai - dari).days >= 15:
        raise HTTPException(status_code=422, detail="Rentang tanggal retur harus berurutan dan maksimal 15 hari kalender.")
    start = datetime.combine(dari, time.min, WIB)
    end = datetime.combine(sampai + timedelta(days=1), time.min, WIB) - timedelta(seconds=1)
    return int(start.timestamp()), int(end.timestamp())


def _nominal(value: Any) -> Decimal | None:
    if value is None:
        return None
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("invalid amount")
    return number


def normalisasi_retur(row: dict) -> dict:
    if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k].strip() for k in ("return_sn", "order_sn", "status")):
        raise ValueError("missing return identity/status")
    result = {
        "nomor_retur": row["return_sn"], "nomor_pesanan": row["order_sn"], "status": row["status"],
        "alasan": row.get("reason"), "alasan_pembeli": row.get("text_reason"),
        "alasan_peninjauan": row.get("reassessed_request_reason"),
        "nominal_refund": _nominal(row.get("refund_amount")), "mata_uang": row.get("currency"),
        "perlu_pengembalian_barang": row.get("needs_logistics"), "solusi": row.get("return_solution"),
        "dibuat_at": row.get("create_time"), "diperbarui_at": row.get("update_time"),
        "tenggat_at": row.get("due_date"), "tenggat_kirim_at": row.get("return_ship_due_date"),
        "tenggat_penjual_at": row.get("return_seller_due_date"), "nomor_resi": row.get("tracking_number"),
        "kurir": row.get("reverse_logistic_channel_name"), "status_negosiasi": row.get("negotiation_status"),
        "status_bukti": row.get("seller_proof_status"), "status_kompensasi": row.get("seller_compensation_status"),
        "items": [],
    }
    items = row.get("item", [])
    for key in ("dibuat_at", "diperbarui_at", "tenggat_at", "tenggat_kirim_at", "tenggat_penjual_at", "solusi"):
        value = result[key]
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("invalid timestamp/solution")
    if result["perlu_pengembalian_barang"] is not None and type(result["perlu_pengembalian_barang"]) is not bool:
        raise ValueError("invalid logistics flag")
    for key in ("alasan", "alasan_pembeli", "alasan_peninjauan", "mata_uang", "nomor_resi", "kurir",
                "status_negosiasi", "status_bukti", "status_kompensasi"):
        if result[key] is not None and not isinstance(result[key], str):
            raise ValueError("invalid text field")
    if not isinstance(items, list):
        raise ValueError("invalid items")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid item")
        if item.get("amount") is not None and (type(item["amount"]) is not int or item["amount"] < 0):
            raise ValueError("invalid quantity")
        for key in ("name", "item_sku", "variation_sku"):
            if item.get(key) is not None and not isinstance(item[key], str):
                raise ValueError("invalid item text")
        result["items"].append({
            "item_id": str(item["item_id"]) if item.get("item_id") is not None else None,
            "model_id": str(item["model_id"]) if item.get("model_id") is not None else None,
            "nama": item.get("name") or "", "sku": item.get("item_sku"), "sku_varian": item.get("variation_sku"),
            "qty": item.get("amount"), "harga": _nominal(item.get("item_price")),
            # Partial refund amount is different from item price; never substitute one for the other.
            "nominal_refund": _nominal(item.get("refund_amount")),
        })
    return result


async def daftar_retur(session, akun, dari: date, sampai: date, halaman: int = 1, per_halaman: int = 40) -> dict:
    start, end = rentang_retur(dari, sampai)
    if halaman < 1 or not 1 <= per_halaman <= 100:
        raise HTTPException(status_code=422, detail="Paginasi retur tidak valid.")
    data = await signed_shop_request(session, akun, PATH_LIST, params={
        "page_no": halaman, "page_size": per_halaman, "create_time_from": start, "create_time_to": end,
    })
    response = data.get("response")
    try:
        if not isinstance(response, dict) or not isinstance(response.get("return"), list) or type(response.get("more")) is not bool:
            raise ValueError("incomplete page")
        rows = [normalisasi_retur(row) for row in response["return"]]
        if len({r["nomor_retur"] for r in rows}) != len(rows) or (response["more"] and not rows):
            raise ValueError("invalid pagination")
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ShopeeAPIError(PATH_LIST, "incomplete_response", "Halaman retur tidak lengkap. Coba muat ulang.", data.get("request_id")) from exc
    return {"items": rows, "halaman": halaman, "per_halaman": per_halaman, "ada_lagi": response["more"]}


async def detail_retur(session, akun, nomor_retur: str) -> dict:
    data = await signed_shop_request(session, akun, PATH_DETAIL, params={"return_sn": nomor_retur})
    try:
        row = normalisasi_retur(data.get("response"))
        if row["nomor_retur"] != nomor_retur:
            raise ValueError("wrong return")
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ShopeeAPIError(PATH_DETAIL, "incomplete_response", "Detail retur yang diminta tidak dikonfirmasi Shopee.", data.get("request_id")) from exc
    return row


async def konfirmasi_retur(session, akun, nomor_retur: str) -> dict:
    await detail_retur(session, akun, nomor_retur)
    path = "/api/v2/returns/confirm"
    data = await signed_shop_request(session, akun, path, method="POST", body={"return_sn": nomor_retur})
    if (data.get("response") or {}).get("return_sn") != nomor_retur:
        raise ShopeeAPIError(path, "unconfirmed_response", "Persetujuan belum terkonfirmasi. Segarkan sebelum mencoba ulang.", data.get("request_id"))
    return {"ok": True, "request_id": data.get("request_id"), "warnings": []}
