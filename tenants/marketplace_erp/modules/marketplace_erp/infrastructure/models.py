"""ORM models -- marketplace_erp tenant.

Tahap 1 (foundation): UserMarketplaceErp, AkunMarketplace, Produk (SKU induk),
ProdukListing.

Tahap 2 (this file also): stock reservation ledger + unified order inbox
(Pesanan/ItemPesanan). Multi-warehouse is minimal -- a single DEFAULT Gudang
row is enough; full multi-gudang allocation is deferred (see IDEAL_FOLLOWUPS).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tenants.marketplace_erp.modules.marketplace_erp.infrastructure.database import MarketplaceErpBase


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Plain string enums (not Postgres ENUM) so adding a value later is a one-line
# change with no ALTER TYPE migration -- same reasoning as tenants/toko ERP.
PLATFORM_MARKETPLACE = ("shopee", "tiktokshop", "lazada", "blibli")

STATUS_PESANAN = ("unpaid", "to_ship", "shipped", "completed", "cancelled")

STATUS_RESERVASI = ("aktif", "released", "consumed")

REASON_STOK_LEDGER = ("adjust", "reserve", "release", "ship", "return", "sync_in", "transfer_in", "transfer_out")

DEFAULT_GUDANG_KODE = "DEFAULT"

STATUS_SETTLEMENT = ("draft", "matched", "discrepancy", "paid")


class UserMarketplaceErp(MarketplaceErpBase):
    """Admin/staff login for this tenant. Own table, own JWT cookie --
    zero relation to tenants/toko's UserToko."""

    __tablename__ = "mpe_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="owner")
    # Added after Tahap 2: existing Postgres databases get this column via
    # seeder.ensure_marketplace_erp_schema() (ALTER TABLE ... ADD COLUMN IF
    # NOT EXISTS) because create_all never alters an existing table.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class AkunMarketplace(MarketplaceErpBase):
    """One row = one shop authorized (or pending) on one platform.
    Credentials stay as plain columns; never return tokens in list/summary."""

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
    pesanan: Mapped[list["Pesanan"]] = relationship(back_populates="akun")


class Produk(MarketplaceErpBase):
    """SKU induk. `stok` is the available-quantity cache, updated atomically
    with StokReservasi / StokLedger (Tahap 2). Physical on-hand = stok +
    sum(aktif reservations)."""

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
    ledger: Mapped[list["StokLedger"]] = relationship(back_populates="produk")
    reservasi: Mapped[list["StokReservasi"]] = relationship(back_populates="produk")


class ProdukListing(MarketplaceErpBase):
    """Maps one Produk to one listing on one AkunMarketplace shop."""

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


# --- Tahap 2: warehouse / stock / orders ------------------------------------


class Gudang(MarketplaceErpBase):
    """Warehouse. Tahap 2 ships a single DEFAULT row; multi-gudang allocation
    (transfers, per-warehouse reservation) is deferred."""

    __tablename__ = "mpe_gudang"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    kode: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    ledger: Mapped[list["StokLedger"]] = relationship(back_populates="gudang")
    reservasi: Mapped[list["StokReservasi"]] = relationship(back_populates="gudang")


class StokLedger(MarketplaceErpBase):
    """Append-only stock movement. qty_delta is signed: negative for reserve/
    ship outbound, positive for release/adjust-in/return/sync_in."""

    __tablename__ = "mpe_stok_ledger"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    produk_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_produk.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gudang_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("mpe_gudang.id", ondelete="SET NULL"), nullable=True, index=True
    )
    qty_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    ref_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ref_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    catatan: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    produk: Mapped["Produk"] = relationship(back_populates="ledger")
    gudang: Mapped[Optional["Gudang"]] = relationship(back_populates="ledger")


class StokReservasi(MarketplaceErpBase):
    """Qty held for a pending order (typically status to_ship) until ship
    (consumed) or cancel (released). Prevents oversell across shops."""

    __tablename__ = "mpe_stok_reservasi"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    produk_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_produk.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gudang_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("mpe_gudang.id", ondelete="SET NULL"), nullable=True, index=True
    )
    pesanan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_pesanan.id", ondelete="CASCADE"), nullable=False, index=True
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="aktif", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    produk: Mapped["Produk"] = relationship(back_populates="reservasi")
    gudang: Mapped[Optional["Gudang"]] = relationship(back_populates="reservasi")
    pesanan: Mapped["Pesanan"] = relationship(back_populates="reservasi")


class Pesanan(MarketplaceErpBase):
    """Unified OMS inbox row. Upsert key: (platform, id_eksternal). Status is
    normalized across platforms; platform-native mapping lives in adapters."""

    __tablename__ = "mpe_pesanan"
    __table_args__ = (UniqueConstraint("platform", "id_eksternal", name="uq_mpe_pesanan_platform_eksternal"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    platform: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    id_eksternal: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    akun_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("mpe_akun_marketplace.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unpaid", index=True)
    nama_pembeli: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    total: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    tersinkron_marketplace: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    catatan_sinkron: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Pengiriman (Tahap 3) -- manual input for now (no courier API wired
    # yet); nullable/additive, added to existing Postgres DBs via
    # seeder.ensure_marketplace_erp_schema() self-heal ALTER TABLE, same
    # pattern as UserMarketplaceErp.must_change_password.
    kurir: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    nomor_resi: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tanggal_kirim: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    items: Mapped[list["ItemPesanan"]] = relationship(back_populates="pesanan")
    reservasi: Mapped[list["StokReservasi"]] = relationship(back_populates="pesanan")
    akun: Mapped[Optional["AkunMarketplace"]] = relationship(back_populates="pesanan")


class ItemPesanan(MarketplaceErpBase):
    """Line snapshot at ingest time. Optional FK to Produk / ProdukListing
    for stock reservation; null when the line has no mapped SKU yet."""

    __tablename__ = "mpe_item_pesanan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    pesanan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_pesanan.id", ondelete="CASCADE"), nullable=False, index=True
    )
    produk_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("mpe_produk.id", ondelete="SET NULL"), nullable=True, index=True
    )
    listing_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("mpe_produk_listing.id", ondelete="SET NULL"), nullable=True, index=True
    )
    nama_produk: Mapped[str] = mapped_column(String(255), nullable=False)
    harga_satuan: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)

    pesanan: Mapped["Pesanan"] = relationship(back_populates="items")


# --- Tahap 3: staff scoping, settlement -------------------------------------


class StaffAkunMarketplace(MarketplaceErpBase):
    """Many-to-many link: which AkunMarketplace (shop) rows a `staff`-role
    UserMarketplaceErp is allowed to touch. Owner is never restricted (see
    infrastructure/auth.py::akun_ids_diizinkan) -- this table only matters
    for the `staff` role. One row per (user, akun) pair, same pattern as
    tenants/toko's toko_staff_akun."""

    __tablename__ = "mpe_staff_akun"
    __table_args__ = (UniqueConstraint("user_id", "akun_id", name="uq_mpe_staff_akun_user_akun"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    akun_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_akun_marketplace.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Settlement(MarketplaceErpBase):
    """One payout/statement batch from a marketplace platform for one shop
    over one period. Entered manually for now (no platform statement API
    wired yet) -- purpose is reconciling "money that actually landed" vs
    local order data, which is the #1 thing owners ask an ERP for once
    orders start flowing from a real platform.

    `status` starts `draft`. `discrepancy` is set automatically by
    application/services.py when `net` doesn't reconcile against
    gross_sales - fees within a small epsilon -- callers can still set
    `matched`/`paid` explicitly once reviewed."""

    __tablename__ = "mpe_settlement"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    akun_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_akun_marketplace.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    periode_mulai: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    periode_selesai: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    gross_sales: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    fee_platform: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    fee_payment: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    ongkir_subsidi: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    penalti: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    net: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    catatan: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    akun: Mapped["AkunMarketplace"] = relationship()


# --- Tahap 4: iklan (ads) ----------------------------------------------------

STATUS_IKLAN = ("draft", "aktif", "dijeda", "selesai")


class IklanCampaign(MarketplaceErpBase):
    """One ad campaign on one shop/platform. Spend is entered manually per
    day (`IklanMetrikHarian`) -- no ads API is wired yet (Shopee Ads/TikTok
    Ads/Lazada Sponsored Discovery/Blibli Ads all require their own,
    separate partner approval from the shop OAuth this tenant already has).

    Optional `produk_id` is what turns this from "a spend tracker" into an
    ERP feature: linking a campaign to the SKU it promotes lets
    application/services.py compute ROAS from *actual* Pesanan/ItemPesanan
    data for that produk in the campaign's window, not a number the owner
    has to enter by hand."""

    __tablename__ = "mpe_iklan_campaign"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    akun_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_akun_marketplace.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    produk_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("mpe_produk.id", ondelete="SET NULL"), nullable=True, index=True
    )
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    budget_harian: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    tanggal_mulai: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tanggal_selesai: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    catatan: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    akun: Mapped["AkunMarketplace"] = relationship()
    produk: Mapped[Optional["Produk"]] = relationship()
    metrik: Mapped[list["IklanMetrikHarian"]] = relationship(back_populates="campaign")


class IklanMetrikHarian(MarketplaceErpBase):
    """One row per (campaign, tanggal) -- manual daily entry of impression/
    klik/biaya from the platform's own ads dashboard. Upsert on that pair so
    re-entering the same day corrects it instead of duplicating."""

    __tablename__ = "mpe_iklan_metrik_harian"
    __table_args__ = (UniqueConstraint("campaign_id", "tanggal", name="uq_mpe_iklan_metrik_campaign_tanggal"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_iklan_campaign.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tanggal: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    impression: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    klik: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    biaya: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    campaign: Mapped["IklanCampaign"] = relationship(back_populates="metrik")
