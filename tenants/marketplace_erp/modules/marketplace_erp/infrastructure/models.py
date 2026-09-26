"""ORM models -- foundation (Tahap 1) of the marketplace_erp tenant.

Scope of this file: auth (UserMarketplaceErp), marketplace shop accounts
(AkunMarketplace) and the centralized product master (Produk = SKU induk)
with its per-platform listing mapping (ProdukListing). These three concepts
are the foundation every later module (stok, pesanan, pengiriman, chat,
keuangan, iklan, laporan) is built on top of:

- Produk (SKU induk) is the single source of truth for one physical item.
- ProdukListing maps that one Produk to N per-platform listings (one row
  per (platform, id_eksternal)) across any number of AkunMarketplace shops,
  which is what makes "update stock once, push everywhere" possible later.

Not yet covered here (deliberately -- separate follow-up modules): gudang/
stock reservation, pesanan (orders), pengiriman (shipping), chat, keuangan
(settlement), iklan (ads). Adding those later only means adding new tables
that reference Produk/AkunMarketplace by id -- no change needed here.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Supported marketplace platforms. Plain string enum (not a Postgres ENUM
# type) so adding a new platform later is a one-line change here, no ALTER
# TYPE migration -- same reasoning as tenants/toko/modules/erp.
PLATFORM_MARKETPLACE = ("shopee", "tiktokshop", "lazada", "blibli")


class UserMarketplaceErp(MarketplaceErpBase):
    """Admin/staff login for this tenant. Own table, own JWT cookie (see
    infrastructure/auth.py) -- zero relation to tenants/toko's UserToko."""

    __tablename__ = "mpe_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="owner")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class AkunMarketplace(MarketplaceErpBase):
    """One row = one shop authorized (or pending authorization) on one
    platform. A single owner can connect many shops per platform (e.g. 10
    Shopee shops via a Shopee sub-account main login) -- `platform` alone
    never identifies a shop, every row also carries its own external shop
    id/credentials.

    Credentials stored as plain columns (not encrypted) for the same reason
    as tenants/toko/modules/erp: this tenant's DB is already isolated/
    dedicated. Never return access_token/refresh_token in list/summary API
    responses -- only in a masked single-akun detail view (see
    application/services.py once that lands)."""

    __tablename__ = "mpe_akun_marketplace"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    platform: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    nama_toko: Mapped[str] = mapped_column(String(255), nullable=False)
    id_toko_eksternal: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    access_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_kedaluwarsa: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="belum_terhubung")
    catatan: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    listing: Mapped[list["ProdukListing"]] = relationship(back_populates="akun")


class Produk(MarketplaceErpBase):
    """SKU induk -- one row per physical product, independent of any
    platform. `stok` here is the single centralized stock number (Tahap 1:
    one implicit warehouse; multi-gudang/reservasi is a later module that
    will split this into its own stock-ledger table without changing this
    column's meaning for existing rows)."""

    __tablename__ = "mpe_produk"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    sku_induk: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    deskripsi: Mapped[str] = mapped_column(Text, nullable=False, default="")
    harga_dasar: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    stok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    foto_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    listing: Mapped[list["ProdukListing"]] = relationship(back_populates="produk")


class ProdukListing(MarketplaceErpBase):
    """Maps one Produk (SKU induk) to one listing on one AkunMarketplace
    shop. `id_eksternal` is the product id on the platform side, unique per
    (platform, id_eksternal) so re-sync upserts instead of duplicating.
    `harga_jual`/`stok_listing` are nullable overrides -- when NULL, the
    listing is meant to mirror Produk.harga_dasar/stok as-is; a value here
    means this one listing intentionally differs (e.g. platform-specific
    pricing)."""

    __tablename__ = "mpe_produk_listing"
    __table_args__ = (UniqueConstraint("platform", "id_eksternal", name="uq_mpe_listing_platform_eksternal"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    produk_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_produk.id", ondelete="CASCADE"), nullable=False, index=True
    )
    akun_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_akun_marketplace.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    id_eksternal: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    harga_jual: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True)
    stok_listing: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    produk: Mapped["Produk"] = relationship(back_populates="listing")
    akun: Mapped["AkunMarketplace"] = relationship(back_populates="listing")
