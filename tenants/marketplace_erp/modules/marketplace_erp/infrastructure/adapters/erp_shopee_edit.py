"""Apply a one-screen product edit to Shopee: only the changed parts, in the order Shopee needs.

update_item -> delete_model -> update_tier_variation / init_tier_variation -> add_model -> update_model
-> update_price -> update_stock. Stock and price go straight to Shopee (the ERP keeps no stock of its own).
Shopee has no transaction: a failing step stops the rest and the error names the steps already applied.
"""
from fastapi import HTTPException

from . import erp_shopee as provider, erp_shopee_commerce as commerce, erp_shopee_management as management, erp_shopee_workflows as workflows
from ...application.commerce_schemas import ModelAdd, ModelsInit
from ...application.edit_schemas import ProductEdit

LANGKAH = {
    "informasi": "Informasi produk",
    "hapus_varian": "Hapus varian",
    "variasi": "Nama variasi & pilihan",
    "tambah_varian": "Tambah varian",
    "varian": "SKU/berat/pre-order varian",
    "harga": "Harga",
    "stok": "Stok",
}


def tier_body(current: dict, tiers, keep_models: list[dict]) -> dict:
    """standardise_tier_variation for renamed/appended options, keeping Shopee's standard ids when unchanged."""
    old = current.get("tier_variation") or []
    std = current.get("standardise_tier_variation") or []
    if len(tiers) != len(old):
        raise HTTPException(409, "Menambah atau menghapus jenis variasi pada produk yang sudah bervarian belum didukung. Ubah di Seller Center.")
    out = []
    for t, (tier, before) in enumerate(zip(tiers, old)):
        names = [o.get("option") for o in before.get("option_list") or []]
        known = [o for o in tier.options if o.asal is not None]
        if [o.asal for o in known] != list(range(len(names))) or any(o.asal is None for o in tier.options[: len(names)]):
            raise HTTPException(409, "Pilihan lama tidak boleh dihapus atau diurutkan ulang di sini; pilihan baru ditambahkan di akhir.")
        s = std[t] if t < len(std) and isinstance(std[t], dict) else {}
        s_opts = s.get("variation_option_list") or []
        renamed_tier = tier.name.strip() != str(before.get("name") or "").strip()
        variation_id = 0 if renamed_tier else int(s.get("variation_id") or 0)
        row = {"variation_id": variation_id, "variation_option_list": []}
        if variation_id == 0:
            row["variation_name"] = tier.name.strip()
        elif s.get("variation_group_id"):
            row["variation_group_id"] = s["variation_group_id"]
        for o in tier.options:
            prev = (s_opts[o.asal] if o.asal is not None and o.asal < len(s_opts) else {}) or {}
            image = prev.get("image_id") or ((before.get("option_list") or [{}])[o.asal].get("image") or {}).get("image_id") if o.asal is not None else None
            same = o.asal is not None and o.option.strip() == str(names[o.asal] or "").strip()
            option_id = int(prev.get("variation_option_id") or 0) if same and variation_id else 0
            entry = {"variation_option_id": option_id}
            if option_id == 0:
                entry["variation_option_name"] = o.option.strip()
            if image:
                entry["image_id"] = image
            row["variation_option_list"].append(entry)
        out.append(row)
    return {"standardise_tier_variation": out, "model_list": [{"model_id": int(m["model_id"]), "tier_index": m["tier_index"]} for m in keep_models]}


def tiers_changed(current: dict, tiers) -> bool:
    old = [(str(t.get("name") or "").strip(), [str(o.get("option") or "").strip() for o in t.get("option_list") or []]) for t in current.get("tier_variation") or []]
    return old != [(t.name.strip(), [o.option.strip() for o in t.options]) for t in tiers]


def failures(path, data, kind):
    resp = data.get("response") or {}
    failed = resp.get("failure_list") or []
    if data.get("error") or failed or not data.get("request_id"):
        reason = "; ".join(str(f.get("failed_reason") or f) for f in failed) or data.get("message") or data.get("error") or "tanpa konfirmasi"
        raise provider.ShopeeAPIError(path, "partial_failure", f"{kind} ditolak Shopee: {reason}", data.get("request_id"))


async def post(session, akun, path, body):
    return await provider.signed_shop_request(session, akun, path, method="POST", body=body)


async def apply(session, akun, item_id, p: ProductEdit) -> dict:
    item_id = int(item_id)
    done: list[str] = []
    warnings: list[str] = []
    current = await workflows.read(session, akun, "/api/v2/product/get_model_list", {"item_id": item_id})
    models = current.get("model") or []
    known = {int(m["model_id"]) for m in models}
    has_variants = bool(models)
    if not set(p.delete_models) <= known or not {m.model_id for m in p.models} <= known:
        raise HTTPException(409, "Daftar varian berubah di Shopee. Muat ulang halaman lalu ulangi.")
    if (p.price is not None or p.stock is not None) and has_variants:
        raise HTTPException(422, "Produk bervarian: ubah harga/stok per varian.")
    if p.delete_models and len(known - set(p.delete_models)) + len(p.add_models) < 1:
        raise HTTPException(409, "Minimal satu varian harus tersisa.")

    async def step(key, coro):
        try:
            result = await coro
        except Exception as exc:  # report what already reached Shopee
            detail = getattr(exc, "detail", None) or getattr(exc, "message", None) or str(exc)
            sudah = ", ".join(LANGKAH[d] for d in done) or "belum ada"
            raise HTTPException(getattr(exc, "status_code", 502) if isinstance(exc, HTTPException) else 502,
                                f"Gagal pada langkah '{LANGKAH[key]}': {detail} Sudah tersimpan di Shopee: {sudah}.") from exc
        done.append(key)
        warnings.extend((result or {}).get("warnings", []) if isinstance(result, dict) else [])
        return result

    if p.item:
        await step("informasi", management.update_item(session, akun, item_id, p.item))
    for model_id in p.delete_models:
        await step("hapus_varian", commerce.acknowledged(session, akun, "/api/v2/product/delete_model", {"item_id": item_id, "model_id": model_id}))
    keep = [m for m in models if int(m["model_id"]) not in set(p.delete_models)]
    if p.tiers and not has_variants:
        if p.price is not None or p.stock is not None:
            raise HTTPException(422, "Saat membuat variasi, harga & stok diisi per varian baru.")
        await step("variasi", commerce.init_models(session, akun, item_id, ModelsInit(
            tiers=[{"name": t.name.strip(), "options": [{"option": o.option.strip()} for o in t.options]} for t in p.tiers],
            models=[m.model_dump() for m in p.add_models])))
    else:
        if p.tiers and tiers_changed(current, p.tiers):
            body = tier_body(current, p.tiers, keep)

            async def tiers():
                data = await post(session, akun, "/api/v2/product/update_tier_variation", {"item_id": item_id, **body})
                failures("update_tier_variation", data, "Variasi")
                return {"warnings": [str(data["warning"])] if data.get("warning") else []}
            await step("variasi", tiers())
        if p.add_models:
            await step("tambah_varian", commerce.add_models(session, akun, item_id, ModelAdd(models=[m.model_dump() for m in p.add_models])))
    edits = [m for m in p.models if m.model_sku is not None or m.weight is not None or m.pre_order is not None]
    if edits:
        rows = []
        by_id = {int(m["model_id"]): m for m in models}
        for m in edits:
            row = {"model_id": m.model_id, "model_sku": m.model_sku if m.model_sku is not None else by_id[m.model_id].get("model_sku") or ""}
            if m.weight is not None:
                row["weight"] = float(m.weight)
            if m.pre_order is not None:
                row["pre_order"] = m.pre_order.model_dump()
            rows.append(row)

        async def update_model():
            data = await post(session, akun, "/api/v2/product/update_model", {"item_id": item_id, "model": rows})
            failures("update_model", data, "Varian")
            return {}
        await step("varian", update_model())
    prices = [{"model_id": m.model_id, "original_price": float(m.price)} for m in p.models if m.price is not None]
    if p.price is not None:
        prices = [{"model_id": 0, "original_price": float(p.price)}]
    if prices:
        async def price():
            data = await post(session, akun, "/api/v2/product/update_price", {"item_id": item_id, "price_list": prices})
            failures("update_price", data, "Harga")
            return {}
        await step("harga", price())
    stocks = [{"model_id": m.model_id, "seller_stock": [{"stock": m.stock}]} for m in p.models if m.stock is not None]
    if p.stock is not None:
        stocks = [{"model_id": 0, "seller_stock": [{"stock": p.stock}]}]
    if stocks:
        async def stock():
            data = await post(session, akun, "/api/v2/product/update_stock", {"item_id": item_id, "stock_list": stocks})
            failures("update_stock", data, "Stok")
            return {}
        await step("stok", stock())
    if any(m.price is not None for m in p.models) or p.price is not None:
        warnings.append("Harga yang sedang ikut promosi Shopee tetap memakai harga promosi sampai promosinya selesai.")
    return {"ok": True, "langkah": [LANGKAH[d] for d in dict.fromkeys(done)], "warnings": warnings}
