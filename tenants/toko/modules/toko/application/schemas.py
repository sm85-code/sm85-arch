from __future__ import annotations

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    nama: str
    email: EmailStr
    password: str


class ProdukIn(BaseModel):
    nama: str
    deskripsi: str = ""
    kategori: str = ""
    harga: Decimal
    stok: int = 0
    foto_url: Optional[str] = None


class ProdukPatch(BaseModel):
    nama: Optional[str] = None
    deskripsi: Optional[str] = None
    kategori: Optional[str] = None
    harga: Optional[Decimal] = None
    stok: Optional[int] = None
    foto_url: Optional[str] = None
    aktif: Optional[bool] = None


class KeranjangItemIn(BaseModel):
    produk_id: str
    qty: int = 1


class KeranjangItemPatch(BaseModel):
    qty: int


class StatusPesananIn(BaseModel):
    status: str


class CekOngkirIn(BaseModel):
    kode_pos_asal: str
    kode_pos_tujuan: str
    berat_gram: int = 1000
    nilai_barang: Decimal = Decimal("0")


class PengirimanIn(BaseModel):
    kurir: str
    layanan: str
    nama_penerima: str
    telepon_penerima: str
    alamat_tujuan: str
    kota_tujuan: str = ""
    provinsi_tujuan: str = ""
    kode_pos_tujuan: str = ""


class StatusPengirimanIn(BaseModel):
    status: str
    tracking_id: Optional[str] = None
