"""Marketplace workflow orchestration; durable publication receipts survive remote timeouts."""

import asyncio
import hashlib
import json

import requests
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import services
from ..infrastructure.models import PublikasiMarketplace, Pesanan
from ..infrastructure.adapters import erp_shopee as shopee, erp_shopee_workflows as adapter


async def receipt(session, akun_id, operation_id):
    return (
        await session.execute(
            select(PublikasiMarketplace).where(
                PublikasiMarketplace.akun_id == akun_id, PublikasiMarketplace.operation_id == str(operation_id)
            )
        )
    ).scalar_one_or_none()


def result_from_record(record, payload_hash=None):
    if payload_hash and record.payload_hash != payload_hash:
        raise HTTPException(
            status_code=409, detail="ID operasi telah dipakai untuk data berbeda. Periksa hasil publikasi sebelumnya."
        )
    result = json.loads(record.result_json)
    if not result:
        result = {
            "ok": False,
            "status": record.status,
            "item_id": record.item_id,
            "warnings": [
                "Operasi masih berjalan atau hasil belum pasti. Jangan membuat ulang produk; periksa katalog/Seller Centre."
            ],
        }
    return result | {"operation_id": record.operation_id}


async def publish(session, akun_id, payload):
    payload_hash = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
    existing = await receipt(session, akun_id, payload.operation_id)
    if existing:
        return result_from_record(existing, payload_hash)
    akun = await services.akun_shopee_pengelolaan(session, akun_id, "publikasi produk")
    meta = await adapter.metadata(session, akun, payload.category_id)
    adapter.validate_publication(payload, meta)
    record = PublikasiMarketplace(akun_id=akun_id, operation_id=str(payload.operation_id), payload_hash=payload_hash)
    session.add(record)
    try:
        await session.commit()  # Claim is durable BEFORE add_item. Never replay the same operation.
    except IntegrityError:
        await session.rollback()
        existing = await receipt(session, akun_id, payload.operation_id)
        if existing:
            return result_from_record(existing, payload_hash)
        raise
    result = {"ok": False, "status": "belum_pasti", "item_id": None, "warnings": [], "request_id": None}
    try:
        data = await shopee.signed_shop_request(
            session, akun, "/api/v2/product/add_item", method="POST", body=adapter.parent_body(payload)
        )
        ident = (data.get("response") or {}).get("item_id")
        if type(ident) is not int or ident <= 0:
            raise shopee.ShopeeAPIError(
                "add_item", "unconfirmed_response", "ID produk belum terkonfirmasi.", data.get("request_id")
            )
        record.item_id = str(ident)
        record.status = "varian" if payload.models else "produk_dibuat"
        record.result_json = json.dumps(
            {
                "ok": False,
                "status": record.status,
                "item_id": str(ident),
                "warnings": ["Produk sudah dibuat; tahap berikutnya belum dikonfirmasi. Jangan membuat ulang."],
            }
        )
        await session.commit()
        result.update(item_id=str(ident), request_id=data.get("request_id"), status="sebagian")
        if data.get("warning"):
            result["warnings"].append(str(data["warning"]))
        if payload.models:
            await asyncio.sleep(5)  # Documented minimum propagation delay after add_item.
            data = await shopee.signed_shop_request(
                session,
                akun,
                "/api/v2/product/init_tier_variation",
                method="POST",
                body=adapter.variant_body(payload, ident),
            )
            response = data.get("response") or {}
            models = response.get("model") or []
            expected = {tuple(m.tier_index) for m in payload.models}
            actual = {tuple(m.get("tier_index", [])) for m in models}
            if (
                str(response.get("item_id")) != str(ident)
                or actual != expected
                or len(models) != len(expected)
                or any(not m.get("model_id") for m in models)
            ):
                raise shopee.ShopeeAPIError(
                    "init_tier_variation",
                    "unconfirmed_response",
                    "Varian belum terkonfirmasi lengkap. Produk tetap disembunyikan.",
                    data.get("request_id"),
                )
            result["request_id"] = data.get("request_id")
        if payload.aktif:
            data = await shopee.ubah_status_produk(session, akun, ident, False)
            result["request_id"] = data.get("request_id")
        result.update(ok=True, status="aktif" if payload.aktif else "disembunyikan")
        try:
            snapshot = await shopee.ambil_satu_produk(session, akun, ident)
            await services.simpan_katalog_shopee(session, akun, [snapshot], lengkap=False)
        except HTTPException as exc:
            result["warnings"].append(f"Produk dibuat; snapshot belum tersedia. Sinkronkan katalog: {exc.detail}")
    except (HTTPException, requests.RequestException, ValueError, TypeError) as exc:
        result["item_id"] = record.item_id
        result["status"] = "sebagian" if record.item_id else "belum_pasti"
        result["warnings"].append(str(getattr(exc, "detail", "Koneksi Shopee terputus; hasil belum pasti.")))
        result["warnings"].append(
            "Jangan membuat ulang produk. Periksa ID produk/katalog dan lanjutkan produk yang sama di Seller Centre."
        )
    record.status = result["status"]
    record.result_json = json.dumps(result)
    await session.commit()
    return result | {"operation_id": record.operation_id}


async def wallet(session, akun_id, dari, sampai, offset):
    akun = await services.akun_shopee_pengelolaan(session, akun_id, "transaksi dana")
    result = await adapter.wallet(session, akun, dari, sampai, offset)
    numbers = {r["order_sn"] for r in result["items"] if r.get("order_sn")}
    linked = {}
    if numbers:
        rows = (
            await session.execute(
                select(Pesanan.id, Pesanan.id_eksternal).where(
                    Pesanan.akun_id == akun.id, Pesanan.id_eksternal.in_(numbers)
                )
            )
        ).all()
        linked = {r.id_eksternal: r.id for r in rows}
    for row in result["items"]:
        row["pesanan_id"] = linked.get(row.get("order_sn"))
    return result
