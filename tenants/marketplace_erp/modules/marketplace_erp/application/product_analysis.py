"""Isolated Shopee product read model. Never sync or mutate operational rows."""
import json
import re
from decimal import Decimal
from datetime import datetime, time, timedelta
from fastapi import HTTPException
from sqlalchemy import case, cast, func, select
from sqlalchemy.dialects.postgresql import JSONB
from ..infrastructure.models import (
    AkunMarketplace as A, ItemPesanan as I, KatalogShopee as K,
    Pesanan as O, Produk as P, ProdukKeluarga as F, ProdukVarian as V,
)
from .summary import JAKARTA
LIMIT = 20000
NOTES = [
    "Hanya snapshot ERP. Periode memakai tanggal pesanan Asia/Jakarta; status terakhir.",
    "Omzet: subtotal barang setelah diskon item, sebelum biaya marketplace dan ongkir. Bukan omzet bersih atau dana pencairan.",
    "Belum bayar, batal, status tidak dikenal dan retur/refund dikeluarkan. Ledger refund per baris belum tersedia; retur sebagian pada status biasa belum dapat dikoreksi. Angka sementara.",
    "Tanpa pemetaan master, listing terpisah per toko/item/model. Nama mirip tidak digabung. Tinjau pemetaan melalui pengelolaan listing/master Produk yang ada; analisis tidak mengubahnya.",
    "Stok adalah snapshot katalog beserta waktu pengambilan, bukan jaminan stok saat ini. URL membuka produk; pilih varian sesuai pesan.",
]
def public_link(shop, item):
    if all(re.fullmatch(r"[1-9][0-9]*", str(x or "")) for x in (shop, item)):
        return f"https://shopee.co.id/product/{shop}/{item}"
    return None

def project(rows, search="", page=1, size=25):
    groups = {}
    needle = search.casefold().strip()
    for r in rows:
        if needle and needle not in " ".join(str(r.get(k) or "") for k in
                ("name", "variant", "item_sku", "model_sku", "master_sku", "family_name", "master_name")).casefold():
            continue
        listing = f"{r['account']}:{r['item']}" if r["item"] else f"unknown:{r['line']}"
        pk = f"family:{r['family']}" if r["family"] else (
            f"master:{r['product']}" if r["product"] else f"listing:{listing}")
        has_variant = bool(r["variant"] or r["model"] not in (None, "", "0") or r["family"])
        vk = f"master:{r['product']}" if r["product"] else f"{listing}:{r['model'] or 'none'}"
        name = r["family_name"] or r["master_name"] or r["name"] or "Produk belum bernama"
        g = groups.setdefault(pk, dict(key=pk, name=name, units=0, sales=Decimal(0), variants={}, shops=[]))
        units, sales = int(r["units"]), Decimal(r["sales"])
        g["units"] += units
        g["sales"] += sales
        target = g
        if has_variant:
            target = g["variants"].setdefault(vk, dict(key=vk,
                name=r["variant"] or (r["master_name"] if r["family"] else None) or "Nama varian belum tersedia",
                units=0, sales=Decimal(0), shops=[]))
            target["units"] += units
            target["sales"] += sales
        target["shops"].append(dict(key=f"{listing}:{r['model']}:{r['line']}", name=r["shop_name"] or "Toko belum bernama",
            units=units, sales=sales, shop_id=r["shop_id"], item_id=r["item"], model_id=r["model"],
            url=public_link(r["shop_id"], r["item"])))
    result = sorted(groups.values(), key=lambda g: (-g["units"], g["name"], g["key"]))
    for g in result:
        g["variants"] = sorted(g["variants"].values(), key=lambda v: (-v["units"], v["name"], v["key"]))
        for v in [g, *g["variants"]]:
            shops = {}
            for s in v["shops"]:
                key = (s["shop_id"], s["item_id"], s["model_id"]) if s["item_id"] else s["key"]
                if key in shops:
                    shops[key]["units"] += s["units"]
                    shops[key]["sales"] += s["sales"]
                else:
                    shops[key] = dict(s)
            v["shops"] = sorted(shops.values(), key=lambda s: (-s["units"], s["name"], s["key"]))
    return dict(rows=result[(page-1)*size:page*size], total=len(result),
                totals=dict(units=sum(g["units"] for g in result), sales=sum((g["sales"] for g in result), Decimal(0))))


async def read(session, start, end, allowed, shop="", search="", page=1, size=25):
    begin = datetime.combine(start, time.min, JAKARTA)
    finish = datetime.combine(end + timedelta(days=1), time.min, JAKARTA)
    fields = [
        A.id.label("account"), A.nama_toko.label("shop_name"), A.id_toko_eksternal.label("shop_id"),
        I.item_id_eksternal.label("item"), I.model_id_eksternal.label("model"),
        I.produk_id.label("product"), V.keluarga_id.label("family"), F.nama.label("family_name"),
        P.nama.label("master_name"), P.sku_induk.label("master_sku"),
        case((I.item_id_eksternal.is_(None), I.id), else_=None).label("unidentified"),
        I.nama_produk.label("name"), I.model_name.label("variant"), I.item_sku, I.model_sku,
    ]
    stmt = select(*fields, func.min(I.id).label("line"), func.sum(I.qty).label("units"),
                  func.sum(I.subtotal).label("sales")).select_from(I).join(O, O.id == I.pesanan_id).join(A, A.id == O.akun_id)
    stmt = stmt.outerjoin(P, P.id == I.produk_id).outerjoin(V, V.produk_id == P.id).outerjoin(F, F.id == V.keluarga_id)
    # Cancellation in either status wins, including stale local shipped.
    stmt = stmt.where(O.platform == "shopee", A.platform == "shopee",
        func.coalesce(O.dipesan_at, O.created_at) >= begin, func.coalesce(O.dipesan_at, O.created_at) < finish,
        cast(O.detail_json, JSONB)["currency"].astext == "IDR",
        O.status.in_(["to_ship", "shipped", "completed"]),
        func.coalesce(O.status_marketplace, "").in_(["", "READY_TO_SHIP", "PROCESSED", "SHIPPED", "COMPLETED", "TO_CONFIRM_RECEIVE"]),
        I.qty > 0, I.subtotal >= 0)
    if allowed is not None:
        stmt = stmt.where(A.id.in_(allowed))
    if shop:
        stmt = stmt.where(A.id == shop)
    data = (await session.execute(stmt.group_by(*fields).limit(LIMIT+1))).mappings().all()
    if len(data) > LIMIT:
        raise HTTPException(422, "Terlalu banyak kelompok. Persempit periode atau toko; hasil tidak dipotong diam-diam.")
    return project([dict(r) for r in data], search, page, size)


def catalogue_rows(entries, search=""):
    rows = []
    needle = search.casefold().strip()
    for e in entries:
        try:
            variants = json.loads(e["variants"] or "[]")
        except (ValueError, TypeError):
            variants = []
        if not isinstance(variants, list):
            variants = []
        for n, v in enumerate(variants or [{}]):
            if not isinstance(v, dict):
                continue
            name, variant = e["name"] or "Produk belum bernama", str(v.get("nama") or "")
            sku = str(v.get("sku") or e["sku"] or "")
            if needle and needle not in f"{name} {variant} {sku} {e['sku']}".casefold():
                continue
            url = public_link(e["shop_id"], e["item"])
            message = f"Produk {name} tercantum di {e['shop_name']}. Silakan buka link berikut"
            message += f" dan pilih varian {variant}" if variant else ""
            message += f": {url}" if url else "."
            rows.append(dict(key=f"{e['account']}:{e['item']}:{v.get('model_id', n)}", name=name,
                variant=variant, sku=sku, shop=e["shop_name"], shop_id=e["shop_id"], item_id=e["item"],
                model_id=v.get("model_id"), stock=v.get("stok", e["stock"] if not variants else None),
                status=e["status"], as_of=e["as_of"], url=url, message=message if url else None,
                reason=None if url else "shop_id atau item_id belum lengkap/valid"))
    return sorted(rows, key=lambda r: (r["name"], r["variant"], r["shop"], r["key"]))


async def links(session, allowed, shop="", search="", page=1, size=25):
    stmt = select(K.akun_id.label("account"), A.nama_toko.label("shop_name"),
        A.id_toko_eksternal.label("shop_id"), K.item_id.label("item"), K.nama.label("name"),
        K.sku, K.varian_json.label("variants"), K.stok_shopee.label("stock"), K.status,
        K.diambil_at.label("as_of")).join(A, A.id == K.akun_id).where(A.platform == "shopee")
    if allowed is not None:
        stmt = stmt.where(A.id.in_(allowed))
    if shop:
        stmt = stmt.where(A.id == shop)
    entries = (await session.execute(stmt.limit(LIMIT+1))).mappings().all()
    if len(entries) > LIMIT:
        raise HTTPException(422, "Katalog terlalu besar. Pilih satu toko.")
    rows = catalogue_rows(entries, search)
    return dict(rows=rows[(page-1)*size:page*size], total=len(rows))
