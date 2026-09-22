from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, EmailStr, field_validator

_TELEPON_RE = re.compile(r"^\+?[0-9][0-9\-\s]{7,19}$")
_KODE_POS_RE = re.compile(r"^[0-9]{5}$")


def _validate_telepon(value: str) -> str:
    value = value.strip()
    if not _TELEPON_RE.match(value):
        raise ValueError("Nomor telepon tidak valid (8-20 digit, boleh diawali +)")
    return value


def _validate_kode_pos(value: str) -> str:
    value = value.strip()
    if value and not _KODE_POS_RE.match(value):
        raise ValueError("Kode pos harus 5 digit angka")
    return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    nama: str
    email: EmailStr
    password: str


class GoogleLoginRequest(BaseModel):
    id_token: str


class ProdukIn(BaseModel):
    nama: str
    deskripsi: str = ""
    kategori_id: Optional[str] = None
    harga: Decimal
    stok: int = 0
    foto_url: Optional[str] = None


class ProdukPatch(BaseModel):
    nama: Optional[str] = None
    deskripsi: Optional[str] = None
    kategori_id: Optional[str] = None
    harga: Optional[Decimal] = None
    stok: Optional[int] = None
    foto_url: Optional[str] = None
    aktif: Optional[bool] = None


class KategoriIn(BaseModel):
    nama: str


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
    ongkir: Decimal = Decimal("0")
    nama_penerima: str
    telepon_penerima: str
    alamat_tujuan: str
    kota_tujuan: str = ""
    provinsi_tujuan: str = ""
    kode_pos_tujuan: str = ""

    _v_telepon = field_validator("telepon_penerima")(_validate_telepon)
    _v_kodepos = field_validator("kode_pos_tujuan")(_validate_kode_pos)


class StatusPengirimanIn(BaseModel):
    status: str
    tracking_id: Optional[str] = None


class AlamatIn(BaseModel):
    label: str = "Rumah"
    nama_penerima: str
    telepon_penerima: str
    alamat_lengkap: str
    kota: str = ""
    provinsi: str = ""
    kode_pos: str = ""
    utama: bool = False

    _v_telepon = field_validator("telepon_penerima")(_validate_telepon)
    _v_kodepos = field_validator("kode_pos")(_validate_kode_pos)


class AlamatPatch(BaseModel):
    label: Optional[str] = None
    nama_penerima: Optional[str] = None
    telepon_penerima: Optional[str] = None
    alamat_lengkap: Optional[str] = None
    kota: Optional[str] = None
    provinsi: Optional[str] = None
    kode_pos: Optional[str] = None
    utama: Optional[bool] = None

    @field_validator("telepon_penerima")
    @classmethod
    def _v_telepon(cls, v):
        return _validate_telepon(v) if v is not None else v

    @field_validator("kode_pos")
    @classmethod
    def _v_kodepos(cls, v):
        return _validate_kode_pos(v) if v is not None else v


class PesanChatIn(BaseModel):
    isi: str
