from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field

Periode = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="YYYY-MM")


class PembayaranPemasokIn(BaseModel):
    tanggal: Optional[date] = None
    akun_id: Optional[str] = None  # kosong -> Kas utama
    order_ids: Optional[list[str]] = None  # kosong -> semua yang siap dibayar


class ItemSiapBayarOut(BaseModel):
    order_id: str
    no_order: str
    produk_id: str
    qty: int
    tgl_diambil: date
    jumlah: Decimal
    terlambat: bool  # diambil sebelum minggu acuan (belum terbayar di Selasa sebelumnya)


class PemasokSiapBayarOut(BaseModel):
    pemasok_id: str
    nama: str
    jenis: str
    subtotal: Decimal
    items: list[ItemSiapBayarOut]


class SiapBayarOut(BaseModel):
    selasa: date
    batas_diambil: date  # Sabtu sebelum Selasa: order diambil s.d. tanggal ini ikut dibayar
    sudah_dicatat_id: Optional[str]
    total: Decimal
    pemasok: list[PemasokSiapBayarOut]


class PembayaranPemasokOut(BaseModel):
    id: str
    selasa: date
    tanggal: date
    akun_id: str
    total: Decimal
    dibatalkan: bool
    model_config = ConfigDict(from_attributes=True)


class ItemRincianOut(BaseModel):
    order_id: str
    no_order: str
    produk_sku: str
    produk_nama: str
    qty: int
    pemasok_nama: str
    jumlah: Decimal


class PembayaranPemasokDetailOut(PembayaranPemasokOut):
    """Di laporan keuangan tampil sebagai 1 transaksi (total); di sini rinciannya per order/barang."""

    transaksi_id: Optional[str]
    total_qty: int
    items: list[ItemRincianOut]


class PenerimaanResellerIn(BaseModel):
    tanggal: Optional[date] = None
    pelanggan_id: str
    akun_id: Optional[str] = None  # kosong -> Kas utama
    order_ids: Optional[list[str]] = None  # kosong -> semua piutang pelanggan ini


class PiutangItemOut(BaseModel):
    order_id: str
    no_order: str
    tanggal_order: date
    jumlah: Decimal


class PiutangPelangganOut(BaseModel):
    pelanggan_id: str
    nama: str
    subtotal: Decimal
    items: list[PiutangItemOut]


class PenerimaanResellerOut(BaseModel):
    id: str
    tanggal: date
    pelanggan_id: str
    akun_id: str
    total: Decimal
    dibatalkan: bool
    model_config = ConfigDict(from_attributes=True)


class KaryawanIn(BaseModel):
    nama: str = Field(min_length=1, max_length=255)
    peran: str = "lainnya"
    gaji_bulanan: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    user_id: Optional[str] = None


class KaryawanPatch(BaseModel):
    nama: Optional[str] = Field(default=None, min_length=1, max_length=255)
    peran: Optional[str] = None
    gaji_bulanan: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    user_id: Optional[str] = None
    aktif: Optional[bool] = None


class KaryawanOut(BaseModel):
    id: str
    nama: str
    peran: str
    gaji_bulanan: Decimal
    user_id: Optional[str]
    aktif: bool
    model_config = ConfigDict(from_attributes=True)


class GajiPeriodeIn(BaseModel):
    periode: str = Periode
    tanggal: Optional[date] = None  # hanya untuk bayar; kosong -> hari ini


class GajiOut(BaseModel):
    id: str
    periode: str
    karyawan_id: str
    jumlah: Decimal
    tanggal_bayar: Optional[date]
    model_config = ConfigDict(from_attributes=True)

    @computed_field
    @property
    def jatuh_tempo(self) -> date:
        """Dibayar tanggal 1 bulan berikutnya."""
        tahun, bulan = int(self.periode[:4]), int(self.periode[5:])
        return date(tahun + (bulan == 12), bulan % 12 + 1, 1)


class BagiHasilIn(BaseModel):
    periode: str = Periode


class BagiHasilOut(BaseModel):
    periode: str
    pemasukan: Decimal
    pengeluaran: Decimal
    laba_bersih: Decimal
    persen_admin: Decimal
    persen_owner: Decimal
    bagian_admin: Decimal
    bagian_owner: Decimal


class BagiHasilTersimpanOut(BaseModel):
    id: str
    periode: str
    laba_bersih: Decimal
    persen_admin: Decimal
    persen_owner: Decimal
    bagian_admin: Decimal
    bagian_owner: Decimal
    tanggal_bayar: Optional[date]
    dibatalkan: bool
    model_config = ConfigDict(from_attributes=True)


class LanggananIn(BaseModel):
    nama: str = Field(min_length=1, max_length=128)
    jumlah_bulanan: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)


class LanggananPatch(BaseModel):
    nama: Optional[str] = Field(default=None, min_length=1, max_length=128)
    jumlah_bulanan: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    aktif: Optional[bool] = None


class LanggananOut(BaseModel):
    id: str
    nama: str
    jumlah_bulanan: Decimal
    aktif: bool
    model_config = ConfigDict(from_attributes=True)


class SisihanIn(BaseModel):
    tanggal: Optional[date] = None


class SisihanItemOut(BaseModel):
    jenis: str  # gaji | langganan
    nama: str
    jumlah: Decimal


class SisihanOut(BaseModel):
    selasa: date
    periode: str  # bulan yang didanai
    minggu_ke: int  # Selasa ke-N bulan ini; cicilan hanya 1-4
    items: list[SisihanItemOut]
    total: Decimal
    saldo_kas_utama: Decimal
    cukup: bool
    sudah_dicatat_id: Optional[str]
    catatan: str = ""


class SisihanTersimpanOut(BaseModel):
    id: str
    selasa: date
    periode: str
    minggu_ke: int
    total: Decimal
    dibatalkan: bool
    model_config = ConfigDict(from_attributes=True)


class TagihanItemIn(BaseModel):
    langganan_id: str
    jumlah: Decimal = Field(ge=0, max_digits=14, decimal_places=2)  # tagihan sebenarnya


class TagihanBayarIn(BaseModel):
    periode: str = Periode
    tanggal: Optional[date] = None
    items: Optional[list[TagihanItemIn]] = None  # kosong -> semua langganan aktif sebesar jumlah_bulanan


class TagihanOut(BaseModel):
    id: str
    periode: str
    langganan_id: str
    jumlah: Decimal
    tanggal_bayar: date
    dibatalkan: bool
    model_config = ConfigDict(from_attributes=True)
