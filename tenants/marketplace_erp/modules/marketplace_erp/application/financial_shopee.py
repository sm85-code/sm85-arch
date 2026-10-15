"""Read-only Shopee comparison by shipping date, Monday–Saturday (WIB)."""

from datetime import date, datetime, time, timedelta
from unicodedata import normalize

from fastapi import HTTPException
from sqlalchemy import or_, select

from .financial_reports import WIB, ZERO, money
from ..infrastructure.models import AkunMarketplace, Pesanan, SettlementPesanan


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


async def selected_shops(session):
    shops = (await session.execute(
        select(AkunMarketplace.id, AkunMarketplace.nama_toko)
        .where(AkunMarketplace.platform == "shopee")
        .order_by(AkunMarketplace.nama_toko, AkunMarketplace.id)
    )).all()
    shops = [s for s in shops if shop_key(s.nama_toko) in SHOP_ORDER]
    shops.sort(key=lambda s: (SHOP_ORDER[shop_key(s.nama_toko)], s.id))
    return shops


def unfinished_query(shop_ids):
    return select(
        Pesanan.id, Pesanan.id_eksternal, Pesanan.akun_id,
        Pesanan.tanggal_kirim.label("tanggal"), Pesanan.total, Pesanan.status, Pesanan.status_marketplace,
    ).where(
        Pesanan.akun_id.in_(shop_ids),
        Pesanan.platform == "shopee", Pesanan.status == "shipped",
        or_(Pesanan.status_marketplace.is_(None),
            Pesanan.status_marketplace.in_(["SHIPPED", "TO_CONFIRM_RECEIVE"])),
    )


def shipping_day(stamp):
    return (stamp.replace(tzinfo=WIB) if stamp.tzinfo is None else stamp.astimezone(WIB)).date()


async def weekly_income(session, periode: str, jumlah_kolom: int | None = None, *, released: bool = False, sampai: date | None = None):
    try:
        first = date.fromisoformat(f"{periode}-01")
    except ValueError:
        raise HTTPException(422, "Bulan laporan tidak valid")
    if sampai is not None and sampai.replace(day=1) != first:
        raise HTTPException(422, "Batas data harus berada dalam bulan laporan")
    following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    monday = first - timedelta(days=first.weekday())
    count = jumlah_kolom or ((following - timedelta(days=1) - monday).days // 7 + 1)
    periods = [
        {"mulai": (monday + timedelta(days=7*i)).isoformat(),
         "akhir": (monday + timedelta(days=7*i+5)).isoformat()}
        for i in range(count)
    ]
    shops = await selected_shops(session)
    cells = {s.id: [ZERO for _ in periods] for s in shops}
    if released:
        ordered = SettlementPesanan.dirilis_at
        query = select(
            SettlementPesanan.akun_id, ordered.label("tanggal"), SettlementPesanan.jumlah_cair.label("total")
        ).where(SettlementPesanan.akun_id.in_([s.id for s in shops]))
    else:
        ordered = Pesanan.tanggal_kirim
        query = unfinished_query([s.id for s in shops])
    stop = monday + timedelta(days=count*7)
    if sampai is not None:
        stop = min(stop, sampai + timedelta(days=1))
    rows = (await session.execute(query.where(
        ordered >= datetime.combine(monday, time.min, WIB),
        ordered < datetime.combine(stop, time.min, WIB),
    ))).all()
    for row in rows:
        if row.akun_id not in cells:
            continue
        # SQLite strips timezone; PostgreSQL retains it.
        dt = row.tanggal
        day = shipping_day(dt)
        if day.weekday() == 6:
            continue
        index = (day - monday).days // 7
        if not 0 <= index < count:
            continue
        cells[row.akun_id][index] += money(row.total)

    def output(estimate):
        # Keep zero-valued legacy fields for previously deployed clients.
        return {"estimasi": "0" if released else str(estimate), "riil": str(estimate) if released else "0", "belum_riil": 0, "jumlah": str(estimate)}

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
        "periode": periode, "minggu": periods, "sampai": sampai.isoformat() if sampai else None,
        "toko": shop_rows,
        "catatan": ("Dana pesanan yang sudah cair, dikelompokkan berdasarkan tanggal cair (dirilis_at) WIB, Senin–Sabtu. Minggu dan pencairan tanpa tanggal cair tidak dihitung. Nilai riil memakai jumlah_cair yang tersimpan di ERP, tanpa melihat tanggal kirim atau status pesanan saat ini. Angka ini jumlah dana cair per periode, bukan saldo wallet tersisa setelah penarikan. Ini pembanding ERP, tidak menambah pemasukan buku utama." if released else "Berdasarkan tanggal dikirim (WIB) dan status terkini; Minggu dan pesanan tanpa tanggal kirim tidak dihitung. Hanya dalam pengiriman atau diterima tetapi belum selesai (SHIPPED / TO_CONFIRM_RECEIVE). Semua angka memakai total pesanan bruto sebagai estimasi, sebelum potongan; pesanan selesai tidak dihitung. Ini pembanding ERP, bukan tambahan pemasukan buku utama."),
    }
