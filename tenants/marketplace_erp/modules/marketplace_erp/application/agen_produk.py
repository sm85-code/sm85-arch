"""Daily product snapshot and the 7-vs-28-day trend.

The pull job is off unless AGEN_PRODUK_SNAPSHOT_MENIT is greater than 0.
Sales velocity comes from ERP orders, not from a Shopee write.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import ItemPesanan, KatalogShopee, Pesanan
from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models_agen import AgenProdukSnapshot

logger = logging.getLogger(__name__)
WIB = timezone(timedelta(hours=7))
MAKS_ITEM = 200


def interval_seconds() -> float:
    raw = os.getenv("AGEN_PRODUK_SNAPSHOT_MENIT", "0").strip()
    try:
        menit = float(raw)
    except ValueError:
        return 0.0
    return max(menit, 0.0) * 60


def _hari(saat: datetime | None = None):
    return (saat or datetime.now(WIB)).astimezone(WIB).date()


def _diagnosis(payload, item_id: str) -> dict:
    for row in (payload or {}).get("success_item_list") or []:
        if str(row.get("item_id")) == item_id:
            tugas = row.get("unfinished_task") or []
            return {"tingkat": row.get("quality_level"), "masalah": len(tugas) if isinstance(tugas, list) else None}
    return {}


async def _simpan(session, akun_id: str, item_id: str, tanggal, angka: dict, diagnosis: dict) -> None:
    row = (await session.execute(
        select(AgenProdukSnapshot).where(
            AgenProdukSnapshot.akun_id == akun_id,
            AgenProdukSnapshot.item_id == item_id,
            AgenProdukSnapshot.tanggal == tanggal,
        )
    )).scalar_one_or_none()
    if row is None:
        row = AgenProdukSnapshot(akun_id=akun_id, item_id=item_id, tanggal=tanggal)
        session.add(row)
    from decimal import Decimal

    row.views = angka.get("views")
    row.likes = angka.get("likes")
    row.sold = angka.get("sale")
    row.rating = None if angka.get("rating_star") is None else Decimal(str(angka["rating_star"]))
    row.komentar = angka.get("comment_count")
    row.diagnosis_json = json.dumps(diagnosis, ensure_ascii=False)
    row.diambil_at = datetime.now(timezone.utc)


async def tarik_toko(session: AsyncSession, akun, tanggal=None) -> dict:
    """Read extra info and diagnosis for up to MAKS_ITEM catalogue items. No Shopee write."""
    from fastapi import HTTPException

    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.adapters import erp_shopee_insights as insights

    hari = tanggal or _hari()
    ids = [
        r for r in (await session.execute(
            select(KatalogShopee.item_id).where(KatalogShopee.akun_id == akun.id).order_by(KatalogShopee.item_id).limit(MAKS_ITEM + 1)
        )).scalars()
    ]
    dipotong = len(ids) > MAKS_ITEM
    ids = ids[:MAKS_ITEM]
    if not ids:
        return {"akun_id": akun.id, "baru": 0, "catatan": []}
    stats, gagal = await insights.product_stats_many(session, akun, ids)
    try:
        hasil = await insights.diagnosis(session, akun, [int(i) for i in stats if str(i).isdigit()])
    except HTTPException as exc:
        hasil = {}
        gagal = list(gagal) + [str(exc.detail)[:120]]
    for item_id, angka in stats.items():
        await _simpan(session, akun.id, str(item_id), hari, angka if isinstance(angka, dict) else {}, _diagnosis(hasil, str(item_id)))
    await session.commit()
    catatan = ["sebagian statistik gagal"] if gagal else []
    if dipotong:
        catatan.append(f"dibatasi {MAKS_ITEM} produk")
    return {"akun_id": akun.id, "baru": len(stats), "catatan": catatan, "diagnosis": bool(hasil)}


async def tarik_semua(session: AsyncSession) -> list[dict]:
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import AkunMarketplace

    toko = (await session.execute(
        select(AkunMarketplace).where(AkunMarketplace.platform == "shopee", AkunMarketplace.status != "nonaktif")
    )).scalars().all()
    return [await tarik_toko(session, akun) for akun in toko]


async def _terjual(session, akun_id: str, item_id: str, hari, panjang: int) -> int:
    awal = datetime.combine(hari - timedelta(days=panjang - 1), datetime.min.time(), tzinfo=WIB)
    saat = func.coalesce(Pesanan.dipesan_at, Pesanan.created_at)
    jumlah = (await session.execute(
        select(func.coalesce(func.sum(ItemPesanan.qty), 0))
        .join(Pesanan, Pesanan.id == ItemPesanan.pesanan_id)
        .where(
            Pesanan.akun_id == akun_id,
            Pesanan.status != "cancelled",
            ItemPesanan.item_id_eksternal == item_id,
            saat >= awal,
        )
    )).scalar_one()
    return int(jumlah)


async def _patokan(session, akun_id: str, item_id: str, sampai):
    return (await session.execute(
        select(AgenProdukSnapshot)
        .where(
            AgenProdukSnapshot.akun_id == akun_id,
            AgenProdukSnapshot.item_id == item_id,
            AgenProdukSnapshot.tanggal <= sampai,
        )
        .order_by(AgenProdukSnapshot.tanggal.desc())
        .limit(1)
    )).scalar_one_or_none()


def _delta(baru, lama):
    if baru is None or lama is None or baru.tanggal == lama.tanggal or baru.views is None or lama.views is None:
        return None
    return baru.views - lama.views


async def tren(session: AsyncSession, akun_id: str, item_id: str, saat: datetime | None = None) -> dict:
    hari = _hari(saat)
    tujuh = await _terjual(session, akun_id, item_id, hari, 7)
    dua_delapan = await _terjual(session, akun_id, item_id, hari, 28)
    sekarang = await _patokan(session, akun_id, item_id, hari)
    lalu_7 = await _patokan(session, akun_id, item_id, hari - timedelta(days=7))
    lalu_28 = await _patokan(session, akun_id, item_id, hari - timedelta(days=28))
    diagnosis = json.loads(sekarang.diagnosis_json or "{}") if sekarang else {}
    return {
        "akun_id": akun_id,
        "item_id": item_id,
        "tanggal": hari.isoformat(),
        "terjual_7": tujuh,
        "terjual_28": dua_delapan,
        "kecepatan_7": round(tujuh / 7, 2),
        "kecepatan_28": round(dua_delapan / 28, 2),
        "views": None if sekarang is None else sekarang.views,
        "views_delta_7": _delta(sekarang, lalu_7),
        "views_delta_28": _delta(sekarang, lalu_28),
        "likes": None if sekarang is None else sekarang.likes,
        "sold_kumulatif": None if sekarang is None else sekarang.sold,
        "rating": None if sekarang is None or sekarang.rating is None else float(sekarang.rating),
        "diagnosis": diagnosis,
    }


async def jalankan_berkala(session_factory, interval: float) -> None:
    while True:
        try:
            async with session_factory() as session:
                await tarik_semua(session)
        except Exception:
            logger.exception("Snapshot produk gagal; diulang pada putaran berikut")
        await asyncio.sleep(interval)
