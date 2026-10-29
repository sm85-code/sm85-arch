"""Additive tables for the ads agent. New tables only: nothing here alters or drops existing data.

Registered on MarketplaceErpBase so startup ``create_all`` creates them on databases that
do not have them yet. Existing tables are left untouched.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

# One row per shop. A missing row means: 20% max change, no budget/CPC cap, no wait, autonomy off.
class AgenIklanBatas(MarketplaceErpBase):
    __tablename__ = "mpe_agen_iklan_batas"

    akun_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_akun_marketplace.id", ondelete="CASCADE"), primary_key=True
    )
    anggaran_harian_maks: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True)
    maks_persen_perubahan: Mapped[int] = mapped_column(Integer, nullable=False, default=20, server_default="20")
    maks_cpc: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True)
    min_hari_antar_perubahan: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    otonomi: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    updated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class AgenIklanStop(MarketplaceErpBase):
    """Kill switch. ``id`` is ``global`` or a shop id. ``aktif`` means writes are blocked."""

    __tablename__ = "mpe_agen_iklan_stop"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    alasan: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    oleh: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class AgenProdukSnapshot(MarketplaceErpBase):
    """One day's product stats pulled from Shopee (views, likes, sold, rating, a short diagnosis)."""

    __tablename__ = "mpe_agen_produk_snapshot"
    __table_args__ = (UniqueConstraint("akun_id", "item_id", "tanggal", name="uq_mpe_agen_produk_snapshot"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    akun_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_akun_marketplace.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[str] = mapped_column(String(32), nullable=False)
    tanggal: Mapped[date] = mapped_column(Date, nullable=False)
    views: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    likes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sold: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rating: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    komentar: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    diagnosis_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    diambil_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AgenIklanJadwal(MarketplaceErpBase):
    """Hours (WIB) a campaign may stay on. ``aktif`` is off until someone turns that row on."""

    __tablename__ = "mpe_agen_iklan_jadwal"
    __table_args__ = (UniqueConstraint("akun_id", "campaign_id", name="uq_mpe_agen_iklan_jadwal_akun_kampanye"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    akun_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_akun_marketplace.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id: Mapped[str] = mapped_column(String(32), nullable=False)
    jam_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    dijeda_jadwal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class AgenIklanAudit(MarketplaceErpBase):
    """Who changed ads settings or limits, with the before/after payload and the caller's request id."""

    __tablename__ = "mpe_agen_iklan_audit"
    __table_args__ = (UniqueConstraint("akun_id", "request_id", name="uq_mpe_agen_iklan_audit_akun_request"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    akun_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("mpe_akun_marketplace.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kampanye_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    aksi: Mapped[str] = mapped_column(String(32), nullable=False)
    pelaku_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    pelaku_nama: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    sebelum_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    sesudah_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    alasan: Mapped[str] = mapped_column(Text, nullable=False, default="")
    request_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    persetujuan: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    kering: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    hasil: Mapped[str] = mapped_column(String(32), nullable=False, default="tersimpan")
    dibuat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
