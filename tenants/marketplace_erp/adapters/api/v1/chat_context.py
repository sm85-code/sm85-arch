"""Read-only, shop-scoped cards and authoritative recipients for seller chat."""

import json

from fastapi import HTTPException
from sqlalchemy import select, or_, func, cast, JSON, String
from sqlalchemy.orm import selectinload

from tenants.marketplace_erp.modules.marketplace_erp.application import services
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import Pesanan, KatalogShopee
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee as api


def buyer_matches(order, target):
    detail = json.loads(order.detail_json or "{}")
    buyer_id = detail.get("buyer_user_id")
    if buyer_id:
        return str(buyer_id) == str(target["to_id"])
    return (
        bool(target.get("to_name"))
        and (order.nama_pembeli or "").strip().casefold() == target["to_name"].strip().casefold()
    )


async def buyer_orders(session, akun, targets, *, items=False, limit=500):
    """Match immutable buyer IDs even if the username in an old snapshot changed."""
    names = {t.get("to_name", "").strip().lower() for t in targets if t.get("to_name")}
    ids = {str(t["to_id"]) for t in targets if str(t.get("to_id", "")).isdigit()}
    if session.get_bind().dialect.name == "postgresql":
        buyer_id = cast(Pesanan.detail_json, JSON)["buyer_user_id"].as_string()
    else:
        buyer_id = cast(func.json_extract(Pesanan.detail_json, "$.buyer_user_id"), String)
    stmt = select(Pesanan).where(
        Pesanan.akun_id == akun.id,
        Pesanan.platform == "shopee",
        or_(func.lower(func.trim(Pesanan.nama_pembeli)).in_(names), buyer_id.in_(ids)),
    )
    if items:
        stmt = stmt.options(selectinload(Pesanan.items))
    return (
        (
            await session.execute(
                stmt.order_by(func.coalesce(Pesanan.dipesan_at, Pesanan.created_at).desc(), Pesanan.id).limit(limit)
            )
        )
        .scalars()
        .all()
    )


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
    rows = await buyer_orders(session, akun, [target], items=True, limit=100)
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


def message_reference(message):
    """Bounded native/rich-text reference extraction; quoted messages are excluded."""
    records = []
    def read(value, depth=0):
        if depth > 6 or len(records) >= 100:
            return
        if isinstance(value, str) and len(value) <= 16000 and value.lstrip().startswith(('{', '[')):
            try:
                read(json.loads(value), depth + 1)
            except (ValueError, RecursionError):
                pass
        elif isinstance(value, dict):
            records.append(value)
            for key, child in list(value.items())[:100]:
                if key not in ('quoted_msg', 'quoted_message'):
                    read(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:30]:
                read(child, depth + 1)
    read(message.get('content'))
    read(message.get('source_content'))
    result = {}
    for key in ('item_id', 'model_id', 'order_sn', 'model_name', 'variation_name'):
        values = {str(r[key]) for r in records if r.get(key) is not None and str(r[key]).strip()}
        if len(values) == 1:
            result[key] = next(iter(values))
    return result


def message_product_card(row, reference):
    card = product_card(row)
    model_id = reference.get('model_id')
    model_name = reference.get('model_name') or reference.get('variation_name')
    if not model_id and not model_name:
        return card
    variants = json.loads(row.varian_json or '[]')
    matches = [v for v in variants if str(v.get('model_id')) == model_id] if model_id else [v for v in variants if v.get('nama') == model_name]
    variant = matches[0] if len(matches) == 1 else None
    card['varian'] = variant.get('nama') if variant else model_name or 'Varian belum tersinkron'
    card['model_id'] = model_id
    card['harga'] = str(variant['harga']) if variant and variant.get('harga') not in (None, '') else None
    card['foto'] = (variant.get('foto') if variant else None) or card['foto']
    return card
