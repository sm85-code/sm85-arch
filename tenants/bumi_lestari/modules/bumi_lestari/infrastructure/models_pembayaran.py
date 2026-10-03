"""ORM models -- bumi_lestari Tahap 3/4: pembayaran Selasa, penerimaan reseller, gaji, bagi hasil.

Setiap pembayaran/penerimaan membuat transaksi di keuangan (BlTransaksi.ref_jenis/ref_id) dan
membatalkannya bila dibatalkan. Utang ke pemasok dan piutang reseller diturunkan dari order,
bukan disimpan: order "sudah dibayar" bila ada item pada pembayaran yang tidak dibatalkan.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase, kolom_tambahan_column


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


PERAN_KARYAWAN = ("kas_kecil_packing", "order_non_kayu", "tukang_cat", "asisten_tukang_cat", "lainnya")
REF_PEMBAYARAN_PEMASOK = "pembayaran_pemasok"
REF_PENERIMAAN_RESELLER = "penerimaan_reseller"
REF_GAJI = "gaji"
REF_BAGI_HASIL = "bagi_hasil"
REF_SISIHAN = "sisihan"
REF_TAGIHAN = "tagihan"


class _Kirim:
    """Status posting berkelompok (draf/terkirim) untuk sumber uang dari order (spesifikasi 8.14)."""

    status_kirim: Mapped[str] = mapped_column(String(16), nullable=False, default="draf", server_default="terkirim")
    kiriman_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)


class _Batal:
    dibatalkan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dibatalkan_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    alasan_batal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class BlPembayaranPemasok(_Kirim, _Batal, BumiLestariBase):
    """Pembayaran ke tukang kayu + supplier untuk satu Selasa acuan (boleh lebih dari satu per minggu)."""

    __tablename__ = "bl_pembayaran_pemasok"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    selasa: Mapped[date] = mapped_column(Date, nullable=False, index=True)  # Selasa acuan periode pembayaran
    tanggal: Mapped[date] = mapped_column(Date, nullable=False)
    akun_id: Mapped[str] = mapped_column(ForeignKey("bl_akun_kas.id"), nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    dibuat_oleh: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BlPembayaranPemasokItem(BumiLestariBase):
    __tablename__ = "bl_pembayaran_pemasok_item"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    pembayaran_id: Mapped[str] = mapped_column(ForeignKey("bl_pembayaran_pemasok.id"), nullable=False, index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("bl_order.id"), nullable=False, index=True)
    pemasok_id: Mapped[str] = mapped_column(ForeignKey("bl_pemasok.id"), nullable=False)
    jumlah: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)


class BlPenerimaanReseller(_Kirim, _Batal, BumiLestariBase):
    """Pembayaran dari penjual lain (reseller), biasanya tiap Selasa, untuk satu atau beberapa order."""

    __tablename__ = "bl_penerimaan_reseller"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tanggal: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    pelanggan_id: Mapped[str] = mapped_column(ForeignKey("bl_pelanggan.id"), nullable=False, index=True)
    akun_id: Mapped[str] = mapped_column(ForeignKey("bl_akun_kas.id"), nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    dibuat_oleh: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BlPenerimaanResellerItem(BumiLestariBase):
    __tablename__ = "bl_penerimaan_reseller_item"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    penerimaan_id: Mapped[str] = mapped_column(ForeignKey("bl_penerimaan_reseller.id"), nullable=False, index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("bl_order.id"), nullable=False, index=True)
    jumlah: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)


class BlKaryawan(BumiLestariBase):
    """Karyawan tetap bergaji bulanan. Admin dan owner tidak bergaji (hanya bagi hasil)."""

    __tablename__ = "bl_karyawan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    peran: Mapped[str] = mapped_column(String(32), nullable=False, default="lainnya")
    gaji_bulanan: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("bl_users.id"), nullable=True)
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    kolom_tambahan: Mapped[dict] = kolom_tambahan_column()


class BlGaji(BumiLestariBase):
    """Gaji satu karyawan untuk satu periode (YYYY-MM), dibayar tanggal 1 bulan berikutnya."""

    __tablename__ = "bl_gaji"
    __table_args__ = (UniqueConstraint("periode", "karyawan_id", name="uq_bl_gaji_periode_karyawan"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    periode: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    karyawan_id: Mapped[str] = mapped_column(ForeignKey("bl_karyawan.id"), nullable=False)
    jumlah: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)  # snapshot gaji saat disiapkan
    tanggal_bayar: Mapped[Optional[date]] = mapped_column(Date, nullable=True)  # None = belum dibayar
    dibayar_oleh: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class BlBagiHasil(_Batal, BumiLestariBase):
    """Bagi hasil bulanan admin & owner dari laba bersih (laba <= 0 -> bagian 0)."""

    __tablename__ = "bl_bagi_hasil"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    periode: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    laba_bersih: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    persen_admin: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)  # snapshot proporsi
    persen_owner: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    bagian_admin: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    bagian_owner: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    tanggal_bayar: Mapped[Optional[date]] = mapped_column(Date, nullable=True)  # None = draft
    dibuat_oleh: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BlLangganan(BumiLestariBase):
    """Daftar langganan/utilitas: listrik, air, wifi, kebersihan, iuran BUMDES, langganan Komplace, dst.
    `jumlah_bulanan` hanya perkiraan untuk mengisi otomatis formulir bayar."""

    __tablename__ = "bl_langganan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    jumlah_bulanan: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))  # perkiraan
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class BlSisihan(_Batal, BumiLestariBase):
    """Cicilan mingguan (tiap Selasa, 4 kali per bulan) gaji ke Dana cadangan."""

    __tablename__ = "bl_sisihan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    selasa: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    periode: Mapped[str] = mapped_column(String(7), nullable=False, index=True)  # bulan yang didanai
    minggu_ke: Mapped[int] = mapped_column(Integer, nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    transfer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dibuat_oleh: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BlProvisi(_Batal, BumiLestariBase):
    """Beban yang diakui di laporan keuangan: cicilan mingguan (minggu_ke 1-4) dan penyesuaian saat dibayar
    (minggu_ke 0, bisa negatif). Total per item per bulan = jumlah yang akhirnya dibayar."""

    __tablename__ = "bl_provisi"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tanggal: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    periode: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    minggu_ke: Mapped[int] = mapped_column(Integer, nullable=False)
    jenis: Mapped[str] = mapped_column(String(16), nullable=False)  # gaji
    karyawan_id: Mapped[Optional[str]] = mapped_column(ForeignKey("bl_karyawan.id"), nullable=True, index=True)
    jumlah: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    sumber_jenis: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # sisihan | gaji
    sumber_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class BlTagihan(_Batal, BumiLestariBase):
    """Tagihan langganan yang sudah dibayar. Dibayar langsung saat tagihan datang (kebanyakan minggu ke-4);
    beban diakui saat dibayar, tidak dicicil."""

    __tablename__ = "bl_tagihan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    periode: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    langganan_id: Mapped[str] = mapped_column(ForeignKey("bl_langganan.id"), nullable=False)
    jumlah: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)  # tagihan sebenarnya
    tanggal_bayar: Mapped[date] = mapped_column(Date, nullable=False)
    dibayar_oleh: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BlDokumenBagikan(BumiLestariBase):
    """Dokumen (invoice/PO) yang dibagikan lewat tautan WhatsApp. Isi disimpan sebagai salinan saat dikirim,
    jadi PDF yang dibuka penerima sama persis dengan yang dikirim. Diakses lewat token acak tanpa login."""

    __tablename__ = "bl_dokumen_bagikan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    jenis: Mapped[str] = mapped_column(String(16), nullable=False)  # invoice | po
    nomor: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    dibuat_oleh: Mapped[str] = mapped_column(String(64), nullable=False)
    kedaluwarsa: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
