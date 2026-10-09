"""Isolated ORM models for the store (online shop) database.

Two sub-tenants share this one database but never share an account table:
``store_admin_users`` (admin panel) and ``store_buyer_users`` (storefront).
A buyer account can therefore never be promoted to an admin by role
confusion, and the Google login (buyers only) can never touch an admin row.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, false, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tenants.store.modules.store.infrastructure.database import StoreBase


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# owner: full access incl. staff management. admin: everything else in the
# admin panel. Only an owner can create/edit/delete admin accounts.
ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ADMIN_ROLES_STORE = (ROLE_OWNER, ROLE_ADMIN)


class AdminStore(StoreBase):
    __tablename__ = "store_admin_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True, default=ROLE_ADMIN)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PembeliStore(StoreBase):
    __tablename__ = "store_buyer_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    # Nullable: accounts created through "Masuk dengan Google" have no
    # password of our own -- they always log in by verifying a Google token.
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    google_sub: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


METODE_PROSES_PESANAN = ("pickup", "drop_off")


class PengaturanStore(StoreBase):
    """Single-row global settings (fixed id => at most one row)."""

    __tablename__ = "store_pengaturan"

    SINGLETON_ID = "global"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: PengaturanStore.SINGLETON_ID)
    metode_proses_pesanan: Mapped[str] = mapped_column(String(16), nullable=False, default="drop_off")
    # Shipping: couriers offered at checkout (comma separated Biteship codes) and the pickup address.
    # Empty values fall back to the BITESHIP_* environment variables.
    kurir_aktif: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    asal_nama: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    asal_telepon: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default="")
    asal_alamat: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    asal_kode_pos: Mapped[str] = mapped_column(String(8), nullable=False, default="", server_default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class KategoriStore(StoreBase):
    __tablename__ = "store_kategori"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)


SUMBER_PRODUK = ("manual", "erp")


class ProdukStore(StoreBase):
    __tablename__ = "store_produk"
    __table_args__ = (CheckConstraint("stok >= 0", name="ck_store_produk_stok_nonneg"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    # Public URL segment (/produk/<slug>). Stays the same when the product is renamed, so links
    # and search rankings survive; unique, filled by services.unique_slug (and backfilled at startup).
    slug: Mapped[Optional[str]] = mapped_column(String(160), unique=True, nullable=True, index=True)
    deskripsi: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kategori_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("store_kategori.id", ondelete="SET NULL"), nullable=True, index=True
    )
    harga: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    stok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Object key in the media bucket (never a full URL) -- the public URL is
    # built from MEDIA_BASE_URL (see infrastructure/media_storage.py), so
    # moving storage or domain is a one-env-var change, not a data migration.
    foto_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Provenance: "manual" (uploaded here) or "erp" (copied from the
    # marketplace_erp tenant). erp_produk_id / platform_asal are SOFT
    # references to the other database (no foreign key across databases);
    # they are NULL for manual products. Stock is store-owned either way
    # (no live link to the ERP ledger).
    sumber: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    erp_produk_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    platform_asal: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # Actual weight (grams) and packed size (cm) of one piece; a variant may override them. Courier APIs price on these.
    berat_gram: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    panjang_cm: Mapped[Decimal] = mapped_column(Numeric(8, 1), nullable=False, default=Decimal("0"), server_default="0")
    lebar_cm: Mapped[Decimal] = mapped_column(Numeric(8, 1), nullable=False, default=Decimal("0"), server_default="0")
    tinggi_cm: Mapped[Decimal] = mapped_column(Numeric(8, 1), nullable=False, default=Decimal("0"), server_default="0")
    # Ready stock ships within 2 days; a pre-order product says how many days (3-14) it takes to make/obtain.
    preorder: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    cod: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    hari_proses: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    kategori: Mapped[Optional["KategoriStore"]] = relationship()
    foto: Mapped[list["FotoProduk"]] = relationship(
        back_populates="produk", order_by="FotoProduk.urutan", cascade="all, delete-orphan"
    )
    varian: Mapped[list["VarianProduk"]] = relationship(
        back_populates="produk", order_by="VarianProduk.urutan", cascade="all, delete-orphan"
    )


class FotoProduk(StoreBase):
    """Gallery photo of a product (at most 7). The first one (urutan 0) is the cover, mirrored in ProdukStore.foto_key."""

    __tablename__ = "store_produk_foto"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    produk_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("store_produk.id", ondelete="CASCADE"), nullable=False, index=True
    )
    foto_key: Mapped[str] = mapped_column(String(512), nullable=False)
    urutan: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    produk: Mapped["ProdukStore"] = relationship(back_populates="foto")


class VarianProduk(StoreBase):
    """Optional variant of a product (colour, size, ...): its own price, stock, weight and size.
    Empty price/weight/size fall back to the product's. A product with variants is bought by picking one."""

    __tablename__ = "store_produk_varian"
    __table_args__ = (
        CheckConstraint("stok >= 0", name="ck_store_varian_stok_nonneg"),
        UniqueConstraint("produk_id", "nama", name="uq_store_varian_nama"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    produk_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("store_produk.id", ondelete="CASCADE"), nullable=False, index=True
    )
    nama: Mapped[str] = mapped_column(String(120), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    opsi_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    harga: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), nullable=True)
    stok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    berat_gram: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    panjang_cm: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 1), nullable=True)
    lebar_cm: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 1), nullable=True)
    tinggi_cm: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 1), nullable=True)
    foto_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("store_produk_foto.id", ondelete="SET NULL"), nullable=True
    )
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    urutan: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    produk: Mapped["ProdukStore"] = relationship(back_populates="varian")


class AlamatStore(StoreBase):
    """Buyer address book. PengirimanStore keeps its own snapshot, so rows
    here can change or disappear without touching old orders."""

    __tablename__ = "store_alamat"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("store_buyer_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(64), nullable=False, default="Rumah")
    nama_penerima: Mapped[str] = mapped_column(String(255), nullable=False)
    telepon_penerima: Mapped[str] = mapped_column(String(32), nullable=False)
    alamat_lengkap: Mapped[str] = mapped_column(Text, nullable=False)
    kota: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    provinsi: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    kode_pos: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    # Kecamatan / desa-kelurahan chosen from the region lists, and the Kemendagri code of the village
    # (e.g. 32.73.01.1001) -- what a courier API needs to price and route a parcel.
    kecamatan: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    kelurahan: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    kode_wilayah: Mapped[str] = mapped_column(String(16), nullable=False, default="", server_default="")
    utama: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ItemKeranjang(StoreBase):
    """One row per (buyer, produk, variant) in the cart; varian_id is NULL for products without variants."""

    __tablename__ = "store_item_keranjang"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("store_buyer_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    produk_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("store_produk.id", ondelete="CASCADE"), nullable=False, index=True
    )
    varian_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("store_produk_varian.id", ondelete="CASCADE"), nullable=True, index=True
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    produk: Mapped["ProdukStore"] = relationship()
    varian: Mapped[Optional["VarianProduk"]] = relationship()


# NULL varian_id must still be unique per (buyer, product): COALESCE makes NULLs comparable.
Index(
    "uq_store_keranjang_line",
    ItemKeranjang.user_id,
    ItemKeranjang.produk_id,
    func.coalesce(ItemKeranjang.varian_id, ""),
    unique=True,
)


# menunggu_pembayaran -> dibayar -> diproses -> dikirim -> selesai, or
# dibatalkan from menunggu_pembayaran/dibayar.
STATUS_PESANAN = (
    "menunggu_konfirmasi",  # COD order waiting for the seller to confirm
    "menunggu_pembayaran",
    "dibayar",
    "diproses",
    "dikirim",
    "selesai",
    "dibatalkan",
)


class PesananStore(StoreBase):
    __tablename__ = "store_pesanan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("store_buyer_users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="menunggu_pembayaran", index=True)
    total: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    metode_pembayaran: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    payment_state: Mapped[str] = mapped_column(String(32), nullable=False, default="idle", server_default="idle")
    checkout_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gateway_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    items: Mapped[list["ItemPesanan"]] = relationship(back_populates="pesanan")


class ItemPesanan(StoreBase):
    """Snapshot of product name/price at order time."""

    __tablename__ = "store_item_pesanan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    pesanan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("store_pesanan.id", ondelete="CASCADE"), nullable=False, index=True
    )
    produk_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("store_produk.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    nama_produk: Mapped[str] = mapped_column(String(255), nullable=False)
    harga_satuan: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    # Variant snapshot (the variant row may be deleted later) and the lead time promised at order time.
    varian_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("store_produk_varian.id", ondelete="SET NULL"), nullable=True
    )
    nama_varian: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="")
    preorder: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    hari_proses: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")

    pesanan: Mapped["PesananStore"] = relationship(back_populates="items")


STATUS_PENGIRIMAN = ("menunggu_pickup", "dikirim", "diterima", "bermasalah")


class PengirimanStore(StoreBase):
    """1:1 with PesananStore. Recipient/address are snapshotted here."""

    __tablename__ = "store_pengiriman"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    pesanan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("store_pesanan.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    kurir: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    layanan: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    layanan_nama: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    ongkir: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    biaya_cod: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"), server_default="0")
    nama_penerima: Mapped[str] = mapped_column(String(255), nullable=False)
    telepon_penerima: Mapped[str] = mapped_column(String(32), nullable=False)
    alamat_tujuan: Mapped[str] = mapped_column(Text, nullable=False)
    kota_tujuan: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    provinsi_tujuan: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    kode_pos_tujuan: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    kecamatan_tujuan: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    kelurahan_tujuan: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    kode_wilayah_tujuan: Mapped[str] = mapped_column(String(16), nullable=False, default="", server_default="")
    tracking_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Biteship order created from the admin: its id and the tracking id used to follow the parcel.
    booking_state: Mapped[str] = mapped_column(String(32), nullable=False, default="idle", server_default="idle")
    biteship_order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    biteship_tracking_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="menunggu_pickup", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    pesanan: Mapped["PesananStore"] = relationship()


class PercakapanStore(StoreBase):
    """One support-chat thread per buyer."""

    __tablename__ = "store_percakapan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("store_buyer_users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    unread_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unread_pembeli: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    pembeli: Mapped["PembeliStore"] = relationship()
    pesan: Mapped[list["PesanChatStore"]] = relationship(back_populates="percakapan", order_by="PesanChatStore.created_at")


class PesanChatStore(StoreBase):
    __tablename__ = "store_pesan_chat"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    percakapan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("store_percakapan.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Sender id points into store_admin_users or store_buyer_users depending
    # on pengirim_admin, so it is deliberately not a foreign key.
    pengirim_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pengirim_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    isi: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Optional attachment (image/video object key under chat/) and a shared product card.
    lampiran_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    lampiran_jenis: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    produk_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("store_produk.id", ondelete="SET NULL"), nullable=True
    )
    pesanan_id: Mapped[Optional[str]] = mapped_column(String(64), ForeignKey("store_pesanan.id", ondelete="SET NULL"), nullable=True)
    pesanan: Mapped[Optional["PesananStore"]] = relationship()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    percakapan: Mapped["PercakapanStore"] = relationship(back_populates="pesan")
    produk: Mapped[Optional["ProdukStore"]] = relationship()


class MediaCleanup(StoreBase):
    __tablename__ = "store_media_cleanup"
    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
