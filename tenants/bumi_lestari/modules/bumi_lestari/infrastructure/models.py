"""ORM models -- bumi_lestari tenant, Tahap 1 (keuangan dasar).

Users, akun kas (termasuk kas kecil imprest), kategori, transaksi, transfer.
Transaksi/transfer tidak pernah dihapus: dibatalkan (dibatalkan=True) supaya
jejak audit tetap ada dan saldo dihitung ulang dari baris yang tidak batal.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Plain string enums (not Postgres ENUM): adding a value needs no ALTER TYPE.
ROLE_USER = ("owner", "staff")
JENIS_AKUN = ("kas", "bank", "ewallet", "kas_kecil")
JENIS_KATEGORI = ("pemasukan", "pengeluaran")
JENIS_TRANSAKSI = ("masuk", "keluar")
JENIS_TRANSFER = ("biasa", "pengisian_kas_kecil")

KODE_KAS_UTAMA = "KAS_UTAMA"
KODE_SALDO_SHOPEE = "SALDO_SHOPEE"
KODE_KAS_KECIL = "KAS_KECIL"
PLAFON_KAS_KECIL_DEFAULT = Decimal("3000000")


class BlUser(BumiLestariBase):
    __tablename__ = "bl_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="staff")
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BlAkunKas(BumiLestariBase):
    __tablename__ = "bl_akun_kas"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    kode: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    jenis: Mapped[str] = mapped_column(String(32), nullable=False, default="kas")
    saldo_awal: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    # Hanya untuk jenis "kas_kecil": saldo yang dijaga lewat pengisian mingguan.
    plafon: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BlKategori(BumiLestariBase):
    __tablename__ = "bl_kategori"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    jenis: Mapped[str] = mapped_column(String(16), nullable=False)
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class BlTransaksi(BumiLestariBase):
    __tablename__ = "bl_transaksi"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tanggal: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    akun_id: Mapped[str] = mapped_column(ForeignKey("bl_akun_kas.id"), nullable=False, index=True)
    kategori_id: Mapped[str] = mapped_column(ForeignKey("bl_kategori.id"), nullable=False)
    jenis: Mapped[str] = mapped_column(String(16), nullable=False)
    jumlah: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    keterangan: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dibuat_oleh: Mapped[str] = mapped_column(String(64), nullable=False)
    # Transaksi yang dibuat otomatis oleh pembayaran/penerimaan/gaji/bagi hasil menunjuk ke sumbernya,
    # supaya pembatalan sumber ikut membatalkan transaksinya.
    ref_jenis: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    ref_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    dibatalkan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dibatalkan_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    alasan_batal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BlTransfer(BumiLestariBase):
    __tablename__ = "bl_transfer"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tanggal: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    dari_akun_id: Mapped[str] = mapped_column(ForeignKey("bl_akun_kas.id"), nullable=False, index=True)
    ke_akun_id: Mapped[str] = mapped_column(ForeignKey("bl_akun_kas.id"), nullable=False, index=True)
    jumlah: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    jenis: Mapped[str] = mapped_column(String(32), nullable=False, default="biasa")
    keterangan: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dibuat_oleh: Mapped[str] = mapped_column(String(64), nullable=False)
    dibatalkan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dibatalkan_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    alasan_batal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


PROFIL_ID = "profil"


class BlProfil(BumiLestariBase):
    """Profil UMKM -- satu baris (id = PROFIL_ID)."""

    __tablename__ = "bl_profil"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=PROFIL_ID)
    nama_usaha: Mapped[str] = mapped_column(String(255), nullable=False, default="PT. Bumi Lestari Indonesia")
    # Perubahan badan usaha CV -> PT: dokumen bertanggal sebelum `nama_usaha_berlaku_mulai` tetap memakai
    # nama lama (nilai awal; bisa diubah di halaman profil).
    nama_usaha_lama: Mapped[str] = mapped_column(String(255), nullable=False, default="CV. Bumi Lestari Indonesia")
    nama_usaha_berlaku_mulai: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=date(2026, 10, 4))
    alamat: Mapped[str] = mapped_column(
        Text, nullable=False,
        default="Jl. Ampel Kuning, Padasuka, Desa Wonoharjo, Kec. Pangandaran, Kab. Pangandaran, Jawa Barat",
    )
    telepon: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    catatan: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Tujuan pembayaran yang dicetak di invoice, mis. "QRIS Pangeran Homeware" atau nomor rekening.
    info_pembayaran: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Biaya proses pesanan untuk order penjual lain: flat per order, tidak tergantung ukuran barang.
    biaya_proses_order: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("10000"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class BlProporsiBagiHasil(BumiLestariBase):
    """Proporsi bagi hasil dari laba bersih: tepat dua penerima, "admin" dan "owner".

    Total persen harus 100. Diubah dari halaman profil UMKM; perhitungan bagi hasil
    periode menyalin (snapshot) proporsi yang berlaku saat dihitung, jadi perubahan
    tidak mengubah periode lama.
    """

    __tablename__ = "bl_proporsi_bagi_hasil"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    penerima: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    persen: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
