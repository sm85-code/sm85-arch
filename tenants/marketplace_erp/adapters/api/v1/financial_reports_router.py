"""Every report descendant is admin-only, including sources, history and exports."""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import financial_reports as reports
from tenants.marketplace_erp.modules.marketplace_erp.application.financial_drive_import import DriveImport, import_drive
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    LaporanKeuanganAwal,
    LaporanKeuanganAsal,
    LaporanKeuanganTutup,
    LaporanKeuanganAudit,
    UserMarketplaceErp,
)

admin = require_roles_marketplace_erp("admin")
router = APIRouter(prefix="/laporan-keuangan", tags=["Laporan Keuangan"], dependencies=[Depends(admin)])


@router.get("")
async def overview(
    periode: str = Query(pattern=r"^\d{4}-\d{2}$"), session: AsyncSession = Depends(get_db_marketplace_erp)
):
    return await reports.overview(session, periode)


@router.get("/pemasukan-shopee")
async def shopee_income(
    periode: str = Query(pattern=r"^\d{4}-\d{2}$"),
    jumlah_kolom: int | None = Query(default=None, ge=1, le=7),
    session: AsyncSession = Depends(get_db_marketplace_erp),
):
    from tenants.marketplace_erp.modules.marketplace_erp.application.financial_shopee import weekly_income

    return await weekly_income(session, periode, jumlah_kolom)


@router.get("/saldo-penjual-shopee")
async def shopee_released_funds(
    periode: str = Query(pattern=r"^\d{4}-\d{2}$"),
    jumlah_kolom: int | None = Query(default=None, ge=1, le=7),
    session: AsyncSession = Depends(get_db_marketplace_erp),
):
    from tenants.marketplace_erp.modules.marketplace_erp.application.financial_shopee import weekly_income

    return await weekly_income(session, periode, jumlah_kolom, released=True)


@router.post("/saldo-awal")
async def save_opening(
    payload: reports.Opening,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    actor: UserMarketplaceErp = Depends(admin),
):
    if payload.tanggal >= datetime.now(ZoneInfo("Asia/Jakarta")).date():
        raise HTTPException(422, "Saldo awal harus bertanggal sebelum hari ini")
    row = await session.scalar(select(LaporanKeuanganAwal).where(LaporanKeuanganAwal.id == "global").with_for_update())
    if await session.scalar(select(LaporanKeuanganTutup.tanggal).limit(1)):
        raise HTTPException(409, "Saldo awal terkunci setelah laporan pertama ditutup")
    if reports.totals(payload.saldo.model_dump())["selisih"] != 0:
        raise HTTPException(422, "Saldo awal harus seimbang: aset = utang + modal + saldo laba")
    if row is None:
        row = LaporanKeuanganAwal(id="global")
        session.add(row)
    row.tanggal, row.saldo_json, row.created_by = payload.tanggal, payload.saldo.model_dump_json(), actor.id
    session.add(
        LaporanKeuanganAudit(
            tanggal=payload.tanggal,
            action="opening",
            reason="Saldo awal laporan",
            snapshot_json=payload.model_dump_json(),
            created_by=actor.id,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Saldo awal baru saja disimpan admin lain; muat ulang")
    return {"tersimpan": True}


@router.get("/sumber")
async def source_data(tanggal: date, session: AsyncSession = Depends(get_db_marketplace_erp)):
    previous, before = await reports.baseline(session, tanggal)
    origin = await session.get(LaporanKeuanganAsal, "global")
    return {
        "sumber_utama": "google_drive" if origin else "erp",
        "mulai": (previous + timedelta(days=1)).isoformat(),
        "saldo_sebelumnya": reports.serialize(before.model_dump()),
        "sumber": await reports.sources(session, previous + timedelta(days=1), tanggal),
    }


@router.post("/pratinjau")
async def preview(payload: reports.CloseInput, session: AsyncSession = Depends(get_db_marketplace_erp)):
    return await reports.preview(session, payload)


@router.post("/tutup")
async def close(
    payload: reports.CloseInput,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    actor: UserMarketplaceErp = Depends(admin),
):
    return await reports.close(session, payload, actor)


@router.post("/{tanggal}/buka-kembali")
async def reopen(
    tanggal: date,
    payload: reports.ReopenInput,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    actor: UserMarketplaceErp = Depends(admin),
):
    return await reports.reopen(session, tanggal, payload.alasan, actor)


@router.get("/riwayat")
async def history(session: AsyncSession = Depends(get_db_marketplace_erp)):
    rows = (
        await session.scalars(select(LaporanKeuanganAudit).order_by(LaporanKeuanganAudit.created_at.desc()).limit(100))
    ).all()
    return [
        {
            "id": r.id,
            "tanggal": r.tanggal.isoformat(),
            "action": r.action,
            "alasan": r.reason,
            "oleh": r.created_by,
            "waktu": r.created_at.isoformat(),
            "snapshot": json.loads(r.snapshot_json),
        }
        for r in rows
    ]


@router.post("/impor-drive")
async def drive_import(
    payload: DriveImport,
    session: AsyncSession = Depends(get_db_marketplace_erp),
    actor: UserMarketplaceErp = Depends(admin),
):
    return await import_drive(session, payload, actor)


@router.get("/{tanggal}/pembanding")
async def comparison(tanggal: date, session: AsyncSession = Depends(get_db_marketplace_erp)):
    record = await session.get(LaporanKeuanganTutup, tanggal)
    if not record:
        raise HTTPException(404, "Laporan tidak ditemukan")
    report = json.loads(record.snapshot_json)
    source = await reports.sources(session, date.fromisoformat(report["mulai"]), tanggal)
    end = report["hasil"]["saldo_akhir"]
    return {
        "laporan": {
            "piutang_pengiriman": end["piutang_pengiriman"],
            "saldo_toko": end["saldo_toko"],
            "piutang_total": str(reports.money(end["piutang_pengiriman"]) + reports.money(end["saldo_toko"])),
            "biaya_iklan": report["hasil"]["biaya_iklan"],
        },
        "erp": {
            "piutang_pengiriman_bruto": str(
                sum((reports.money(o["total_bruto"]) for o in source["pesanan_pengiriman"]), reports.ZERO)
            ),
            "saldo_toko": None,
            "piutang_belum_ditarik": None,
            "biaya_iklan": source["biaya_iklan"],
            "pesanan": source["pesanan_pengiriman"],
        },
        "dibaca_at": datetime.now(ZoneInfo("Asia/Jakarta")).isoformat(),
        "catatan": "Saldo tertahan tidak dipisahkan di ERP. Pengiriman ERP yang dibaca saat ini bernilai bruto; bukan saldo bersih historis. Piutang gabungan belum dapat dihitung tanpa data wallet/penarikan yang lengkap.",
    }
