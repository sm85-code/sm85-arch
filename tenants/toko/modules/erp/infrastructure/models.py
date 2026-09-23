"""ORM models for the toko-erp (marketplace ERP) submodule.

Second capability of the `toko` tenant, alongside the web storefront in
`tenants/toko/modules/toko/`. This submodule aggregates orders, chats and a
product catalog synced (one-way, in a follow-up step) from Shopee, Lazada
and Blibli seller-center exports/APIs -- it is NOT a live connection to
those platforms yet, see infrastructure/erp_shopee.py, erp_lazada.py and
erp_blibli.py.

Reuses the toko web module's isolated database (TokoBase / DATABASE_URL_TOKO,
see tenants/toko/modules/toko/infrastructure/database.py) rather than
standing up a third Postgres instance for this tenant: both capabilities
belong to the same "toko" tenant/business and a shop owner logging into one
backend should not need a second DB connection string. All tables here are
still fully separate (own `toko_erp_*` tables, own primary keys) from the
web module's tables -- the only relationship between the two schemas is the
one explicit, additive FK described on ProdukToko.sumber_erp_produk_id in
tenants/toko/modules/toko/infrastructure/models.py.
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


# Marketplace platform yang didukung. String enum sederhana (bukan Postgres
# ENUM type) supaya menambah platform baru nanti cukup tambah nilai di sini,
# tanpa migrasi ALTER TYPE.
PLATFORM_ERP = ("shopee", "lazada", "blibli")


STATUS_AKUN_ERP = ("aktif", "nonaktif")


class AkunMarketplace(TokoBase):
    """Satu baris = satu toko/shop yang sudah (atau sedang proses)
    terautentikasi di satu platform marketplace. Diperkenalkan karena
    admin toko bisa punya BEBERAPA toko pada platform yang sama (mis. 3
    toko Shopee) -- `platform` saja tidak cukup untuk membedakan baris
    ProdukERP/PesananERP/PercakapanERP milik toko yang mana, maka semua
    baris itu sekarang juga membawa `akun_id` yang menunjuk ke sini.

    Kredensial (access_token/refresh_token/token_kedaluwarsa): disimpan
    sebagai kolom biasa (bukan terenkripsi) karena DB toko ini sudah
    terisolasi/dedicated untuk tenant ini (lihat database.py) -- enkripsi
    at-rest di luar scope task ini. Yang WAJIB dijaga: kolom-kolom ini
    TIDAK boleh ikut ke response list/summary manapun secara default,
    lihat services.akun_out() (list/summary) vs services.akun_out_detail()
    (satu-satunya tempat token bisa muncul, dan itupun di-mask). Endpoint
    yang mengisi kredensial ini untuk sekarang adalah PATCH admin biasa
    (lihat erp_router.py) -- bukan callback OAuth sungguhan, karena
    aplikasi partner Shopee/Lazada/Blibli belum ada (lihat
    infrastructure/erp_<platform>.py); OAuth beneran adalah pekerjaan
    lanjutan begitu partner_id/partner_key/app_key nyata sudah ada.
    """

    __tablename__ = "toko_erp_akun"
    __table_args__ = (
        # Partial unique constraint: (platform, id_toko_eksternal) unik
        # HANYA saat id_toko_eksternal terisi (akun yang belum diotorisasi
        # / belum tahu shop id-nya boleh punya id_toko_eksternal NULL
        # lebih dari satu baris -- dicegah menduplikasi toko yang SAMA di
        # platform yang sama, bukan mencegah banyak akun "belum
        # terhubung"). SQLite (dipakai di tests) tidak mendukung partial
        # unique index lewat UniqueConstraint biasa, jadi ini didukung
        # dengan pengecekan duplikat di service layer
        # (services.create_akun_marketplace) -- constraint DB di sini
        # sengaja tidak ditulis sebagai partial unique index untuk
        # menghindari perbedaan perilaku SQLite vs Postgres; lihat
        # services.py untuk penjelasan lebih lanjut.
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    platform: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    nama_toko: Mapped[str] = mapped_column(String(255), nullable=False)
    id_toko_eksternal: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    access_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_kedaluwarsa: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="aktif")
    catatan: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class ProdukERP(TokoBase):
    """Snapshot produk dari seller-center marketplace. `id_eksternal` adalah
    ID produk di platform asal -- dipakai bareng `platform` sebagai kunci
    idempoten saat sinkronisasi (upsert on (platform, id_eksternal)) supaya
    sync ulang tidak menduplikasi baris."""

    __tablename__ = "toko_erp_produk"
    __table_args__ = (UniqueConstraint("platform", "id_eksternal", name="uq_erp_produk_platform_eksternal"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    platform: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    id_eksternal: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Nullable karena baris lama (sebelum AkunMarketplace ada) atau baris
    # yang dibuat tanpa memilih akun tidak boleh rusak -- tapi create_*
    # baru di service layer mewajibkan akun_id (lihat services.py).
    akun_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("toko_erp_akun.id", ondelete="SET NULL"), nullable=True, index=True
    )
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    deskripsi: Mapped[str] = mapped_column(Text, nullable=False, default="")
    harga: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    stok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    foto_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


# Status pesanan marketplace dinormalisasi ke 5 nilai generik yang sama
# untuk ketiga platform, alih-alih memakai istilah asli tiap platform
# (mis. Shopee "UNPAID"/"TO_SHIP"/"SHIPPED"/"COMPLETED"/"CANCELLED", Lazada
# "pending"/"ready_to_ship"/"shipped"/"delivered", Blibli "NEW"/"PROCESS"/
# dst). Alasan: laporan & filter status di satu inbox admin harus bisa
# lintas-platform tanpa if/else per platform di tiap tempat yang membaca
# status -- pemetaan istilah-asli-ke-status-generik ini jadi satu-satunya
# tempat yang perlu disentuh kalau ada platform baru atau istilah baru,
# lihat masing-masing infrastructure/erp_<platform>.py.
STATUS_PESANAN_ERP = ("unpaid", "to_ship", "shipped", "completed", "cancelled")


class PesananERP(TokoBase):
    __tablename__ = "toko_erp_pesanan"
    __table_args__ = (UniqueConstraint("platform", "id_eksternal", name="uq_erp_pesanan_platform_eksternal"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    platform: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    id_eksternal: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    akun_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("toko_erp_akun.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unpaid", index=True)
    nama_pembeli: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    total: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    # One-way local->marketplace sync attempt outcome, recorded on every
    # status-change attempt that pushes to the platform adapter (currently
    # only the transition to "to_ship" -- see services.ubah_status_pesanan_erp
    # and infrastructure/erp_<platform>.py::proses_pesanan). Nullable/
    # additive; rows never pushed (or created before this field existed)
    # stay at the default. tersinkron_marketplace is only ever True when the
    # adapter call actually completed without raising -- both the
    # "not configured" (503) and "not implemented" adapter paths, and any
    # other exception, are recorded as a failed attempt (False) with the
    # reason in catatan_sinkron so the local status update is never blocked
    # by the marketplace push not being wired up yet.
    tersinkron_marketplace: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    catatan_sinkron: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    items: Mapped[list["ItemPesananERP"]] = relationship(back_populates="pesanan")
    akun: Mapped[Optional["AkunMarketplace"]] = relationship()


class ItemPesananERP(TokoBase):
    """Snapshot nama/harga item pada saat sinkronisasi -- sama seperti
    ItemPesanan (toko web), tidak mengikuti perubahan ProdukERP belakangan."""

    __tablename__ = "toko_erp_item_pesanan"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    pesanan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("toko_erp_pesanan.id", ondelete="CASCADE"), nullable=False, index=True
    )
    produk_erp_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("toko_erp_produk.id", ondelete="SET NULL"), nullable=True, index=True
    )
    nama_produk: Mapped[str] = mapped_column(String(255), nullable=False)
    harga_satuan: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)

    pesanan: Mapped["PesananERP"] = relationship(back_populates="items")


class PercakapanERP(TokoBase):
    """Satu thread chat pembeli-marketplace per (platform, id_eksternal
    pembeli) -- terpisah total dari PercakapanToko (chat toko web), supaya
    inbox pembeli web tidak tercampur dengan inbox pembeli marketplace.
    Inbox gabungan lintas-3-platform di admin adalah query yang filter
    kolom `platform`, bukan tabel gabungan -- lihat services.list_percakapan."""

    __tablename__ = "toko_erp_percakapan"
    __table_args__ = (
        UniqueConstraint("platform", "id_eksternal_pembeli", name="uq_erp_percakapan_platform_pembeli"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    platform: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    akun_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("toko_erp_akun.id", ondelete="SET NULL"), nullable=True, index=True
    )
    id_eksternal_pembeli: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    nama_pembeli: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    unread_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    pesan: Mapped[list["PesanChatERP"]] = relationship(back_populates="percakapan", order_by="PesanChatERP.created_at")


class PesanChatERP(TokoBase):
    __tablename__ = "toko_erp_pesan_chat"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    percakapan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("toko_erp_percakapan.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Pesan dari admin dikirim lewat akun kita (pengirim_admin=True, tanpa
    # user_id -- admin toko login lewat UserToko tapi pesan marketplace
    # tidak perlu menautkan ke baris itu, cukup role saat kirim, sama
    # seperti pengirim_admin di PesanChatToko). Pesan dari pembeli
    # marketplace tidak punya User account di sistem kita sama sekali.
    pengirim_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    isi: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    percakapan: Mapped["PercakapanERP"] = relationship(back_populates="pesan")
