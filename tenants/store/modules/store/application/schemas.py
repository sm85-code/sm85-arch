from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

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


# The store ships within Java, Bali and Lampung (Kemendagri province codes: Banten, DKI Jakarta, Jawa Barat,
# Jawa Tengah, DI Yogyakarta, Jawa Timur, Bali, Lampung).
PROVINSI_DILAYANI = frozenset({"36", "31", "32", "33", "34", "35", "51", "18"})
_KODE_WILAYAH_RE = re.compile(r"^\d{2}\.\d{2}\.\d{2}\.\d{4}$")


def _validate_kode_wilayah(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    if not _KODE_WILAYAH_RE.match(value):
        raise ValueError("Kode wilayah tidak valid")
    if value[:2] not in PROVINSI_DILAYANI:
        raise ValueError("Pengiriman hanya ke Pulau Jawa, Bali, dan Lampung")
    return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., max_length=72)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=72)
    new_password: str = Field(..., min_length=8, max_length=72)


class RegisterRequest(BaseModel):
    nama: str
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=72)


class GoogleLoginRequest(BaseModel):
    id_token: str


MAKS_FOTO_PRODUK = 7
MAKS_VARIAN = 50
HARI_PROSES_READY = 2
HARI_PREORDER_MIN, HARI_PREORDER_MAX = 3, 14

_BERAT = Field(0, ge=0, le=500_000)  # grams
_DIMENSI = Field(Decimal("0"), ge=0, le=1000)  # centimetres


def normalisasi_proses(preorder: bool, hari: int) -> int:
    """Ready stock is processed within 2 days (whatever was sent); a pre-order must say 3-14 days."""
    if not preorder:
        return HARI_PROSES_READY
    if not HARI_PREORDER_MIN <= hari <= HARI_PREORDER_MAX:
        raise ValueError(f"Pre-order harus {HARI_PREORDER_MIN} sampai {HARI_PREORDER_MAX} hari")
    return hari


class ProdukIn(BaseModel):
    nama: str
    deskripsi: str = ""
    kategori_id: Optional[str] = None
    harga: Decimal = Field(..., ge=0)
    stok: int = Field(0, ge=0)
    berat_gram: int = _BERAT
    panjang_cm: Decimal = _DIMENSI
    lebar_cm: Decimal = _DIMENSI
    tinggi_cm: Decimal = _DIMENSI
    preorder: bool = False
    hari_proses: int = HARI_PROSES_READY

    @model_validator(mode="after")
    def _proses(self):
        self.hari_proses = normalisasi_proses(self.preorder, self.hari_proses)
        return self


class ProdukPatch(BaseModel):
    nama: Optional[str] = None
    deskripsi: Optional[str] = None
    kategori_id: Optional[str] = None
    harga: Optional[Decimal] = Field(None, ge=0)
    stok: Optional[int] = Field(None, ge=0)
    aktif: Optional[bool] = None
    berat_gram: Optional[int] = Field(None, ge=0, le=500_000)
    panjang_cm: Optional[Decimal] = Field(None, ge=0, le=1000)
    lebar_cm: Optional[Decimal] = Field(None, ge=0, le=1000)
    tinggi_cm: Optional[Decimal] = Field(None, ge=0, le=1000)
    preorder: Optional[bool] = None
    hari_proses: Optional[int] = None


class VarianIn(BaseModel):
    """One variant in the full list sent to PUT /produk/{id}/varian (id = an existing variant to keep/update)."""

    id: Optional[str] = None
    nama: str = Field(..., min_length=1, max_length=120)
    sku: str = Field("", max_length=64)
    harga: Optional[Decimal] = Field(None, ge=0)
    stok: int = Field(0, ge=0)
    berat_gram: Optional[int] = Field(None, ge=0, le=500_000)
    panjang_cm: Optional[Decimal] = Field(None, ge=0, le=1000)
    lebar_cm: Optional[Decimal] = Field(None, ge=0, le=1000)
    tinggi_cm: Optional[Decimal] = Field(None, ge=0, le=1000)
    foto_id: Optional[str] = None
    aktif: bool = True

    @field_validator("nama")
    @classmethod
    def _nama(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Nama varian wajib diisi")
        return v


class VarianListIn(BaseModel):
    varian: list[VarianIn] = Field(default_factory=list, max_length=MAKS_VARIAN)


class FotoUrutanIn(BaseModel):
    ids: list[str] = Field(..., max_length=MAKS_FOTO_PRODUK)


class KategoriIn(BaseModel):
    nama: str


class KeranjangItemIn(BaseModel):
    produk_id: str
    qty: int = 1
    varian_id: Optional[str] = None


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
    kecamatan_tujuan: str = ""
    kelurahan_tujuan: str = ""
    kode_wilayah_tujuan: str = ""

    _v_telepon = field_validator("telepon_penerima")(_validate_telepon)
    _v_kodepos = field_validator("kode_pos_tujuan")(_validate_kode_pos)
    _v_wilayah = field_validator("kode_wilayah_tujuan")(_validate_kode_wilayah)


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
    kecamatan: str = ""
    kelurahan: str = ""
    kode_wilayah: str = ""
    utama: bool = False

    _v_telepon = field_validator("telepon_penerima")(_validate_telepon)
    _v_kodepos = field_validator("kode_pos")(_validate_kode_pos)
    _v_wilayah = field_validator("kode_wilayah")(_validate_kode_wilayah)


class AlamatPatch(BaseModel):
    label: Optional[str] = None
    nama_penerima: Optional[str] = None
    telepon_penerima: Optional[str] = None
    alamat_lengkap: Optional[str] = None
    kota: Optional[str] = None
    provinsi: Optional[str] = None
    kode_pos: Optional[str] = None
    kecamatan: Optional[str] = None
    kelurahan: Optional[str] = None
    kode_wilayah: Optional[str] = None
    utama: Optional[bool] = None

    @field_validator("telepon_penerima")
    @classmethod
    def _v_telepon(cls, v):
        return _validate_telepon(v) if v is not None else v

    @field_validator("kode_pos")
    @classmethod
    def _v_kodepos(cls, v):
        return _validate_kode_pos(v) if v is not None else v

    @field_validator("kode_wilayah")
    @classmethod
    def _v_wilayah(cls, v):
        return _validate_kode_wilayah(v) if v is not None else v


class PesanChatIn(BaseModel):
    isi: str = Field(..., min_length=1, max_length=2000)


class StaffIn(BaseModel):
    """POST /staff -- owner only. Always creates a plain ``admin``; owner
    accounts come from the seed only."""

    nama: str
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=72)


class StaffPatch(BaseModel):
    nama: Optional[str] = None


class PengaturanPatch(BaseModel):
    metode_proses_pesanan: str

    @field_validator("metode_proses_pesanan")
    @classmethod
    def _v_metode(cls, v: str) -> str:
        from tenants.store.modules.store.infrastructure.models import METODE_PROSES_PESANAN

        v = (v or "").strip().lower()
        if v not in METODE_PROSES_PESANAN:
            raise ValueError(f"metode_proses_pesanan harus salah satu dari {METODE_PROSES_PESANAN}")
        return v
