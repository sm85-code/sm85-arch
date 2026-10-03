"""Perhitungan laba bersih basis kas + beban cicilan gaji -- dipakai bagi hasil, dashboard, dan laporan."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application.provisi_core import beban_provisi
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import BlKategori, BlTransaksi
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_t3 import REF_GAJI

KATEGORI_BAGI_HASIL = "Bagi hasil"
# Bukan biaya usaha: tidak mengurangi laba bersih yang dibagi.
KATEGORI_BUKAN_BIAYA = ("Prive", KATEGORI_BAGI_HASIL)
LABEL_GAJI_CICILAN = "Gaji karyawan (cicilan)"


@dataclass
class Baris:
    kategori: str
    jumlah: Decimal
    jumlah_transaksi: int = 0


@dataclass
class RingkasanLaba:
    pemasukan: list[Baris] = field(default_factory=list)
    biaya: list[Baris] = field(default_factory=list)
    di_luar_laba: list[Baris] = field(default_factory=list)  # Prive, Bagi hasil

    @property
    def total_pemasukan(self) -> Decimal:
        return sum((b.jumlah for b in self.pemasukan), Decimal("0"))

    @property
    def total_biaya(self) -> Decimal:
        return sum((b.jumlah for b in self.biaya), Decimal("0"))

    @property
    def laba(self) -> Decimal:
        return self.total_pemasukan - self.total_biaya


async def _per_kategori(session: AsyncSession, jenis: str, awal: date, akhir: date, *, dalam=None, kecuali=None, kecuali_ref=()):
    stmt = (
        select(BlKategori.nama, func.coalesce(func.sum(BlTransaksi.jumlah), 0), func.count(BlTransaksi.id))
        .join(BlKategori, BlKategori.id == BlTransaksi.kategori_id)
        .where(
            BlTransaksi.jenis == jenis, BlTransaksi.dibatalkan.is_(False),
            BlTransaksi.tanggal >= awal, BlTransaksi.tanggal <= akhir,
        )
        .group_by(BlKategori.nama)
        .order_by(BlKategori.nama)
    )
    if dalam:
        stmt = stmt.where(BlKategori.nama.in_(dalam))
    if kecuali:
        stmt = stmt.where(BlKategori.nama.not_in(kecuali))
    if kecuali_ref:
        stmt = stmt.where(or_(BlTransaksi.ref_jenis.is_(None), BlTransaksi.ref_jenis.not_in(kecuali_ref)))
    return [Baris(n, Decimal(str(j)), int(c)) for n, j, c in (await session.execute(stmt)).all()]


async def ringkasan_laba(session: AsyncSession, awal: date, akhir: date) -> RingkasanLaba:
    """Laba = pemasukan - biaya. Transfer tidak dihitung. Gaji diakui lewat cicilan mingguan (bukan saat
    dibayar), jadi transaksi pembayaran gaji dikecualikan; langganan diakui saat dibayar."""
    hasil = RingkasanLaba()
    hasil.pemasukan = await _per_kategori(session, "masuk", awal, akhir)
    hasil.biaya = await _per_kategori(session, "keluar", awal, akhir, kecuali=KATEGORI_BUKAN_BIAYA, kecuali_ref=(REF_GAJI,))
    cicilan = await beban_provisi(session, awal, akhir)
    if cicilan != 0:
        hasil.biaya.append(Baris(LABEL_GAJI_CICILAN, cicilan))
    hasil.di_luar_laba = await _per_kategori(session, "keluar", awal, akhir, dalam=KATEGORI_BUKAN_BIAYA)
    return hasil
