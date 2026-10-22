"""Internal work queue. No marketplace adapter, payment, stock or journal side effects."""

import json
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    AkunMarketplace,
    PLATFORM_MANUAL,
    ItemPesanan,
    Pesanan,
    ProcessingCatalog,
    ProcessingJob,
    ProcessingPrice,
    ProcessingPaintColor,
    ProcessingProductionStructure,
    ProcessingOrderSequence,
    ProcessingParty,
    UserMarketplaceErp,
)

admin = require_roles_marketplace_erp("admin")
router = APIRouter(dependencies=[Depends(admin)])

# The Data Produksi page that holds each kind of price.
HALAMAN_HARGA = {
    "produksi": "data_produk_kayu",
    "non_kayu": "data_katalog_manual",
    "pengecatan": "data_tarif_pengecatan",
    "packing": "data_tarif_packing",
}


# The Pemetaan Order tab (page of the role settings) of each route and stage.
HALAMAN_TAB = {
    "tukang": "processing_tukang",
    "supplier": "processing_supplier",
    "pengecatan": "processing_pengecatan",
    "packing": "processing_packing",
}
TAB_STAGE = {"produksi": "tukang", "supplier": "supplier", "pengecatan": "pengecatan", "packing": "packing"}


async def izin_tab(session, user, tab: str) -> None:
    """Changing a job needs "tulis" on the tab it belongs to; the route rule only knows it is some Pemetaan Order tab."""
    from tenants.marketplace_erp.modules.marketplace_erp.application import akses

    punya = await akses.tingkat_halaman(session, await akses.peran_pengguna(session, user), HALAMAN_TAB[tab])
    if punya != "tulis":
        raise HTTPException(403, "Akses ditolak")


async def izin_harga(session, user, kind: str) -> None:
    """Changing a price needs "tulis" on the page of its kind; the route rule can only tell that it is some price page."""
    from tenants.marketplace_erp.modules.marketplace_erp.application import akses

    punya = await akses.tingkat_halaman(session, await akses.peran_pengguna(session, user), HALAMAN_HARGA[kind])
    if punya != "tulis":
        raise HTTPException(403, "Akses ditolak")
Money = Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=2)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class VarianInput(Strict):
    nama: str = Field(max_length=60)
    nilai: str = Field(max_length=100)


class PriceInput(Strict):
    version: int = Field(ge=0)
    kind: Literal["produksi", "pengecatan", "non_kayu", "packing"]
    produk: str = Field(min_length=1, max_length=300)
    jenis: str | None = Field(default=None, max_length=200)
    packing_type: Literal["biasa", "kayu"] | None = None
    structure_model_id: str | None = Field(default=None, max_length=80)
    warna: str | None = Field(default=None, max_length=200)
    color_id: str | None = Field(default=None, max_length=80)
    production_price_id: str | None = Field(default=None, max_length=80)
    sku: str = Field(default="", max_length=200)
    ukuran: str = Field(default="", max_length=100)
    # Manual (non-wood) products: any number of variant attributes, e.g. Ukuran 10x20 and Jenis Basah.
    varian: list[VarianInput] | None = Field(default=None, max_length=8)
    # Optional only for manual products ("harga menyusul"); every other kind still requires it.
    harga: Money | None = None
    catatan: str = Field(default="", max_length=2000)


class StructureInput(Strict):
    version: int = Field(ge=0)
    kind: Literal["kategori", "produk", "model"]
    parent_id: str = Field(default="", max_length=80)
    code: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)


class ColorInput(Strict):
    version: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=200)


@router.get("/paint-colors")
async def paint_colors(session: AsyncSession = Depends(get_db_marketplace_erp)):
    return {"items": [dict(id=r.id, name=r.name, version=r.version) for r in
                      (await session.scalars(select(ProcessingPaintColor).order_by(ProcessingPaintColor.name))).all()]}


@router.put("/paint-colors/{color_id}")
async def save_paint_color(color_id: str, payload: ColorInput, session: AsyncSession = Depends(get_db_marketplace_erp)):
    if not color_id or len(color_id) > 80:
        raise HTTPException(422, "ID warna tidak valid")
    name = " ".join(payload.name.split())
    if not name or name.casefold() == "polos / tanpa cat":
        raise HTTPException(422, "Isi nama warna cat; tanpa cat dipilih pada pemetaan")
    current = await session.get(ProcessingPaintColor, color_id)
    if (current and current.version != payload.version) or (not current and payload.version):
        raise HTTPException(409, "Warna telah berubah. Muat ulang.")
    try:
        if current:
            result = await session.execute(update(ProcessingPaintColor).where(
                ProcessingPaintColor.id == color_id, ProcessingPaintColor.version == payload.version
            ).values(name=name, normalized_name=name.casefold(), version=payload.version + 1))
            if result.rowcount != 1:
                raise HTTPException(409, "Warna baru saja berubah. Muat ulang.")
        else:
            session.add(ProcessingPaintColor(id=color_id, name=name, normalized_name=name.casefold(), version=1))
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Nama warna sudah ada")
    return {"id": color_id, "tersimpan": True}


async def structure_nodes(session):
    return {r.id: r for r in (await session.scalars(select(ProcessingProductionStructure))).all()}


def structure_fields(nodes, model_id, size):
    model = nodes.get(model_id)
    product = nodes.get(model.parent_key) if model else None
    category = nodes.get(product.parent_key) if product else None
    if (
        not model
        or model.kind != "model"
        or not product
        or product.kind != "produk"
        or not category
        or category.kind != "kategori"
    ):
        raise HTTPException(422, "Pilih kategori, produk, dan model yang tersedia")
    normalized = re.sub(r"\s+", "", size).replace("×", "x").replace("*", "x")
    normalized = re.sub(r"(?<=\d)X(?=\d)", "x", normalized)
    if not normalized or not re.fullmatch(r"[A-Za-z0-9.,xX-]+", normalized):
        raise HTTPException(422, "Isi ukuran, misalnya 150x200")
    sku = ".".join([category.code, product.code, model.code, normalized])
    if len(sku) > 200:
        raise HTTPException(422, "Gabungan kode dan ukuran terlalu panjang")
    return dict(
        structure_model_id=model.id,
        category_id=category.id,
        product_id=product.id,
        jenis=category.name,
        kategori_kode=category.code,
        produk=product.name,
        sku_induk=product.code,
        model=model.name,
        model_kode=model.code,
        ukuran=normalized,
        sku=sku,
    )


@router.get("/production-structure")
async def production_structure(session: AsyncSession = Depends(get_db_marketplace_erp)):
    return {
        "items": [
            dict(id=n.id, kind=n.kind, parent_id=n.parent_key, code=n.code, name=n.name, version=n.version)
            for n in (await structure_nodes(session)).values()
            if n.kind != "varian"
        ]
    }


@router.put("/production-structure/{node_id}")
async def save_structure(
    node_id: str, payload: StructureInput, session: AsyncSession = Depends(get_db_marketplace_erp)
):
    await lock_prices(session)
    if not node_id or len(node_id) > 80:
        raise HTTPException(422, "ID struktur tidak valid")
    code = payload.code.upper()
    pattern = r"[A-Z0-9]+(?:[.-][A-Z0-9]+)*" if payload.kind == "produk" else r"[A-Z0-9]+(?:-[A-Z0-9]+)*"
    if not re.fullmatch(pattern, code):
        raise HTTPException(422, "Kode memakai huruf/angka tanpa spasi; titik hanya untuk SKU induk")
    nodes = await structure_nodes(session)
    current = nodes.get(node_id)
    expected_parent = {"kategori": None, "produk": "kategori", "model": "produk"}[payload.kind]
    parent = nodes.get(payload.parent_id)
    if (expected_parent is None and payload.parent_id) or (
        expected_parent and (not parent or parent.kind != expected_parent)
    ):
        raise HTTPException(422, "Induk struktur tidak sesuai")
    if current and (current.kind != payload.kind or current.parent_key != payload.parent_id):
        raise HTTPException(422, "Jenis dan induk struktur yang sudah dibuat tidak dapat diganti")
    if (current and current.version != payload.version) or (not current and payload.version):
        raise HTTPException(409, "Struktur telah berubah. Muat ulang.")
    try:
        if current:
            result = await session.execute(
                update(ProcessingProductionStructure)
                .where(
                    ProcessingProductionStructure.id == node_id,
                    ProcessingProductionStructure.version == payload.version,
                )
                .values(code=code, name=payload.name, version=payload.version + 1)
            )
            if result.rowcount != 1:
                raise HTTPException(409, "Struktur baru saja berubah. Muat ulang.")
        else:
            session.add(
                ProcessingProductionStructure(
                    id=node_id, kind=payload.kind, parent_key=payload.parent_id, code=code, name=payload.name, version=1
                )
            )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Kode sudah digunakan pada kategori/produk ini")
    return {"id": node_id, "tersimpan": True}


async def lock_prices(session):
    # Serialize retirement against new order assignments in PostgreSQL.
    if session.bind.dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(8265071801)"))


def product_name(price):
    """Name sent to the supplier: the product plus its variants, e.g. "Pupuk (Ukuran: 5 kg, Jenis: Basah)"."""
    varian = price.get("varian") or []
    if not varian:
        return price["produk"]
    return f'{price["produk"]} ({", ".join(v["nama"] + ": " + v["nilai"] for v in varian)})'[:300]


async def price_list(session, *, include_inactive=False, include_deleted=False):
    original = await session.get(ProcessingCatalog, "global")
    data = json.loads(original.payload_json) if original else {}
    prices = {r["id"]: dict(r, kind="produksi", version=0) for r in data.get("harga_produksi", [])}
    for row in (await session.scalars(select(ProcessingPrice))).all():
        prices[row.id] = dict(json.loads(row.payload_json), id=row.id, kind=row.kind, version=row.version)
    nodes = await structure_nodes(session)
    for value in prices.values():
        if not value.get("deleted") and value.get("structure_model_id"):
            value.update(structure_fields(nodes, value["structure_model_id"], value["ukuran"]))
    return [dict(p, active=p.get("active", True)) for p in prices.values()
            if (include_deleted or not p.get("deleted"))
            and (include_inactive or p.get("active", True))]


async def used_price_ids(session):
    used = set()
    for row in (await session.scalars(select(ProcessingJob))).all():
        value = json.loads(row.payload_json)
        for field in ("price_id", "paint_price_id"):
            if value.get(field):
                used.add(value[field])
        for field in ("production_reference", "painting_reference"):
            if (value.get(field) or {}).get("id"):
                used.add(value[field]["id"])
    for price in await price_list(session, include_inactive=True):
        if price.get("production_price_id"):
            used.add(price["production_price_id"])
    return used


@router.get("/prices")
async def prices(include_inactive: bool = False, session: AsyncSession = Depends(get_db_marketplace_erp)):
    used = await used_price_ids(session)
    return {"items": [dict(p, in_use=p["id"] in used) for p in
                      await price_list(session, include_inactive=include_inactive)]}


async def change_price_status(session, price_id, version, *, activate=False):
    await lock_prices(session)
    current = next((p for p in await price_list(session, include_inactive=True) if p["id"] == price_id), None)
    if not current:
        raise HTTPException(404, "Produk katalog tidak ditemukan")
    if current["version"] != version:
        raise HTTPException(409, "Produk telah berubah. Muat ulang katalog.")
    if current["kind"] not in ("produksi", "non_kayu"):
        raise HTTPException(422, "Aksi ini hanya untuk katalog produk")
    used = price_id in await used_price_ids(session)
    action = "activated" if activate else "deactivated" if used else "deleted"
    value = dict(current, active=activate, deleted=action == "deleted")
    value.pop("version", None)
    if version:
        result = await session.execute(update(ProcessingPrice).where(
            ProcessingPrice.id == price_id, ProcessingPrice.version == version
        ).values(payload_json=json.dumps(value), version=version + 1))
        if result.rowcount != 1:
            raise HTTPException(409, "Produk baru saja berubah. Muat ulang katalog.")
    else:
        # Override imported entries too, so an original import cannot revive a removed product.
        session.add(ProcessingPrice(id=price_id, kind=current["kind"], payload_json=json.dumps(value), version=1))
    if action == "deleted":
        await session.execute(delete(ProcessingProductionStructure).where(
            ProcessingProductionStructure.id == price_id, ProcessingProductionStructure.kind == "varian"
        ))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Produk baru saja berubah. Muat ulang katalog.")
    return {"id": price_id, "action": action}


async def izin_harga_id(session, user, price_id: str) -> None:
    current = next((p for p in await price_list(session, include_inactive=True, include_deleted=True) if p["id"] == price_id), None)
    if current:
        await izin_harga(session, user, current["kind"])


@router.delete("/prices/{price_id}")
async def retire_price(price_id: str, version: int = Query(ge=0),
                       session: AsyncSession = Depends(get_db_marketplace_erp),
                       actor: UserMarketplaceErp = Depends(admin)):
    await izin_harga_id(session, actor, price_id)
    return await change_price_status(session, price_id, version)


@router.post("/prices/{price_id}/activate")
async def activate_price(price_id: str, version: int = Query(ge=0),
                         session: AsyncSession = Depends(get_db_marketplace_erp),
                         actor: UserMarketplaceErp = Depends(admin)):
    await izin_harga_id(session, actor, price_id)
    return await change_price_status(session, price_id, version, activate=True)


@router.put("/prices/{price_id}")
async def save_price(price_id: str, payload: PriceInput, session: AsyncSession = Depends(get_db_marketplace_erp),
                     actor: UserMarketplaceErp = Depends(admin)):
    await izin_harga(session, actor, payload.kind)
    await lock_prices(session)
    if payload.kind == "pengecatan" and payload.warna is not None and not payload.warna:
        raise HTTPException(422, "Pilih warna cat untuk tarif pengecatan")
    if len(price_id) > 80:
        raise HTTPException(422, "ID harga terlalu panjang")
    current = next((p for p in await price_list(session, include_inactive=True, include_deleted=True) if p["id"] == price_id), None)
    if current and current.get("deleted"):
        raise HTTPException(409, "Produk telah dihapus. Buat produk baru.")
    if current and (current["version"] != payload.version or current["kind"] != payload.kind):
        raise HTTPException(409, "Harga telah berubah. Muat ulang sebelum menyimpan.")
    if not current and payload.version != 0:
        raise HTTPException(409, "Harga tidak ditemukan; muat ulang")
    if payload.harga is None and payload.kind != "non_kayu":
        raise HTTPException(422, "Harga wajib diisi")
    value = dict(current or {}, **payload.model_dump(mode="json", exclude={"version"}, exclude_none=True), id=price_id)
    if payload.harga is None:
        value.pop("harga", None)
    if payload.varian is not None:
        if payload.kind != "non_kayu":
            raise HTTPException(422, "Varian hanya untuk produk manual")
        cleaned, seen = [], set()
        for item in payload.varian:
            nama, nilai = " ".join(item.nama.split()), " ".join(item.nilai.split())
            if not nama and not nilai:
                continue
            if not nama or not nilai:
                raise HTTPException(422, "Isi nama dan nilai pada setiap varian")
            if nama.casefold() in seen:
                raise HTTPException(422, f"Nama varian {nama} dipakai dua kali")
            seen.add(nama.casefold())
            cleaned.append({"nama": nama, "nilai": nilai})
        label = " · ".join(v["nilai"] for v in cleaned)
        if len(label) > 100:
            raise HTTPException(422, "Nilai varian terlalu panjang")
        # `ukuran` stays the one-line summary of the variants, so search, lists and order mapping keep working.
        value["varian"], value["ukuran"] = cleaned, label
    elif payload.kind == "non_kayu":
        value.pop("varian", None)
    if payload.kind == "pengecatan":
        if payload.production_price_id:
            source = next((p for p in await price_list(session, include_inactive=True)
                           if p["id"] == payload.production_price_id
                           and (p.get("active", True) or (current or {}).get("production_price_id") == p["id"])
                           and p["kind"] == "produksi" and p.get("structure_model_id")), None)
            if not source:
                raise HTTPException(422, "Pilih produk produksi yang sudah disusun strukturnya")
            for field in ("produk", "sku", "ukuran", "jenis", "model", "model_kode"):
                value[field] = source.get(field, "")
        elif payload.production_price_id == "":
            # Manual tariff: the model name (in `produk`) and the size are typed, not taken from a production product.
            for field in ("production_price_id", "model", "model_kode", "jenis"):
                value.pop(field, None)
        if payload.color_id:
            color = await session.get(ProcessingPaintColor, payload.color_id)
            if not color:
                raise HTTPException(422, "Warna tidak ditemukan; pilih kembali")
            value["warna"] = color.name
    elif payload.production_price_id or payload.color_id:
        raise HTTPException(422, "Produk pengecatan dan warna hanya untuk tarif pengecatan")
    model_id = value.get("structure_model_id")
    if model_id:
        if payload.kind != "produksi":
            raise HTTPException(422, "Struktur hanya untuk harga produksi")
        value.update(structure_fields(await structure_nodes(session), model_id, payload.ukuran))
    elif current and current.get("structure_model_id"):
        raise HTTPException(422, "Pilih model untuk harga yang sudah terstruktur")
    if payload.kind == "packing":
        value.setdefault("packing_type", "biasa")
    value.setdefault("sumber", "Input admin")
    serialized = json.dumps(value)
    try:
        if current and current["version"]:
            result = await session.execute(
                update(ProcessingPrice)
                .where(
                    ProcessingPrice.id == price_id,
                    ProcessingPrice.version == payload.version,
                )
                .values(payload_json=serialized, version=payload.version + 1)
            )
            if result.rowcount != 1:
                raise HTTPException(409, "Harga baru saja berubah; muat ulang")
        else:
            session.add(ProcessingPrice(id=price_id, kind=payload.kind, payload_json=serialized, version=1))
        if model_id:
            variant = await session.get(ProcessingProductionStructure, price_id)
            if variant and variant.kind != "varian":
                raise HTTPException(409, "ID harga berbenturan dengan struktur")
            if variant:
                variant.parent_key = model_id
                variant.code = value["ukuran"].casefold()
                variant.name = value["ukuran"]
            else:
                session.add(
                    ProcessingProductionStructure(
                        id=price_id,
                        kind="varian",
                        parent_key=model_id,
                        code=value["ukuran"].casefold(),
                        name=value["ukuran"],
                        version=1,
                    )
                )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Harga berubah atau ukuran pada model ini sudah ada; muat ulang")
    return {"tersimpan": True}


class BulkProductionItem(Strict):
    id: str = Field(min_length=1, max_length=80)
    version: int = Field(ge=0)
    ukuran: str = Field(min_length=1, max_length=100)
    harga: Money


class BulkProductionInput(Strict):
    structure_model_id: str = Field(min_length=1, max_length=80)
    items: list[BulkProductionItem] = Field(min_length=1, max_length=100)


@router.post("/prices/bulk-structure")
async def bulk_production_structure(payload: BulkProductionInput,
                                    session: AsyncSession = Depends(get_db_marketplace_erp),
                                    actor: UserMarketplaceErp = Depends(admin)):
    await izin_harga(session, actor, "produksi")
    await lock_prices(session)
    ids = {item.id for item in payload.items}
    if len(ids) != len(payload.items):
        raise HTTPException(422, "Produk yang sama dipilih lebih dari sekali")
    all_prices = {p["id"]: p for p in await price_list(session, include_inactive=True)}
    nodes = await structure_nodes(session)
    occupied = {(p.get("structure_model_id"), p["ukuran"].casefold()) for id_, p in all_prices.items()
                if id_ not in ids and p.get("structure_model_id")}
    occupied_skus = {p.get("sku", "").casefold() for id_, p in all_prices.items()
                     if id_ not in ids and p["kind"] == "produksi" and p.get("sku")}
    prepared = []
    for index, item in enumerate(payload.items, 1):
        current = all_prices.get(item.id)
        if not current or current["kind"] != "produksi" or not current.get("active", True):
            raise HTTPException(409, f"Baris {index}: produk tidak tersedia atau nonaktif. Muat ulang katalog.")
        if current["version"] != item.version:
            raise HTTPException(409, f"Baris {index}: produk sudah berubah. Muat ulang katalog.")
        try:
            fields = structure_fields(nodes, payload.structure_model_id, item.ukuran)
        except HTTPException as exc:
            raise HTTPException(exc.status_code, f"Baris {index}: {exc.detail}") from exc
        key = (payload.structure_model_id, fields["ukuran"].casefold())
        if key in occupied or fields["sku"].casefold() in occupied_skus:
            raise HTTPException(409, f"Baris {index}: ukuran {fields['ukuran']} sudah digunakan pada model ini.")
        occupied.add(key)
        occupied_skus.add(fields["sku"].casefold())
        value = dict(current, **fields)
        value["harga"] = str(item.harga)
        value.pop("version", None)
        prepared.append((item, value))
    try:
        # Rebuild selected variant reservations together, allowing sizes to be swapped in one save.
        await session.execute(delete(ProcessingProductionStructure).where(
            ProcessingProductionStructure.id.in_(ids), ProcessingProductionStructure.kind == "varian"
        ))
        for item, value in prepared:
            if item.version:
                result = await session.execute(update(ProcessingPrice).where(
                    ProcessingPrice.id == item.id, ProcessingPrice.version == item.version
                ).values(payload_json=json.dumps(value), version=item.version + 1))
                if result.rowcount != 1:
                    raise HTTPException(409, "Produk baru saja berubah. Tidak ada perubahan yang disimpan.")
            else:
                session.add(ProcessingPrice(id=item.id, kind="produksi", payload_json=json.dumps(value), version=1))
            session.add(ProcessingProductionStructure(
                id=item.id, kind="varian", parent_key=payload.structure_model_id,
                code=value["ukuran"].casefold(), name=value["ukuran"], version=1
            ))
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Ukuran/SKU berbenturan atau produk berubah. Tidak ada perubahan yang disimpan.")
    except HTTPException:
        await session.rollback()
        raise
    return {"tersimpan": True, "count": len(prepared)}


def processed():
    return (
        Pesanan.platform == "shopee",
        Pesanan.status.in_(["to_ship", "shipped", "completed"]),
        Pesanan.status_marketplace.in_(["PROCESSED", "SHIPPED", "TO_CONFIRM_RECEIVE", "COMPLETED"]),
    )


def siap():
    """Orders whose items can be mapped: Shopee orders already processed, and manual orders still being worked on."""
    return or_(and_(*processed()), and_(Pesanan.platform == PLATFORM_MANUAL, Pesanan.status.in_(["to_ship", "shipped", "completed"])))


def order_item(item, order, shop):
    detail = json.loads(order.detail_json or "{}")
    deadline = detail.get("ship_by_date")
    try:
        deadline = datetime.fromtimestamp(int(deadline), timezone.utc).isoformat() if deadline else None
    except (ValueError, TypeError, OverflowError, OSError):
        deadline = None
    base = dict(
        item_id=item.id,
        order_id=order.id,
        order_number=order.id_eksternal,
        shop=shop or "Tanpa toko",
        customer="",
        produk=item.nama_produk,
        varian=item.model_name,
        qty=item.qty,
        foto=item.foto_url,
        ordered_at=(order.dipesan_at or order.created_at).isoformat(),
        deadline=deadline,
        note=detail.get("message_to_seller") or "",
        source="erp",
    )
    manual = detail.get("manual") if order.platform == PLATFORM_MANUAL else None
    if manual:
        reseller = manual.get("jenis") == "reseller"
        base.update(
            source="reseller" if reseller else "internal",
            customer=order.nama_pembeli,
            reference=manual.get("reference", ""),
            shop=manual.get("toko_nama") or ("Reseller" if reseller else "Internal"),
            seller_code=(manual.get("seller_code") or "").upper() if reseller else "",
            manual_order_code=None if reseller else order.id_eksternal,
            note=(manual.get("catatan_item") or {}).get(item.id, ""),
            order_number="Menunggu SKU" if reseller else order.id_eksternal,
        )
    return base


PER_PAGE = 50
# Same words as the Pesanan menu; "dipetakan" lists the items that already have a mapping (to release or re-map them).
CANDIDATE_STATUS = {"menunggu": "to_ship", "dikirim": "shipped", "selesai": "completed"}


@router.get("/candidates")
async def candidates(
    q: str = Query("", max_length=200),
    page: int = Query(1, ge=1),
    status: Literal["menunggu", "dikirim", "selesai", "semua", "dipetakan"] = "menunggu",
    session: AsyncSession = Depends(get_db_marketplace_erp),
):
    has_job = select(ProcessingJob.id).where(ProcessingJob.source_item_id == ItemPesanan.id).exists()
    if status == "dipetakan":
        # Mapped items stay listed whatever the order became, so a cancelled one can be released too.
        filters = [Pesanan.platform.in_(["shopee", PLATFORM_MANUAL]), has_job]
    else:
        filters = [siap(), ~has_job]
        if status in CANDIDATE_STATUS:
            filters.append(Pesanan.status == CANDIDATE_STATUS[status])
    if q.strip():
        text = "%" + q.strip() + "%"
        filters.append(
            or_(
                Pesanan.id_eksternal.ilike(text),
                ItemPesanan.nama_produk.ilike(text),
                ItemPesanan.model_name.ilike(text),
                AkunMarketplace.nama_toko.ilike(text),
            )
        )
    base = (
        select(ItemPesanan, Pesanan, AkunMarketplace.nama_toko)
        .join(Pesanan, Pesanan.id == ItemPesanan.pesanan_id)
        .outerjoin(AkunMarketplace, AkunMarketplace.id == Pesanan.akun_id)
        .where(*filters)
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await session.execute(
            base.order_by(Pesanan.created_at.desc(), ItemPesanan.id).offset((page - 1) * PER_PAGE).limit(PER_PAGE)
        )
    ).all()
    items = [dict(order_item(*row), status=row[1].status) for row in rows]
    if status == "dipetakan" and items:
        jobs = {
            j.source_item_id: j
            for j in await session.scalars(
                select(ProcessingJob).where(ProcessingJob.source_item_id.in_([i["item_id"] for i in items]))
            )
        }
        for item, row in zip(items, rows):
            job = jobs.get(item["item_id"])
            data = json.loads(job.payload_json) if job else {}
            order = row[1]
            item["job"] = job and {
                "id": job.id,
                "version": job.version,
                "route": data.get("route"),
                "stage": data.get("stage"),
                "cancelled": order.status == "cancelled" or order.status_marketplace in ["CANCELLED", "IN_CANCEL"],
            }
    return {"items": items, "total": total, "page": page, "per_page": PER_PAGE}


class SelectItems(Strict):
    route: Literal["tukang", "supplier"]
    item_ids: list[str] = Field(min_length=1, max_length=100)


def draft(data, route, actor):
    return dict(
        data,
        route=route,
        stage="draft",
        color="",
        packing_type="biasa",
        packing_price="0",
        party_id="",
        party_name="",
        price_id="",
        production_price=None,
        paint_price_id="",
        paint_price="0",
        production_done_at=None,
        received_at=None,
        painting_done_at=None,
        packing_done_at=None,
        events=[{"action": "dibuat", "actor": actor.id, "at": datetime.now(timezone.utc).isoformat()}],
    )


@router.post("/jobs/select")
async def select_items(
    payload: SelectItems,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    actor: UserMarketplaceErp = Depends(admin),
):
    await izin_tab(session, actor, payload.route)
    ids = set(payload.item_ids)
    rows = (
        await session.execute(
            select(ItemPesanan, Pesanan, AkunMarketplace.nama_toko)
            .join(Pesanan, Pesanan.id == ItemPesanan.pesanan_id)
            .outerjoin(AkunMarketplace, AkunMarketplace.id == Pesanan.akun_id)
            .where(ItemPesanan.id.in_(ids), siap())
        )
    ).all()
    if len(rows) != len(ids):
        raise HTTPException(409, "Sebagian item tidak lagi memenuhi syarat; muat ulang pesanan")
    for item, order, shop in rows:
        session.add(
            ProcessingJob(
                source_item_id=item.id,
                payload_json=json.dumps(draft(order_item(item, order, shop), payload.route, actor)),
            )
        )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Item sudah masuk pemetaan tukang atau supplier; muat ulang")
    return {"tersimpan": len(rows)}


class ManualItem(Strict):
    produk: str = Field(min_length=1, max_length=300)
    varian: str = Field(default="", max_length=300)
    qty: int = Field(ge=1, le=100000)
    note: str = Field(default="", max_length=2000)


class ManualOrder(Strict):
    order_code: str | None = Field(default=None, max_length=200)
    seller_code: str | None = Field(default=None, max_length=2)
    source: Literal["internal", "reseller"] = "reseller"
    route: Literal["tukang", "supplier"] = "tukang"
    customer: str = Field(min_length=1, max_length=200)
    ordered_at: datetime
    deadline: datetime
    reference: str = Field(default="", max_length=200)
    items: list[ManualItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def dates(self):
        if self.source == "internal" and self.order_code is not None and not self.order_code:
            raise ValueError("Isi kode pesanan internal")
        if (
            self.source == "reseller"
            and self.seller_code is not None
            and not re.fullmatch(r"[A-Za-z]{2}", self.seller_code)
        ):
            raise ValueError("Kode penjual lain harus 2 huruf")
        if self.ordered_at.tzinfo is None or self.deadline.tzinfo is None:
            raise ValueError("Tanggal harus memiliki zona waktu")
        if self.deadline < self.ordered_at:
            raise ValueError("Batas kirim tidak boleh sebelum tanggal pesanan")
        return self


@router.post("/jobs/manual")
async def manual_order(
    payload: ManualOrder,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    actor: UserMarketplaceErp = Depends(admin),
):
    await izin_tab(session, actor, payload.route)
    prefix = "RSL-" if payload.source == "reseller" else "INT-"
    number = prefix + datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8].upper()
    display_number = (
        payload.order_code
        if payload.source == "internal" and payload.order_code is not None
        else "Menunggu SKU"
        if payload.source == "reseller" and payload.seller_code
        else number
    )
    for item in payload.items:
        data = dict(
            item.model_dump(),
            source=payload.source,
            order_id=number,
            order_number=display_number,
            seller_code=(payload.seller_code or "").upper() if payload.source == "reseller" else "",
            manual_order_code=payload.order_code if payload.source == "internal" else None,
            reference=payload.reference,
            customer=payload.customer,
            shop="Reseller" if payload.source == "reseller" else "Internal",
            ordered_at=payload.ordered_at.isoformat(),
            deadline=payload.deadline.isoformat(),
            foto=None,
        )
        session.add(ProcessingJob(payload_json=json.dumps(draft(data, payload.route, actor))))
    await session.commit()
    return {"order_id": number, "order_number": display_number}


def packing_termasuk_cat(data) -> bool:
    """A reseller's wood product that is painted already has ordinary packing in the paint price: it is neither paid nor billed apart."""
    return (
        data["source"] == "reseller"
        and data["route"] == "tukang"
        and data.get("color") != "Polos / Tanpa Cat"
        and data.get("packing_type", "biasa") == "biasa"
        and data.get("packing_by") != "supplier"
    )


def computed(row, data, order=None):
    qty = data["qty"]
    production = Decimal(data["production_price"] or "0")
    paint = (
        Decimal(data.get("paint_price") or "0")
        if data["route"] == "tukang" and data.get("color") != "Polos / Tanpa Cat"
        else Decimal(0)
    )
    termasuk = packing_termasuk_cat(data)
    packing = Decimal(0) if termasuk else Decimal(data.get("packing_price") or "0")
    fee = Decimal(10000) if data["source"] == "reseller" and data["route"] == "tukang" else Decimal(0)
    reseller = data["source"] == "reseller"
    return dict(
        data,
        id=row.id,
        version=row.version,
        production_total=str(production * qty),
        paint_total=str(paint * qty),
        packing_total=str(packing * qty),
        process_fee=str(fee * qty),
        packing_termasuk_cat=termasuk,
        # What a reseller is billed, in components: the product (all a craftsman is paid), paint with ordinary packing,
        # wood packing apart (only when the type is wood), and the process fee.
        harga_produk_total=str(production * qty) if reseller else None,
        cat_packing_total=str((paint + (packing if data.get("packing_type") != "kayu" else 0)) * qty) if reseller else None,
        packing_kayu_total=str(packing * qty) if reseller and data.get("packing_type") == "kayu" else "0",
        reseller_total=str((production + paint + packing + fee) * qty) if reseller else None,
        order_cancelled=bool(
            order and (order.status == "cancelled" or order.status_marketplace in ["CANCELLED", "IN_CANCEL"])
        ),
    )


WIB = ZoneInfo("Asia/Jakarta")
async def biaya_produksi(session, awal: date, akhir: date) -> dict:
    """Arus Kas references from Pemetaan Order for the stages finished in a date range (WIB). Read only: nothing is posted.

    "pembayaran" is what is paid out: the production price to a craftsman or supplier, and packing only when a supplier
    packs. Paint and our own packing are nobody's bill. "tagihan" is what each reseller is billed, in components."""
    bayar: dict[tuple[str, str, str], Decimal] = {}
    tagih: dict[tuple[str, str], dict[str, Decimal]] = {}
    for row in (await session.scalars(select(ProcessingJob))).all():
        data = json.loads(row.payload_json)
        qty = data["qty"]
        tukang_selesai = data.get("production_done_at") or data.get("received_at")
        if tukang_selesai:
            hari = datetime.fromisoformat(tukang_selesai).astimezone(WIB).date()
            jumlah = Decimal(data.get("production_price") or "0") * qty
            if awal <= hari <= akhir and jumlah:
                tahap = "produksi" if data["route"] == "tukang" else "supplier"
                key = (hari.isoformat(), tahap, data.get("party_name") or "-")
                bayar[key] = bayar.get(key, Decimal(0)) + jumlah
        if data.get("packing_done_at"):
            hari = datetime.fromisoformat(data["packing_done_at"]).astimezone(WIB).date()
            if not awal <= hari <= akhir:
                continue
            hitung = computed(row, data)
            if data.get("packing_by") == "supplier" and Decimal(hitung["packing_total"]):
                key = (hari.isoformat(), "packing", data.get("packing_party_name") or "-")
                bayar[key] = bayar.get(key, Decimal(0)) + Decimal(hitung["packing_total"])
            if data["source"] == "reseller":
                kom = tagih.setdefault((hari.isoformat(), data.get("customer") or "-"), {})
                for nama, nilai in (
                    ("harga_produk", hitung["harga_produk_total"]),
                    ("cat_packing", hitung["cat_packing_total"]),
                    ("biaya_proses", hitung["process_fee"]),
                    ("packing_kayu", hitung["packing_kayu_total"]),
                ):
                    kom[nama] = kom.get(nama, Decimal(0)) + Decimal(nilai)
    return {
        "pembayaran": [{"tanggal": t, "tahap": h, "pihak": p, "jumlah": str(v)} for (t, h, p), v in sorted(bayar.items())],
        "tagihan": [
            {"tanggal": t, "reseller": r, **{k: str(v) for k, v in kom.items()}} for (t, r), kom in sorted(tagih.items())
        ],
    }


@router.get("/jobs")
async def jobs(session: AsyncSession = Depends(get_db_marketplace_erp)):
    rows = (
        await session.scalars(select(ProcessingJob).order_by(ProcessingJob.created_at.desc(), ProcessingJob.id))
    ).all()
    # Current cancellation is read without altering the imported order or losing production history.
    order_ids = {json.loads(r.payload_json).get("order_id") for r in rows if r.source_item_id}
    orders = (
        {o.id: o for o in (await session.scalars(select(Pesanan).where(Pesanan.id.in_(order_ids)))).all()}
        if order_ids
        else {}
    )
    return {
        "items": [computed(r, d, orders.get(d.get("order_id"))) for r in rows for d in [json.loads(r.payload_json)]]
    }


class JobEdit(Strict):
    version: int = Field(ge=1)
    party_id: str = Field(default="", max_length=80)
    price_id: str = Field(default="", max_length=80)
    production_price: Money | None = None
    production_sku: str | None = Field(default=None, max_length=200)
    catalog_mode: Literal["manual", "otomatis"] | None = None
    supplier_product_id: str | None = Field(default=None, max_length=80)
    supplier_product_name: str | None = Field(default=None, max_length=300)
    supplier_sku: str | None = Field(default=None, max_length=200)
    refresh_catalog_price: bool = False
    color: str = Field(default="", max_length=200)
    paint_price_id: str = Field(default="", max_length=80)
    paint_price: Money = Decimal(0)
    packing_type: Literal["biasa", "kayu"] = "biasa"
    packing_price: Money = Decimal(0)
    packing_by: Literal["internal", "supplier"] = "internal"
    packing_party_id: str = Field(default="", max_length=80)


async def pasang_packing(session, data):
    """Packing is done once, by us or by a supplier (any supplier, not only the one that made it); a supplier is named so it is paid."""
    if data.get("packing_by") == "supplier":
        pihak = next((p for p in await party_list(session, "supplier") if p["id"] == data.get("packing_party_id")), None)
        if not pihak:
            raise HTTPException(422, "Pilih supplier yang packing")
        data["packing_party_name"] = pihak["nama"]
    else:
        data.update(packing_by="internal", packing_party_id="", packing_party_name="")


async def load_job(session, job_id, version):
    row = await session.get(ProcessingJob, job_id)
    if not row:
        raise HTTPException(404, "Pemetaan tidak ditemukan")
    if row.version != version:
        raise HTTPException(409, "Pemetaan berubah oleh pengguna lain. Muat ulang.")
    return row, json.loads(row.payload_json)


async def persist(session, row, data, action, actor):
    data["events"].append({"action": action, "actor": actor.id, "at": datetime.now(timezone.utc).isoformat()})
    result = await session.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == row.id, ProcessingJob.version == row.version)
        .values(payload_json=json.dumps(data), version=row.version + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(409, "Pemetaan baru saja berubah. Muat ulang.")
    await session.commit()
    session.expire(row)
    return {"tersimpan": True}


async def next_order_sequence(session, base):
    increment = (
        update(ProcessingOrderSequence)
        .where(ProcessingOrderSequence.id == base)
        .values(
            last_number=ProcessingOrderSequence.last_number + 1,
        )
        .returning(ProcessingOrderSequence.last_number)
    )
    number = await session.scalar(increment)
    if number is not None:
        return number
    try:
        async with session.begin_nested():
            session.add(ProcessingOrderSequence(id=base, last_number=1))
            await session.flush()
        return 1
    except IntegrityError:
        # Another admin reserved the first number while this request was creating it.
        return await session.scalar(increment)


@router.put("/jobs/{job_id}")
async def edit_job(
    job_id: str,
    payload: JobEdit,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    actor: UserMarketplaceErp = Depends(admin),
):
    await lock_prices(session)
    row, data = await load_job(session, job_id, payload.version)
    await izin_tab(session, actor, data["route"])
    if data["stage"] not in ["draft", "produksi", "supplier"]:
        raise HTTPException(409, "Pekerjaan sudah dilanjutkan. Buka kembali tahap sebelumnya untuk mengubah pemetaan.")
    parties = await party_list(session, "tukang" if data["route"] == "tukang" else "supplier")
    party = next((p for p in parties if p["id"] == payload.party_id), None)
    if not party:
        raise HTTPException(422, "Pilih tukang kayu atau supplier yang tersedia")
    previous = dict(data)
    data.update(
        payload.model_dump(
            mode="json",
            exclude={
                "version",
                "production_sku",
                "catalog_mode",
                "supplier_product_id",
                "supplier_product_name",
                "supplier_sku",
                "refresh_catalog_price",
            },
        ),
        party_name=party["nama"],
    )
    price = None
    if data["route"] == "supplier":
        mode = payload.catalog_mode or previous.get("catalog_mode", "manual")
        if mode == "otomatis":
            product_id = payload.supplier_product_id or previous.get("supplier_product_id", "")
            if not product_id:
                raise HTTPException(422, "Pilih produk dari katalog otomatis")
            if (
                previous.get("catalog_mode") == mode
                and previous.get("supplier_product_id") == product_id
                and previous.get("production_price") is not None
                and not payload.refresh_catalog_price
            ):
                reference = previous.get("supplier_reference")
                data["production_price"] = previous["production_price"]
            else:
                from .processing_supplier_catalog import fetch_supplier_catalog

                catalog = await fetch_supplier_catalog(session)
                chosen = next((p for p in catalog["items"] if p["id"] == product_id), None)
                if not chosen:
                    raise HTTPException(422, "Produk tidak ditemukan di katalog otomatis")
                if chosen["qty_on_hand"] <= 0 and product_id != previous.get("supplier_product_id"):
                    raise HTTPException(409, "Stok produk di supplier habis; pilih produk lain")
                reference = dict(chosen, read_at=catalog["read_at"])
                data["production_price"] = str(chosen["sell_price"])
            data.update(
                catalog_mode=mode,
                supplier_product_id=product_id,
                supplier_reference=reference,
                supplier_product_name=reference["name"],
                supplier_sku=reference["sku"],
                price_id="",
                production_reference=None,
            )
        else:
            price = next(
                (
                    p
                    for p in await price_list(session, include_inactive=True)
                    if (p.get("active", True) or p["id"] == previous.get("price_id")) and p["id"] == payload.price_id
                    and (p["kind"] == "non_kayu" or (p["kind"] == "produksi" and p["id"] == previous.get("price_id")))
                ),
                None,
            )
            if payload.price_id and not price:
                raise HTTPException(422, "Pilih produk dari katalog manual non kayu")
            if price and price.get("harga") is None and not (
                previous.get("price_id") == price["id"] and previous.get("production_price") is not None
            ):
                raise HTTPException(422, "Isi harga produk ini dulu di Katalog Manual")
            if price:
                data.update(
                    production_price=previous["production_price"]
                    if previous.get("price_id") == price["id"] and previous.get("production_price") is not None
                    else str(price["harga"]),
                    production_reference=previous.get("production_reference")
                    if previous.get("price_id") == price["id"]
                    else price,
                )
            data.update(
                catalog_mode="manual",
                supplier_product_id="",
                supplier_reference=None,
                supplier_product_name=product_name(price)
                if price
                else payload.supplier_product_name or previous.get("supplier_product_name") or data["produk"],
                supplier_sku=price.get("sku", "")
                if price
                else payload.supplier_sku
                if payload.supplier_sku is not None
                else previous.get("supplier_sku", ""),
            )
    else:
        price = next(
            (p for p in await price_list(session, include_inactive=True) if (p.get("active", True) or p["id"] == previous.get("price_id")) and p["id"] == payload.price_id and p["kind"] == "produksi"), None
        )
        if payload.price_id and not price:
            raise HTTPException(422, "Harga produksi tidak ditemukan")
        if price:
            if previous.get("price_id") == payload.price_id and previous.get("production_price") is not None:
                data.update(
                    production_price=previous["production_price"],
                    production_reference=previous.get("production_reference"),
                )
            else:
                data.update(production_price=str(price["harga"]), production_reference=price)
        elif data["source"] == "erp" and data["route"] != "supplier":
            raise HTTPException(422, "Pilih produk dari daftar harga produksi")
    if data["production_price"] is None:
        raise HTTPException(422, "Harga produksi/harga beli wajib diisi")
    if data["route"] == "tukang":
        if not payload.color:
            raise HTTPException(422, "Tentukan warna atau Polos / Tanpa Cat")
        if payload.color == "Polos / Tanpa Cat":
            data.update(paint_price="0", paint_price_id="", painting_reference=None)
        elif data["source"] == "reseller":
            painting = next(
                (
                    p
                    for p in await price_list(session, include_inactive=True)
                    if (p.get("active", True) or p["id"] == previous.get("paint_price_id")) and p["id"] == payload.paint_price_id and p["kind"] == "pengecatan"
                ),
                None,
            )
            if painting:
                reference = (
                    previous.get("painting_reference") or {}
                    if previous.get("paint_price_id") == payload.paint_price_id
                    else painting
                )
                if reference.get("warna") and reference["warna"].casefold() != payload.color.casefold():
                    raise HTTPException(422, "Warna cat tidak sesuai dengan tarif pengecatan yang dipilih")
                if previous.get("paint_price_id") == payload.paint_price_id:
                    data.update(
                        paint_price=previous["paint_price"], painting_reference=previous.get("painting_reference")
                    )
                else:
                    data.update(paint_price=str(painting["harga"]), painting_reference=painting)
            elif payload.paint_price_id:
                raise HTTPException(422, "Harga pengecatan tidak ditemukan")
        else:
            data.update(paint_price="0", paint_price_id="", painting_reference=None)
    else:
        data.update(paint_price="0", paint_price_id="", painting_reference=None)
    if data["source"] == "reseller" and data.get("seller_code"):
        reference_sku = (
            data.get("supplier_sku", "")
            if data["route"] == "supplier"
            else (data.get("production_reference") or {}).get("sku")
            if price
            else ""
        )
        sku = reference_sku or payload.production_sku or previous.get("production_sku", "")
        if not sku:
            raise HTTPException(422, "Tentukan SKU produksi untuk membuat kode pesanan penjual lain")
        cutoff = datetime.fromisoformat(data["deadline"]).astimezone(ZoneInfo("Asia/Jakarta"))
        base = f"{sku}-{cutoff:%d%m}-{data['seller_code']}"
        sequence = previous.get("order_sequence") if previous.get("numbering_base") == base else None
        if sequence is None:
            sequence = await next_order_sequence(session, base)
        data.update(
            production_sku=sku, numbering_base=base, order_sequence=sequence, order_number=f"{base}-{sequence:02d}"
        )
    await pasang_packing(session, data)
    if data["stage"] == "draft":
        data["stage"] = "produksi" if data["route"] == "tukang" else "supplier"
    return await persist(session, row, data, "dipetakan", actor)


class Transition(Strict):
    version: int = Field(ge=1)
    stage: Literal["produksi", "supplier", "pengecatan", "packing"]
    reopen: bool = False


TIMES = {
    "produksi": "production_done_at",
    "supplier": "received_at",
    "pengecatan": "painting_done_at",
    "packing": "packing_done_at",
}


@router.post("/jobs/{job_id}/transition")
async def transition(
    job_id: str,
    payload: Transition,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    actor: UserMarketplaceErp = Depends(admin),
):
    row, data = await load_job(session, job_id, payload.version)
    await izin_tab(session, actor, TAB_STAGE[payload.stage])
    stage = payload.stage
    if stage == "produksi" and data["route"] != "tukang" or stage == "supplier" and data["route"] != "supplier":
        raise HTTPException(409, "Tahap tidak sesuai jalur pengerjaan")
    next_stage = "pengecatan" if stage == "produksi" and data["color"] != "Polos / Tanpa Cat" else "packing"
    if stage == "packing":
        next_stage = "siap_kirim"
    if payload.reopen:
        if data["stage"] != next_stage or not data.get(TIMES[stage]):
            raise HTTPException(409, "Tahap berikutnya sudah selesai atau belum ada penyelesaian untuk dibuka kembali")
        data[TIMES[stage]] = None
        data["stage"] = stage
    else:
        if data["stage"] != stage:
            raise HTTPException(409, "Tahap sudah berubah; muat ulang agar tidak tercatat dua kali")
        if row.source_item_id:
            order = await session.get(Pesanan, data["order_id"])
            if order and (order.status == "cancelled" or order.status_marketplace in ["CANCELLED", "IN_CANCEL"]):
                raise HTTPException(
                    409, "Pesanan dibatalkan/meminta pembatalan. Periksa sebelum melanjutkan pekerjaan."
                )
        data[TIMES[stage]] = datetime.now(timezone.utc).isoformat()
        data["stage"] = next_stage
    return await persist(session, row, data, ("buka_kembali_" if payload.reopen else "selesai_") + stage, actor)


@router.delete("/jobs/{job_id}")
async def remove_draft(job_id: str, version: int, session: AsyncSession = Depends(get_db_marketplace_erp),
                       actor: UserMarketplaceErp = Depends(admin)):
    from sqlalchemy import delete

    row, data = await load_job(session, job_id, version)
    await izin_tab(session, actor, data["route"])
    # A manual order is the work item itself (there is no order behind it), so it can be deleted at any stage;
    # an order item from Shopee only while it is a draft, later it is released from its mapping instead.
    if data["stage"] != "draft" and row.source_item_id is not None:
        raise HTTPException(409, "Hanya draf yang belum dipetakan bisa dihapus")
    result = await session.execute(
        delete(ProcessingJob).where(ProcessingJob.id == row.id, ProcessingJob.version == version)
    )
    if result.rowcount != 1:
        raise HTTPException(409, "Draf berubah; muat ulang")
    await session.commit()
    return {"terhapus": True}


# What a mapping fills in; cancelling it clears these and the order itself stays.
MAPPING_KEYS = (
    "production_reference", "painting_reference", "supplier_reference", "supplier_product_id", "supplier_product_name",
    "supplier_sku", "catalog_mode", "production_sku", "numbering_base", "order_sequence",
)


@router.post("/jobs/{job_id}/reset")
async def reset_manual_mapping(
    job_id: str,
    version: int = Query(ge=1),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    actor: UserMarketplaceErp = Depends(admin),
):
    """Cancel the mapping of a manual order: who works on it, prices, colour, packing and progress go back to a draft.
    The order itself (customer, dates, items, code) stays; a reseller code shows "Menunggu SKU" again until it is mapped."""
    row, data = await load_job(session, job_id, version)
    await izin_tab(session, actor, data["route"])
    if row.source_item_id is not None:
        raise HTTPException(409, "Item pesanan Shopee dilepas dari pemetaan, bukan dibatalkan")
    if data["stage"] == "draft":
        raise HTTPException(409, "Pesanan ini belum dipetakan")
    events = data["events"]
    data = draft(data, data["route"], actor)
    data["events"] = events
    for key in MAPPING_KEYS:
        data.pop(key, None)
    if data.get("source") == "reseller" and data.get("seller_code"):
        data["order_number"] = "Menunggu SKU"
    return await persist(session, row, data, "pemetaan_dibatalkan", actor)


@router.post("/jobs/{job_id}/release")
async def release_mapping(job_id: str, version: int, session: AsyncSession = Depends(get_db_marketplace_erp),
                          actor: UserMarketplaceErp = Depends(admin)):
    """Take an order item out of its mapping at any stage (a cancelled order, or one to map again); the data entered for it is dropped."""
    from sqlalchemy import delete

    row, data = await load_job(session, job_id, version)
    await izin_tab(session, actor, data["route"])
    if not row.source_item_id:
        raise HTTPException(409, "Pesanan manual tidak bisa dilepas dari pemetaan")
    result = await session.execute(
        delete(ProcessingJob).where(ProcessingJob.id == row.id, ProcessingJob.version == version)
    )
    if result.rowcount != 1:
        raise HTTPException(409, "Pemetaan berubah; muat ulang")
    await session.commit()
    return {"dilepas": True}


class PackingEdit(Strict):
    version: int = Field(ge=1)
    packing_type: Literal["biasa", "kayu"]
    packing_price: Money
    packing_by: Literal["internal", "supplier"] = "internal"
    packing_party_id: str = Field(default="", max_length=80)


@router.put("/jobs/{job_id}/packing")
async def edit_packing(
    job_id: str,
    payload: PackingEdit,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    actor: UserMarketplaceErp = Depends(admin),
):
    row, data = await load_job(session, job_id, payload.version)
    await izin_tab(session, actor, "packing")
    if data["stage"] != "packing":
        raise HTTPException(409, "Packing hanya bisa diubah saat menunggu packing")
    data.update(payload.model_dump(mode="json", exclude={"version"}))
    await pasang_packing(session, data)
    return await persist(session, row, data, "ubah_packing", actor)


class PartyEdit(Strict):
    keahlian: str | None = Field(default=None, max_length=1000)
    alamat: str | None = Field(default=None, max_length=1000)
    no_wa: str | None = Field(default=None, max_length=50)
    no_rekening: str | None = Field(default=None, max_length=100)
    nama_bank: str | None = Field(default=None, max_length=100)
    version: int = Field(ge=0)
    nama: str = Field(min_length=1, max_length=200)
    catatan: str = Field(default="", max_length=2000)


async def party_list(session, kind):
    original = await session.get(ProcessingCatalog, "global")
    catalog = json.loads(original.payload_json) if original else {}
    parties = {p["id"]: dict(p, version=0) for p in catalog.get(kind, [])}
    for row in (await session.scalars(select(ProcessingParty).where(ProcessingParty.kind == kind))).all():
        parties[row.source_id] = dict(json.loads(row.payload_json), id=row.source_id, version=row.version)
    return list(parties.values())


@router.put("/parties/{kind}/{party_id}")
async def save_party(
    kind: Literal["tukang", "supplier"],
    party_id: str,
    payload: PartyEdit,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    actor: UserMarketplaceErp = Depends(admin),
):
    if not party_id or len(party_id) > 80:
        raise HTTPException(422, "ID tidak valid")
    current = next((p for p in await party_list(session, kind) if p["id"] == party_id), None)
    if (current and current["version"] != payload.version) or (not current and payload.version != 0):
        raise HTTPException(409, "Data berubah oleh admin lain. Muat ulang.")
    # Identical names within the same directory would make selection ambiguous.
    name_key = " ".join(payload.nama.casefold().split())
    if any(
        p["id"] != party_id and " ".join(p["nama"].casefold().split()) == name_key
        for p in await party_list(session, kind)
    ):
        raise HTTPException(409, "Nama sudah ada di daftar ini")
    data = dict(
        current or {},
        **payload.model_dump(exclude={"version"}, exclude_none=True),
        sumber=(current or {}).get("sumber", "Input admin"),
        updated_by=actor.id,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        if current and current["version"]:
            result = await session.execute(
                update(ProcessingParty)
                .where(
                    ProcessingParty.kind == kind,
                    ProcessingParty.source_id == party_id,
                    ProcessingParty.version == payload.version,
                )
                .values(payload_json=json.dumps(data), version=payload.version + 1)
            )
            if result.rowcount != 1:
                raise HTTPException(409, "Data baru saja berubah. Muat ulang.")
        else:
            session.add(ProcessingParty(kind=kind, source_id=party_id, payload_json=json.dumps(data), version=1))
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Data baru saja berubah. Muat ulang.")
    return {"tersimpan": True}
