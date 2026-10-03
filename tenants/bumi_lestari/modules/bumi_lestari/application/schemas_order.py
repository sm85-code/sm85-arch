from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field

Uang = Field(ge=0, max_digits=14, decimal_places=2)


class ProdukIn(BaseModel):
    sku: str = Field(min_length=1, max_length=128)
    nama: str = Field(min_length=1, max_length=255)
    jenis_produk: str = "kayu"
    harga_jual: Decimal = Uang
    biaya_pokok_default: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)


class ProdukPatch(BaseModel):
    nama: Optional[str] = Field(default=None, min_length=1, max_length=255)
    harga_jual: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    biaya_pokok_default: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    aktif: Optional[bool] = None


class ProdukOut(BaseModel):
    id: str
    sku: str
    nama: str
    jenis_produk: str
    harga_jual: Decimal
    biaya_pokok_default: Decimal
    aktif: bool
    model_config = ConfigDict(from_attributes=True)


class PemasokIn(BaseModel):
    nama: str = Field(min_length=1, max_length=255)
    jenis: str
    kontak: str = Field(default="", max_length=255)
    catatan: str = Field(default="", max_length=1000)


class PemasokOut(PemasokIn):
    id: str
    aktif: bool
    model_config = ConfigDict(from_attributes=True)


class SaluranIn(BaseModel):
    nama: str = Field(min_length=1, max_length=128)
    jenis: str
    akun_id: Optional[str] = None


class SaluranOut(SaluranIn):
    id: str
    aktif: bool
    model_config = ConfigDict(from_attributes=True)


class PelangganIn(BaseModel):
    nama: str = Field(min_length=1, max_length=255)
    kontak: str = Field(default="", max_length=255)
    tempo_hari: int = Field(default=0, ge=0, le=365)
    catatan: str = Field(default="", max_length=1000)


class PelangganOut(PelangganIn):
    id: str
    aktif: bool
    model_config = ConfigDict(from_attributes=True)


class HargaGrosirIn(BaseModel):
    """Empat komponen harga per unit untuk penjual lain: barang, cat + jasa, packing (biasa/kayu),
    biaya proses (tergantung ukuran barang)."""

    produk_id: str
    pelanggan_id: str
    harga: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    harga_cat_jasa: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    harga_packing_biasa: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    harga_packing_kayu: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    biaya_proses: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)


class HargaGrosirOut(HargaGrosirIn):
    id: str
    model_config = ConfigDict(from_attributes=True)


class OrderIn(BaseModel):
    no_order: str = Field(default="", max_length=128)
    tanggal_order: Optional[date] = None
    saluran_id: str
    pelanggan_id: Optional[str] = None
    nama_pembeli: str = Field(default="", max_length=255)
    produk_id: str
    qty: int = Field(default=1, ge=1, le=10000)
    # Kosong -> harga grosir pelanggan (bila ada) -> harga jual katalog.
    harga_satuan: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    harga_cat_jasa: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    jenis_packing: str = "biasa"
    harga_packing: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    biaya_proses: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    warna: str = Field(default="", max_length=128)
    potongan_marketplace: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    pemasok_id: Optional[str] = None
    # Kosong -> biaya pokok default katalog x qty.
    biaya_pokok: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    butuh_cat: Optional[bool] = None  # kosong -> True untuk produk kayu, False untuk non kayu
    catatan: str = Field(default="", max_length=1000)


class OrderPatch(BaseModel):
    nama_pembeli: Optional[str] = Field(default=None, max_length=255)
    harga_satuan: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    harga_cat_jasa: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    jenis_packing: Optional[str] = None
    harga_packing: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    biaya_proses: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    warna: Optional[str] = Field(default=None, max_length=128)
    potongan_marketplace: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    pemasok_id: Optional[str] = None
    biaya_pokok: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    butuh_cat: Optional[bool] = None
    catatan: Optional[str] = Field(default=None, max_length=1000)


class OrderStatusIn(BaseModel):
    status: str
    tanggal: Optional[date] = None


class OrderOut(BaseModel):
    id: str
    no_order: str
    tanggal_order: date
    saluran_id: str
    pelanggan_id: Optional[str]
    nama_pembeli: str
    produk_id: str
    qty: int
    harga_satuan: Decimal
    harga_cat_jasa: Decimal
    jenis_packing: str
    harga_packing: Decimal
    biaya_proses: Decimal
    warna: str
    potongan_marketplace: Decimal
    pemasok_id: Optional[str]
    biaya_pokok: Decimal
    butuh_cat: bool
    status: str
    tgl_pesan_pemasok: Optional[date]
    tgl_diambil: Optional[date]
    tgl_dicat: Optional[date]
    tgl_dikirim: Optional[date]
    tgl_selesai: Optional[date]
    catatan: str
    model_config = ConfigDict(from_attributes=True)

    @computed_field
    @property
    def total_penjualan(self) -> Decimal:
        # (barang + cat/jasa + packing + biaya proses) per unit x qty. Order polos: cat/jasa = 0,
        # tetapi packing tetap dibayar.
        return (self.harga_satuan + self.harga_cat_jasa + self.harga_packing + self.biaya_proses) * self.qty

    @computed_field
    @property
    def laba_kotor(self) -> Decimal:
        return self.total_penjualan - self.potongan_marketplace - self.biaya_pokok
