"""Additive keu_* ledger. Existing BUMI identities/categories stay in this database."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import JSON, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, false, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.database import BumiLestariBase

JSON_DATA = JSON().with_variant(JSONB(), "postgresql")


def new_id() -> str:
    return str(uuid4())


def pk():
    return mapped_column(String(64), primary_key=True, default=new_id)


def created():
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class KeuSaluran(BumiLestariBase):
    __tablename__ = "keu_saluran"
    __table_args__ = (
        UniqueConstraint("sistem", "akun_ref", name="uq_keu_saluran_sumber"),
        CheckConstraint("sistem IN ('store', 'marketplace_erp', 'manual')", name="ck_keu_saluran_sistem"),
        CheckConstraint("length(trim(akun_ref)) > 0", name="ck_keu_saluran_ref"),
    )
    id: Mapped[str] = pk()
    nama: Mapped[str] = mapped_column(String(128), nullable=False)
    sistem: Mapped[str] = mapped_column(String(32), nullable=False)
    akun_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    created_at: Mapped[datetime] = created()


class KeuAkun(BumiLestariBase):
    __tablename__ = "keu_akun"
    __table_args__ = (CheckConstraint("jenis IN ('kas', 'bank', 'ewallet')", name="ck_keu_akun_jenis"),)
    id: Mapped[str] = pk()
    kode: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    nama: Mapped[str] = mapped_column(String(128), nullable=False)
    jenis: Mapped[str] = mapped_column(String(16), nullable=False)
    saldo_awal: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"), server_default="0")
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class KeuPelanggan(BumiLestariBase):
    __tablename__ = "keu_pelanggan"
    __table_args__ = (CheckConstraint("segmen IS NULL OR segmen IN ('umkm', 'reseller')", name="ck_keu_pelanggan_segmen"),)
    id: Mapped[str] = pk()
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    segmen: Mapped[str | None] = mapped_column(String(16))
    kontak: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = created()


class KeuVendor(BumiLestariBase):
    __tablename__ = "keu_vendor"
    __table_args__ = (
        CheckConstraint("jenis IN ('tukang_kayu', 'supplier')", name="ck_keu_vendor_jenis"),
        CheckConstraint("length(trim(kode)) > 0", name="ck_keu_vendor_kode"),
    )
    id: Mapped[str] = pk()
    kode: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, default=lambda: f"VND-{new_id()}")
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    jenis: Mapped[str] = mapped_column(String(16), nullable=False)
    kontak: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    alamat: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    keterangan: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = created()

    @property
    def tipe(self) -> str:
        # Preserve existing stored kinds and allocation references across upgrades.
        return "kayu" if self.jenis == "tukang_kayu" else "non_kayu"

    @property
    def status(self) -> str:
        return "aktif" if self.aktif else "non_aktif"


class KeuVendorSlot(BumiLestariBase):
    __tablename__ = "keu_vendor_slot"
    __table_args__ = (
        UniqueConstraint("vendor_id", name="uq_keu_vendor_slot_vendor"),
        CheckConstraint("jenis IN ('tukang_kayu', 'supplier') AND nomor >= 1", name="ck_keu_vendor_slot"),
    )
    jenis: Mapped[str] = mapped_column(String(16), primary_key=True)
    nomor: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor_id: Mapped[str | None] = mapped_column(ForeignKey("keu_vendor.id", ondelete="RESTRICT"))

    @property
    def kode(self) -> str:
        return f"{'tk' if self.jenis == 'tukang_kayu' else 'sup'}-{self.nomor}"


class KeuProduk(BumiLestariBase):
    __tablename__ = "keu_produk"
    __table_args__ = (
        CheckConstraint("jenis IN ('kayu', 'non_kayu')", name="ck_keu_produk_jenis"),
        CheckConstraint("biaya_acuan >= 0", name="ck_keu_produk_biaya"),
        CheckConstraint("harga_jual >= 0", name="ck_keu_produk_harga"),
        CheckConstraint("status IN ('draf', 'master') AND (status <> 'master' OR jenis IS NOT NULL)", name="ck_keu_produk_status"),
    )
    id: Mapped[str] = pk()
    sku: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    sku_induk: Mapped[str | None] = mapped_column(String(128))
    nama_asli: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    gambar_url: Mapped[str] = mapped_column(String(2048), nullable=False, default="", server_default="")
    varian_list: Mapped[list[dict[str, str]]] = mapped_column(JSON_DATA, nullable=False, default=list, server_default="[]")
    harga_jual: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"), server_default="0")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="master", server_default="master")
    nama: Mapped[str] = mapped_column(String(255), nullable=False)
    jenis: Mapped[str | None] = mapped_column(String(16))
    biaya_acuan: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"), server_default="0")
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class KeuPesanan(BumiLestariBase):
    __tablename__ = "keu_pesanan"
    __table_args__ = (
        UniqueConstraint("saluran_id", "sumber_ref", name="uq_keu_pesanan_sumber"),
        CheckConstraint("segmen_snapshot IS NULL OR segmen_snapshot IN ('umkm', 'reseller')", name="ck_keu_pesanan_segmen"),
        CheckConstraint("status IN ('draf', 'aktif', 'selesai', 'batal')", name="ck_keu_pesanan_status"),
        CheckConstraint("total_sumber >= 0", name="ck_keu_pesanan_total"),
    )
    id: Mapped[str] = pk()
    saluran_id: Mapped[str] = mapped_column(ForeignKey("keu_saluran.id", ondelete="RESTRICT"), nullable=False, index=True)
    sumber_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    nomor: Mapped[str] = mapped_column(String(128), nullable=False)
    tanggal: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    pelanggan_id: Mapped[str | None] = mapped_column(ForeignKey("keu_pelanggan.id", ondelete="RESTRICT"))
    segmen_snapshot: Mapped[str | None] = mapped_column(String(16))
    status_sumber: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draf", server_default="draf")
    total_sumber: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    sumber_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created()


class KeuItem(BumiLestariBase):
    __tablename__ = "keu_item"
    __table_args__ = (
        UniqueConstraint("pesanan_id", "sumber_ref", name="uq_keu_item_sumber"),
        CheckConstraint("qty > 0", name="ck_keu_item_qty"),
        CheckConstraint("harga_satuan >= 0 AND subtotal_sumber >= 0", name="ck_keu_item_uang"),
    )
    id: Mapped[str] = pk()
    pesanan_id: Mapped[str] = mapped_column(ForeignKey("keu_pesanan.id", ondelete="RESTRICT"), nullable=False, index=True)
    sumber_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    produk_id: Mapped[str | None] = mapped_column(ForeignKey("keu_produk.id", ondelete="RESTRICT"))
    nama_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    varian_snapshot: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    harga_satuan: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    subtotal_sumber: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)


class KeuAlokasiVendor(BumiLestariBase):
    __tablename__ = "keu_alokasi_vendor"
    __table_args__ = (
        UniqueConstraint("item_id", "vendor_id", name="uq_keu_alokasi_vendor"),
        CheckConstraint("qty > 0 AND biaya_satuan >= 0", name="ck_keu_alokasi_vendor_nilai"),
        CheckConstraint("NOT dibatalkan OR length(trim(alasan)) >= 3", name="ck_keu_alokasi_vendor_batal"),
    )
    id: Mapped[str] = pk()
    item_id: Mapped[str] = mapped_column(ForeignKey("keu_item.id", ondelete="RESTRICT"), nullable=False, index=True)
    vendor_id: Mapped[str] = mapped_column(ForeignKey("keu_vendor.id", ondelete="RESTRICT"), nullable=False, index=True)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    biaya_satuan: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    dibatalkan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    alasan: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    dibuat_oleh: Mapped[str] = mapped_column(ForeignKey("bl_users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = created()


class KeuSettlement(BumiLestariBase):
    __tablename__ = "keu_settlement"
    __table_args__ = (
        UniqueConstraint("saluran_id", "sumber_ref", name="uq_keu_settlement_sumber"),
        CheckConstraint("neto = bruto - potongan + penyesuaian", name="ck_keu_settlement_rekonsiliasi"),
        CheckConstraint("status IN ('draf', 'terkirim', 'dibatalkan')", name="ck_keu_settlement_status"),
    )
    id: Mapped[str] = pk()
    saluran_id: Mapped[str] = mapped_column(ForeignKey("keu_saluran.id", ondelete="RESTRICT"), nullable=False, index=True)
    sumber_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    tanggal_cair: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    bruto: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    potongan: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    penyesuaian: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"), server_default="0")
    neto: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    rincian: Mapped[dict] = mapped_column(JSON_DATA, nullable=False, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draf", server_default="draf")
    alasan_batal: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    dibatalkan_oleh: Mapped[str | None] = mapped_column(ForeignKey("bl_users.id", ondelete="RESTRICT"))
    dibatalkan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created()


class KeuAlokasiSettlement(BumiLestariBase):
    __tablename__ = "keu_alokasi_settlement"
    __table_args__ = (UniqueConstraint("settlement_id", "item_id", name="uq_keu_alokasi_settlement"),)
    id: Mapped[str] = pk()
    settlement_id: Mapped[str] = mapped_column(ForeignKey("keu_settlement.id", ondelete="RESTRICT"), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("keu_item.id", ondelete="RESTRICT"), nullable=False, index=True)
    jumlah: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)


class KeuTransaksi(BumiLestariBase):
    __tablename__ = "keu_transaksi"
    __table_args__ = (
        UniqueConstraint("saluran_id", "sumber_ref", name="uq_keu_transaksi_sumber"),
        CheckConstraint("jenis IN ('masuk', 'keluar')", name="ck_keu_transaksi_jenis"),
        CheckConstraint("jumlah > 0", name="ck_keu_transaksi_jumlah"),
        CheckConstraint("status IN ('draf', 'terkirim', 'dibatalkan')", name="ck_keu_transaksi_status"),
    )
    id: Mapped[str] = pk()
    saluran_id: Mapped[str] = mapped_column(ForeignKey("keu_saluran.id", ondelete="RESTRICT"), nullable=False)
    sumber_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    akun_id: Mapped[str] = mapped_column(ForeignKey("keu_akun.id", ondelete="RESTRICT"), nullable=False, index=True)
    kategori_id: Mapped[str] = mapped_column(ForeignKey("bl_kategori.id", ondelete="RESTRICT"), nullable=False)
    settlement_id: Mapped[str | None] = mapped_column(ForeignKey("keu_settlement.id", ondelete="RESTRICT"))
    tanggal: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    jenis: Mapped[str] = mapped_column(String(16), nullable=False)
    jumlah: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draf", server_default="draf")
    keterangan: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    dibuat_oleh: Mapped[str] = mapped_column(ForeignKey("bl_users.id", ondelete="RESTRICT"), nullable=False)
    alasan_batal: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    dibatalkan_oleh: Mapped[str | None] = mapped_column(ForeignKey("bl_users.id", ondelete="RESTRICT"))
    dibatalkan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created()


class KeuImpor(BumiLestariBase):
    __tablename__ = "keu_impor"
    __table_args__ = (
        UniqueConstraint("saluran_id", "jenis", "file_sha256", "versi_format", name="uq_keu_impor_file"),
        CheckConstraint("jenis IN ('order', 'settlement', 'biaya')", name="ck_keu_impor_jenis"),
        CheckConstraint("status IN ('draf', 'valid', 'diterapkan', 'ditolak')", name="ck_keu_impor_status"),
        CheckConstraint("length(file_sha256) = 64 AND versi_format > 0", name="ck_keu_impor_format"),
    )
    id: Mapped[str] = pk()
    saluran_id: Mapped[str] = mapped_column(ForeignKey("keu_saluran.id", ondelete="RESTRICT"), nullable=False)
    jenis: Mapped[str] = mapped_column(String(16), nullable=False)
    nama_file: Mapped[str] = mapped_column(String(255), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    versi_format: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    pemetaan: Mapped[dict] = mapped_column(JSON_DATA, nullable=False, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draf", server_default="draf")
    dibuat_oleh: Mapped[str] = mapped_column(ForeignKey("bl_users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = created()


class KeuMasukan(BumiLestariBase):
    __tablename__ = "keu_masukan"
    __table_args__ = (
        UniqueConstraint("saluran_id", "entitas", "sumber_ref", "revisi_sha256", name="uq_keu_masukan_revisi"),
        UniqueConstraint("impor_id", "nomor_baris", name="uq_keu_masukan_baris"),
        CheckConstraint("(impor_id IS NULL AND nomor_baris IS NULL) OR (impor_id IS NOT NULL AND nomor_baris IS NOT NULL AND nomor_baris > 0)", name="ck_keu_masukan_asal"),
        CheckConstraint("entitas IN ('produk', 'order', 'settlement', 'biaya')", name="ck_keu_masukan_entitas"),
        CheckConstraint("status IN ('menunggu', 'terproses', 'gagal', 'diabaikan')", name="ck_keu_masukan_status"),
        CheckConstraint("length(revisi_sha256) = 64 AND percobaan >= 0", name="ck_keu_masukan_revisi"),
        Index("ix_keu_masukan_antrian", "status", "coba_lagi_at"),
    )
    id: Mapped[str] = pk()
    saluran_id: Mapped[str] = mapped_column(ForeignKey("keu_saluran.id", ondelete="RESTRICT"), nullable=False)
    impor_id: Mapped[str | None] = mapped_column(ForeignKey("keu_impor.id", ondelete="RESTRICT"))
    nomor_baris: Mapped[int | None] = mapped_column(Integer)
    entitas: Mapped[str] = mapped_column(String(16), nullable=False)
    sumber_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    sumber_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revisi_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON_DATA, nullable=False)
    kesalahan: Mapped[list] = mapped_column(JSON_DATA, nullable=False, default=list, server_default="[]")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="menunggu", server_default="menunggu")
    percobaan: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    coba_lagi_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created()


class KeuCursor(BumiLestariBase):
    __tablename__ = "keu_cursor"
    __table_args__ = (CheckConstraint("entitas IN ('produk', 'order', 'settlement')", name="ck_keu_cursor_entitas"),)
    saluran_id: Mapped[str] = mapped_column(ForeignKey("keu_saluran.id", ondelete="RESTRICT"), primary_key=True)
    entitas: Mapped[str] = mapped_column(String(16), primary_key=True)
    watermark_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watermark_ref: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


KEU_MODELS = (KeuSaluran, KeuAkun, KeuPelanggan, KeuVendor, KeuVendorSlot, KeuProduk, KeuPesanan, KeuItem,
              KeuAlokasiVendor, KeuSettlement, KeuAlokasiSettlement, KeuTransaksi, KeuImpor, KeuMasukan, KeuCursor)
