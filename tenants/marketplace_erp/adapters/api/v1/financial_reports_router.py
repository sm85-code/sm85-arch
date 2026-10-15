"""Every report descendant is admin-only, including sources, history and exports."""

import json
from datetime import date, datetime, time, timedelta
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
    Pesanan,
    AkunMarketplace,
    IklanHarianToko,
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
    sampai: date | None = None,
    session: AsyncSession = Depends(get_db_marketplace_erp),
):
    from tenants.marketplace_erp.modules.marketplace_erp.application.financial_shopee import weekly_income

    return await weekly_income(session, periode, jumlah_kolom, sampai=sampai)


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


@router.get("/{tanggal}/pembanding")
async def comparison(tanggal: date, session: AsyncSession = Depends(get_db_marketplace_erp)):
    record = await session.get(LaporanKeuanganTutup, tanggal)
    if not record:
        raise HTTPException(404, "Laporan tidak ditemukan")
    report = json.loads(record.snapshot_json)
    from tenants.marketplace_erp.modules.marketplace_erp.application.financial_shopee import (
        REPORT_SHOPS, selected_shops, shipping_day, shop_key, unfinished_query,
    )

    month_start = tanggal.replace(day=1)
    start = month_start - timedelta(days=month_start.weekday())
    shops = await selected_shops(session)
    names = {s.id: s.nama_toko for s in shops}
    rows = (await session.execute(unfinished_query(list(names)).where(
        Pesanan.tanggal_kirim >= datetime.combine(start, time.min, reports.WIB),
        Pesanan.tanggal_kirim < datetime.combine(tanggal + timedelta(days=1), time.min, reports.WIB),
    ))).all()
    orders = [
        {"id": o.id, "nomor": o.id_eksternal, "akun_id": o.akun_id, "toko": names[o.akun_id], "total_bruto": str(o.total),
         "tanggal_kirim": shipping_day(o.tanggal).isoformat(), "status": o.status_marketplace or o.status}
        for o in rows if shipping_day(o.tanggal).weekday() != 6
    ]
    orders.sort(key=lambda o: (o["tanggal_kirim"], o["toko"], o["nomor"]))
    ads = (await session.scalars(select(IklanHarianToko.expense).where(
        IklanHarianToko.akun_id.in_(list(names)),
        IklanHarianToko.tanggal >= start, IklanHarianToko.tanggal <= tanggal,
    ))).all()
    snapshots = (await session.scalars(select(LaporanKeuanganTutup).where(
        LaporanKeuanganTutup.tanggal >= month_start, LaporanKeuanganTutup.tanggal <= tanggal,
    ))).all()
    books = [json.loads(row.snapshot_json) for row in snapshots]
    book_ads = sum((reports.money(b["hasil"]["biaya_iklan"]) for b in books), reports.ZERO)
    book_start = min(b["mulai"] for b in books)
    def breakdown(items):
        return {"jumlah_pesanan": len(items), "nominal": str(sum(
            (reports.money(o["total_bruto"]) for o in items), reports.ZERO))}
    by_shop = []
    for name in REPORT_SHOPS:
        ids = {s.id for s in shops if shop_key(s.nama_toko) == shop_key(name)}
        by_shop.append({"nama": name, "akun_ditemukan": bool(ids), "masuk_estimasi_drive": name != "BUMI TANI IND",
                        **breakdown([o for o in orders if o["akun_id"] in ids])})
    by_week = []
    week = start
    while week <= tanggal:
        last = min(week + timedelta(days=5), tanggal)
        by_week.append({"mulai": week.isoformat(), "akhir": last.isoformat(), **breakdown(
            [o for o in orders if week.isoformat() <= o["tanggal_kirim"] <= last.isoformat()])})
        week += timedelta(days=7)
    end = report["hasil"]["saldo_akhir"]
    return {
        "periode": {"mulai": start.isoformat(), "akhir": tanggal.isoformat(), "hari": "Senin–Sabtu"},
        "periode_iklan_laporan": {"mulai": book_start, "akhir": tanggal.isoformat()},
        "laporan": {
            "piutang_pengiriman": end["piutang_pengiriman"],
            "saldo_toko": end["saldo_toko"],
            "piutang_total": str(reports.money(end["piutang_pengiriman"]) + reports.money(end["saldo_toko"])),
            "biaya_iklan": str(book_ads),
        },
        "erp": {
            "piutang_pengiriman_bruto": str(
                sum((reports.money(o["total_bruto"]) for o in orders), reports.ZERO)
            ),
            "saldo_toko": None,
            "piutang_belum_ditarik": None,
            "biaya_iklan": str(sum(ads, reports.ZERO)),
            "pesanan": orders,
            "rincian_toko": by_shop,
            "estimasi_bumi_tani": next(t["nominal"] for t in by_shop if t["nama"] == "BUMI TANI IND"),
            "estimasi_cakupan_drive": str(sum(
                (reports.money(t["nominal"]) for t in by_shop if t["masuk_estimasi_drive"]), reports.ZERO)),
            "catatan_dana_cepat": "Estimasi BUMI TANI IND tidak masuk Google Drive. Dana Cepat: dikirim sebelum pukul 17.00 cair pukul 17.00 hari itu; setelah pukul 17.00 cair hari berikutnya. Jadwal ini keterangan, bukan pengganti tanggal/nilai pencairan riil.",
            "rincian_minggu": by_week,
        },
        "dibaca_at": datetime.now(ZoneInfo("Asia/Jakarta")).isoformat(),
        "catatan": "Pengiriman dibatasi enam toko laporan, tanggal kirim dari Senin pada minggu tanggal 1 sampai tanggal laporan terakhir, Senin–Sabtu, sama dengan tabel estimasi pada tab Pemasukan. Status pesanan memakai kondisi saat dibaca; nilai bruto bukan saldo bersih historis. Estimasi BUMI TANI IND (Dana Cepat) dipisahkan dari subtotal lima toko yang masuk cakupan estimasi Drive. Saldo tertahan dan piutang gabungan terverifikasi belum tersedia.",
    }
