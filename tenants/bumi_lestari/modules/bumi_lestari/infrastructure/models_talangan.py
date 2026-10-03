"""ORM models -- bumi_lestari Fase 2 (item 1.10): talangan (spesifikasi 8.8, AB-TL-1/2).

Talangan = uang pribadi seseorang yang dipakai untuk pengeluaran kas kecil/kas iklan saat saldonya kurang.
Pengeluaran tetap dicatat penuh sebagai biaya: bagian yang tertutup saldo dicatat di akun asal, kekurangannya
dicatat di akun kewajiban virtual `TALANGAN` (saldonya negatif = utang usaha). Pelunasan = transfer Kas utama →
`TALANGAN` (bukan biaya lagi), boleh sebagian. Rincian per orang di `bl_talangan`.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase

REF_TALANGAN = "talangan"


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BlTalangan(BumiLestariBase):
    __tablename__ = "bl_talangan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    tanggal: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    nama: Mapped[str] = mapped_column(String(128), nullable=False, index=True)  # orang yang menalangi
    akun_asal_id: Mapped[str] = mapped_column(ForeignKey("bl_akun_kas.id"), nullable=False, index=True)
    # Bagian pengeluaran yang tertutup saldo akun asal (None bila saldo 0) dan bagian talangan (akun TALANGAN).
    transaksi_id: Mapped[Optional[str]] = mapped_column(ForeignKey("bl_transaksi.id"), nullable=True, index=True)
    transaksi_talangan_id: Mapped[str] = mapped_column(ForeignKey("bl_transaksi.id"), nullable=False, index=True)
    jumlah: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    keterangan: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dibuat_oleh: Mapped[str] = mapped_column(String(64), nullable=False)
    dibatalkan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dibatalkan_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    alasan_batal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BlTalanganBayar(BumiLestariBase):
    """Satu pelunasan (boleh sebagian) = satu transfer Kas utama → TALANGAN."""
    __tablename__ = "bl_talangan_bayar"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    talangan_id: Mapped[str] = mapped_column(ForeignKey("bl_talangan.id"), nullable=False, index=True)
    transfer_id: Mapped[str] = mapped_column(ForeignKey("bl_transfer.id"), nullable=False, index=True)
    tanggal: Mapped[date] = mapped_column(Date, nullable=False)
    jumlah: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    dibuat_oleh: Mapped[str] = mapped_column(String(64), nullable=False)
    dibatalkan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
