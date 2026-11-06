"""Toko Internal & Reseller: shops that sell through the business without a marketplace connection.

Kept apart from the connected marketplace accounts (mpe_akun_marketplace), which carry tokens and sync state. A shop here is
only a name, a kind (own shop or reseller), a platform and a two-letter code used in production order codes. Referenced shops are only switched off; unreferenced shops can be deleted after
confirmation and a full tenant reference check. EasyBoss identity lives on the existing internal-shop rows.
"""
from __future__ import annotations

import re

from sqlalchemy import String, Text, cast, or_

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.models import TokoPenjualan

JENIS = ("internal", "reseller")
# Not the marketplace platforms of mpe_akun_marketplace: those are the ones that can be connected and synced.
PLATFORM = (
    ("shopee", "Shopee"),
    ("tiktokshop", "TikTok"),
    ("lazada", "Lazada"),
    ("blibli", "Blibli"),
    ("webapp", "Web App"),
)
PLATFORM_ID = {p for p, _ in PLATFORM}
MAKS_TOKO = 2000


def _bentuk(t: TokoPenjualan) -> dict:
    return {"id": t.id, "nama": t.nama, "jenis": t.jenis, "platform": t.platform, "kode": t.kode or "", "keterangan": t.keterangan, "aktif": t.aktif, "sumber": t.sumber, "easyboss_shop_id": t.easyboss_shop_id, "marketplace_shop_id": t.marketplace_shop_id, "sinkron_status": t.sinkron_status, "sinkron_at": t.sinkron_at}


def _periksa(nama: str, jenis: str, platform: str, kode: str | None, keterangan: str) -> tuple[str, str | None, str]:
    nama = re.sub(r"\s+", " ", (nama or "").strip())
    if not 1 <= len(nama) <= 120:
        raise HTTPException(422, "Nama toko 1-120 karakter")
    if jenis not in JENIS:
        raise HTTPException(422, "Jenis toko harus Internal atau Reseller")
    if platform not in PLATFORM_ID:
        raise HTTPException(422, "Platform tidak dikenal")
    kode = (kode or "").strip().upper() or None
    if kode is not None and not re.fullmatch(r"[A-Z]{2}", kode):
        raise HTTPException(422, "Kode harus 2 huruf")
    if jenis == "reseller" and kode is None:
        raise HTTPException(422, "Kode penjual wajib untuk reseller")
    keterangan = (keterangan or "").strip()
    if len(keterangan) > 500:
        raise HTTPException(422, "Keterangan maksimal 500 karakter")
    return nama, kode, keterangan


async def daftar(session: AsyncSession) -> dict:
    rows = (await session.scalars(select(TokoPenjualan).order_by(TokoPenjualan.aktif.desc(), TokoPenjualan.jenis, TokoPenjualan.nama_key))).all()
    return {"items": [_bentuk(r) for r in rows], "platform": [{"id": i, "label": n} for i, n in PLATFORM], "jenis": list(JENIS)}


async def _cek_kode(session: AsyncSession, kode: str | None, kecuali: str | None) -> None:
    if kode is None:
        return
    ada = await session.scalar(select(TokoPenjualan.id).where(TokoPenjualan.kode == kode))
    if ada is not None and ada != kecuali:
        raise HTTPException(409, f"Kode {kode} sudah dipakai toko lain")


async def buat(session: AsyncSession, nama: str, jenis: str, platform: str, kode: str | None, keterangan: str) -> dict:
    nama, kode, keterangan = _periksa(nama, jenis, platform, kode, keterangan)
    if ((await session.scalar(select(func.count()).select_from(TokoPenjualan))) or 0) >= MAKS_TOKO:
        raise HTTPException(409, "Terlalu banyak toko")
    await _cek_kode(session, kode, None)
    t = TokoPenjualan(nama=nama, nama_key=nama.casefold(), jenis=jenis, platform=platform, kode=kode, keterangan=keterangan, aktif=True)
    session.add(t)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Toko dengan nama dan platform ini sudah ada")
    return _bentuk(t)


async def ubah(session: AsyncSession, id_: str, nama: str, jenis: str, platform: str, kode: str | None, keterangan: str, aktif: bool) -> dict:
    t = await session.get(TokoPenjualan, id_)
    if t is None:
        raise HTTPException(404, "Toko tidak ditemukan")
    if t.easyboss_shop_id and (jenis != t.jenis or platform != t.platform or nama != t.nama):
        raise HTTPException(422, "Identitas toko EasyBoss dikelola oleh sinkronisasi")
    nama, kode, keterangan = _periksa(nama, jenis, platform, kode, keterangan)
    await _cek_kode(session, kode, t.id)
    t.nama, t.nama_key, t.jenis, t.platform, t.kode, t.keterangan, t.aktif = nama, nama.casefold(), jenis, platform, kode, keterangan, aktif
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Toko dengan nama dan platform ini sudah ada")
    return _bentuk(t)


async def relasi(session: AsyncSession, id_: str) -> dict:
    """Conservative check of ALL tenant tables, including non-FK JSON/history references.

    Locks share the bridge lock; manual creation also locks the shop row. Matching an ID inside a
    text snapshot deliberately counts as a relation rather than risk deleting business history.
    """
    from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase
    result = {}
    for table in MarketplaceErpBase.metadata.sorted_tables:
        if table.name == TokoPenjualan.__tablename__:
            continue
        conditions = [cast(c, Text).contains(id_, autoescape=True) for c in table.c if isinstance(c.type, (String, Text))]
        if conditions:
            count = await session.scalar(select(func.count()).select_from(table).where(or_(*conditions)))
            if count:
                result[table.name] = count
    return result


async def hapus(session: AsyncSession, id_: str, *, confirm: bool = False) -> dict:
    from tenants.marketplace_erp.modules.marketplace_erp.application.order_bridge import exclusive
    async with exclusive(session):
        toko = await session.scalar(select(TokoPenjualan).where(TokoPenjualan.id == id_).with_for_update())
        if toko is None:
            raise HTTPException(404, "Toko tidak ditemukan")
        refs = await relasi(session, id_)
        if not confirm:
            return {"can_delete": not bool(refs), "relations": refs}
        if refs:
            raise HTTPException(409, "Toko memiliki relasi; gunakan Nonaktifkan untuk menjaga histori")
        await session.delete(toko)
        await session.commit()
        return {"deleted": True}
