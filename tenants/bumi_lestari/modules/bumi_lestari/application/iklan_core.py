"""Budget iklan 25/75 per bulan (spesifikasi 8.7 AB-KI-4). Murni baca; dipakai top up, layar Kas iklan, laporan iklan."""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import PROFIL_ID, BlAkunKas, BlProfil, BlTransaksi
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_iklan import GRUP_IKLAN, BlPlatformIklan

NOL = Decimal("0")


@dataclass
class BudgetGrup:
    grup: str
    porsi: Decimal
    budget: Decimal
    terpakai: Decimal
    sisa: Decimal


@dataclass
class BudgetIklan:
    periode: str
    budget_total: Decimal
    dasar: str  # "pengaturan" | "plafon"
    grup: list[BudgetGrup]


def jumlah_selasa(tahun: int, bulan: int) -> int:
    return sum(1 for d in range(1, calendar.monthrange(tahun, bulan)[1] + 1) if date(tahun, bulan, d).weekday() == 1)


async def budget_iklan(session: AsyncSession, tanggal: date, *, kecuali_trx: str | None = None) -> BudgetIklan:
    """Budget & pemakaian per grup bulan `tanggal` (draf ikut dihitung). Budget bulanan = pengaturan, atau bila kosong
    plafon Kas iklan × jumlah Selasa (isi ulang mingguan) bulan itu."""
    awal = tanggal.replace(day=1)
    akhir = tanggal.replace(day=calendar.monthrange(tanggal.year, tanggal.month)[1])
    profil = await session.get(BlProfil, PROFIL_ID)
    porsi = {
        "internal": Decimal(profil.porsi_iklan_internal) if profil else Decimal("25"),
        "eksternal": Decimal(profil.porsi_iklan_eksternal) if profil else Decimal("75"),
    }
    if profil is not None and profil.budget_iklan_bulanan is not None:
        total, dasar = Decimal(profil.budget_iklan_bulanan), "pengaturan"
    else:
        plafon = (
            await session.execute(select(BlAkunKas.plafon).where(BlAkunKas.jenis == "kas_iklan", BlAkunKas.aktif.is_(True)))
        ).scalar() or NOL
        total, dasar = Decimal(plafon) * jumlah_selasa(tanggal.year, tanggal.month), "plafon"
    stmt = (
        select(BlPlatformIklan.grup, func.coalesce(func.sum(BlTransaksi.jumlah), 0))
        .join(BlPlatformIklan, BlPlatformIklan.id == BlTransaksi.platform_iklan_id)
        .where(
            BlTransaksi.dibatalkan.is_(False), BlTransaksi.jenis == "keluar",
            BlTransaksi.tanggal >= awal, BlTransaksi.tanggal <= akhir,
        )
        .group_by(BlPlatformIklan.grup)
    )
    if kecuali_trx:
        stmt = stmt.where(BlTransaksi.id != kecuali_trx)
    pakai = {g: Decimal(n) for g, n in (await session.execute(stmt)).all()}
    grup = []
    for g in GRUP_IKLAN:
        b = (total * porsi[g] / 100).quantize(Decimal("0.01"))
        grup.append(BudgetGrup(grup=g, porsi=porsi[g], budget=b, terpakai=pakai.get(g, NOL), sisa=b - pakai.get(g, NOL)))
    return BudgetIklan(periode=f"{tanggal:%Y-%m}", budget_total=total, dasar=dasar, grup=grup)


async def melebihi_porsi(session: AsyncSession, platform: BlPlatformIklan, tanggal: date, jumlah: Decimal) -> bool:
    b = await budget_iklan(session, tanggal)
    sisa = next(g.sisa for g in b.grup if g.grup == platform.grup)
    return jumlah > sisa
