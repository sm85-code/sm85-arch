"""Weekly receivables from the same Shopee income feed as Menunggu cair.

Read-only: never imports orders, books journals or changes marketplace orders.
Current pending plus actual releases after the first cutoff reconstruct prior balances.
"""
import asyncio
import re
from datetime import date, datetime, time, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from .financial_reports import WIB, ZERO, money
from .financial_shopee import REPORT_SHOPS, shipping_day, shop_key, weekly_periods
from ..infrastructure.models import AkunMarketplace, Pesanan
from ..infrastructure.adapters import erp_shopee, erp_shopee_commerce as commerce


def epoch(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, WIB)
    except (ValueError, OverflowError, OSError):
        return None


async def pending_pages(session, shop, today):
    """Read all explicit cursors; retain known amounts when pagination is unavailable."""
    rows, seen, cursor = {}, set(), ""
    for _ in range(30):
        page = await commerce.income(session, shop, today - timedelta(days=13), today, 2, cursor, 100)
        for row in page["items"]:
            sn = row.get("order_sn")
            if not isinstance(sn, str) or not re.fullmatch(r"[A-Za-z0-9]{1,50}", sn):
                raise HTTPException(424, "Shopee tidak memberikan kode pesanan menunggu cair yang valid")
            rows[sn] = row
        if not page.get("pagination_known"):
            return rows, not rows  # Explicit empty first page is a successful empty pending list.
        following = page.get("next_cursor")
        if not following:
            return rows, True
        if following in seen:
            raise HTTPException(424, "Halaman menunggu cair Shopee berulang")
        seen.add(following)
        cursor = following
    raise HTTPException(424, "Daftar menunggu cair Shopee melebihi batas halaman")


async def weekly_pending_receivables(session, akun_id, periode, jumlah_kolom=None, *, sampai=None, as_of=None):
    monday, periods = weekly_periods(periode, jumlah_kolom, sampai)
    shop = await session.get(AkunMarketplace, akun_id)
    if shop is None or shop.platform != "shopee" or shop_key(shop.nama_toko) not in {shop_key(n) for n in REPORT_SHOPS}:
        raise HTTPException(404, "Toko tidak termasuk enam toko laporan")
    now = as_of or datetime.now(WIB)
    now = now.replace(tzinfo=WIB) if now.tzinfo is None else now.astimezone(WIB)
    start = datetime.combine(monday, time.min, WIB)
    end = min(now, datetime.combine(sampai + timedelta(days=1), time.min, WIB)) if sampai else now
    # Token refresh uses this request's session; network calls stay sequential.
    try:
        async with asyncio.timeout(48):
            pending, complete = await pending_pages(session, shop, now.date())
            releases = await erp_shopee.daftar_escrow(session, shop, int(start.timestamp()), int(now.timestamp())) if start < now else []
            local = (await session.execute(select(Pesanan.id_eksternal, Pesanan.tanggal_kirim).where(
                Pesanan.akun_id == akun_id, Pesanan.platform == "shopee",
            ))).all()
            shipping = {r.id_eksternal: r.tanggal_kirim for r in local if r.tanggal_kirim}
            # Pending income can exist before that order has ever been imported into the ERP.
            missing = [sn for sn in pending.keys() | {r["order_sn"] for r in releases} if sn not in shipping]
            for offset in range(0, len(missing), 30):
                details = await commerce.income_orders(session, shop, missing[offset:offset+30])
                for detail in details["items"]:
                    stamp = epoch(detail.get("pickup_done_time"))
                    if not stamp:
                        times = []
                        for package in detail.get("packages", []) or [{}]:
                            trace = await commerce.tracking(session, shop, detail["order_sn"], package.get("package_number"))
                            times.extend(epoch(event.get("update_time")) for event in trace.get("tracking_info", [])
                                         if event.get("logistics_status") in ("PICKED_UP", "LOGISTICS_PICKUP_DONE")
                                         and epoch(event.get("update_time")))
                        stamp = min(times) if times else None
                    if stamp:
                        shipping[detail["order_sn"]] = stamp
    except TimeoutError as exc:
        raise HTTPException(504, "Pembacaan dana Shopee toko ini belum selesai. Coba lagi.") from exc

    paid = {r["order_sn"]: r for r in releases}
    cutoffs = [min(end, datetime.combine(date.fromisoformat(w["akhir"]) + timedelta(days=1), time.min, WIB)) for w in periods]
    cells = [dict(piutang=ZERO, piutang_lama=ZERO, cair_minggu_lalu=ZERO,
                  piutang_tidak_pasti=0, piutang_lama_tidak_pasti=0, cair_tidak_terpetakan=0) for _ in periods]
    unmapped = 0
    for sn in pending.keys() | paid.keys():
        stamp = shipping.get(sn)
        if stamp is None:
            unmapped += 1
            continue
        day = shipping_day(stamp)
        if day.weekday() == 6:
            continue
        cohort = (day - monday).days // 7
        release = paid.get(sn)
        released_at = release.get("dirilis") if release else None
        if released_at and released_at.tzinfo is None:
            released_at = released_at.replace(tzinfo=WIB)
        estimate = pending.get(sn, {}).get("estimated_escrow_amount")
        # Final payout is the best known net value for reconstructed historical unpaid balances.
        value = estimate if estimate is not None else release.get("payout") if release else None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=WIB)
        for i, cutoff in enumerate(cutoffs):
            if stamp >= cutoff or cohort > i:
                continue
            field = "piutang" if cohort == i else "piutang_lama"
            if release and released_at is None:
                cells[i][field + "_tidak_pasti"] += 1
            elif released_at is None or released_at >= cutoff:
                if value is None:
                    cells[i][field + "_tidak_pasti"] += 1
                else:
                    cells[i][field] += money(value)
        if released_at:
            day = shipping_day(released_at)
            i = (day - monday).days // 7
            if day.weekday() != 6 and 0 <= i < len(cells) and released_at < cutoffs[i] and cohort == i-1:
                if release.get("payout") is None:
                    cells[i]["cair_tidak_terpetakan"] += 1
                else:
                    cells[i]["cair_minggu_lalu"] += money(release["payout"])
    for i, cell in enumerate(cells):
        available = datetime.combine(date.fromisoformat(periods[i]["mulai"]), time.min, WIB) < end
        for field in ("piutang", "piutang_lama", "cair_minggu_lalu"):
            unknown = cell[{"piutang": "piutang_tidak_pasti", "piutang_lama": "piutang_lama_tidak_pasti",
                            "cair_minggu_lalu": "cair_tidak_terpetakan"}[field]]
            uncertain_zero = cell[field] == ZERO and (unknown or unmapped or (field != "cair_minggu_lalu" and not complete))
            cell[field] = str(cell[field]) if available and not uncertain_zero else None
    return dict(akun_id=akun_id, nama=shop.nama_toko, minggu=cells, halaman_lengkap=complete,
                tanpa_tanggal_kirim=unmapped, sumber="shopee_menunggu_cair", dibaca_at=now.isoformat())
