"""Isolated ORM models for the toko (online shop) database.

Foundation scope only: user/auth + product catalog. Cart, orders, payment,
shipping, and reporting modules land in follow-up work once this is proven
out.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tenants.toko.modules.toko.infrastructure.database import TokoBase


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserToko(TokoBase):
    __tablename__ = "toko_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # admin_toko: kelola produk & pesanan. owner: full akses + laporan.
    # pembeli: akun customer (opsional -- checkout sebagai guest juga didukung
    # nanti di modul pesanan).
    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True, default="pembeli")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ProdukToko(TokoBase):
    __tablename__ = "toko_produk"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    deskripsi: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kategori: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    harga: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    stok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    foto_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
