"""Tenant-local keu operations; all commits belong to the request transaction."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application.audit_core import catat_audit
from tenants.bumi_lestari.modules.bumi_lestari.application.services import pastikan_bulan_terbuka
from tenants.bumi_lestari.modules.bumi_lestari.application import schemas_keu as sc
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_keu as m
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlKategori, BlUser

MASTERS = {"saluran": m.KeuSaluran, "akun": m.KeuAkun, "pelanggan": m.KeuPelanggan,
           "vendor": m.KeuVendor, "produk": m.KeuProduk}


def bad(message: str, code: int = 422):
    raise HTTPException(code, message)


def json_value(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(val) for val in value]
    return value


def record(obj):
    return {col.name: json_value(getattr(obj, col.name)) for col in obj.__table__.columns}


async def get(session, model, key, *, lock=False):
    if lock:
        obj = (await session.execute(select(model).where(model.id == key).with_for_update())).scalar_one_or_none()
    else:
        obj = await session.get(model, key)
    if obj is None:
        bad("Data tidak ditemukan", 404)
    return obj


async def lock_item(session, key):
    # Consistent order -> item hierarchy also serializes status transitions and allocation cancellation.
    item = await get(session, m.KeuItem, key)
    order = await get(session, m.KeuPesanan, item.pesanan_id, lock=True)
    item = await get(session, m.KeuItem, key, lock=True)
    return item, order


async def audit(session, user, obj, action="buat", before=None, reason=None):
    await catat_audit(session, user.id, action, obj.__tablename__, getattr(obj, "id", None),
                      sebelum=before, sesudah=record(obj), alasan=reason)


async def channel(session, key, *, manual=False):
    row = await get(session, m.KeuSaluran, key)
    if not row.aktif:
        bad("Saluran belum aktif")
    if manual and row.sistem != "manual":
        bad("Data sumber hanya dapat diisi melalui sinkronisasi", 403)
    return row


async def page(session, model, limit=50, offset=0, search=None):
    stmt = select(model)
    if search:
        field = getattr(model, "nama", getattr(model, "nomor", getattr(model, "sumber_ref", None)))
        if field is not None:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            fields = [field] if model is not m.KeuProduk else [model.nama, model.nama_asli, model.sku, model.sku_induk]
            stmt = stmt.where(or_(*(col.ilike(f"%{escaped}%", escape="\\") for col in fields)))
    count = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = list((await session.execute(stmt.order_by(*model.__table__.primary_key.columns).limit(limit).offset(offset))).scalars())
    output = [record(row) for row in rows]
    if model is m.KeuPesanan and rows:
        items = (await session.execute(select(m.KeuItem).where(m.KeuItem.pesanan_id.in_([r.id for r in rows])).order_by(m.KeuItem.id))).scalars().all()
        groups = {}
        for item in items:
            groups.setdefault(item.pesanan_id, []).append(record(item))
        for result in output:
            result["items"] = groups.get(result["id"], [])
    return {"rows": output, "total": count, "limit": limit, "offset": offset}


async def create_master(session, user, name, payload):
    values = payload.model_dump()
    if name == "produk":
        values.update(nama_asli=payload.nama, status="master", aktif=True)
    obj = MASTERS[name](**values)
    session.add(obj)
    await session.flush()
    await audit(session, user, obj)
    return record(obj)


async def save_product(session, user, key, payload):
    product = await get(session, m.KeuProduk, key, lock=True)
    if product.jenis != payload.jenis:
        allocated = (await session.execute(select(m.KeuAlokasiVendor.id).join(m.KeuItem, m.KeuItem.id == m.KeuAlokasiVendor.item_id)
                     .where(m.KeuItem.produk_id == key, m.KeuAlokasiVendor.dibatalkan.is_(False)).limit(1))).first()
        if allocated:
            bad("Batalkan alokasi sebelum mengganti jenis produk", 409)
    before = record(product)
    for field, value in payload.model_dump().items():
        setattr(product, field, value)
    product.status, product.aktif = "master", True
    await session.flush()
    await audit(session, user, product, "simpan-master", before)
    return record(product)


async def source_product(session, user, line):
    source = line.produk_sumber
    if source is None:
        if line.produk_id:
            await get(session, m.KeuProduk, line.produk_id)
        return line.produk_id
    if line.produk_id:
        product = await get(session, m.KeuProduk, line.produk_id)
        if product.sku != source.sku:
            bad("SKU sumber tidak sesuai produk yang dipetakan", 409)
        return product.id
    product = (await session.execute(select(m.KeuProduk).where(m.KeuProduk.sku == source.sku))).scalar_one_or_none()
    if product:
        return product.id  # Never overwrite an alias, HPP or locally edited variants on repeat pulls.
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    values = source.model_dump()
    values.update(id=m.new_id(), nama=source.nama_asli, jenis=None, status="draf", aktif=False, biaya_acuan=Decimal("0"))
    # A unique child SKU is shared across channels; concurrent pulls cannot create duplicates.
    created_id = (await session.execute(insert(m.KeuProduk).values(**values).on_conflict_do_nothing(index_elements=["sku"])
                                       .returning(m.KeuProduk.id))).scalar_one_or_none()
    product = (await session.execute(select(m.KeuProduk).where(m.KeuProduk.sku == source.sku))).scalar_one()
    if created_id:
        await audit(session, user, product, "draf-sumber")
    return product.id


async def prepare_lines(session, user, payload):
    lines = []
    for line in payload.items:
        product_id = await source_product(session, user, line)
        lines.append(line.model_copy(update={"produk_id": product_id}))
    return payload.model_copy(update={"items": lines})


async def slots(session):
    rows = (await session.execute(select(m.KeuVendorSlot, m.KeuVendor).outerjoin(m.KeuVendor, m.KeuVendor.id == m.KeuVendorSlot.vendor_id)
                                  .order_by(m.KeuVendorSlot.jenis, m.KeuVendorSlot.nomor))).all()
    return [{**record(slot), "kode": slot.kode, "vendor": record(vendor) if vendor else None} for slot, vendor in rows]


async def set_slot(session, user, code, payload):
    prefix, sep, number = code.partition("-")
    if prefix not in {"tk", "sup"} or not sep or not number.isdigit():
        bad("Slot tidak ditemukan", 404)
    kind = "tukang_kayu" if prefix == "tk" else "supplier"
    slot = (await session.execute(select(m.KeuVendorSlot).where(m.KeuVendorSlot.jenis == kind, m.KeuVendorSlot.nomor == int(number)).with_for_update())).scalar_one_or_none()
    if slot is None:
        bad("Slot tidak ditemukan", 404)
    if payload.jenis != kind:
        bad("Jenis vendor tidak sesuai slot")
    vendor = await get(session, m.KeuVendor, slot.vendor_id, lock=True) if slot.vendor_id else m.KeuVendor(**payload.model_dump())
    before = record(vendor) if slot.vendor_id else None
    for key, value in payload.model_dump().items():
        setattr(vendor, key, value)
    session.add(vendor)
    await session.flush()
    slot.vendor_id = vendor.id
    await session.flush()
    await audit(session, user, vendor, "ubah-slot", before)
    return {**record(slot), "kode": slot.kode, "vendor": record(vendor)}


async def order_detail(session, key):
    order = await get(session, m.KeuPesanan, key)
    items = (await session.execute(select(m.KeuItem).where(m.KeuItem.pesanan_id == key).order_by(m.KeuItem.id))).scalars().all()
    return {**record(order), "items": [record(item) for item in items]}


async def create_order(session: AsyncSession, user: BlUser, payload: sc.PesananIn, *, from_source=False):
    saluran = await channel(session, payload.saluran_id, manual=not from_source)
    # Channel lock serializes manual imports/source upserts sharing the same key.
    await session.execute(select(m.KeuSaluran).where(m.KeuSaluran.id == saluran.id).with_for_update())
    old = (await session.execute(select(m.KeuPesanan).where(m.KeuPesanan.saluran_id == saluran.id, m.KeuPesanan.sumber_ref == payload.sumber_ref).with_for_update())).scalar_one_or_none()
    if old and from_source:
        return await update_source_order(session, user, old, payload)
    payload = await prepare_lines(session, user, payload)
    if old:
        current = await order_detail(session, old.id)
        expected = payload.model_dump()
        # Idempotent retry must not silently discard a different order with the same reference.
        same = all(getattr(old, k) == v for k, v in expected.items() if k not in {"items", "sumber_updated_at", "segmen_snapshot"})
        expected_segment = (await get(session, m.KeuPelanggan, payload.pelanggan_id)).segmen if payload.pelanggan_id else payload.segmen_snapshot
        same = same and old.segmen_snapshot == expected_segment
        actual_items = [sc.ItemIn.model_validate({k: r[k] for k in sc.ItemIn.model_fields if k in r}).model_dump(exclude={"produk_sumber"}) for r in current["items"]]
        if same and sorted(actual_items, key=lambda r: r["sumber_ref"]) == sorted([line.model_dump(exclude={"produk_sumber"}) for line in payload.items], key=lambda r: r["sumber_ref"]):
            return current
        bad("Referensi pesanan sudah dipakai dengan data berbeda", 409)
    if payload.pelanggan_id:
        customer = await get(session, m.KeuPelanggan, payload.pelanggan_id)
        segment = customer.segmen
    else:
        segment = payload.segmen_snapshot
    values = payload.model_dump(exclude={"items", "segmen_snapshot"})
    obj = m.KeuPesanan(**values, segmen_snapshot=segment)
    session.add(obj)
    await session.flush()
    for line in payload.items:
        if line.produk_id:
            await get(session, m.KeuProduk, line.produk_id)
        session.add(m.KeuItem(pesanan_id=obj.id, **line.model_dump(exclude={"produk_sumber"})))
    await session.flush()
    await audit(session, user, obj)
    return await order_detail(session, obj.id)


async def update_source_order(session, user, order, payload):
    if order.sumber_updated_at and payload.sumber_updated_at:
        old_time = order.sumber_updated_at
        if old_time.tzinfo is None:
            from datetime import timezone
            old_time = old_time.replace(tzinfo=timezone.utc)
        if payload.sumber_updated_at < old_time:
            return await order_detail(session, order.id)
    before = record(order)
    order.status_sumber = payload.status_sumber
    order.sumber_updated_at = payload.sumber_updated_at
    existing = {item.sumber_ref: item for item in (await session.execute(select(m.KeuItem).where(m.KeuItem.pesanan_id == order.id).with_for_update())).scalars()}
    locked = set((await session.execute(select(m.KeuAlokasiVendor.item_id).where(m.KeuAlokasiVendor.item_id.in_([i.id for i in existing.values()]), m.KeuAlokasiVendor.dibatalkan.is_(False)))).scalars())
    settled = set((await session.execute(select(m.KeuAlokasiSettlement.item_id).where(m.KeuAlokasiSettlement.item_id.in_([i.id for i in existing.values()])))).scalars())
    # Financial/production revisions require manual reconciliation, never overwrite allocated snapshots.
    if locked or settled or order.status != "draf":
        for item in payload.items:
            old = existing.get(item.sumber_ref)
            if old is None or (old.qty, old.harga_satuan, old.subtotal_sumber) != (item.qty, item.harga_satuan, item.subtotal_sumber):
                bad("Perubahan sumber memerlukan rekonsiliasi karena item sudah dialokasikan", 409)
        if set(existing) != {i.sumber_ref for i in payload.items} or order.total_sumber != payload.total_sumber:
            bad("Perubahan sumber memerlukan rekonsiliasi", 409)
    else:
        if set(existing) - {i.sumber_ref for i in payload.items}:
            bad("Item sumber hilang; tinjau pembatalan secara manual", 409)
        order.total_sumber = payload.total_sumber
        payload = await prepare_lines(session, user, payload)
        for item in payload.items:
            row = existing.get(item.sumber_ref)
            if row is None:
                session.add(m.KeuItem(pesanan_id=order.id, **item.model_dump(exclude={"produk_sumber"})))
            else:
                if row.produk_id is None:
                    row.produk_id = item.produk_id
                for key, value in item.model_dump(exclude={"produk_id", "produk_sumber"}).items():
                    setattr(row, key, value)
    await session.flush()
    await audit(session, user, order, "sinkron", before)
    return await order_detail(session, order.id)


async def map_item(session, user, key, payload):
    item, order = await lock_item(session, key)
    if order.status in {"selesai", "batal"}:
        bad("Pesanan sudah selesai atau batal", 409)
    if (await session.execute(select(m.KeuAlokasiVendor.id).where(m.KeuAlokasiVendor.item_id == key, m.KeuAlokasiVendor.dibatalkan.is_(False)))).first():
        bad("Batalkan alokasi sebelum mengganti produk", 409)
    product = await get(session, m.KeuProduk, payload.produk_id)
    if product.status != "master" or not product.aktif:
        bad("Simpan produk sebagai master sebelum melakukan pemetaan", 409)
    before = record(item)
    item.produk_id = payload.produk_id
    await session.flush()
    await audit(session, user, item, "petakan", before)
    return record(item)


async def order_status(session, user, key, payload):
    order = await get(session, m.KeuPesanan, key, lock=True)
    transitions = {"draf": {"aktif", "batal"}, "aktif": {"selesai", "batal"}, "selesai": set(), "batal": set()}
    if payload.status not in transitions[order.status]:
        bad("Transisi status tidak diizinkan", 409)
    if payload.status == "batal" and len(payload.alasan) < 3:
        bad("Alasan pembatalan wajib diisi")
    ids = list((await session.execute(select(m.KeuItem.id).where(m.KeuItem.pesanan_id == key))).scalars())
    if payload.status == "batal":
        if (await session.execute(select(m.KeuAlokasiVendor.id).where(m.KeuAlokasiVendor.item_id.in_(ids), m.KeuAlokasiVendor.dibatalkan.is_(False)))).first() or (await session.execute(select(m.KeuAlokasiSettlement.id).where(m.KeuAlokasiSettlement.item_id.in_(ids)))).first():
            bad("Batalkan alokasi terlebih dahulu; settlement harus dikoreksi terpisah", 409)
    if payload.status in {"aktif", "selesai"}:
        for item in (await session.execute(select(m.KeuItem).where(m.KeuItem.pesanan_id == key).with_for_update())).scalars():
            allocated = (await session.execute(select(func.coalesce(func.sum(m.KeuAlokasiVendor.qty), 0)).where(m.KeuAlokasiVendor.item_id == item.id, m.KeuAlokasiVendor.dibatalkan.is_(False)))).scalar_one()
            if not item.produk_id or allocated != item.qty:
                bad("Petakan produk dan lengkapi alokasi vendor sebelum memajukan status", 409)
    before = record(order)
    order.status = payload.status
    await session.flush()
    await audit(session, user, order, "status", before, payload.alasan)
    return record(order)


async def allocate_vendor(session, user, payload):
    item, order = await lock_item(session, payload.item_id)
    if order.status in {"selesai", "batal"}:
        bad("Pesanan sudah selesai atau batal", 409)
    product = await get(session, m.KeuProduk, item.produk_id) if item.produk_id else None
    vendor = await get(session, m.KeuVendor, payload.vendor_id)
    slot = (await session.execute(select(m.KeuVendorSlot).where(m.KeuVendorSlot.vendor_id == vendor.id))).first()
    if product is None or product.status != "master" or not product.aktif or not vendor.aktif or not slot:
        bad("Pilih produk dan vendor aktif yang terdaftar di slot")
    if vendor.jenis != ("tukang_kayu" if product.jenis == "kayu" else "supplier"):
        bad("Jenis vendor tidak sesuai produk")
    used = (await session.execute(select(func.coalesce(func.sum(m.KeuAlokasiVendor.qty), 0)).where(m.KeuAlokasiVendor.item_id == item.id, m.KeuAlokasiVendor.dibatalkan.is_(False)))).scalar_one()
    row = (await session.execute(select(m.KeuAlokasiVendor).where(m.KeuAlokasiVendor.item_id == item.id, m.KeuAlokasiVendor.vendor_id == vendor.id))).scalar_one_or_none()
    if row and not row.dibatalkan:
        bad("Vendor sudah dialokasikan untuk item ini", 409)
    if used + payload.qty > item.qty:
        bad("Kuantitas alokasi melebihi item", 409)
    if row:
        before = record(row)
        row.qty, row.biaya_satuan, row.dibatalkan, row.alasan = payload.qty, payload.biaya_satuan, False, ""
    else:
        before = None
        row = m.KeuAlokasiVendor(**payload.model_dump(), dibuat_oleh=user.id)
        session.add(row)
    await session.flush()
    await audit(session, user, row, "alokasi", before)
    return record(row)


async def cancel_allocation(session, user, key, payload):
    row = await get(session, m.KeuAlokasiVendor, key)
    _, order = await lock_item(session, row.item_id)
    if order.status in {"selesai", "batal"}:
        bad("Pesanan sudah selesai atau batal; buat koreksi terpisah", 409)
    row = await get(session, m.KeuAlokasiVendor, key, lock=True)
    before = record(row)
    row.dibatalkan, row.alasan = True, payload.alasan
    await session.flush()
    await audit(session, user, row, "batal", before, payload.alasan)
    return record(row)


async def create_settlement(session, user, payload, *, from_source=False):
    await channel(session, payload.saluran_id)
    if not from_source:
        sal = await get(session, m.KeuSaluran, payload.saluran_id)
        if sal.sistem == "store":
            bad("Settlement Store memerlukan bukti pencairan; gunakan saluran manual", 403)
    row = (await session.execute(select(m.KeuSettlement).where(m.KeuSettlement.saluran_id == payload.saluran_id, m.KeuSettlement.sumber_ref == payload.sumber_ref))).scalar_one_or_none()
    if row:
        if all(getattr(row, k) == v for k, v in payload.model_dump().items()):
            return record(row)
        bad("Referensi settlement sudah dipakai dengan data berbeda", 409)
    row = m.KeuSettlement(**payload.model_dump())
    session.add(row)
    await session.flush()
    await audit(session, user, row)
    return record(row)


async def allocate_settlement(session, user, payload):
    settlement = await get(session, m.KeuSettlement, payload.settlement_id, lock=True)
    if settlement.status != "draf":
        bad("Settlement sudah terkunci", 409)
    item, order = await lock_item(session, payload.item_id)
    if order.saluran_id != settlement.saluran_id or order.status == "batal":
        bad("Saluran pesanan tidak sesuai atau pesanan batal")
    if (settlement.neto > 0 and payload.jumlah < 0) or (settlement.neto < 0 and payload.jumlah > 0):
        bad("Tanda alokasi tidak sesuai neto settlement")
    row = (await session.execute(select(m.KeuAlokasiSettlement).where(m.KeuAlokasiSettlement.settlement_id == settlement.id, m.KeuAlokasiSettlement.item_id == item.id))).scalar_one_or_none()
    total = (await session.execute(select(func.coalesce(func.sum(m.KeuAlokasiSettlement.jumlah), 0)).where(m.KeuAlokasiSettlement.settlement_id == settlement.id))).scalar_one()
    remaining = total - (row.jumlah if row else Decimal("0")) + payload.jumlah
    if abs(remaining) > abs(settlement.neto):
        bad("Alokasi melebihi neto settlement", 409)
    before = record(row) if row else None
    if row:
        row.jumlah = payload.jumlah
    else:
        row = m.KeuAlokasiSettlement(**payload.model_dump())
        session.add(row)
    await session.flush()
    await audit(session, user, row, "alokasi", before)
    return record(row)


async def transaction(session, user, payload, *, settlement_id=None):
    await channel(session, payload.saluran_id, manual=settlement_id is None)
    akun = await get(session, m.KeuAkun, payload.akun_id, lock=True)
    if not akun.aktif:
        bad("Akun tidak aktif")
    kategori = await get(session, BlKategori, payload.kategori_id)
    if not kategori.aktif or kategori.jenis != ("pemasukan" if payload.jenis == "masuk" else "pengeluaran"):
        bad("Kategori tidak sesuai jenis transaksi")
    old = (await session.execute(select(m.KeuTransaksi).where(m.KeuTransaksi.saluran_id == payload.saluran_id, m.KeuTransaksi.sumber_ref == payload.sumber_ref))).scalar_one_or_none()
    if old:
        if all(getattr(old, k) == v for k, v in payload.model_dump().items()) and old.settlement_id == settlement_id:
            return record(old)
        bad("Referensi transaksi sudah dipakai", 409)
    row = m.KeuTransaksi(**payload.model_dump(), dibuat_oleh=user.id, settlement_id=settlement_id)
    session.add(row)
    await session.flush()
    await audit(session, user, row)
    return record(row)


async def post_transaction(session, user, key):
    row = await get(session, m.KeuTransaksi, key, lock=True)
    if row.status == "terkirim":
        return record(row)
    if row.status != "draf":
        bad("Transaksi sudah terkunci", 409)
    await pastikan_bulan_terbuka(session, row.tanggal)
    await get(session, m.KeuAkun, row.akun_id, lock=True)
    before = record(row)
    row.status = "terkirim"
    await session.flush()
    await audit(session, user, row, "posting", before)
    return record(row)


async def post_settlement(session, user, key, payload):
    row = await get(session, m.KeuSettlement, key, lock=True)
    if row.status == "terkirim":
        return record(row)
    if row.status != "draf":
        bad("Settlement sudah terkunci", 409)
    await pastikan_bulan_terbuka(session, row.tanggal_cair)
    amounts = list((await session.execute(select(m.KeuAlokasiSettlement.jumlah).where(m.KeuAlokasiSettlement.settlement_id == key))).scalars())
    if not amounts or sum(amounts, Decimal("0")) != row.neto:
        bad("Alokasi harus sama dengan neto settlement", 409)
    before = record(row)
    if row.neto:
        tx = sc.TransaksiIn(saluran_id=row.saluran_id, sumber_ref=f"settlement:{key}", akun_id=payload.akun_id,
                            kategori_id=payload.kategori_id, tanggal=row.tanggal_cair,
                            jenis="masuk" if row.neto > 0 else "keluar", jumlah=abs(row.neto), keterangan=f"Settlement {row.sumber_ref}")
        result = await transaction(session, user, tx, settlement_id=key)
        await post_transaction(session, user, result["id"])
    else:
        await get(session, m.KeuAkun, payload.akun_id)
        await get(session, BlKategori, payload.kategori_id)
    row.status = "terkirim"
    await session.flush()
    await audit(session, user, row, "posting", before)
    return record(row)


async def dashboard(session):
    zero = Decimal("0")
    initial = (await session.execute(select(func.coalesce(func.sum(m.KeuAkun.saldo_awal), 0)))).scalar_one()
    values = (await session.execute(select(m.KeuTransaksi.jenis, func.sum(m.KeuTransaksi.jumlah)).where(m.KeuTransaksi.status == "terkirim").group_by(m.KeuTransaksi.jenis))).all()
    totals = dict(values)
    cost = (await session.execute(select(func.coalesce(func.sum(m.KeuAlokasiVendor.qty * m.KeuAlokasiVendor.biaya_satuan), 0)).where(m.KeuAlokasiVendor.dibatalkan.is_(False)))).scalar_one()
    sales = (await session.execute(select(func.coalesce(func.sum(m.KeuPesanan.total_sumber), 0)).where(m.KeuPesanan.status != "batal"))).scalar_one()
    counts = {}
    for label, model, condition in (("pesanan", m.KeuPesanan, m.KeuPesanan.status != "batal"),
                                    ("belum_dipetakan", m.KeuItem, or_(m.KeuItem.produk_id.is_(None), m.KeuItem.produk_id.in_(select(m.KeuProduk.id).where(or_(m.KeuProduk.status != "master", m.KeuProduk.aktif.is_(False)))))),
                                    ("settlement_draf", m.KeuSettlement, m.KeuSettlement.status == "draf"),
                                    ("masukan_gagal", m.KeuMasukan, m.KeuMasukan.status == "gagal")):
        counts[label] = (await session.execute(select(func.count()).select_from(model).where(condition))).scalar_one()
    return {**counts, "saldo_kas": str(initial + totals.get("masuk", zero) - totals.get("keluar", zero)),
            "kas_masuk": str(totals.get("masuk", zero)), "kas_keluar": str(totals.get("keluar", zero)),
            "nilai_pesanan": str(sales), "biaya_vendor": str(cost)}
