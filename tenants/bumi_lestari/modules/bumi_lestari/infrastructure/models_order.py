"""ORM models -- bumi_lestari Tahap 2: katalog, pemasok, saluran, pelanggan, order.

Satu baris order = satu produk (order marketplace multi-item = beberapa baris dengan
no_order yang sama). Biaya pokok order = harga all-in dari pemasok (tukang kayu: bahan +
jasa; supplier: harga beli). Tidak ada biaya jasa cat per order (tukang cat bergaji tetap).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


JENIS_PRODUK = ("kayu", "non_kayu")
JENIS_PACKING = ("biasa", "kayu")
JENIS_PEMASOK = ("tukang_kayu", "supplier")
JENIS_SALURAN = ("marketplace", "web", "reseller")
STATUS_ORDER_KAYU = ("dipesan", "dikerjakan", "diambil", "dicat", "dikirim", "selesai")
STATUS_ORDER_NON_KAYU = ("dipesan", "diterima", "dikirim", "selesai")
STATUS_BATAL = "batal"


class BlProduk(BumiLestariBase):
    __tablename__ = "bl_produk"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    sku: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    jenis_produk: Mapped[str] = mapped_column(String(16), nullable=False, default="kayu")
    harga_jual: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    biaya_pokok_default: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BlPemasok(BumiLestariBase):
    __tablename__ = "bl_pemasok"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    jenis: Mapped[str] = mapped_column(String(16), nullable=False)
    kontak: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    catatan: Mapped[str] = mapped_column(Text, nullable=False, default="")
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class BlSaluran(BumiLestariBase):
    __tablename__ = "bl_saluran"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    jenis: Mapped[str] = mapped_column(String(16), nullable=False)
    # Akun kas tujuan uang saluran ini (mis. Saldo Shopee). Dipakai pada T3.
    akun_id: Mapped[Optional[str]] = mapped_column(ForeignKey("bl_akun_kas.id"), nullable=True)
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class BlPelanggan(BumiLestariBase):
    """Reseller / penjual lain yang memesan produk ke UMKM."""

    __tablename__ = "bl_pelanggan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    kontak: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    catatan: Mapped[str] = mapped_column(Text, nullable=False, default="")
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class BlHargaGrosir(BumiLestariBase):
    __tablename__ = "bl_harga_grosir"
    __table_args__ = (UniqueConstraint("produk_id", "pelanggan_id", name="uq_bl_harga_grosir"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    produk_id: Mapped[str] = mapped_column(ForeignKey("bl_produk.id"), nullable=False)
    pelanggan_id: Mapped[str] = mapped_column(ForeignKey("bl_pelanggan.id"), nullable=False)
    # Komponen per unit yang dibayar penjual lain: barang, cat + jasa (tergantung ukuran barang, jadi
    # melekat ke produk), packing (biasa atau kayu). Biaya proses pesanan flat per order: ada di profil.
    harga: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)  # barang, per unit
    harga_cat_jasa: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))  # per unit
    harga_packing_biasa: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    harga_packing_kayu: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))


class BlOrder(BumiLestariBase):
    __tablename__ = "bl_order"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    no_order: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    tanggal_order: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    saluran_id: Mapped[str] = mapped_column(ForeignKey("bl_saluran.id"), nullable=False)
    pelanggan_id: Mapped[Optional[str]] = mapped_column(ForeignKey("bl_pelanggan.id"), nullable=True)
    nama_pembeli: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    produk_id: Mapped[str] = mapped_column(ForeignKey("bl_produk.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    harga_satuan: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)  # barang, per unit
    # Hanya terisi untuk order reseller yang dicat; 0 untuk order polos dan saluran lain (harga all-in).
    harga_cat_jasa: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))  # per unit
    # Packing tetap dibayar walau polos: jenis "biasa" atau "kayu" (harga berbeda), per unit.
    jenis_packing: Mapped[str] = mapped_column(String(16), nullable=False, default="biasa")
    harga_packing: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    # Biaya proses pesanan, flat per order (disalin dari profil saat order dibuat; 0 selain order penjual lain).
    biaya_proses: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    # Warna cat (bisa custom). Tidak memengaruhi harga.
    warna: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    potongan_marketplace: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    pemasok_id: Mapped[Optional[str]] = mapped_column(ForeignKey("bl_pemasok.id"), nullable=True)
    biaya_pokok: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    butuh_cat: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="dipesan", index=True)
    tgl_pesan_pemasok: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    tgl_diambil: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    tgl_dicat: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    tgl_dikirim: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    tgl_selesai: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    catatan: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
