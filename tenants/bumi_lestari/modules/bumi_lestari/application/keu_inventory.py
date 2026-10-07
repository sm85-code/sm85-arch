"""FIFO ready stock; every consumed batch retains its quantity and exact carrying cost."""
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from . import keu_ledger as ledger, keu_services as svc
from ..infrastructure import models_keu as m, models_keu_finance as f

ZERO = Decimal("0")


async def used_item(session, key):
    return (await session.execute(select(func.coalesce(func.sum(f.KeuStokMutasi.qty), 0)).join(
        f.KeuJurnal, f.KeuJurnal.id == f.KeuStokMutasi.jurnal_id).where(ledger.visible(),
        f.KeuStokMutasi.item_id == key, f.KeuStokMutasi.jenis == "keluar"))).scalar_one()


async def batches(session, product_id, day=None):
    query = select(f.KeuStokMutasi, f.KeuJurnal.tanggal).join(f.KeuJurnal,
        f.KeuJurnal.id == f.KeuStokMutasi.jurnal_id).where(ledger.visible(),
        f.KeuStokMutasi.produk_id == product_id, f.KeuStokMutasi.jenis == "masuk")
    if day:
        query = query.where(f.KeuJurnal.tanggal <= day)
    rows = (await session.execute(query.order_by(f.KeuJurnal.tanggal, f.KeuJurnal.created_at, f.KeuStokMutasi.id))).all()
    result = []
    for batch, tanggal in rows:
        quantity, cost = (await session.execute(select(func.coalesce(func.sum(f.KeuStokPemakaian.qty), 0),
            func.coalesce(func.sum(f.KeuStokPemakaian.nilai), 0)).join(f.KeuStokMutasi,
            f.KeuStokMutasi.id == f.KeuStokPemakaian.keluar_id).join(f.KeuJurnal,
            f.KeuJurnal.id == f.KeuStokMutasi.jurnal_id).where(ledger.visible(), f.KeuStokPemakaian.masuk_id == batch.id))).one()
        result.append({"id": batch.id, "produk_id": batch.produk_id, "tanggal": tanggal.isoformat(),
                       "qty": batch.qty-quantity, "nilai": str(batch.nilai-cost), "jurnal_id": batch.jurnal_id})
    return result


async def retry(session, reference, payload):
    existing = (await session.execute(select(f.KeuJurnal).where(f.KeuJurnal.sumber_key == reference))).scalar_one_or_none()
    if existing:
        if existing.rincian != ledger.snapshot(payload):
            svc.bad("Referensi stok sudah digunakan dengan data berbeda", 409)
        return await ledger.detail(session, existing)
    return None


async def receive(session, user, payload):
    await ledger.lock(session)
    await ledger.book(session)
    key = f"stok-masuk:{payload.referensi}"
    existing = await retry(session, key, payload)
    if existing:
        return existing
    product = await svc.get(session, m.KeuProduk, payload.produk_id, lock=True)
    if not product.aktif or product.status != "master":
        svc.bad("Pilih produk master aktif", 409)
    future = (await session.execute(select(f.KeuStokMutasi.id).join(f.KeuJurnal,
        f.KeuJurnal.id == f.KeuStokMutasi.jurnal_id).where(ledger.visible(), f.KeuStokMutasi.produk_id == product.id,
        f.KeuStokMutasi.jenis == "keluar", f.KeuJurnal.tanggal > payload.tanggal).limit(1))).first()
    if future:
        svc.bad("Penerimaan mundur akan mengubah FIFO historis; batalkan pemakaian setelah tanggal ini terlebih dahulu", 409)
    lines = [ledger.line(await ledger.coa(session, "PERSEDIAAN"), payload.biaya_total, produk_id=product.id)]
    if payload.akun_kas_id:
        lines.append(ledger.line(await ledger.cash_coa(session, payload.akun_kas_id), -payload.biaya_total, arus="operasional"))
    else:
        vendor = await svc.get(session, m.KeuVendor, payload.vendor_id)
        if vendor.jenis != ("tukang_kayu" if product.jenis == "kayu" else "supplier"):
            svc.bad("Jenis vendor tidak sesuai produk")
        lines.append(ledger.line(await ledger.coa(session, "UTANG-VENDOR"), -payload.biaya_total, vendor_id=vendor.id))
    journal = await ledger.post(session, user, key, "stok_masuk", payload.tanggal, lines,
        payload.keterangan, rincian=ledger.snapshot(payload))
    session.add(f.KeuStokMutasi(jurnal_id=journal["id"], produk_id=product.id, jenis="masuk", qty=payload.qty, nilai=payload.biaya_total))
    await session.flush()
    return journal


async def fulfill(session, user, payload):
    await ledger.lock(session)
    await ledger.book(session)
    key = f"stok-keluar:{payload.referensi}"
    existing = await retry(session, key, payload)
    if existing:
        return existing
    item, order = await svc.lock_item(session, payload.item_id)
    if order.status in {"selesai", "batal"} or not item.produk_id:
        svc.bad("Pilih item pesanan aktif yang sudah dipetakan", 409)
    product = await svc.get(session, m.KeuProduk, item.produk_id, lock=True)
    if not product.aktif or product.status != "master":
        svc.bad("Produk belum menjadi master aktif", 409)
    vendor_qty = (await session.execute(select(func.coalesce(func.sum(m.KeuAlokasiVendor.qty), 0)).where(
        m.KeuAlokasiVendor.item_id == item.id, m.KeuAlokasiVendor.dibatalkan.is_(False)))).scalar_one()
    if payload.qty+vendor_qty+await used_item(session, item.id) > item.qty:
        svc.bad("Alokasi vendor dan stok melebihi kuantitas item", 409)
    future = (await session.execute(select(f.KeuStokMutasi.id).join(f.KeuJurnal,
        f.KeuJurnal.id == f.KeuStokMutasi.jurnal_id).where(ledger.visible(), f.KeuStokMutasi.produk_id == product.id,
        f.KeuStokMutasi.jenis == "keluar", f.KeuJurnal.tanggal > payload.tanggal).limit(1))).first()
    if future:
        svc.bad("Pemakaian FIFO harus berurutan tanggal; batalkan pemakaian berikutnya terlebih dahulu", 409)
    remaining, takes = payload.qty, []
    for batch in await batches(session, product.id, payload.tanggal):
        take = min(remaining, batch["qty"])
        if not take:
            continue
        value = Decimal(batch["nilai"]) if take == batch["qty"] else (Decimal(batch["nilai"])*take/batch["qty"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        takes.append((batch["id"], take, value))
        remaining -= take
        if not remaining:
            break
    if remaining:
        svc.bad("Stok ready pada tanggal tersebut tidak cukup", 409)
    total = sum((value for _, _, value in takes), ZERO)
    journal = await ledger.post(session, user, key, "stok_keluar", payload.tanggal,
        [ledger.line(await ledger.coa(session, "HPP-STOK"), total, produk_id=product.id),
         ledger.line(await ledger.coa(session, "PERSEDIAAN"), -total, produk_id=product.id)],
        payload.keterangan, rincian=ledger.snapshot(payload), pesanan_id=order.id)
    movement = f.KeuStokMutasi(jurnal_id=journal["id"], produk_id=product.id, item_id=item.id, jenis="keluar", qty=payload.qty, nilai=total)
    session.add(movement)
    await session.flush()
    for batch_id, qty, value in takes:
        session.add(f.KeuStokPemakaian(keluar_id=movement.id, masuk_id=batch_id, qty=qty, nilai=value))
    await session.flush()
    return journal


async def cancellation_guard(session, journal):
    movement = (await session.execute(select(f.KeuStokMutasi).where(f.KeuStokMutasi.jurnal_id == journal.id))).scalar_one_or_none()
    if movement and movement.jenis == "masuk":
        out = aliased(f.KeuStokMutasi)
        used = (await session.execute(select(f.KeuStokPemakaian.id).join(out, out.id == f.KeuStokPemakaian.keluar_id).join(
            f.KeuJurnal, f.KeuJurnal.id == out.jurnal_id).where(ledger.visible(), f.KeuStokPemakaian.masuk_id == movement.id).limit(1))).first()
        if used:
            svc.bad("Batch sudah dipakai; batalkan pemakaian FIFO terkait terlebih dahulu", 409)

    if movement and movement.jenis == "keluar":
        later = (await session.execute(select(f.KeuStokMutasi.id).join(f.KeuJurnal,
            f.KeuJurnal.id == f.KeuStokMutasi.jurnal_id).where(ledger.visible(),
            f.KeuStokMutasi.produk_id == movement.produk_id, f.KeuStokMutasi.jenis == "keluar",
            f.KeuJurnal.id != journal.id,
            (f.KeuJurnal.tanggal > journal.tanggal) |
            ((f.KeuJurnal.tanggal == journal.tanggal) & (f.KeuJurnal.created_at > journal.created_at))).limit(1))).first()
        if later:
            svc.bad("Batalkan pemakaian FIFO berikutnya terlebih dahulu agar jejak biaya tetap konsisten", 409)
