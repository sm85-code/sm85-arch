"""Batch catalogue reads from the same read-only source session as the orders."""
from sqlalchemy import select
from sqlalchemy.orm import selectinload


def metadata(sku, parent, name, image, variant, price):
    if not (sku or "").strip():
        return None  # No fabricated SKU and no merging different variants under a parent SKU.
    return {"sku": sku.strip(), "sku_induk": (parent or "").strip() or None,
            "nama_asli": name, "gambar_url": image or "", "harga_jual": str(price),
            "varian_list": [{"kategori": "Varian", "nilai": variant}] if variant else []}


async def product_metadata(source, system, orders):
    lines = [line for order in orders for line in order.items]
    product_ids = {line.produk_id for line in lines if line.produk_id}
    if system == "marketplace_erp":
        from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import Produk
        products = {row.id: row for row in (await source.execute(select(Produk).where(Produk.id.in_(product_ids)))).scalars()} if product_ids else {}
        output = {}
        for line in lines:
            product = products.get(line.produk_id)
            sku = line.model_sku or (line.item_sku if not line.model_name and not line.model_id_eksternal else "")
            parent = product.sku_induk if product else line.item_sku
            output[line.id] = metadata(sku, parent, line.nama_produk,
                line.foto_url or (product.foto_url if product else None), line.model_name, line.harga_satuan)
        return output
    from tenants.store.modules.store.infrastructure.models import ProdukStore
    from tenants.store.modules.store.infrastructure.media_storage import media_url
    products = {row.id: row for row in (await source.execute(select(ProdukStore).where(ProdukStore.id.in_(product_ids))
                .options(selectinload(ProdukStore.varian), selectinload(ProdukStore.foto)))).scalars()} if product_ids else {}
    output = {}
    for line in lines:
        product = products.get(line.produk_id)
        variant = next((row for row in product.varian if row.id == line.varian_id), None) if product else None
        photo = next((row for row in product.foto if variant and row.id == variant.foto_id), None) if product else None
        # Store has no parent SKU column. Variant SKU and existing media_url are authoritative.
        output[line.id] = metadata(variant.sku if variant else None, None, line.nama_produk,
            media_url(photo.foto_key if photo else product.foto_key) if product else None, line.nama_varian, line.harga_satuan)
    return output
