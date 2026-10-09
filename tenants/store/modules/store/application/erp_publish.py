"""Publish a marketplace-neutral master family; Store inventory remains independent."""
import json

from fastapi import HTTPException
from sqlalchemy import select, text

from tenants.marketplace_erp.modules.marketplace_erp.application import services as erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import Produk, ProdukKeluarga, ProdukVarian, KatalogShopee
from tenants.store.modules.store.infrastructure.models import ProdukStore
from tenants.store.modules.store.infrastructure.media_import import import_foto_dari_url
from . import services
from .schemas import VarianIn, MAKS_FOTO_PRODUK


async def publish_master(session, store_session, produk_id, payload, importer=import_foto_dari_url):
    master = await erp.get_produk(session, produk_id)
    link = await session.get(ProdukVarian, master.id)
    family = await session.get(ProdukKeluarga, link.keluarga_id) if link else None
    members = (await session.execute(select(Produk, ProdukVarian).join(ProdukVarian, ProdukVarian.produk_id == Produk.id).where(
        ProdukVarian.keluarga_id == family.id
    ).order_by(Produk.sku_induk))).all() if family else [(master, None)]
    identity = f"family:{family.id}" if family else master.id
    aliases = {identity, *(p.id for p, _ in members)}
    sources = []
    for p, _ in members:
        if p.source_katalog_id:
            aliases.add(f"shopee:{p.source_katalog_id}")
            k = await session.get(KatalogShopee, p.source_katalog_id)
            if k and k not in sources:
                sources.append(k)
    if store_session.get_bind().dialect.name == "postgresql":
        await store_session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "store:" + identity})
    old = list((await store_session.execute(select(ProdukStore).where(ProdukStore.erp_produk_id.in_(aliases)))).scalars())
    if len(old) > 1:
        raise HTTPException(409, "Sumber ini memiliki beberapa salinan lama. Rekonsiliasi salinan sebelum menerbitkan keluarga agar pesanan dan stok tidak hilang")
    legacy_id = old[0].erp_produk_id if old else None
    legacy_stock = old[0].stok if old else 0
    if old:
        old[0].erp_produk_id = identity
        await store_session.flush()
    urls = list(dict.fromkeys([u for k in sources for u in erp.katalog_out(k, lengkap=True)["foto"]] + [p.foto_url for p, _ in members if p.foto_url]))[:MAKS_FOTO_PRODUK]
    keys = []
    photo_failed = False
    if payload.salin_foto:
        for url in urls:
            key = await importer(url)
            if key:
                keys.append(key)
            else:
                photo_failed = True
    from tenants.marketplace_erp.modules.marketplace_erp.application.stock_settings import get_settings
    listings = await erp.list_listing(session, produk_id=master.id)
    platform = next((li.platform for li in listings if li.aktif), None) or (listings[0].platform if listings else None)
    default_stock = master.stok if (await get_settings(session))["gudang_aktif"] and not family else 0
    copy, created = await services.upsert_produk_dari_erp(
        store_session, erp_produk_id=identity, nama=family.nama if family else master.nama,
        deskripsi=master.deskripsi, harga=payload.harga if payload.harga is not None else master.harga_dasar,
        stok=payload.stok if payload.stok is not None else default_stock, platform_asal="shopee" if sources else platform,
        foto_key=keys[0] if keys else None, aktif=payload.aktif and not photo_failed,
        berat_gram=master.berat_gram, panjang_cm=master.panjang_cm, lebar_cm=master.lebar_cm,
        tinggi_cm=master.tinggi_cm, preorder=master.preorder, hari_proses=master.hari_proses,
    )
    for key in keys[1:]:
        if not any(f.foto_key == key for f in copy.foto):
            await services._tambah_foto_ke_galeri(store_session, copy, key)
    if family:
        current = {v.sku: v for v in copy.varian}
        # Preserve store-only variants and their inventory; ERP republishing never deletes stock.
        inputs = [VarianIn(id=v.id, nama=v.nama, sku=v.sku, harga=v.harga, stok=v.stok, expected_stok=v.stok,
                          aktif=v.aktif, foto_id=v.foto_id, berat_gram=v.berat_gram,
                          panjang_cm=v.panjang_cm, lebar_cm=v.lebar_cm, tinggi_cm=v.tinggi_cm) for v in copy.varian]
        for p, option in members:
            name = " / ".join(str(o["opsi"]) for o in json.loads(option.opsi_json))
            if not name or len(name) > 120:
                raise HTTPException(422, "Nama pilihan varian kosong atau terlalu panjang untuk Store")
            previous = current.get(p.sku_induk)
            v = VarianIn(id=previous.id if previous else None, nama=name, sku=p.sku_induk, opsi=json.loads(option.opsi_json),
                         harga=p.harga_dasar, stok=previous.stok if previous else (legacy_stock if legacy_id == p.id else 0),
                         expected_stok=previous.stok if previous else None, aktif=p.aktif,
                         berat_gram=p.berat_gram, panjang_cm=p.panjang_cm, lebar_cm=p.lebar_cm, tinggi_cm=p.tinggi_cm)
            inputs = [i for i in inputs if i.sku != p.sku_induk] + [v]
        await services.ganti_varian(store_session, copy.id, inputs)
        await store_session.refresh(copy, attribute_names=["varian"])
    for k in sources:
        k.dikirim_toko_id = copy.id
    await session.flush()
    return {"dibuat": created, "foto_disalin": bool(keys), "foto_gagal": photo_failed,
            "pesan": "Foto belum lengkap; produk disimpan sebagai draft" if photo_failed else None,
            "produk": services.produk_out(copy)}
