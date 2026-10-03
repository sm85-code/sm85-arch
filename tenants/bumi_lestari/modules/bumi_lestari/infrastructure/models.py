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

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Plain string enums (not Postgres ENUM): adding a value needs no ALTER TYPE.
ROLE_USER = ("owner", "staff")
JENIS_AKUN = ("kas", "bank", "ewallet", "kas_kecil", "kas_iklan")
# Akun imprest: saldo dijaga di plafon lewat pengisian mingguan dari kas utama.
JENIS_IMPRESET = ("kas_kecil", "kas_iklan")
JENIS_KATEGORI = ("pemasukan", "pengeluaran")
JENIS_TRANSAKSI = ("masuk", "keluar")
JENIS_TRANSFER = ("biasa", "pengisian_kas_kecil", "pengisian_kas_iklan", "sisihan_dana")

KODE_KAS_UTAMA = "KAS_UTAMA"
KODE_SALDO_SHOPEE = "SALDO_SHOPEE"
KODE_KAS_KECIL = "KAS_KECIL"
PLAFON_KAS_KECIL_DEFAULT = Decimal("3000000")
KODE_KAS_IKLAN = "KAS_IKLAN"
KODE_DANA_CADANGAN = "DANA_CADANGAN"  # gaji & langganan disisihkan mingguan, dibayar awal bulan
PLAFON_KAS_IKLAN_DEFAULT = Decimal("2000000")

# Posting berkelompok (spesifikasi 8.14): entri sumber berstatus draf sampai "Kirim ke laporan keuangan".
STATUS_DRAF = "draf"
STATUS_TERKIRIM = "terkirim"
STATUS_KIRIMAN_DIBATALKAN = "dibatalkan"
SUMBER_KIRIMAN = ("kas_kecil", "kas_iklan", "penerimaan_reseller", "pembayaran_pemasok")


class BlUser(BumiLestariBase):
    __tablename__ = "bl_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="staff")
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Naik setiap ganti/reset password, ganti role, nonaktif, atau "keluar dari semua perangkat";
    # token lama (sv berbeda) ditolak.
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BlAkunKas(BumiLestariBase):
    __tablename__ = "bl_akun_kas"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    kode: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    jenis: Mapped[str] = mapped_column(String(32), nullable=False, default="kas")
    saldo_awal: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    # Hanya untuk akun imprest (kas_kecil, kas_iklan): saldo yang dijaga lewat pengisian mingguan.
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
    __table_args__ = (Index("uq_bl_transaksi_sumber", "sumber_sistem", "sumber_ref", unique=True),)

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
    # draf = belum masuk buku besar/laporan (kas kecil, kas iklan, uang dari order); terkirim = resmi.
    status_kirim: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATUS_TERKIRIM, server_default=STATUS_TERKIRIM, index=True
    )
    kiriman_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    # Referensi sistem asal untuk sinkronisasi idempoten (mis. "marketplace_erp" + id settlement); lihat INTEGRASI.md.
    sumber_sistem: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    sumber_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Koreksi atas bulan yang sudah tutup buku (YYYY-MM); transaksi tetap bertanggal & dihitung di bulan berjalan.
    koreksi_periode: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)
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


class BlKiriman(BumiLestariBase):
    """Satu kali "Kirim ke laporan keuangan" untuk satu sumber (spesifikasi 8.14 AB-KR-3)."""

    __tablename__ = "bl_kiriman"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nomor: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)  # KRM-YYYYMMDD-NNN
    sumber: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sampai_tanggal: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    jumlah_entri: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_TERKIRIM, index=True)
    dikirim_oleh: Mapped[str] = mapped_column(String(64), nullable=False)
    dikirim_pada: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    dibatalkan_oleh: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    dibatalkan_pada: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    alasan_batal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tutup_kas_mingguan_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class BlKirimanItem(BumiLestariBase):
    __tablename__ = "bl_kiriman_item"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    kiriman_id: Mapped[str] = mapped_column(ForeignKey("bl_kiriman.id"), nullable=False, index=True)
    ref_jenis: Mapped[str] = mapped_column(String(32), nullable=False)  # transaksi / pembayaran_pemasok / ...
    ref_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    transaksi_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tanggal: Mapped[date] = mapped_column(Date, nullable=False)
    jumlah: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)


class BlAuditLog(BumiLestariBase):
    """Log audit dasar (spesifikasi 8.13 AB-IN-6): siapa, kapan, aksi, entitas, sebelum/sesudah, alasan."""

    __tablename__ = "bl_audit_log"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    waktu: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    aksi: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entitas: Mapped[str] = mapped_column(String(64), nullable=False)
    entitas_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    sebelum: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    sesudah: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    alasan: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


STATUS_DITUTUP = "ditutup"
STATUS_DIBUKA = "dibuka"


class BlTutupBuku(BumiLestariBase):
    """Tutup buku bulanan (spesifikasi 8.10): bulan terkunci + snapshot angka; buka darurat beralasan."""

    __tablename__ = "bl_tutup_buku"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    periode: Mapped[str] = mapped_column(String(7), nullable=False, unique=True)  # YYYY-MM
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_DITUTUP)  # ditutup / dibuka
    ditutup_oleh: Mapped[str] = mapped_column(String(64), nullable=False)
    ditutup_pada: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    dibuka_oleh: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    dibuka_pada: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    alasan_buka: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
