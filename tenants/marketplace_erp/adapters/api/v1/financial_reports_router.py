"""Every report descendant is admin-only, including sources, history and exports."""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.application import financial_manual as manual
from tenants.marketplace_erp.modules.marketplace_erp.application import financial_reports as reports
from tenants.marketplace_erp.modules.marketplace_erp.application import operasional as ops
from tenants.marketplace_erp.modules.marketplace_erp.application.financial_drive_import import DriveImport, import_drive
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.auth import require_roles_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import get_db_marketplace_erp
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import (
    LaporanKeuanganAwal,
    LaporanKeuanganAsal,
    LaporanKeuanganTutup,
    LaporanKeuanganAudit,
    UserMarketplaceErp,
    AkunMarketplace,
)

admin = require_roles_marketplace_erp("admin")
router = APIRouter(prefix="/laporan-keuangan", tags=["Laporan Keuangan"], dependencies=[Depends(admin)])


@router.get("")
async def overview(
    periode: str = Query(pattern=r"^\d{4}-\d{2}$"), session: AsyncSession = Depends(get_db_marketplace_erp)
):
    return await reports.overview(session, periode)


@router.get("/operasional")
async def operasional(
    periode: str = Query(pattern=r"^\d{4}-\d{2}$"), session: AsyncSession = Depends(get_db_marketplace_erp)
):
    """Operasional Umum / Packing entries per date and category, which Arus Kas shows in the matching rows."""
    return await ops.untuk_arus_kas(session, periode)


@router.get("/lembar-manual")
async def manual_sheet(
    periode: str = Query(pattern=r"^\d{4}-\d{2}$"), session: AsyncSession = Depends(get_db_marketplace_erp)
):
    return await manual.muat(session, periode)


@router.put("/lembar-manual")
async def save_manual_sheet(
    payload: manual.Simpan,
    periode: str = Query(pattern=r"^\d{4}-\d{2}$"),
    session: AsyncSession = Depends(get_db_marketplace_erp),
    actor: UserMarketplaceErp = Depends(admin),
):
    await _cek_pembagian_laba(session, periode, payload)
    return await manual.simpan(session, periode, payload, actor)


def _sel_pembagian(sel: dict) -> dict:
    return {k: v for k, v in sel.items() if k.startswith("pl_")}


async def _cek_pembagian_laba(session, periode: str, payload) -> None:
    """The profit split of a month is filed in the next month's Arus Kas; it may only be set once that month is final."""
    saved = await manual.muat(session, periode)
    lama = _sel_pembagian(saved["lembar"]["sel"])
    baru = _sel_pembagian(payload.lembar.sel)
    berubah = {k: v for k, v in baru.items() if lama.get(k) != v and str(v).strip() not in ("", "0")}
    if not berubah:
        return
    y, m = int(periode[:4]), int(periode[5:])
    lalu = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
    if not (await reports.overview(session, lalu))["bulanan"]["final"]:
        raise HTTPException(409, f"Pembagian laba {lalu} hanya bisa diisi setelah tutup buku bulan itu final (1 s/d akhir bulan)")


@router.get("/pemasukan-shopee")
async def shopee_income(
    periode: str = Query(pattern=r"^\d{4}-\d{2}$"),
    jumlah_kolom: int | None = Query(default=None, ge=1, le=7),
    sampai: date | None = None,
    session: AsyncSession = Depends(get_db_marketplace_erp),
):
    from tenants.marketplace_erp.modules.marketplace_erp.application.financial_shopee import weekly_income

    return await weekly_income(session, periode, jumlah_kolom, sampai=sampai)


@router.get("/piutang-pengiriman-shopee")
async def shipping_receivables(
    periode: str = Query(pattern=r"^\d{4}-\d{2}$"),
    jumlah_kolom: int | None = Query(default=None, ge=1, le=7),
    sampai: date | None = None,
    session: AsyncSession = Depends(get_db_marketplace_erp),
):
    from tenants.marketplace_erp.modules.marketplace_erp.application.financial_shopee import weekly_shipping_receivables

    return await weekly_shipping_receivables(session, periode, jumlah_kolom, sampai=sampai)


@router.get("/piutang-pengiriman-shopee/{akun_id}", summary="Piutang mingguan dengan mutasi Dana Cepat BUMI TANI IND")
async def shipping_receivables_pending(
    akun_id: str,
    periode: str = Query(pattern=r"^\d{4}-\d{2}$"),
    jumlah_kolom: int | None = Query(default=None, ge=1, le=7),
    sampai: date | None = None,
    session: AsyncSession = Depends(get_db_marketplace_erp),
):
    from tenants.marketplace_erp.modules.marketplace_erp.application.financial_pending import weekly_pending_receivables

    return await weekly_pending_receivables(session, akun_id, periode, jumlah_kolom, sampai=sampai)


@router.get("/saldo-penjual-shopee")
async def shopee_released_funds(
    periode: str = Query(pattern=r"^\d{4}-\d{2}$"),
    jumlah_kolom: int | None = Query(default=None, ge=1, le=7),
    sampai: date | None = None,
    session: AsyncSession = Depends(get_db_marketplace_erp),
):
    from tenants.marketplace_erp.modules.marketplace_erp.application.financial_shopee import weekly_income

    return await weekly_income(session, periode, jumlah_kolom, released=True, sampai=sampai)


@router.get("/saldo-wallet-shopee/{akun_id}")
async def shopee_weekly_wallet(
    akun_id: str,
    tanggal: date,
    session: AsyncSession = Depends(get_db_marketplace_erp),
):
    from tenants.marketplace_erp.modules.marketplace_erp.application import workflows
    from tenants.marketplace_erp.modules.marketplace_erp.application.financial_shopee import SHOP_ORDER, shop_key

    if tanggal.weekday() != 6 or tanggal >= datetime.now(ZoneInfo("Asia/Jakarta")).date():
        raise HTTPException(422, "Saldo mingguan hanya untuk Minggu yang sudah berakhir pukul 23:59 WIB")
    name = await session.scalar(select(AkunMarketplace.nama_toko).where(
        AkunMarketplace.id == akun_id, AkunMarketplace.platform == "shopee",
    ))
    if name is None or shop_key(name) not in SHOP_ORDER:
        raise HTTPException(404, "Toko tidak termasuk enam toko laporan")
    return await workflows.shop_balance_snapshot(session, akun_id, cutoff=tanggal)


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
