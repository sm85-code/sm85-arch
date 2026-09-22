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
    # Nullable: akun yang dibuat lewat "Masuk dengan Google" tidak punya
    # password kita sendiri -- login mereka selalu lewat verifikasi token
    # Google, bukan password_hash ini.
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Subject ID dari Google (klaim "sub" di ID token) -- dipakai untuk
    # menautkan akun Google ke UserToko secara stabil, bukan cuma
    # mengandalkan kecocokan email (email teknisnya bisa berubah pemilik).
    google_sub: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True, index=True)
    # admin_toko: kelola produk & pesanan. owner: full akses + laporan.
    # pembeli: akun customer (opsional -- checkout sebagai guest juga didukung
    # nanti di modul pesanan).
    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True, default="pembeli")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class KategoriToko(TokoBase):
    __tablename__ = "toko_kategori"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)


class ProdukToko(TokoBase):
    __tablename__ = "toko_produk"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    deskripsi: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kategori_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("toko_kategori.id", ondelete="SET NULL"), nullable=True, index=True
    )
    harga: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    stok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    foto_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    kategori: Mapped[Optional["KategoriToko"]] = relationship()


class AlamatToko(TokoBase):
    """Buku alamat pembeli -- boleh lebih dari satu per user, salah satu
    ditandai utama. Dipilih saat checkout alih-alih ketik ulang tiap kali;
    PengirimanToko tetap menyimpan salinannya sendiri sesuai alamat yang
    dipilih saat itu (lihat docstring PengirimanToko), jadi baris ini boleh
    diubah/dihapus tanpa memengaruhi riwayat pesanan lama."""

    __tablename__ = "toko_alamat"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("toko_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(64), nullable=False, default="Rumah")
    nama_penerima: Mapped[str] = mapped_column(String(255), nullable=False)
    telepon_penerima: Mapped[str] = mapped_column(String(32), nullable=False)
    alamat_lengkap: Mapped[str] = mapped_column(Text, nullable=False)
    kota: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    provinsi: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    kode_pos: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    utama: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


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


class PercakapanToko(TokoBase):
    """Satu thread chat per pembeli dengan admin toko -- pola support-chat
    sederhana (bukan multi-thread per topik). unread_admin/unread_pembeli
    dipakai buat badge notifikasi tanpa perlu hitung ulang semua pesan tiap
    kali."""

    __tablename__ = "toko_percakapan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("toko_users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    unread_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unread_pembeli: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    pembeli: Mapped["UserToko"] = relationship()
    pesan: Mapped[list["PesanChatToko"]] = relationship(back_populates="percakapan", order_by="PesanChatToko.created_at")


class PesanChatToko(TokoBase):
    __tablename__ = "toko_pesan_chat"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    percakapan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("toko_percakapan.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pengirim_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("toko_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Disnapshot langsung, bukan selalu join ke UserToko.role -- role pengirim
    # saat pesan dikirim harus tetap sama di riwayat chat walau role user
    # itu berubah belakangan (mis. admin_toko diturunkan jadi pembeli).
    pengirim_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    isi: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    percakapan: Mapped["PercakapanToko"] = relationship(back_populates="pesan")
