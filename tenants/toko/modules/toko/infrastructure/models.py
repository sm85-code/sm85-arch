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

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

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


class ItemKeranjang(TokoBase):
    """One row per (user, produk) in a user's cart. No separate 'Keranjang'
    header table -- the cart is just "all ItemKeranjang rows for this user",
    same simplification most small shop backends use."""

    __tablename__ = "toko_item_keranjang"
    __table_args__ = (UniqueConstraint("user_id", "produk_id", name="uq_keranjang_user_produk"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("toko_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    produk_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("toko_produk.id", ondelete="CASCADE"), nullable=False, index=True
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    produk: Mapped["ProdukToko"] = relationship()


# Status berjenjang: menunggu_pembayaran -> dibayar -> diproses -> dikirim ->
# selesai, atau dibatalkan dari menunggu_pembayaran/dibayar.
STATUS_PESANAN = (
    "menunggu_pembayaran",
    "dibayar",
    "diproses",
    "dikirim",
    "selesai",
    "dibatalkan",
)


class PesananToko(TokoBase):
    __tablename__ = "toko_pesanan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("toko_users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="menunggu_pembayaran", index=True)
    total: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    # Diisi oleh modul pembayaran nanti (metode: tunai/gateway, gateway_ref).
    # Kolom disiapkan sekarang supaya migrasi status pesanan tidak perlu
    # menunggu modul pembayaran selesai dibangun.
    metode_pembayaran: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    gateway_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    items: Mapped[list["ItemPesanan"]] = relationship(back_populates="pesanan")


class ItemPesanan(TokoBase):
    """Snapshot of product name/price at order time -- must not follow
    ProdukToko.harga if the price changes later."""

    __tablename__ = "toko_item_pesanan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    pesanan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("toko_pesanan.id", ondelete="CASCADE"), nullable=False, index=True
    )
    produk_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("toko_produk.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    nama_produk: Mapped[str] = mapped_column(String(255), nullable=False)
    harga_satuan: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)

    pesanan: Mapped["PesananToko"] = relationship(back_populates="items")


# Status pengiriman: menunggu_pickup -> dikirim -> diterima (atau
# bermasalah, untuk kasus retur/gagal antar).
STATUS_PENGIRIMAN = ("menunggu_pickup", "dikirim", "diterima", "bermasalah")


class PengirimanToko(TokoBase):
    """1:1 dengan PesananToko. Alamat & data penerima disnapshot di sini
    (bukan referensi ke profil user) supaya perubahan alamat user nanti
    tidak mengubah riwayat pengiriman pesanan lama."""

    __tablename__ = "toko_pengiriman"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    pesanan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("toko_pesanan.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    kurir: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    layanan: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    ongkir: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    nama_penerima: Mapped[str] = mapped_column(String(255), nullable=False)
    telepon_penerima: Mapped[str] = mapped_column(String(32), nullable=False)
    alamat_tujuan: Mapped[str] = mapped_column(Text, nullable=False)
    kota_tujuan: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    provinsi_tujuan: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    kode_pos_tujuan: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    # Diisi setelah Biteship benar-benar assign kurir (lihat
    # infrastructure/shipping_biteship.py -- belum terhubung ke API asli).
    tracking_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="menunggu_pickup", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    pesanan: Mapped["PesananToko"] = relationship()
