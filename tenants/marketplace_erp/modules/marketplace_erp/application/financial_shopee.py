"""Read-only Shopee comparison by shipping date, Monday–Saturday (WIB)."""

from datetime import date, datetime, time, timedelta
from unicodedata import normalize

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

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


def weekly_periods(periode, jumlah_kolom=None, sampai=None):
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
    return monday, periods


async def weekly_income(session, periode: str, jumlah_kolom: int | None = None, *, released: bool = False, sampai: date | None = None):
    monday, periods = weekly_periods(periode, jumlah_kolom, sampai)
    count = len(periods)
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


async def weekly_shipping_receivables(session, periode, jumlah_kolom=None, *, sampai=None, as_of=None):
    """Read-only shipping cohorts plus their unpaid carry-over and next-week releases."""
    monday, periods = weekly_periods(periode, jumlah_kolom, sampai)
    now = as_of or datetime.now(WIB)
    now = now.replace(tzinfo=WIB) if now.tzinfo is None else now.astimezone(WIB)
    end = min(now, datetime.combine(sampai + timedelta(days=1), time.min, WIB)) if sampai else now
    selected = await selected_shops(session)
    shop_ids = [s.id for s in selected]
    first_stamp = datetime.combine(monday, time.min, WIB)
    stop = min(end, datetime.combine(monday + timedelta(days=7 * len(periods)), time.min, WIB))
    match = and_(Pesanan.akun_id == SettlementPesanan.akun_id,
                 Pesanan.id_eksternal == SettlementPesanan.order_sn, Pesanan.platform == "shopee")
    # Exclude orders known to have been paid before the first reporting week. Old unpaid
    # orders remain eligible, including shipments before the selected month.
    orders = (await session.execute(select(
        Pesanan.akun_id, Pesanan.tanggal_kirim, Pesanan.total, Pesanan.status, Pesanan.status_marketplace,
        SettlementPesanan.id.label("settlement_id"), SettlementPesanan.dirilis_at,
    ).outerjoin(SettlementPesanan, match).where(
        Pesanan.platform == "shopee", Pesanan.akun_id.in_(shop_ids), Pesanan.tanggal_kirim < stop,
        or_(Pesanan.status.in_(["shipped", "completed"]),
            Pesanan.status_marketplace.in_(["SHIPPED", "TO_CONFIRM_RECEIVE", "COMPLETED"])),
        or_(SettlementPesanan.dirilis_at.is_(None), SettlementPesanan.dirilis_at >= first_stamp),
    ))).all()
    settlements = (await session.execute(select(
        SettlementPesanan.akun_id, SettlementPesanan.dirilis_at, SettlementPesanan.jumlah_cair,
        Pesanan.tanggal_kirim,
    ).outerjoin(Pesanan, match).where(
        SettlementPesanan.akun_id.in_(shop_ids), SettlementPesanan.dirilis_at >= first_stamp,
        SettlementPesanan.dirilis_at < stop,
    ))).all()

    def aware(stamp):
        return stamp.replace(tzinfo=WIB) if stamp.tzinfo is None else stamp.astimezone(WIB)

    cutoffs = [min(end, datetime.combine(date.fromisoformat(w["akhir"]) + timedelta(days=1), time.min, WIB))
               for w in periods]
    cells = {s.id: [dict(piutang=ZERO, piutang_lama=ZERO, cair_minggu_lalu=ZERO,
                        piutang_tidak_pasti=0, piutang_lama_tidak_pasti=0, cair_tidak_terpetakan=0)
                   for _ in periods] for s in selected}
    for order in orders:
        day = shipping_day(order.tanggal_kirim)
        if day.weekday() == 6 or order.status == "cancelled" or order.status_marketplace in ("CANCELLED", "IN_CANCEL", "RETURNED"):
            continue
        cohort = (day - monday).days // 7
        unfinished = order.status == "shipped" and order.status_marketplace in (None, "SHIPPED", "TO_CONFIRM_RECEIVE")
        for index in range(max(0, cohort), len(periods)):
            if aware(order.tanggal_kirim) >= cutoffs[index]:
                continue
            cell = cells[order.akun_id][index]
            field = "piutang" if cohort == index else "piutang_lama"
            if order.settlement_id:
                if order.dirilis_at is None:
                    cell[field + "_tidak_pasti"] += 1
                elif aware(order.dirilis_at) >= cutoffs[index]:
                    cell[field] += money(order.total)
            elif unfinished:
                cell[field] += money(order.total)
            else:
                # Completed without release evidence is unknown, not zero or confirmed unpaid.
                cell[field + "_tidak_pasti"] += 1
        if order.settlement_id and order.dirilis_at is None:
            index = cohort + 1
            if 0 <= index < len(periods):
                cells[order.akun_id][index]["cair_tidak_terpetakan"] += 1
    for paid in settlements:
        day = shipping_day(paid.dirilis_at)
        index = (day - monday).days // 7
        if day.weekday() == 6 or not 0 <= index < len(periods) or aware(paid.dirilis_at) >= cutoffs[index]:
            continue
        if paid.tanggal_kirim is None:
            cells[paid.akun_id][index]["cair_tidak_terpetakan"] += 1
            continue
        shipped = shipping_day(paid.tanggal_kirim)
        if shipped.weekday() != 6 and (shipped - monday).days // 7 == index - 1:
            cells[paid.akun_id][index]["cair_minggu_lalu"] += money(paid.jumlah_cair)
    rows = []
    for name in REPORT_SHOPS:
        matched = [s.id for s in selected if shop_key(s.nama_toko) == shop_key(name)]
        weeks = []
        for i, period in enumerate(periods):
            values = [cells[id_][i] for id_ in matched]
            available = bool(matched) and datetime.combine(date.fromisoformat(period["mulai"]), time.min, WIB) < end
            counts = {key: sum(v[key] for v in values) for key in
                      ("piutang_tidak_pasti", "piutang_lama_tidak_pasti", "cair_tidak_terpetakan")}
            amounts = {key: str(sum((v[key] for v in values), ZERO)) if available and not counts[unknown] else None
                       for key, unknown in (("piutang", "piutang_tidak_pasti"),
                                            ("piutang_lama", "piutang_lama_tidak_pasti"),
                                            ("cair_minggu_lalu", "cair_tidak_terpetakan"))}
            weeks.append(dict(**amounts, **counts))
        rows.append(dict(id=shop_key(name), nama=name, akun_ditemukan=bool(matched), minggu=weeks))
    return dict(periode=periode, sampai=end.isoformat(), minggu=[dict(w, cutoff=cutoffs[i].isoformat(),
                hingga=(cutoffs[i] - timedelta(microseconds=1)).date().isoformat())
                for i, w in enumerate(periods)], toko=rows)
