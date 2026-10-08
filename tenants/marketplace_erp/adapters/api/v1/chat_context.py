"""Read-only, shop-scoped cards and authoritative recipients for seller chat."""

import json

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import Pesanan, KatalogShopee
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee as api


def buyer_matches(order, target):
    detail = json.loads(order.detail_json or "{}")
    buyer_id = detail.get("buyer_user_id")
    if buyer_id:
        return str(buyer_id) == str(target["to_id"])
    return bool(target.get("to_name")) and order.nama_pembeli == target["to_name"]


def order_card(row):
    return {
        "id": row.id,
        "order_sn": row.id_eksternal,
        "nama": row.id_eksternal,
        "foto": next((i.foto for i in row.items if i.foto), None),
        "status": row.status,
        "total": str(row.total),
        "items": [{"nama": i.nama_produk, "varian": i.model_name, "qty": i.qty, "foto": i.foto} for i in row.items],
    }


def product_card(row):
    photos = json.loads(row.foto_json or "[]")
    return {
        "id": row.id,
        "item_id": row.item_id,
        "nama": row.nama,
        "foto": photos[0] if photos else None,
        "harga": str(row.harga_min) if row.harga_min is not None else None,
    }


async def get_order(session, order_id):
    row = (
        await session.execute(select(Pesanan).options(selectinload(Pesanan.items)).where(Pesanan.id == order_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Pesanan tidak ditemukan")
    if row.platform != "shopee" or not row.akun_id:
        raise HTTPException(409, "Hubungi pembeli saat ini tersedia untuk pesanan Shopee")
    await services.lengkapi_foto_item(session, [row])
    return row


async def order_target(session, akun, row):
    # Fetch the recipient from Shopee for this authorized order, never from browser input.
    detail = json.loads(row.detail_json or "{}")
    buyer_id = detail.get("buyer_user_id")
    if not buyer_id:
        data = await api.signed_shop_request(
            session,
            akun,
            api._PATH_ORDER_DETAIL,
            params={"order_sn_list": row.id_eksternal, "response_optional_fields": "buyer_user_id,buyer_username"},
        )
        orders = (data.get("response") or {}).get("order_list") or []
        match = next((o for o in orders if o.get("order_sn") == row.id_eksternal), {})
        buyer_id = match.get("buyer_user_id")
    try:
        buyer_id = int(buyer_id)
    except (TypeError, ValueError):
        buyer_id = 0
    if buyer_id <= 0:
        raise HTTPException(
            424, "Shopee belum menyediakan identitas pembeli untuk pesanan ini. Sinkronkan pesanan dan coba lagi."
        )
    return {"to_id": buyer_id, "to_name": row.nama_pembeli, "conversation_id": "", "order_id": row.id}


async def context(session, akun, target, query="", offset=0):
    # IDs take precedence. Username is only a fallback for older synchronized snapshots.
    rows = (
        (
            await session.execute(
                select(Pesanan)
                .options(selectinload(Pesanan.items))
                .where(
                    Pesanan.akun_id == akun.id,
                    Pesanan.platform == "shopee",
                    Pesanan.nama_pembeli == target.get("to_name", ""),
                )
                .order_by(Pesanan.dipesan_at.desc(), Pesanan.created_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    orders = [r for r in rows if buyer_matches(r, target)]
    await services.lengkapi_foto_item(session, orders)
    products = select(KatalogShopee).where(KatalogShopee.akun_id == akun.id, KatalogShopee.status == "NORMAL")
    if query:
        products = products.where(KatalogShopee.nama.icontains(query, autoescape=True))
    products = (
        (await session.execute(products.order_by(KatalogShopee.nama, KatalogShopee.id).offset(offset).limit(21)))
        .scalars()
        .all()
    )
    city = next(
        (
            json.loads(r.detail_json or "{}").get("kota")
            for r in orders
            if json.loads(r.detail_json or "{}").get("kota")
        ),
        None,
    )
    return {
        "kota": city,
        "kota_sumber": "Alamat tujuan pesanan terbaru" if city else None,
        "pesanan": [order_card(r) for r in orders],
        "produk": [product_card(r) for r in products[:20]],
        "produk_ada_lagi": len(products) > 20,
        "produk_offset": offset,
    }


async def attachment(session, akun, target, kind, row_id):
    if kind == "order":
        row = await get_order(session, row_id)
        if row.akun_id != akun.id or not buyer_matches(row, target):
            raise HTTPException(403, "Lampiran pesanan harus milik pembeli dan toko percakapan ini")
        return {"order_sn": row.id_eksternal}
    row = (
        await session.execute(select(KatalogShopee).where(KatalogShopee.id == row_id, KatalogShopee.akun_id == akun.id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(403, "Lampiran produk harus berasal dari toko percakapan ini")
    if row.status != "NORMAL":
        raise HTTPException(409, "Produk tidak aktif di Shopee; segarkan katalog")
    return {"item_id": int(row.item_id)}
