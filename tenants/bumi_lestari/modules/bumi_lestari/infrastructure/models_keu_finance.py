"""Balanced keu general ledger; existing cash masters remain separate from the chart."""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import BumiLestariBase
from .models_keu import JSON_DATA, created, pk


class KeuCoa(BumiLestariBase):
    __tablename__ = "keu_coa"
    __table_args__ = (CheckConstraint("jenis IN ('aset','kewajiban','ekuitas','pendapatan','beban')", name="ck_keu_coa_jenis"),)
    id: Mapped[str] = pk()
    kode: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    nama: Mapped[str] = mapped_column(String(128), nullable=False)
    jenis: Mapped[str] = mapped_column(String(16), nullable=False)
    kelompok: Mapped[str] = mapped_column(String(64), nullable=False)
    kas_akun_id: Mapped[str | None] = mapped_column(ForeignKey("keu_akun.id", ondelete="RESTRICT"), unique=True)
    sistem: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    aktif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class KeuBuku(BumiLestariBase):
    __tablename__ = "keu_buku"
    __table_args__ = (
        CheckConstraint("status IN ('migrasi','aktif')", name="ck_keu_buku_status"),
        CheckConstraint("id='utama'", name="ck_keu_buku_tunggal"),
        CheckConstraint("metode_stok='fifo'", name="ck_keu_buku_stok"),
        CheckConstraint("tanggal_status='updated_at'", name="ck_keu_buku_tanggal"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default="utama")
    tanggal_awal: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="migrasi", server_default="migrasi")
    metode_stok: Mapped[str] = mapped_column(String(16), nullable=False)
    tanggal_status: Mapped[str] = mapped_column(String(16), nullable=False)
    dibuat_oleh: Mapped[str] = mapped_column(ForeignKey("bl_users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = created()


class KeuKategoriCoa(BumiLestariBase):
    __tablename__ = "keu_kategori_coa"
    __table_args__ = (CheckConstraint("arus IN ('operasional','investasi','pendanaan')", name="ck_keu_kategori_arus"),)
    id: Mapped[str] = mapped_column(ForeignKey("bl_kategori.id", ondelete="RESTRICT"), primary_key=True)
    coa_id: Mapped[str] = mapped_column(ForeignKey("keu_coa.id", ondelete="RESTRICT"), nullable=False)
    arus: Mapped[str] = mapped_column(String(16), nullable=False)


class KeuJurnal(BumiLestariBase):
    __tablename__ = "keu_jurnal"
    __table_args__ = (
        CheckConstraint("status IN ('draf','terkirim','dibatalkan')", name="ck_keu_jurnal_status"),
        CheckConstraint("status<>'dibatalkan' OR (length(trim(alasan_batal))>=3 AND dibatalkan_oleh IS NOT NULL AND dibatalkan_at IS NOT NULL)", name="ck_keu_jurnal_batal"),
    )
    id: Mapped[str] = pk()
    sumber_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    jenis: Mapped[str] = mapped_column(String(32), nullable=False)
    tanggal: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draf", server_default="draf")
    keterangan: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    rincian: Mapped[dict] = mapped_column(JSON_DATA, nullable=False, default=dict, server_default="{}")
    transaksi_id: Mapped[str | None] = mapped_column(ForeignKey("keu_transaksi.id", ondelete="RESTRICT"), unique=True)
    settlement_id: Mapped[str | None] = mapped_column(ForeignKey("keu_settlement.id", ondelete="RESTRICT"), unique=True)
    pesanan_id: Mapped[str | None] = mapped_column(ForeignKey("keu_pesanan.id", ondelete="RESTRICT"), index=True)
    dibuat_oleh: Mapped[str] = mapped_column(ForeignKey("bl_users.id", ondelete="RESTRICT"), nullable=False)
    alasan_batal: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    dibatalkan_oleh: Mapped[str | None] = mapped_column(ForeignKey("bl_users.id", ondelete="RESTRICT"))
    dibatalkan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created()


class KeuJurnalBaris(BumiLestariBase):
    __tablename__ = "keu_jurnal_baris"
    __table_args__ = (
        UniqueConstraint("jurnal_id", "nomor", name="uq_keu_jurnal_baris"),
        CheckConstraint("nomor>0 AND ((debet>0 AND kredit=0) OR (kredit>0 AND debet=0))", name="ck_keu_baris_uang"),
        CheckConstraint("arus IS NULL OR arus IN ('operasional','investasi','pendanaan','mutasi')", name="ck_keu_baris_arus"),
    )
    id: Mapped[str] = pk()
    jurnal_id: Mapped[str] = mapped_column(ForeignKey("keu_jurnal.id", ondelete="RESTRICT"), nullable=False, index=True)
    nomor: Mapped[int] = mapped_column(Integer, nullable=False)
    coa_id: Mapped[str] = mapped_column(ForeignKey("keu_coa.id", ondelete="RESTRICT"), nullable=False, index=True)
    debet: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"), server_default="0")
    kredit: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"), server_default="0")
    arus: Mapped[str | None] = mapped_column(String(16))
    pesanan_id: Mapped[str | None] = mapped_column(ForeignKey("keu_pesanan.id", ondelete="RESTRICT"), index=True)
    vendor_id: Mapped[str | None] = mapped_column(ForeignKey("keu_vendor.id", ondelete="RESTRICT"), index=True)
    produk_id: Mapped[str | None] = mapped_column(ForeignKey("keu_produk.id", ondelete="RESTRICT"), index=True)


class KeuStokMutasi(BumiLestariBase):
    __tablename__ = "keu_stok_mutasi"
    __table_args__ = (
        CheckConstraint("jenis IN ('masuk','keluar') AND qty>0 AND nilai>=0", name="ck_keu_stok_mutasi"),
    )
    id: Mapped[str] = pk()
    jurnal_id: Mapped[str] = mapped_column(ForeignKey("keu_jurnal.id", ondelete="RESTRICT"), nullable=False, unique=True)
    produk_id: Mapped[str] = mapped_column(ForeignKey("keu_produk.id", ondelete="RESTRICT"), nullable=False, index=True)
    item_id: Mapped[str | None] = mapped_column(ForeignKey("keu_item.id", ondelete="RESTRICT"), index=True)
    jenis: Mapped[str] = mapped_column(String(16), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    nilai: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)


class KeuStokPemakaian(BumiLestariBase):
    __tablename__ = "keu_stok_pemakaian"
    __table_args__ = (
        UniqueConstraint("keluar_id", "masuk_id", name="uq_keu_stok_pemakaian"),
        CheckConstraint("qty>0 AND nilai>=0 AND keluar_id<>masuk_id", name="ck_keu_stok_pemakaian"),
    )
    id: Mapped[str] = pk()
    keluar_id: Mapped[str] = mapped_column(ForeignKey("keu_stok_mutasi.id", ondelete="RESTRICT"), nullable=False, index=True)
    masuk_id: Mapped[str] = mapped_column(ForeignKey("keu_stok_mutasi.id", ondelete="RESTRICT"), nullable=False, index=True)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    nilai: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)


FINANCE_MODELS = (KeuCoa, KeuBuku, KeuKategoriCoa, KeuJurnal, KeuJurnalBaris, KeuStokMutasi, KeuStokPemakaian)
