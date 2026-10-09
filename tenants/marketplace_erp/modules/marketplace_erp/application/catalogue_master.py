"""Copy catalogue reference data into local SKU masters; never copy physical stock."""

import hashlib
from collections import Counter

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from . import services
from .schemas import ProdukIn, ProdukKeluargaIn
from ..infrastructure.models import KatalogShopee, Produk


async def copy_to_master(session, catalogue_id):
    # Serialize retries for one source; SKU's unique index also protects cross-shop races.
    source = (
        await session.execute(select(KatalogShopee).where(KatalogShopee.id == catalogue_id).with_for_update())
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(404, "Produk katalog tidak ditemukan")
    data = services.katalog_out(source, lengkap=True)
    if data.get("has_model") and not data["varian"]:
        raise HTTPException(422, "Varian katalog belum lengkap. Sinkronkan produk ini terlebih dahulu.")
    variants = data["varian"] or [{}]
    if data["varian"] and any(v.get("model_id") in (None, "", "None") for v in variants):
        raise HTTPException(422, "Identitas varian belum lengkap. Sinkronkan produk ini terlebih dahulu.")
    counts = Counter(str(v.get("sku") or data.get("sku") or "").strip().casefold() for v in variants)
    entries = []
    generated = 0
    for variant in variants:
        sku = str((variant.get("sku") if data["varian"] else data.get("sku")) or "").strip()
        if not sku or len(sku) > 128 or counts[sku.casefold()] > 1:
            key = f"{source.akun_id}:{source.item_id}:{variant.get('model_id') or 'item'}"
            sku = "ERP-" + hashlib.sha256(key.encode()).hexdigest()[:24].upper()
            generated += 1
        existing = (await session.execute(select(Produk).where(Produk.sku_induk == sku))).scalar_one_or_none()
        entries.append((variant, sku, existing))
    if len({sku for _, sku, _ in entries}) != len(entries):
        raise HTTPException(422, "Identitas varian katalog berulang. Sinkronkan produk ini terlebih dahulu.")
    family = None
    tiers = [o["tier"] for o in variants[0].get("opsi", [])]
    if data["varian"] and any(existing is None for _, _, existing in entries):
        if not tiers:
            tiers = ["Varian"]
        family = await services.create_produk_keluarga(session, ProdukKeluargaIn(nama=data["nama"], tiers=tiers))
    result = []
    for variant, sku, existing in entries:
        if existing:
            if not existing.source_katalog_id:
                existing.source_katalog_id = source.id
            result.append({"id": existing.id, "sku": sku, "baru": False})
            continue

        def inherited(key, parent_key=None, default=0):
            value = variant.get(key)
            return (
                value
                if value is not None
                else (data.get(parent_key or key) if data.get(parent_key or key) is not None else default)
            )

        options = variant.get("opsi") or (
            [{"tier": "Varian", "opsi": variant.get("nama") or str(variant.get("model_id"))}] if family else []
        )
        price = (variant.get("harga_asli") or variant.get("harga")) if data["varian"] else data.get("harga_min")
        if price is None or price == "":
            raise HTTPException(422, "Harga katalog belum tersedia. Sinkronkan produk ini terlebih dahulu.")
        payload = ProdukIn(
            sku_induk=sku,
            nama=data["nama"],
            deskripsi=data["deskripsi"],
            harga_dasar=price,
            stok=0,
            stok_referensi=variant.get("stok") if data["varian"] else data.get("stok_shopee"),
            foto_url=variant.get("foto") or data.get("foto_utama"),
            berat_gram=inherited("berat_gram"),
            panjang_cm=inherited("panjang_cm"),
            lebar_cm=inherited("lebar_cm"),
            tinggi_cm=inherited("tinggi_cm"),
            preorder=inherited("preorder", "is_pre_order", False),
            hari_proses=inherited("hari_kirim", "days_to_ship", 2),
            keluarga_id=family["id"] if family else None,
            opsi_varian=options,
        )
        product = await services.create_produk(session, payload)
        product.source_katalog_id = source.id
        await session.flush()
        result.append({"id": product.id, "sku": sku, "baru": True})
    return {"katalog_id": catalogue_id, "nama": source.nama, "produk": result, "sku_dibuat": generated}


async def copy_batch(session, ids):
    results = []
    for catalogue_id in dict.fromkeys(ids):
        try:
            async with session.begin_nested():
                result = await copy_to_master(session, catalogue_id)
            results.append({"ok": True, **result})
        except (HTTPException, ValidationError) as exc:
            message = (
                exc.detail
                if isinstance(exc, HTTPException)
                else "Data varian/pengiriman belum lengkap atau tidak valid. Sinkronkan katalog lalu coba lagi."
            )
            results.append({"ok": False, "katalog_id": catalogue_id, "error": message})
    return {"hasil": results}
