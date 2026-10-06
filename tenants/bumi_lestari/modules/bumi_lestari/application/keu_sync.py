"""Bounded read-only source pull. No Shopee/iPaymu HTTP calls or source writes."""
from __future__ import annotations

import json
from datetime import timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import selectinload

from tenants.bumi_lestari.modules.bumi_lestari.application import keu_services as svc, schemas_keu as sc
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_keu as m


async def source_options():
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import SessionLocal
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AkunMarketplace
    from tenants.store.modules.store.infrastructure.database import SessionLocal as store_session
    result = {"erp": [], "store_tersedia": store_session is not None, "erp_tersedia": SessionLocal is not None}
    if SessionLocal:
        async with SessionLocal() as source:
            rows = (await source.execute(select(AkunMarketplace.id, AkunMarketplace.nama_toko, AkunMarketplace.platform).order_by(AkunMarketplace.nama_toko))).all()
            result["erp"] = [{"id": row.id, "nama": row.nama_toko, "platform": row.platform} for row in rows]
    return result


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def business_date(value):
    return aware(value).astimezone(ZoneInfo("Asia/Jakarta")).date()


def after_cursor(timestamp, key, cursor):
    if cursor.watermark_at is None:
        return None
    return or_(timestamp > cursor.watermark_at,
               and_(timestamp == cursor.watermark_at, key > (cursor.watermark_ref or "")))


async def process(session, user, entry):
    try:
        # Each event is atomic. A failed source revision is durable and can be retried.
        async with session.begin_nested():
            if entry.entitas == "order":
                await svc.create_order(session, user, sc.PesananIn.model_validate(entry.payload), from_source=True)
            else:
                await svc.create_settlement(session, user, sc.SettlementIn.model_validate(entry.payload), from_source=True)
        entry.status, entry.kesalahan = "terproses", []
    except (HTTPException, ValidationError) as exc:
        entry.status = "gagal"
        message = exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, str) else "Validasi data sumber gagal"
        entry.kesalahan = [{"pesan": message[:255]}]
    entry.percobaan += 1
    await session.flush()
    return entry.status


async def retry(session, user, key):
    entry = await svc.get(session, m.KeuMasukan, key, lock=True)
    if entry.impor_id or entry.entitas not in {"order", "settlement"}:
        svc.bad("Gunakan penerapan batch untuk baris impor")
    if entry.status != "gagal":
        svc.bad("Hanya masukan gagal yang dapat dicoba ulang", 409)
    await process(session, user, entry)
    return svc.record(entry)


async def pull(session, user, key, entity):
    if entity not in {"order", "settlement"}:
        svc.bad("Entitas sinkronisasi tidak dikenal")
    channel = await svc.get(session, m.KeuSaluran, key, lock=True)
    if not channel.aktif or channel.sistem == "manual":
        svc.bad("Aktifkan saluran sumber terlebih dahulu")
    if channel.sistem == "store" and entity == "settlement":
        svc.bad("Store tidak menyediakan bukti settlement; hanya order yang dapat ditarik", 422)
    cursor = await session.get(m.KeuCursor, (key, entity))
    if cursor is None:
        cursor = m.KeuCursor(saluran_id=key, entitas=entity)
        session.add(cursor)
        await session.flush()
    if channel.sistem == "marketplace_erp":
        from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import SessionLocal
        from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import Pesanan, SettlementPesanan
        model = Pesanan if entity == "order" else SettlementPesanan
        stamp = Pesanan.updated_at if entity == "order" else SettlementPesanan.diambil_at
        query = select(model).where(model.akun_id == channel.akun_ref)
    else:
        from tenants.store.modules.store.infrastructure.database import SessionLocal
        from tenants.store.modules.store.infrastructure.models import PesananStore
        model, stamp = PesananStore, PesananStore.updated_at
        query = select(model)
    if SessionLocal is None:
        svc.bad("Database sumber belum dikonfigurasi", 503)
    cutoff = after_cursor(stamp, model.id, cursor)
    if cutoff is not None:
        query = query.where(cutoff)
    if entity == "order":
        query = query.options(selectinload(model.items))
    query = query.order_by(stamp, model.id).limit(100)
    processed, failed, seen = 0, 0, 0
    async with SessionLocal() as source:
        if source.bind.dialect.name == "postgresql":
            await source.execute(text("SET TRANSACTION READ ONLY"))
        rows = (await source.execute(query)).scalars().all()
        for source_row in rows:
            if entity == "order":
                lines = [{"sumber_ref": f"{line.__tablename__}:{line.id}", "produk_id": None,
                          "nama_snapshot": line.nama_produk,
                          "varian_snapshot": line.model_name if channel.sistem == "marketplace_erp" else line.nama_varian,
                          "qty": line.qty, "harga_satuan": str(line.harga_satuan), "subtotal_sumber": str(line.subtotal)}
                         for line in source_row.items]
                changed = aware(source_row.updated_at)
                payload = {"saluran_id": key, "sumber_ref": f"{source_row.__tablename__}:{source_row.id}",
                           "nomor": source_row.id_eksternal if channel.sistem == "marketplace_erp" else source_row.id,
                           "tanggal": business_date((source_row.dipesan_at or source_row.created_at) if channel.sistem == "marketplace_erp" else source_row.created_at).isoformat(),
                           "status_sumber": source_row.status, "total_sumber": str(source_row.total),
                           "sumber_updated_at": changed.isoformat(), "items": lines}
            else:
                changed = aware(source_row.diambil_at)
                try:
                    raw = json.loads(source_row.rincian or "{}")
                except (json.JSONDecodeError, TypeError):
                    raw = {"raw_tidak_valid": source_row.rincian}
                gross, net = Decimal(source_row.penjualan), Decimal(source_row.jumlah_cair)
                payload = {"saluran_id": key, "sumber_ref": f"mpe_settlement_pesanan:{source_row.id}",
                           "tanggal_cair": business_date(source_row.dirilis_at).isoformat() if source_row.dirilis_at else None,
                           "bruto": str(gross), "potongan": str(gross - net), "penyesuaian": "0", "neto": str(net),
                           "rincian": {"order_sn": source_row.order_sn, "order_income": raw,
                                       "potongan_metode": "selisih_bruto_neto; periksa rincian provider"}}
            envelope = sc.MasukanIn(saluran_id=key, entitas=entity, sumber_ref=payload["sumber_ref"],
                                    sumber_updated_at=changed, payload=payload)
            digest = sc.checksum_revisi(envelope)
            entry = (await session.execute(select(m.KeuMasukan).where(m.KeuMasukan.saluran_id == key,
                                      m.KeuMasukan.entitas == entity, m.KeuMasukan.sumber_ref == envelope.sumber_ref,
                                      m.KeuMasukan.revisi_sha256 == digest))).scalar_one_or_none()
            if entry is None:
                entry = m.KeuMasukan(**envelope.model_dump(), revisi_sha256=digest)
                session.add(entry)
                await session.flush()
                result = await process(session, user, entry)
                processed += result == "terproses"
                failed += result == "gagal"
            seen += 1
            cursor.watermark_at, cursor.watermark_ref = changed, source_row.id
    await session.flush()
    await svc.audit(session, user, channel, "tarik-sumber")
    return {"dibaca": seen, "terproses": processed, "gagal": failed, "ada_lanjutan": seen == 100}
