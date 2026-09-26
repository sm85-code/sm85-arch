from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr


# --- Auth -----------------------------------------------------------------


class RegisterIn(BaseModel):
    nama: str
    email: EmailStr
    password: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: str
    nama: str
    email: str
    role: str


# --- Akun Marketplace -------------------------------------------------------


class AkunMarketplaceIn(BaseModel):
    platform: str
    nama_toko: str
    id_toko_eksternal: Optional[str] = None
    catatan: Optional[str] = None


class AkunMarketplacePatch(BaseModel):
    nama_toko: Optional[str] = None
    id_toko_eksternal: Optional[str] = None
    status: Optional[str] = None
    catatan: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_kedaluwarsa: Optional[datetime] = None


class AkunMarketplaceOut(BaseModel):
    id: str
    platform: str
    nama_toko: str
    id_toko_eksternal: Optional[str]
    status: str
    catatan: Optional[str]

    model_config = ConfigDict(from_attributes=True)


# --- Produk (SKU induk) + Listing -------------------------------------------


class ProdukIn(BaseModel):
    sku_induk: str
    nama: str
    deskripsi: str = ""
    harga_dasar: Decimal
    stok: int = 0
    foto_url: Optional[str] = None


class ProdukPatch(BaseModel):
    nama: Optional[str] = None
    deskripsi: Optional[str] = None
    harga_dasar: Optional[Decimal] = None
    stok: Optional[int] = None
    foto_url: Optional[str] = None
    aktif: Optional[bool] = None


class ProdukOut(BaseModel):
    id: str
    sku_induk: str
    nama: str
    deskripsi: str
    harga_dasar: Decimal
    stok: int
    foto_url: Optional[str]
    aktif: bool

    model_config = ConfigDict(from_attributes=True)


class ProdukListingIn(BaseModel):
    produk_id: str
    akun_id: str
    platform: str
    id_eksternal: str
    harga_jual: Optional[Decimal] = None
    stok_listing: Optional[int] = None


class ProdukListingPatch(BaseModel):
    harga_jual: Optional[Decimal] = None
    stok_listing: Optional[int] = None
    aktif: Optional[bool] = None


class ProdukListingOut(BaseModel):
    id: str
    produk_id: str
    akun_id: str
    platform: str
    id_eksternal: str
    harga_jual: Optional[Decimal]
    stok_listing: Optional[int]
    aktif: bool

    model_config = ConfigDict(from_attributes=True)
