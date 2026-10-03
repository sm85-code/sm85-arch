"""Skema talangan (spesifikasi 8.8, Fase 2 item 1.10)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class TalanganBayarOut(BaseModel):
    id: str
    transfer_id: str
    tanggal: date
    jumlah: Decimal
    dibatalkan: bool


class TalanganOut(BaseModel):
    id: str
    tanggal: date
    nama: str
    akun_asal_id: str
    akun_asal_nama: str
    kategori_id: str
    keterangan: str
    total_pengeluaran: Decimal  # bagian akun asal + talangan
    jumlah: Decimal  # bagian talangan
    terbayar: Decimal
    sisa: Decimal
    status_kirim: str  # status kiriman pengeluarannya (draf/terkirim)
    dibatalkan: bool
    alasan_batal: Optional[str] = None
    created_at: datetime
    bayar: list[TalanganBayarOut] = []


class LunasiTalanganIn(BaseModel):
    jumlah: Optional[Decimal] = Field(default=None, gt=0, max_digits=14, decimal_places=2)  # kosong = lunasi sisa
    tanggal: Optional[date] = None
