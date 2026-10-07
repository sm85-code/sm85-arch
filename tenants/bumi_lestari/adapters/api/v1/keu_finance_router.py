"""Ledger endpoints installed onto the existing guarded tenant-local /keu router."""
from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application import keu_accounting as accounting, keu_inventory as inventory, keu_ledger as ledger, keu_services as svc, schemas_keu as legacy, schemas_keu_finance as sc
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure import models_keu as m, models_keu_finance as f
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlKategori, BlUser


def install(router, DB, ACTOR):
    from .keu_expenses_router import install as install_expenses
    install_expenses(router, DB, ACTOR)

    @router.get("/buku")
    async def book(session: AsyncSession = DB):
        settings = await ledger.book(session, required=False)
        return {"aktif": settings is not None and settings.status == "aktif", "pengaturan": svc.record(settings) if settings else None}

    @router.post("/buku/aktivasi")
    async def initialize(payload: sc.BukuIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
        return await accounting.initialize(session, actor, payload)

    @router.get("/coa", response_model=legacy.PageOut)
    async def coa_list(session: AsyncSession = DB, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return await svc.page(session, f.KeuCoa, limit, offset)

    @router.post("/coa", status_code=201)
    async def coa_create(payload: sc.CoaIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
        await ledger.book(session)
        row = f.KeuCoa(**payload.model_dump())
        session.add(row)
        await session.flush()
        await svc.audit(session, actor, row)
        return svc.record(row)

    @router.patch("/coa/{key}")
    async def coa_edit(key: str, payload: sc.CoaIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
        row = await svc.get(session, f.KeuCoa, key, lock=True)
        if row.sistem or (await session.execute(select(f.KeuJurnalBaris.id).where(f.KeuJurnalBaris.coa_id == key).limit(1))).first():
            if (row.kode, row.jenis, row.kelompok) != (payload.kode, payload.jenis, payload.kelompok):
                svc.bad("Identitas akun sistem/terpakai tidak dapat diubah", 409)
        before = svc.record(row)
        for field, value in payload.model_dump().items():
            setattr(row, field, value)
        await session.flush()
        await svc.audit(session, actor, row, "ubah-coa", before)
        return svc.record(row)

    @router.patch("/coa/{key}/status")
    async def coa_status(key: str, payload: legacy.MasterStatusIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
        row = await svc.get(session, f.KeuCoa, key, lock=True)
        if row.sistem:
            svc.bad("Akun sistem tidak dapat dinonaktifkan", 409)
        before = svc.record(row)
        row.aktif = payload.aktif
        await session.flush()
        await svc.audit(session, actor, row, "status-coa", before)
        return svc.record(row)

    @router.get("/kategori-coa")
    async def category_mappings(session: AsyncSession = DB):
        return [svc.record(row) for row in (await session.execute(select(f.KeuKategoriCoa).order_by(f.KeuKategoriCoa.id))).scalars()]

    @router.put("/kategori-coa/{key}")
    async def category_mapping(key: str, payload: sc.KategoriCoaIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
        await svc.get(session, BlKategori, key)
        account = await svc.get(session, f.KeuCoa, payload.coa_id)
        if not account.aktif or account.kelompok in {"kas", "utang_vendor", "persediaan", "piutang_pengiriman", "piutang_escrow", "hpp_stok", "hpp_vendor"}:
            svc.bad("Gunakan modul khusus untuk akun kas, piutang, vendor, dan persediaan")
        row = await session.get(f.KeuKategoriCoa, key)
        before = svc.record(row) if row else None
        if row is None:
            row = f.KeuKategoriCoa(id=key, **payload.model_dump())
            session.add(row)
        else:
            row.coa_id, row.arus = payload.coa_id, payload.arus
        await session.flush()
        await svc.audit(session, actor, row, "pemetaan-kategori", before)
        return svc.record(row)

    @router.post("/settlement/manual-bukti", status_code=201)
    async def manual_settlement(payload: sc.SettlementBuktiIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
        values = payload.model_dump(exclude={"bukti_pencairan"})
        values["rincian"] = {**values["rincian"], "bukti_pencairan": payload.bukti_pencairan,
                             "provenance": "konfirmasi_manual_internal", "dicatat_oleh": actor.id}
        return await svc.create_settlement(session, actor, legacy.SettlementIn.model_validate(values), from_source=True)

    @router.post("/mutasi", status_code=201)
    async def transfer(payload: sc.MutasiIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
        return await ledger.transfer(session, actor, payload)

    @router.post("/jurnal", status_code=201)
    async def manual(payload: sc.JurnalIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
        return await ledger.manual(session, actor, payload)

    @router.get("/jurnal", response_model=legacy.PageOut)
    async def journals(session: AsyncSession = DB, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                       status: Literal["pengerjaan", "batal", "semua"] = "pengerjaan", jenis: str | None = Query(None, max_length=32),
                       tanggal_awal: date | None = None, tanggal_akhir: date | None = None):
        if (tanggal_awal is None) != (tanggal_akhir is None) or (tanggal_awal and tanggal_awal > tanggal_akhir):
            svc.bad("Rentang tanggal tidak valid")
        query = select(f.KeuJurnal)
        if status == "pengerjaan":
            query = query.where(ledger.visible())
        elif status == "batal":
            query = query.where(f.KeuJurnal.status == "dibatalkan")
        if jenis:
            query = query.where(f.KeuJurnal.jenis == jenis)
        if tanggal_awal:
            query = query.where(f.KeuJurnal.tanggal >= tanggal_awal, f.KeuJurnal.tanggal <= tanggal_akhir)
        total = (await session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
        rows = (await session.execute(query.order_by(f.KeuJurnal.tanggal.desc(), f.KeuJurnal.id).offset(offset).limit(limit))).scalars().all()
        return {"rows": [await ledger.detail(session, row) for row in rows], "total": total, "limit": limit, "offset": offset}

    @router.post("/jurnal/{key}/unpost")
    async def unpost(key: str, payload: legacy.BatalIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
        return await ledger.unpost(session, actor, key, payload)

    @router.get("/laporan-keuangan")
    async def reports(tanggal_awal: date, tanggal_akhir: date, session: AsyncSession = DB):
        return await ledger.reports(session, tanggal_awal, tanggal_akhir)

    @router.get("/buku-kas", response_model=legacy.PageOut)
    async def cashbook(tanggal_awal: date, tanggal_akhir: date, akun_id: str | None = Query(None, max_length=64),
                      limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), session: AsyncSession = DB):
        await ledger.book(session)
        if tanggal_awal > tanggal_akhir:
            svc.bad("Tanggal awal tidak boleh melebihi tanggal akhir")
        running = func.sum(f.KeuJurnalBaris.debet-f.KeuJurnalBaris.kredit).over(partition_by=f.KeuJurnalBaris.coa_id,
            order_by=(f.KeuJurnal.tanggal, f.KeuJurnal.created_at, f.KeuJurnal.id, f.KeuJurnalBaris.nomor))
        query = select(f.KeuJurnalBaris.id, f.KeuJurnalBaris.jurnal_id, f.KeuJurnalBaris.debet, f.KeuJurnalBaris.kredit,
            f.KeuJurnalBaris.arus, f.KeuJurnal.tanggal, f.KeuJurnal.created_at, f.KeuJurnal.id.label("ordre"), f.KeuJurnalBaris.nomor, f.KeuJurnal.keterangan, f.KeuCoa.kas_akun_id.label("akun_id"),
            f.KeuCoa.nama, running.label("saldo")).join(f.KeuJurnal, f.KeuJurnal.id == f.KeuJurnalBaris.jurnal_id).join(
            f.KeuCoa, f.KeuCoa.id == f.KeuJurnalBaris.coa_id).where(ledger.visible(), f.KeuCoa.kas_akun_id.is_not(None), f.KeuJurnal.tanggal <= tanggal_akhir)
        if akun_id:
            query = query.where(f.KeuCoa.kas_akun_id == akun_id)
        subquery = query.subquery()
        filtered = select(subquery).where(subquery.c.tanggal >= tanggal_awal)
        total = (await session.execute(select(func.count()).select_from(filtered.subquery()))).scalar_one()
        rows = (await session.execute(filtered.order_by(subquery.c.tanggal, subquery.c.created_at, subquery.c.ordre, subquery.c.nomor).offset(offset).limit(limit))).mappings().all()
        return {"rows": [svc.json_value(dict(row)) for row in rows], "total": total, "limit": limit, "offset": offset}

    @router.post("/stok/masuk", status_code=201)
    async def stock_receive(payload: sc.StokMasukIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
        return await inventory.receive(session, actor, payload)

    @router.post("/stok/keluar", status_code=201)
    async def stock_fulfill(payload: sc.StokKeluarIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
        return await inventory.fulfill(session, actor, payload)

    @router.get("/stok", response_model=legacy.PageOut)
    async def stock(session: AsyncSession = DB, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        await ledger.book(session)
        products = (await session.execute(select(m.KeuProduk).order_by(m.KeuProduk.id).offset(offset).limit(limit))).scalars().all()
        rows = []
        for product in products:
            lots = await inventory.batches(session, product.id)
            rows.append({"id": product.id, "sku": product.sku, "nama": product.nama, "qty": sum(lot["qty"] for lot in lots),
                         "nilai": str(sum((Decimal(lot["nilai"]) for lot in lots), Decimal("0"))), "batch": lots})
        total = (await session.execute(select(func.count()).select_from(m.KeuProduk))).scalar_one()
        return {"rows": rows, "total": total, "limit": limit, "offset": offset}

    @router.get("/piutang", response_model=legacy.PageOut)
    async def receivables(session: AsyncSession = DB, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        await ledger.book(session)
        query = select(m.KeuPesanan).where(m.KeuPesanan.status != "batal").order_by(m.KeuPesanan.id)
        total = (await session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
        rows = []
        for order in (await session.execute(query.offset(offset).limit(limit))).scalars():
            rows.append({"id": order.id, "nomor": order.nomor, "status_sumber": order.status_sumber,
                "pengiriman": str(await accounting.order_balance(session, order.id, "PIUTANG-KIRIM")),
                "escrow": str(await accounting.order_balance(session, order.id, "PIUTANG-ESCROW"))})
        return {"rows": rows, "total": total, "limit": limit, "offset": offset}

    @router.post("/piutang/rekonsiliasi")
    async def receivable_reconcile(payload: sc.PiutangIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
        await ledger.book(session)
        order = await svc.get(session, m.KeuPesanan, payload.pesanan_id, lock=True)
        result = await accounting.receivable(session, actor, order, payload.posisi, payload.tanggal,
            f"rekonsiliasi:{payload.referensi}", provenance="konfirmasi_manual")
        return result or {"id": order.id, "tidak_berubah": True}

    @router.post("/vendor/pembayaran", status_code=201)
    async def pay_vendor(payload: sc.BayarVendorIn, session: AsyncSession = DB, actor: BlUser = ACTOR):
        return await accounting.pay_vendor(session, actor, payload)

    @router.get("/utang-vendor", response_model=legacy.PageOut)
    async def vendor_debts(session: AsyncSession = DB, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        await ledger.book(session)
        vendors = (await session.execute(select(m.KeuVendor).order_by(m.KeuVendor.id).offset(offset).limit(limit))).scalars().all()
        total = (await session.execute(select(func.count()).select_from(m.KeuVendor))).scalar_one()
        return {"rows": [{"id": v.id, "nama": v.nama, "utang": str(await accounting.vendor_debt(session, v.id))} for v in vendors], "total": total, "limit": limit, "offset": offset}
