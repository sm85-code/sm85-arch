"""Internal work queue. No marketplace adapter, payment, stock or journal side effects."""

import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    AkunMarketplace,
    ItemPesanan,
    Pesanan,
    ProcessingCatalog,
    ProcessingJob,
    ProcessingPrice,
    ProcessingProductionStructure,
    ProcessingOrderSequence,
    ProcessingParty,
    UserMarketplaceErp,
)

admin = require_roles_marketplace_erp("admin")
router = APIRouter(dependencies=[Depends(admin)])
Money = Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=2)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class PriceInput(Strict):
    version: int = Field(ge=0)
    kind: Literal["produksi", "pengecatan"]
    produk: str = Field(min_length=1, max_length=300)
    jenis: str | None = Field(default=None, max_length=200)
    structure_model_id: str | None = Field(default=None, max_length=80)
    warna: str | None = Field(default=None, max_length=200)
    sku: str = Field(default="", max_length=200)
    ukuran: str = Field(default="", max_length=100)
    harga: Money
    catatan: str = Field(default="", max_length=2000)


class StructureInput(Strict):
    version: int = Field(ge=0)
    kind: Literal["kategori", "produk", "model"]
    parent_id: str = Field(default="", max_length=80)
    code: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)


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


async def price_list(session):
    original = await session.get(ProcessingCatalog, "global")
    data = json.loads(original.payload_json) if original else {}
    prices = {r["id"]: dict(r, kind="produksi", version=0) for r in data.get("harga_produksi", [])}
    for row in (await session.scalars(select(ProcessingPrice))).all():
        prices[row.id] = dict(json.loads(row.payload_json), id=row.id, kind=row.kind, version=row.version)
    nodes = await structure_nodes(session)
    for value in prices.values():
        if value.get("structure_model_id"):
            value.update(structure_fields(nodes, value["structure_model_id"], value["ukuran"]))
    return list(prices.values())


@router.get("/prices")
async def prices(session: AsyncSession = Depends(get_db_marketplace_erp)):
    return {"items": await price_list(session)}


@router.put("/prices/{price_id}")
async def save_price(price_id: str, payload: PriceInput, session: AsyncSession = Depends(get_db_marketplace_erp)):
    if payload.kind == "pengecatan" and payload.warna is not None and not payload.warna:
        raise HTTPException(422, "Pilih warna cat untuk tarif pengecatan")
    if len(price_id) > 80:
        raise HTTPException(422, "ID harga terlalu panjang")
    current = next((p for p in await price_list(session) if p["id"] == price_id), None)
    if current and (current["version"] != payload.version or current["kind"] != payload.kind):
        raise HTTPException(409, "Harga telah berubah. Muat ulang sebelum menyimpan.")
    if not current and payload.version != 0:
        raise HTTPException(409, "Harga tidak ditemukan; muat ulang")
    value = dict(current or {}, **payload.model_dump(mode="json", exclude={"version"}, exclude_none=True), id=price_id)
    model_id = value.get("structure_model_id")
    if model_id:
        if payload.kind != "produksi":
            raise HTTPException(422, "Struktur hanya untuk harga produksi")
        value.update(structure_fields(await structure_nodes(session), model_id, payload.ukuran))
    elif current and current.get("structure_model_id"):
        raise HTTPException(422, "Pilih model untuk harga yang sudah terstruktur")
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


def processed():
    return (
        Pesanan.platform == "shopee",
        Pesanan.status.in_(["to_ship", "shipped", "completed"]),
        Pesanan.status_marketplace.in_(["PROCESSED", "SHIPPED", "TO_CONFIRM_RECEIVE", "COMPLETED"]),
    )


def order_item(item, order, shop):
    detail = json.loads(order.detail_json or "{}")
    deadline = detail.get("ship_by_date")
    try:
        deadline = datetime.fromtimestamp(int(deadline), timezone.utc).isoformat() if deadline else None
    except (ValueError, TypeError, OverflowError, OSError):
        deadline = None
    return dict(
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


@router.get("/candidates")
async def candidates(
    q: str = Query("", max_length=200),
    page: int = Query(1, ge=1),
    session: AsyncSession = Depends(get_db_marketplace_erp),
):
    filters = [*processed(), ~select(ProcessingJob.id).where(ProcessingJob.source_item_id == ItemPesanan.id).exists()]
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
            base.order_by(Pesanan.created_at.desc(), ItemPesanan.id).offset((page - 1) * 25).limit(25)
        )
    ).all()
    return {"items": [order_item(*row) for row in rows], "total": total, "page": page}


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
    ids = set(payload.item_ids)
    rows = (
        await session.execute(
            select(ItemPesanan, Pesanan, AkunMarketplace.nama_toko)
            .join(Pesanan, Pesanan.id == ItemPesanan.pesanan_id)
            .outerjoin(AkunMarketplace, AkunMarketplace.id == Pesanan.akun_id)
            .where(ItemPesanan.id.in_(ids), *processed())
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


def computed(row, data, order=None):
    qty = data["qty"]
    production = Decimal(data["production_price"] or "0")
    paint = (
        Decimal(data.get("paint_price") or "0")
        if data["route"] == "tukang" and data.get("color") != "Polos / Tanpa Cat"
        else Decimal(0)
    )
    packing = Decimal(data.get("packing_price") or "0")
    fee = Decimal(10000) if data["source"] == "reseller" and data["route"] == "tukang" else Decimal(0)
    return dict(
        data,
        id=row.id,
        version=row.version,
        production_total=str(production * qty),
        paint_total=str(paint * qty),
        packing_total=str(packing * qty),
        process_fee=str(fee * qty),
        reseller_total=str((production + paint + packing + fee) * qty) if data["source"] == "reseller" else None,
        order_cancelled=bool(
            order and (order.status == "cancelled" or order.status_marketplace in ["CANCELLED", "IN_CANCEL"])
        ),
    )


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
    color: str = Field(default="", max_length=200)
    paint_price_id: str = Field(default="", max_length=80)
    paint_price: Money = Decimal(0)
    packing_type: Literal["biasa", "kayu"] = "biasa"
    packing_price: Money = Decimal(0)


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
    row, data = await load_job(session, job_id, payload.version)
    if data["stage"] not in ["draft", "produksi", "supplier"]:
        raise HTTPException(409, "Pekerjaan sudah dilanjutkan. Buka kembali tahap sebelumnya untuk mengubah pemetaan.")
    parties = await party_list(session, "tukang" if data["route"] == "tukang" else "supplier")
    party = next((p for p in parties if p["id"] == payload.party_id), None)
    if not party:
        raise HTTPException(422, "Pilih tukang kayu atau supplier yang tersedia")
    previous = dict(data)
    data.update(payload.model_dump(mode="json", exclude={"version", "production_sku"}), party_name=party["nama"])
    price = next(
        (p for p in await price_list(session) if p["id"] == payload.price_id and p["kind"] == "produksi"), None
    )
    if payload.price_id and not price:
        raise HTTPException(422, "Harga produksi tidak ditemukan")
    if price:
        if previous.get("price_id") == payload.price_id and previous.get("production_price") is not None:
            data.update(
                production_price=previous["production_price"], production_reference=previous.get("production_reference")
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
                    for p in await price_list(session)
                    if p["id"] == payload.paint_price_id and p["kind"] == "pengecatan"
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
        reference_sku = (data.get("production_reference") or {}).get("sku") if price else ""
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
async def remove_draft(job_id: str, version: int, session: AsyncSession = Depends(get_db_marketplace_erp)):
    from sqlalchemy import delete

    row, data = await load_job(session, job_id, version)
    if data["stage"] != "draft":
        raise HTTPException(409, "Hanya draf yang belum dipetakan bisa dihapus")
    result = await session.execute(
        delete(ProcessingJob).where(ProcessingJob.id == row.id, ProcessingJob.version == version)
    )
    if result.rowcount != 1:
        raise HTTPException(409, "Draf berubah; muat ulang")
    await session.commit()
    return {"terhapus": True}


class PackingEdit(Strict):
    version: int = Field(ge=1)
    packing_type: Literal["biasa", "kayu"]
    packing_price: Money


@router.put("/jobs/{job_id}/packing")
async def edit_packing(
    job_id: str,
    payload: PackingEdit,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    actor: UserMarketplaceErp = Depends(admin),
):
    row, data = await load_job(session, job_id, payload.version)
    if data["stage"] != "packing":
        raise HTTPException(409, "Packing hanya bisa diubah saat menunggu packing")
    data.update(payload.model_dump(mode="json", exclude={"version"}))
    return await persist(session, row, data, "ubah_packing", actor)


class PartyEdit(Strict):
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
