"""ORM models -- marketplace_erp tenant.

Tahap 1 (foundation): UserMarketplaceErp, AkunMarketplace, Produk (SKU induk),
ProdukListing.

Tahap 2 (this file also): stock reservation ledger + unified order inbox
(Pesanan/ItemPesanan). Multi-warehouse is minimal -- a single DEFAULT Gudang
row is enough; full multi-gudang allocation is deferred (see IDEAL_FOLLOWUPS).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, text
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
    # Login name: free text (letters, digits . _ -), unique, set by an admin. Always set by the app; the column is
    # nullable only so tables from before this column existed can be migrated (see seeder).
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True, index=True)
    # Optional contact email (also accepted at login). The owner of the account sets it in their profile.
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, unique=True, index=True)
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
    # When an automatic order sync last claimed this shop; throttles repeated refreshes of the order page.
    terakhir_sinkron_pesanan: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Incremental order sync: watermark = start of the last SUCCESSFUL sync (the next one only asks for orders
    # changed since then); sinkron_penuh = last time the whole 15-day window was re-read as a safety net.
    watermark_sinkron_pesanan: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    sinkron_penuh_pesanan_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
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
    # Shipping weight/size and lead time (ready stock = 2 days, pre-order = 3-14 days).
    berat_gram: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    panjang_cm: Mapped[Decimal] = mapped_column(Numeric(8, 1), nullable=False, default=Decimal("0"), server_default="0")
    lebar_cm: Mapped[Decimal] = mapped_column(Numeric(8, 1), nullable=False, default=Decimal("0"), server_default="0")
    tinggi_cm: Mapped[Decimal] = mapped_column(Numeric(8, 1), nullable=False, default=Decimal("0"), server_default="0")
    preorder: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    hari_proses: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")
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
    # Raw marketplace status (e.g. Shopee READY_TO_SHIP / PROCESSED). Set only for orders that follow the
    # marketplace (pulled by sync); NULL for orders typed in by hand. Lets the UI tell "needs processing"
    # from "arranged, waiting for the courier" -- both are status to_ship locally.
    status_marketplace: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # When the shipping label was last generated for this order and by whom (marks "already printed" so
    # the same parcel is not printed twice; can be set or cleared by hand).
    resi_dicetak_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resi_dicetak_oleh: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Pengiriman (Tahap 3) -- manual input for now (no courier API wired
    # yet); nullable/additive, added to existing Postgres DBs via
    # seeder.ensure_marketplace_erp_schema() self-heal ALTER TABLE, same
    # pattern as UserMarketplaceErp.must_change_password.
    kurir: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    nomor_resi: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tanggal_kirim: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the buyer placed the order on the marketplace (Shopee create_time). ``created_at`` is only when the ERP
    # first saw it, so reports and date filters use coalesce(dipesan_at, created_at).
    dipesan_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    detail_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
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
    model_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    item_sku: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    model_sku: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # Photo and Shopee ids of the line as the order came in: told apart at a glance when many product names look alike,
    # and used to find the catalogue entry of orders pulled before these were stored.
    foto_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    item_id_eksternal: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    model_id_eksternal: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    harga_satuan: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)

    @property
    def foto(self) -> Optional[str]:
        """The photo to show: the one stored with the line, else the one looked up in the catalogue (see
        services.lengkapi_foto_item)."""
        return self.foto_url or getattr(self, "_foto_tampil", None)

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


class SettlementPesanan(MarketplaceErpBase):
    """What Shopee released for one order (v2.payment.get_escrow_list + get_escrow_detail), one row per
    (shop, order number). Released escrow is final, so a stored row is not read again.

    ``jumlah_cair`` is Shopee's payout_amount from the list; the other money columns come from the
    detail's order_income (fees are positive costs, ``ongkir`` keeps Shopee's sign: negative = the seller
    bears it). ``rincian`` keeps the whole order_income (without item lines) for fields not mapped yet."""

    __tablename__ = "mpe_settlement_pesanan"
    __table_args__ = (UniqueConstraint("akun_id", "order_sn", name="uq_mpe_settlement_pesanan_akun_sn"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    akun_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_akun_marketplace.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_sn: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    dirilis_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    jumlah_cair: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    penjualan: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    voucher_penjual: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    komisi: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    layanan: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    transaksi: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    ongkir: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    subsidi_ongkir: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    penyesuaian: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    escrow: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    rincian: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    diambil_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ShopeePush(MarketplaceErpBase):
    """Log of every push Shopee sent to our webhook (accepted or not): proof that pushes arrive, and what to look at
    when one is rejected (``diagnosis`` holds the verification attempt, never the key). Old rows are pruned."""

    __tablename__ = "mpe_shopee_push"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    diterima_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    kode: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    jenis: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    shop_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    order_sn: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    hasil: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    catatan: Mapped[str] = mapped_column(Text, nullable=False, default="")
    badan: Mapped[str] = mapped_column(Text, nullable=False, default="")


class IklanHarianToko(MarketplaceErpBase):
    """Shopee Ads performance of one shop for one day (v2.ads.get_all_cpc_ads_daily_performance, shop level, all
    campaigns together). One row per (shop, day); pulling a day again refreshes it, since Shopee settles the
    7-day attribution figures (orders, GMV, ROAS) after the day."""

    __tablename__ = "mpe_iklan_harian_toko"
    __table_args__ = (UniqueConstraint("akun_id", "tanggal", name="uq_mpe_iklan_harian_toko_akun_tanggal"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    akun_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_akun_marketplace.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tanggal: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    impression: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    direct_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    broad_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    direct_item_sold: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    broad_item_sold: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    direct_gmv: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    broad_gmv: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    expense: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    diambil_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class IklanSaldoToko(MarketplaceErpBase):
    """Latest Shopee Ads credit balance of a shop (v2.ads.get_total_balance, paid + free credits)."""

    __tablename__ = "mpe_iklan_saldo_toko"

    akun_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_akun_marketplace.id", ondelete="CASCADE"), primary_key=True
    )
    saldo: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    data_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    diambil_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ModalProduk(MarketplaceErpBase):
    """Seller's cost price (modal) of one Shopee item, as Rp per unit OR as a percentage of the selling price (never
    both). Used to judge whether an ad pays for itself (break-even ROAS)."""

    __tablename__ = "mpe_modal_produk"

    akun_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_akun_marketplace.id", ondelete="CASCADE"), primary_key=True
    )
    item_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    modal_rp: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True)
    modal_persen: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    updated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class SaranIklanAi(MarketplaceErpBase):
    """One run of the AI ads advisor: who asked, for which shop, and what it cost (also the daily quota counter)."""

    __tablename__ = "mpe_saran_iklan_ai"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    akun_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("mpe_akun_marketplace.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    token_masuk: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_keluar: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    biaya_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"))
    jumlah_saran: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


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


class KatalogShopee(MarketplaceErpBase):
    """Snapshot of one Shopee item as pulled from a shop (one row per item, variants kept as JSON).

    Read-only reference for picking what goes to the online store: pulling it never touches Shopee
    and never creates Produk / stock. ``dikirim_toko_id`` is the id of the store copy once sent.
    """

    __tablename__ = "mpe_katalog_shopee"
    __table_args__ = (UniqueConstraint("akun_id", "item_id", name="uq_mpe_katalog_akun_item"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    akun_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mpe_akun_marketplace.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[str] = mapped_column(String(32), nullable=False)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    sku: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    deskripsi: Mapped[str] = mapped_column(Text, nullable=False, default="")
    foto_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    varian_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    detail_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    harga_min: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True)
    harga_max: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True)
    stok_shopee: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    berat_gram: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    panjang_cm: Mapped[Decimal] = mapped_column(Numeric(8, 1), nullable=False, default=Decimal("0"))
    lebar_cm: Mapped[Decimal] = mapped_column(Numeric(8, 1), nullable=False, default=Decimal("0"))
    tinggi_cm: Mapped[Decimal] = mapped_column(Numeric(8, 1), nullable=False, default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="NORMAL")
    diambil_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    dikirim_toko_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    dikirim_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
