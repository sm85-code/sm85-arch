from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


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


# --- Tahap 2: Stock ---------------------------------------------------------


class StokAdjustIn(BaseModel):
    produk_id: str
    qty_delta: int
    catatan: Optional[str] = None
    gudang_id: Optional[str] = None


class StokLedgerOut(BaseModel):
    id: str
    produk_id: str
    gudang_id: Optional[str]
    qty_delta: int
    reason: str
    ref_type: Optional[str]
    ref_id: Optional[str]
    catatan: Optional[str]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GudangOut(BaseModel):
    id: str
    kode: str
    nama: str
    aktif: bool

    model_config = ConfigDict(from_attributes=True)


class StokReservasiOut(BaseModel):
    id: str
    produk_id: str
    gudang_id: Optional[str]
    pesanan_id: str
    qty: int
    status: str

    model_config = ConfigDict(from_attributes=True)


# --- Tahap 2: Orders OMS ----------------------------------------------------


class ItemPesananIn(BaseModel):
    nama_produk: str
    harga_satuan: Decimal
    qty: int = Field(gt=0)
    produk_id: Optional[str] = None
    listing_id: Optional[str] = None
    subtotal: Optional[Decimal] = None


class PesananIn(BaseModel):
    platform: str
    id_eksternal: str
    akun_id: Optional[str] = None
    status: str = "unpaid"
    nama_pembeli: str = ""
    total: Optional[Decimal] = None
    items: list[ItemPesananIn] = Field(default_factory=list)


class PesananStatusIn(BaseModel):
    status: str


class ItemPesananOut(BaseModel):
    id: str
    produk_id: Optional[str]
    listing_id: Optional[str]
    nama_produk: str
    harga_satuan: Decimal
    qty: int
    subtotal: Decimal

    model_config = ConfigDict(from_attributes=True)


class PesananOut(BaseModel):
    id: str
    platform: str
    id_eksternal: str
    akun_id: Optional[str]
    status: str
    nama_pembeli: str
    total: Decimal
    tersinkron_marketplace: bool
    catatan_sinkron: Optional[str]
    items: list[ItemPesananOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- OAuth ------------------------------------------------------------------


class OAuthStartOut(BaseModel):
    platform: str
    akun_id: str
    authorize_url: str
