"""Inti pencatatan beban cicilan (provisi) gaji & langganan -- tanpa ketergantungan ke t3_services."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application.services import _batalkan
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_t3 import BlProvisi


async def terkumpul(
    session: AsyncSession, periode: str, *, jenis: str, karyawan_id: str | None = None, langganan_id: str | None = None
) -> Decimal:
    stmt = select(func.coalesce(func.sum(BlProvisi.jumlah), 0)).where(
        BlProvisi.periode == periode, BlProvisi.jenis == jenis, BlProvisi.dibatalkan.is_(False)
    )
    if karyawan_id:
        stmt = stmt.where(BlProvisi.karyawan_id == karyawan_id)
    if langganan_id:
        stmt = stmt.where(BlProvisi.langganan_id == langganan_id)
    return Decimal(str((await session.execute(stmt)).scalar_one() or 0))


async def sesuaikan(
    session: AsyncSession, *, tanggal: date, periode: str, jenis: str, target: Decimal, sumber_jenis: str,
    sumber_id: str, karyawan_id: str | None = None, langganan_id: str | None = None,
) -> None:
    """Saat dibayar: buat baris penyesuaian agar total beban bulan itu = jumlah yang dibayar (bisa + atau -)."""
    selisih = Decimal(target) - await terkumpul(
        session, periode, jenis=jenis, karyawan_id=karyawan_id, langganan_id=langganan_id
    )
    if selisih != 0:
        session.add(
            BlProvisi(
                tanggal=tanggal, periode=periode, minggu_ke=0, jenis=jenis, karyawan_id=karyawan_id,
                langganan_id=langganan_id, jumlah=selisih, sumber_jenis=sumber_jenis, sumber_id=sumber_id,
            )
        )
        await session.flush()


async def batalkan_provisi_sumber(session: AsyncSession, sumber_jenis: str, sumber_id: str, alasan: str) -> None:
    rows = (
        await session.execute(
            select(BlProvisi).where(
                BlProvisi.sumber_jenis == sumber_jenis, BlProvisi.sumber_id == sumber_id, BlProvisi.dibatalkan.is_(False)
            )
        )
    ).scalars()
    for row in rows:
        _batalkan(row, alasan)


async def beban_provisi(session: AsyncSession, awal: date, akhir: date) -> Decimal:
    stmt = select(func.coalesce(func.sum(BlProvisi.jumlah), 0)).where(
        BlProvisi.tanggal >= awal, BlProvisi.tanggal <= akhir, BlProvisi.dibatalkan.is_(False)
    )
    return Decimal(str((await session.execute(stmt)).scalar_one() or 0))
