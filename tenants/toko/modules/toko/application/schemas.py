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
