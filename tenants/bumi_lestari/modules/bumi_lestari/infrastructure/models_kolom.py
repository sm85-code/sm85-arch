"""Definisi kolom (spesifikasi 10.4/10.5): kolom tambahan per entitas + penggantian label kolom inti.

Baris `lapisan="inti"` hanya dibuat saat Admin mengganti label/urutan/tampil kolom inti; daftar kolom inti tetap dari kode."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase

TIPE_KOLOM = ("teks", "angka", "mata_uang", "tanggal", "pilihan", "ya_tidak")
TIPE_LAPORAN = ("teks", "pilihan", "ya_tidak", "tanggal")
LAPISAN_INTI = "inti"
LAPISAN_TAMBAHAN = "tambahan"
MAKS_AKTIF = 20  # AB-DM-10


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BlDefinisiKolom(BumiLestariBase):
    __tablename__ = "bl_definisi_kolom"
    __table_args__ = (UniqueConstraint("entitas", "kunci", name="uq_bl_definisi_kolom_kunci"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    entitas: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    kunci: Mapped[str] = mapped_column(String(64), nullable=False)
    lapisan: Mapped[str] = mapped_column(String(16), nullable=False, default=LAPISAN_TAMBAHAN)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    label_bawaan: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tipe: Mapped[str] = mapped_column(String(16), nullable=False, default="teks")
    wajib: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pilihan: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # [{"nilai": "JNE", "arsip": false}]
    nilai_bawaan: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    min: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    maks: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    tampil_form: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tampil_tabel: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    bisa_filter: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ikut_ekspor: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    untuk_laporan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tampil_staf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    urutan: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    dibuat_oleh: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
