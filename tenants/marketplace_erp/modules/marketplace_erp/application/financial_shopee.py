"""Read-only Shopee comparison by shipping date, Monday–Saturday (WIB)."""

from datetime import date, datetime, time, timedelta

from fastapi import HTTPException
from sqlalchemy import and_, select

from .financial_reports import WIB, ZERO, money
from ..infrastructure.models import AkunMarketplace, Pesanan, SettlementPesanan


async def weekly_income(session, periode: str, jumlah_kolom: int | None = None):
    try:
        first = date.fromisoformat(f"{periode}-01")
    except ValueError:
        raise HTTPException(422, "Bulan laporan tidak valid")
    following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    monday = first - timedelta(days=first.weekday())
    count = jumlah_kolom or ((following - timedelta(days=1) - monday).days // 7 + 1)
    periods = [
        {"mulai": (monday + timedelta(days=7*i)).isoformat(),
         "akhir": (monday + timedelta(days=7*i+5)).isoformat()}
        for i in range(count)
    ]
    shops = (await session.execute(
        select(AkunMarketplace.id, AkunMarketplace.nama_toko)
        .where(AkunMarketplace.platform == "shopee")
        .order_by(AkunMarketplace.nama_toko, AkunMarketplace.id)
    )).all()
    cells = {s.id: [{"estimasi": ZERO, "riil": ZERO, "belum_riil": 0} for _ in periods] for s in shops}
    ordered = Pesanan.tanggal_kirim
    rows = (await session.execute(
        select(Pesanan.akun_id, ordered.label("tanggal"), Pesanan.status, Pesanan.total,
               SettlementPesanan.jumlah_cair)
        .outerjoin(SettlementPesanan, and_(
            SettlementPesanan.akun_id == Pesanan.akun_id,
            SettlementPesanan.order_sn == Pesanan.id_eksternal))
        .where(
            Pesanan.platform == "shopee", Pesanan.status.in_(["shipped", "completed"]),
            ordered >= datetime.combine(monday, time.min, WIB),
            ordered < datetime.combine(monday + timedelta(days=count*7), time.min, WIB))
    )).all()
    for row in rows:
        if row.akun_id not in cells:
            continue
        # SQLite strips timezone; PostgreSQL retains it.
        dt = row.tanggal
        day = (dt.replace(tzinfo=WIB) if dt.tzinfo is None else dt.astimezone(WIB)).date()
        if day.weekday() == 6:
            continue
        index = (day - monday).days // 7
        if not 0 <= index < count:
            continue
        cell = cells[row.akun_id][index]
        if row.status == "shipped":
            cell["estimasi"] += money(row.total)
        elif row.jumlah_cair is None:
            cell["belum_riil"] += 1
        else:
            cell["riil"] += money(row.jumlah_cair)

    def output(cell):
        return {
            "estimasi": str(cell["estimasi"]), "riil": str(cell["riil"]),
            "belum_riil": cell["belum_riil"],
            "jumlah": None if cell["belum_riil"] else str(cell["estimasi"] + cell["riil"]),
        }

    return {
        "periode": periode, "minggu": periods,
        "toko": [{"id": s.id, "nama": s.nama_toko, "minggu": [output(c) for c in cells[s.id]]} for s in shops],
        "catatan": "Berdasarkan tanggal dikirim (WIB) dan status terkini; Minggu dan pesanan tanpa tanggal kirim tidak dihitung. Pengiriman memakai total pesanan bruto sebagai estimasi, sebelum potongan. Pesanan selesai memakai jumlah pencairan riil tersimpan. Ini pembanding ERP, bukan tambahan pemasukan buku utama.",
    }
