"""Perhitungan laba bersih basis kas + beban cicilan gaji -- dipakai bagi hasil, dashboard, dan laporan."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenants.bumi_lestari.modules.bumi_lestari.application.provisi_core import beban_provisi
from tenants.bumi_lestari.modules.bumi_lestari.application.kategori_core import (
    KATEGORI_KERUGIAN_RETUR,
    KATEGORI_PRODUKSI,
    KATEGORI_SETORAN_MODAL,
)
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models import STATUS_TERKIRIM, BlKategori, BlTransaksi
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_order import STATUS_RETUR, BlOrder
from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.models_pembayaran import (
    REF_GAJI,
    BlPembayaranPemasok,
    BlPembayaranPemasokItem,
)

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


async def _per_kategori(
    session: AsyncSession, jenis: str, awal: date, akhir: date, *, dalam=None, kecuali=None, kecuali_ref=(), jenis_kategori=None,
    bukan_jenis_kategori=None,
):
    stmt = (
        select(BlKategori.nama, func.coalesce(func.sum(BlTransaksi.jumlah), 0), func.count(BlTransaksi.id))
        .join(BlKategori, BlKategori.id == BlTransaksi.kategori_id)
        .where(
            BlTransaksi.jenis == jenis, BlTransaksi.dibatalkan.is_(False),
            BlTransaksi.status_kirim == STATUS_TERKIRIM,  # draf belum masuk laporan
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
    if jenis_kategori:
        stmt = stmt.where(BlKategori.jenis == jenis_kategori)
    if bukan_jenis_kategori:
        stmt = stmt.where(BlKategori.jenis != bukan_jenis_kategori)
    return [Baris(n, Decimal(str(j)), int(c)) for n, j, c in (await session.execute(stmt)).all()]


async def reklas_retur(session: AsyncSession, awal: date, akhir: date) -> Decimal:
    """Biaya tukang/supplier untuk order retur yang pindah dari HPP ke Kerugian retur (AB-BC-3).

    Diakui pada bulan yang lebih akhir antara tanggal retur dan tanggal pembayaran pemasok (terkirim), jadi bulan
    yang sudah tutup buku tidak berubah. Laba bersih tetap; hanya kelompok biayanya yang berpindah."""
    rows = (
        await session.execute(
            select(BlPembayaranPemasokItem.jumlah, BlOrder.tgl_retur, BlPembayaranPemasok.tanggal)
            .join(BlPembayaranPemasok, BlPembayaranPemasok.id == BlPembayaranPemasokItem.pembayaran_id)
            .join(BlOrder, BlOrder.id == BlPembayaranPemasokItem.order_id)
            .where(
                BlOrder.status == STATUS_RETUR, BlOrder.tgl_retur.is_not(None), BlOrder.tgl_retur <= akhir,
                BlPembayaranPemasok.dibatalkan.is_(False), BlPembayaranPemasok.status_kirim == STATUS_TERKIRIM,
                BlPembayaranPemasok.tanggal <= akhir,
            )
        )
    ).all()
    return sum((Decimal(j) for j, tr, tb in rows if awal <= max(tr, tb) <= akhir), Decimal("0"))


def _geser(baris: list[Baris], kategori: str, delta: Decimal) -> None:
    for b in baris:
        if b.kategori == kategori:
            b.jumlah += delta
            return
    baris.append(Baris(kategori, delta))


async def ringkasan_laba(session: AsyncSession, awal: date, akhir: date) -> RingkasanLaba:
    """Laba = pemasukan - biaya (hanya entri terkirim). Transfer dan setoran modal tidak dihitung. Gaji diakui lewat cicilan mingguan (bukan saat
    dibayar), jadi transaksi pembayaran gaji dikecualikan; langganan diakui saat dibayar."""
    hasil = RingkasanLaba()
    # Setoran modal bukan pendapatan (spesifikasi 8.12).
    hasil.pemasukan = await _per_kategori(session, "masuk", awal, akhir, kecuali=(KATEGORI_SETORAN_MODAL,))
    # Uang keluar berkategori pemasukan (retur/penyesuaian negatif dari pencairan, AB-BC-4) mengurangi pemasukannya.
    for b in await _per_kategori(session, "keluar", awal, akhir, kecuali=(KATEGORI_SETORAN_MODAL,), jenis_kategori="pemasukan"):
        _geser(hasil.pemasukan, b.kategori, -b.jumlah)
    hasil.biaya = await _per_kategori(
        session, "keluar", awal, akhir, kecuali=KATEGORI_BUKAN_BIAYA, kecuali_ref=(REF_GAJI,),
        bukan_jenis_kategori="pemasukan",
    )
    reklas = await reklas_retur(session, awal, akhir)
    if reklas:
        _geser(hasil.biaya, KATEGORI_PRODUKSI, -reklas)
        _geser(hasil.biaya, KATEGORI_KERUGIAN_RETUR, reklas)
        hasil.biaya.sort(key=lambda b: b.kategori)
    cicilan = await beban_provisi(session, awal, akhir)
    if cicilan != 0:
        hasil.biaya.append(Baris(LABEL_GAJI_CICILAN, cicilan))
    hasil.di_luar_laba = await _per_kategori(session, "keluar", awal, akhir, dalam=KATEGORI_BUKAN_BIAYA)
    return hasil
