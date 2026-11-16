"""Bounded, read-only ERP snapshot. No sync, operational service or accounting writes.

Money = stored item sales (Shopee discounted item subtotal / EasyBoss line_total),
never buyer payment, shipping or escrow. Missing/refund data propagates as null.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, text

from ..infrastructure.models import AkunMarketplace, BridgeOrder, ItemPesanan, Pesanan, TokoPenjualan
from .bridge_status import native_state

JAKARTA = ZoneInfo("Asia/Jakarta")
MAX_ORDERS = 10000
PLATFORMS = ("shopee", "tiktokshop", "lazada")
STATUSES = ("semua", "perlu_diproses", "dikirim", "selesai", "dibatalkan", "retur")
LIMITATIONS = [
    "Hanya pesanan yang sudah tersimpan di ERP; bukan seluruh histori marketplace. Tidak ada sinkronisasi saat Summary dibuka.",
    "Periode memakai tanggal pesanan Asia/Jakarta. Status adalah snapshot terakhir yang tersimpan, bukan status pada tanggal akhir periode.",
    "Omzet kotor = subtotal barang setelah diskon item, sebelum pembatalan/retur; tidak termasuk ongkir, biaya marketplace atau escrow.",
    "Omzet bersih, jumlah/nilai retur, dan estimasi menunggu pencairan belum tersedia: ERP belum memiliki ledger refund dan snapshot pencairan lengkap yang dapat direkonsiliasi per pesanan. Tidak dianggap nol.",
    "Unit terjual = kuantitas pesanan selain belum bayar, batal dan retur yang diketahui. Retur sebagian belum dapat dikoreksi; bukan angka penjualan final.",
]


def period(preset="bulan_ini", start=None, end=None, now=None):
    today = (now or datetime.now(timezone.utc)).astimezone(JAKARTA).date()
    if preset == "hari_ini":
        start = end = today
    elif preset == "kemarin":
        start = end = today - timedelta(days=1)
    elif preset in ("7", "30"):
        start, end = today - timedelta(days=int(preset) - 1), today
    elif preset == "bulan_ini":
        start, end = today.replace(day=1), today
    elif preset == "bulan_lalu":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif preset != "khusus" or not start or not end:
        raise HTTPException(422, "Pilih periode atau rentang tanggal yang valid")
    if end < start or (end - start).days > 365:
        raise HTTPException(422, "Rentang tanggal harus berurutan dan maksimal 366 hari")
    return start, end


def obj(value):
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def money(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def phase(platform, raw, local, snapshot):
    raw = raw or ""
    if any(x in raw.upper() for x in ("RETURN", "REFUND")):
        return "retur"
    state = native_state(platform, raw)
    if state:
        return state[1]
    if platform != "shopee":
        # Unknown native status must never fall back to stale local to_ship.
        return {"unpaid": "belum_bayar", "cancelled": "dibatalkan", "shipped": "dikirim",
                "completed": "selesai", "ready": "perlu_diproses"}.get(snapshot.get("status"), "tidak_dikenal")
    if local == "to_ship":
        return "menunggu_kurir" if raw == "PROCESSED" else "perlu_diproses"
    return {"unpaid": "belum_bayar", "shipped": "dikirim", "completed": "selesai",
            "cancelled": "dibatalkan"}.get(local, "tidak_dikenal")


def record(row):
    r = dict(row)
    bridge = r["bridge_platform"] in ("tiktokshop", "lazada")
    source = obj(r["snapshot_json"]) if bridge else obj(r["detail_json"])
    p = r["bridge_platform"] if bridge else r["platform"]
    shop = "internal:" + r["toko_id"] if bridge and r["toko_id"] else r["akun_id"]
    native_shop = (r.get("bridge_marketplace_shop") or r["shop_id"]) if bridge else (r["external_shop"] or r["akun_id"])
    sn = r["order_id"] if bridge else r["id_eksternal"]
    raw = r["source_status"] if bridge else r["status_marketplace"]
    status = phase(p, raw, r["status"], source)
    when = r["ordered"]
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    amount = money(r["sales"]) if r["line_count"] else None
    qty = int(r["units"]) if r["line_count"] else None
    if bridge:
        lines = source.get("items")
        if source.get("complete") is True and isinstance(lines, list) and lines:
            # Unique stable item ids: split packages cannot inflate quantity or sales.
            ids = [i.get("item_id") for i in lines]
            if all(ids) and len(set(ids)) == len(ids):
                qty = sum(i["quantity"] for i in lines) if all(isinstance(i.get("quantity"), int) and i["quantity"] > 0 for i in lines) else None
                values = [money(i.get("line_total")) for i in lines]
                amount = sum(values, Decimal(0)) if all(v is not None for v in values) else None
            else:
                qty = amount = None
        else:
            qty = amount = None
    if source.get("currency") != "IDR":
        amount = None
    return {"identity": (p, native_shop or shop or "unknown", sn), "platform": p, "shop": shop or "unknown",
            "name": r["shop_name"] or "Toko belum terpetakan", "number": sn,
            "status": status, "date": when.astimezone(JAKARTA).date(), "units": qty, "gross": amount,
            "fallback_date": r["dipesan_at"] is None, "updated": r["source_updated"] or r["updated_at"]}


def metrics(rows):
    def total(values):
        return None if any(v is None for v in values) else sum(values, Decimal(0))
    n = len(rows)
    active = [r for r in rows if r["status"] not in ("dibatalkan", "belum_bayar", "retur", "tidak_dikenal")]
    gross = total([r["gross"] for r in rows])
    return {"orders": n, "units": None if any(r["status"] == "tidak_dikenal" for r in rows) else total([r["units"] for r in active]),
            "completed": sum(r["status"] == "selesai" for r in rows),
            "processing": sum(r["status"] == "perlu_diproses" for r in rows),
            "shipping": sum(r["status"] == "dikirim" for r in rows),
            "cancelled": sum(r["status"] == "dibatalkan" for r in rows), "returns": None,
            "waiting_courier": sum(r["status"] == "menunggu_kurir" for r in rows),
            "unknown": sum(r["status"] == "tidak_dikenal" for r in rows),
            "average_units": (total([r["units"] for r in active]) / len(active)) if active and not any(r["status"] == "tidak_dikenal" for r in rows) and all(r["units"] is not None for r in active) else None,
            "gross": gross, "net": None, "pending": None,
            "average_value": gross / n if n and gross is not None else None,
            "completed_sales": total([r["gross"] for r in rows if r["status"] == "selesai"]),
            "shipping_sales": total([r["gross"] for r in rows if r["status"] == "dikirim"]),
            "cancellation_value": total([r["gross"] for r in rows if r["status"] == "dibatalkan"]),
            "return_value": None, "missing_sales": sum(r["gross"] is None for r in rows),
            "known_sales": sum((r["gross"] for r in rows if r["gross"] is not None), Decimal(0))}


def project(rows, start, end, platform="semua", shop="", status="semua", page=1, page_size=25):
    unique = {}
    duplicates = 0
    for row in rows:
        r = record(row)
        if r["identity"] in unique:
            duplicates += 1
        # Deterministic latest source snapshot, never sum duplicate identities.
        if r["identity"] not in unique or str(r["updated"]) > str(unique[r["identity"]]["updated"]):
            unique[r["identity"]] = r
    selected = [r for r in unique.values() if start <= r["date"] <= end
                and (platform == "semua" or r["platform"] == platform)
                and (not shop or r["shop"] == shop) and (status == "semua" or r["status"] == status)]
    summary = metrics(selected)
    groups = []
    for p in PLATFORMS:
        values = [r for r in selected if r["platform"] == p]
        if platform != "semua" and platform != p:
            continue
        shops = []
        for key in sorted({r["shop"] for r in values}):
            members = [r for r in values if r["shop"] == key]
            shops.append({"id": key, "name": members[0]["name"], **metrics(members)})
        groups.append({"platform": p, **metrics(values), "shops": shops})
    for g in groups:
        for entry in [g, *g["shops"]]:
            entry["contribution"] = entry["gross"] / summary["gross"] * 100 if summary["gross"] and entry["gross"] is not None else None
    trend = [{"date": (start + timedelta(days=i)).isoformat(), **metrics([r for r in selected if r["date"] == start + timedelta(days=i)])}
             for i in range((end - start).days + 1)]
    selected.sort(key=lambda r: (r["date"], r["number"]), reverse=True)
    return {"totals": summary, "marketplaces": groups, "trend": trend,
            "orders": [{k: r[k] for k in ("platform", "name", "number", "date", "status", "units", "gross")} for r in selected[(page - 1) * page_size:page * page_size]],
            "page": page, "page_size": page_size, "total_orders": len(selected),
            "quality": {"deduplicated": duplicates, "fallback_dates": sum(r["fallback_date"] for r in selected),
                        "unknown_status": summary["unknown"], "missing_sales": summary["missing_sales"]},
            "limitations": LIMITATIONS, "start": start, "end": end, "timezone": "Asia/Jakarta",
            "as_of": datetime.now(timezone.utc), "last_source_update": max((str(r["updated"]) for r in selected), default=None)}


async def read(session, start: date, end: date, allowed=None, configured_readonly=False, **filters):
    """Maximum 10,000 rows / 366 days. Existing date/foreign-key indexes; no remote IO."""
    if not configured_readonly and session.bind.dialect.name == "postgresql":
        # This must be the FIRST command on the dedicated Summary session.
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
    ordered = func.coalesce(Pesanan.dipesan_at, Pesanan.created_at)
    start_at = datetime.combine(start, time.min, JAKARTA).astimezone(timezone.utc)
    end_at = datetime.combine(end + timedelta(days=1), time.min, JAKARTA).astimezone(timezone.utc)
    units = select(func.sum(ItemPesanan.qty)).where(ItemPesanan.pesanan_id == Pesanan.id).scalar_subquery()
    sales = select(func.sum(ItemPesanan.subtotal)).where(ItemPesanan.pesanan_id == Pesanan.id).scalar_subquery()
    count = select(func.count(ItemPesanan.id)).where(ItemPesanan.pesanan_id == Pesanan.id).scalar_subquery()
    query = select(Pesanan.id, Pesanan.platform, Pesanan.id_eksternal, Pesanan.akun_id, Pesanan.status,
                   Pesanan.status_marketplace, Pesanan.detail_json, Pesanan.dipesan_at, Pesanan.updated_at,
                   ordered.label("ordered"), units.label("units"), sales.label("sales"), count.label("line_count"),
                   BridgeOrder.platform.label("bridge_platform"), BridgeOrder.toko_id, BridgeOrder.shop_id,
                   BridgeOrder.order_id, BridgeOrder.source_status, BridgeOrder.snapshot_json,
                   TokoPenjualan.marketplace_shop_id.label("bridge_marketplace_shop"),
                   BridgeOrder.updated_at.label("source_updated"), AkunMarketplace.id_toko_eksternal.label("external_shop"),
                   func.coalesce(TokoPenjualan.nama, AkunMarketplace.nama_toko).label("shop_name"))\
        .outerjoin(BridgeOrder, BridgeOrder.pesanan_id == Pesanan.id)\
        .outerjoin(AkunMarketplace, AkunMarketplace.id == Pesanan.akun_id)\
        .outerjoin(TokoPenjualan, TokoPenjualan.id == BridgeOrder.toko_id)\
        .where(or_(Pesanan.platform.in_(PLATFORMS), BridgeOrder.platform.in_(("lazada", "tiktokshop"))),
               or_(and_(Pesanan.dipesan_at >= start_at, Pesanan.dipesan_at < end_at),
                   and_(Pesanan.dipesan_at.is_(None), Pesanan.created_at >= start_at, Pesanan.created_at < end_at)))
    if allowed is not None:
        native = [x for x in allowed if not x.startswith("internal:")]
        internal = [x[9:] for x in allowed if x.startswith("internal:")]
        query = query.where(or_(Pesanan.akun_id.in_(native), BridgeOrder.toko_id.in_(internal)))
    rows = (await session.execute(query.order_by(Pesanan.id).limit(MAX_ORDERS + 1))).mappings().all()
    if len(rows) > MAX_ORDERS:
        raise HTTPException(422, "Lebih dari 10.000 pesanan. Persempit rentang tanggal; hasil parsial tidak ditampilkan.")
    result = project(rows, start, end, **filters)
    # Only public shop columns, never credential-bearing ORM records.
    native = (await session.execute(select(AkunMarketplace.id, AkunMarketplace.platform, AkunMarketplace.nama_toko)
                                   .where(AkunMarketplace.platform.in_(PLATFORMS)).limit(501))).all()
    internal = (await session.execute(select(TokoPenjualan.id, TokoPenjualan.platform, TokoPenjualan.nama)
                                     .where(TokoPenjualan.sumber == "easyboss", TokoPenjualan.platform.in_(PLATFORMS)).limit(501))).all()
    if len(native) > 500 or len(internal) > 500:
        raise HTTPException(422, "Daftar toko melebihi batas Summary")
    result["shops"] = [{"id": key, "platform": p, "name": name} for key, p, name in
                       [*native, *(("internal:" + key, p, name) for key, p, name in internal)]
                       if allowed is None or key in allowed]
    return result
