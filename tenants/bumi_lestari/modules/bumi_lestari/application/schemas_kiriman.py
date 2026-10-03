"""Schemas -- posting berkelompok "Kirim ke laporan keuangan" (bl_kiriman) dan log audit."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class EntriDrafOut(BaseModel):
    ref_jenis: str  # transaksi / pembayaran_pemasok / penerimaan_reseller
    ref_id: str
    tanggal: date
    jenis: str  # masuk / keluar
    jumlah: Decimal
    keterangan: str


class DrafSumberOut(BaseModel):
    sumber: str
    label: str
    jumlah_entri: int
    total_masuk: Decimal
    total_keluar: Decimal
    total: Decimal  # jumlah nilai entri (masuk + keluar)
    tanggal_tertua: Optional[date] = None
    entri: list[EntriDrafOut] = []


class KirimIn(BaseModel):
    sumber: str
    sampai_tanggal: Optional[date] = None  # kosong -> semua draf sumber ini
    tutup_kas_mingguan_id: Optional[str] = Field(default=None, max_length=64)


class KirimSemuaIn(BaseModel):
    sampai_tanggal: Optional[date] = None
    tutup_kas_mingguan_id: Optional[str] = Field(default=None, max_length=64)


class KirimanItemOut(BaseModel):
    ref_jenis: str
    ref_id: str
    transaksi_id: str
    tanggal: date
    jumlah: Decimal
    model_config = ConfigDict(from_attributes=True)


class KirimanOut(BaseModel):
    id: str
    nomor: str
    sumber: str
    sampai_tanggal: Optional[date] = None
    jumlah_entri: int
    total: Decimal
    status: str  # terkirim / dibatalkan
    dikirim_oleh: str
    dikirim_pada: datetime
    dibatalkan_oleh: Optional[str] = None
    dibatalkan_pada: Optional[datetime] = None
    alasan_batal: Optional[str] = None
    tutup_kas_mingguan_id: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class KirimanDetailOut(KirimanOut):
    items: list[KirimanItemOut] = []


class AuditOut(BaseModel):
    id: str
    waktu: datetime
    user_id: Optional[str] = None
    aksi: str
    entitas: str
    entitas_id: Optional[str] = None
    sebelum: Optional[dict[str, Any]] = None
    sesudah: Optional[dict[str, Any]] = None
    alasan: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)
