"""Read-only Shopee comparison by shipping date, Monday–Saturday (WIB)."""

from datetime import date, datetime, time, timedelta
from unicodedata import normalize

from fastapi import HTTPException
from sqlalchemy import or_, select

from .financial_reports import WIB, ZERO, money
from ..infrastructure.models import AkunMarketplace, Pesanan


REPORT_SHOPS = (
    "BUMI TANI IND",
    "BUMI LESTARI INDONESIA",
    "RESTU BUMI IND",
    "MAJAPAHIT STORE IND",
    "CAHAYA LANGIT IND",
    "AZFA Furniture Official",
)
def shop_key(name):
    return " ".join(normalize("NFKC", name).casefold().split())


SHOP_ORDER = {shop_key(name): index for index, name in enumerate(REPORT_SHOPS)}


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
    shops = [s for s in shops if shop_key(s.nama_toko) in SHOP_ORDER]
    shops.sort(key=lambda s: (SHOP_ORDER[shop_key(s.nama_toko)], s.id))
    cells = {s.id: [ZERO for _ in periods] for s in shops}
    ordered = Pesanan.tanggal_kirim
    rows = (await session.execute(
        select(Pesanan.akun_id, ordered.label("tanggal"), Pesanan.total)
        .where(
            Pesanan.akun_id.in_([s.id for s in shops]),
            Pesanan.platform == "shopee", Pesanan.status == "shipped",
            or_(Pesanan.status_marketplace.is_(None),
                Pesanan.status_marketplace.in_(["SHIPPED", "TO_CONFIRM_RECEIVE"])),
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
        cells[row.akun_id][index] += money(row.total)

    def output(estimate):
        # Keep zero-valued legacy fields for previously deployed clients.
        return {"estimasi": str(estimate), "riil": "0", "belum_riil": 0, "jumlah": str(estimate)}

    shop_rows = []
    for name in REPORT_SHOPS:
        matched = [s for s in shops if shop_key(s.nama_toko) == shop_key(name)]
        values = [sum((cells[s.id][i] for s in matched), ZERO) for i in range(count)]
        shop_rows.append({
            "id": shop_key(name), "nama": name, "akun_ditemukan": bool(matched),
            "minggu": [output(v) if matched else {
                "estimasi": None, "riil": "0", "belum_riil": 0, "jumlah": None
            } for v in values],
        })
    return {
        "periode": periode, "minggu": periods,
        "toko": shop_rows,
        "catatan": "Berdasarkan tanggal dikirim (WIB) dan status terkini; Minggu dan pesanan tanpa tanggal kirim tidak dihitung. Hanya dalam pengiriman atau diterima tetapi belum selesai (SHIPPED / TO_CONFIRM_RECEIVE). Semua angka memakai total pesanan bruto sebagai estimasi, sebelum potongan; pesanan selesai tidak dihitung. Ini pembanding ERP, bukan tambahan pemasukan buku utama.",
    }
