"""ORM models -- bumi_lestari Fase 2.4/2.5: format file penghasilan & tabel standar pencairan (spesifikasi 8.3, 10.3).

Semua pencairan dari semua saluran (file marketplace, entri manual iPaymu, kelak sinkronisasi marketplace_erp)
disimpan di satu tabel standar `bl_pencairan_baris`; laporan & pencocokan hanya membaca tabel ini.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


STATUS_FORMAT = ("draf", "aktif", "arsip")
KOLOM_TUJUAN = ("kode_pesanan", "tanggal_cair", "harga_jual", "potongan_biaya", "jumlah_cair", "jenis_baris")
OPERASI_KOLOM = ("ambil", "jumlahkan", "mutlak", "balik_tanda")
JENIS_BARIS = ("pesanan", "penyesuaian", "retur")
STATUS_COCOK = ("cocok", "selisih", "tidak_cocok")
MODE_CATAT = ("bruto", "neto")
REF_PENCAIRAN = "pencairan"


class BlFormatPenghasilan(BumiLestariBase):
    """Satu versi format file penghasilan untuk satu saluran. Hanya satu versi aktif per saluran."""
    __tablename__ = "bl_format_penghasilan"
    __table_args__ = (UniqueConstraint("saluran_id", "versi", name="uq_bl_format_penghasilan_versi"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    saluran_id: Mapped[str] = mapped_column(ForeignKey("bl_saluran.id"), nullable=False, index=True)
    nama: Mapped[str] = mapped_column(String(128), nullable=False)
    versi: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draf")
    jenis_file: Mapped[str] = mapped_column(String(8), nullable=False, default="xlsx")  # xlsx / csv
    nama_sheet: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)  # None = sheet pertama
    baris_header: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # 1-based
    baris_data_mulai: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # None = header + 1
    format_tanggal: Mapped[str] = mapped_column(String(32), nullable=False, default="dd/mm/yyyy")
    pemisah_desimal: Mapped[str] = mapped_column(String(1), nullable=False, default=",")
    pemisah_ribuan: Mapped[str] = mapped_column(String(1), nullable=False, default=".")
    aturan_tanda: Mapped[str] = mapped_column(String(16), nullable=False, default="mutlak")  # mutlak/positif/kurung
    aturan_jenis_baris: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    satuan_baris: Mapped[str] = mapped_column(String(16), nullable=False, default="per_pesanan")  # per_pesanan/per_produk
    aturan_abaikan: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    contoh_file: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    contoh_nama: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    hasil_uji: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    lulus_uji: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Format bawaan yang belum pernah diuji dengan file asli (mis. Shopee sementara) ditandai di sini.
    catatan: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dibuat_oleh: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    diaktifkan_oleh: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    diaktifkan_pada: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BlFormatPenghasilanKolom(BumiLestariBase):
    __tablename__ = "bl_format_penghasilan_kolom"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    format_id: Mapped[str] = mapped_column(ForeignKey("bl_format_penghasilan.id"), nullable=False, index=True)
    kolom_tujuan: Mapped[str] = mapped_column(String(32), nullable=False)
    kolom_sumber: Mapped[str] = mapped_column(String(255), nullable=False)  # nama header (atau huruf kolom, mis. "C")
    operasi: Mapped[str] = mapped_column(String(16), nullable=False, default="ambil")
    nama_rincian: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)  # komponen potongan
    urutan: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class BlPencairanUnggahan(BumiLestariBase):
    """Batch unggahan (file, entri manual iPaymu, atau sinkronisasi). Sumber kiriman "pencairan"."""
    __tablename__ = "bl_pencairan_unggahan"
    __table_args__ = (Index("uq_bl_pencairan_unggahan_sumber", "sumber_sistem", "sumber_ref", unique=True),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    saluran_id: Mapped[str] = mapped_column(ForeignKey("bl_saluran.id"), nullable=False, index=True)
    format_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    format_versi: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    nama_file: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Tanggal entri untuk kiriman/tutup buku = tanggal cair terakhir; periode = rentang tanggal cair.
    tanggal: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    periode_dari: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    periode_sampai: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    jumlah_baris: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))  # total jumlah cair dibukukan
    total_harga_jual: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    total_potongan: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    status_kirim: Mapped[str] = mapped_column(String(16), nullable=False, default="draf", server_default="draf")
    kiriman_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    diunggah_oleh: Mapped[str] = mapped_column(String(64), nullable=False)
    sumber_sistem: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    sumber_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    dibatalkan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dibatalkan_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    alasan_batal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BlPencairanBaris(BumiLestariBase):
    """Tabel standar pencairan (kolom spesifikasi 8.3.2 + 10.5)."""
    __tablename__ = "bl_pencairan_baris"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    unggahan_id: Mapped[str] = mapped_column(ForeignKey("bl_pencairan_unggahan.id"), nullable=False, index=True)
    saluran_id: Mapped[str] = mapped_column(ForeignKey("bl_saluran.id"), nullable=False, index=True)
    kode_pesanan: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    tanggal_cair: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    harga_jual: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    potongan_biaya: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    rincian_biaya: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    jumlah_cair: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    jenis_baris: Mapped[str] = mapped_column(String(16), nullable=False, default="pesanan")
    mode_catat: Mapped[str] = mapped_column(String(8), nullable=False, default="bruto")
    order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    status_cocok: Mapped[str] = mapped_column(String(16), nullable=False, default="tidak_cocok")
    selisih: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    # Anti-duplikat: saluran + kode pesanan + jenis baris + tanggal cair + jumlah cair. Baris batal/diganti diberi akhiran.
    kunci_unik: Mapped[str] = mapped_column(String(400), nullable=False, unique=True)
    baris_file: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    data_asli: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    masalah: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # Status order sebelum baris retur menandainya retur (dipulihkan bila kiriman dibatalkan).
    status_order_sebelum: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    dibatalkan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
