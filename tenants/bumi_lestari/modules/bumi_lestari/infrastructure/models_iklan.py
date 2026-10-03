"""ORM models -- bumi_lestari Fase 2.9/2.10: platform iklan & budget 25/75, log plafon (spesifikasi 8.7)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase

GRUP_IKLAN = ("internal", "eksternal")


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BlPlatformIklan(BumiLestariBase):
    """Platform tempat top up iklan. Grup internal = iklan di dalam marketplace; eksternal = Meta, Google, dll."""
    __tablename__ = "bl_platform_iklan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    grup: Mapped[str] = mapped_column(String(16), nullable=False)
    saluran_id: Mapped[Optional[str]] = mapped_column(ForeignKey("bl_saluran.id"), nullable=True)
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BlPlafonLog(BumiLestariBase):
    """Setiap perubahan plafon kas kecil/kas iklan (AB-KI-5, KP-KI-3)."""
    __tablename__ = "bl_plafon_log"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    akun_id: Mapped[str] = mapped_column(ForeignKey("bl_akun_kas.id"), nullable=False, index=True)
    tanggal: Mapped[date] = mapped_column(Date, nullable=False)
    dari: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    ke: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    oleh: Mapped[str] = mapped_column(String(64), nullable=False)
    alasan: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
