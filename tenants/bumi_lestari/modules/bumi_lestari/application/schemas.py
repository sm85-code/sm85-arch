from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_BYTES = 72  # bcrypt only reads the first 72 bytes
USER_ROLES = ("admin", "owner", "staff")  # admin > owner > staff
Uang = Decimal


def _validate_new_password(value: str) -> str:
    if len(value) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password minimal {PASSWORD_MIN_LENGTH} karakter")
    if len(value.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise ValueError(f"Password maksimal {PASSWORD_MAX_BYTES} byte")
    if not value.strip():
        raise ValueError("Password tidak boleh kosong")
    return value


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _new_password_ok(cls, value: str) -> str:
        return _validate_new_password(value)


class UserCreateIn(BaseModel):
    nama: str = Field(min_length=1, max_length=255)
    email: EmailStr
    password: str
    role: str = "staff"

    @field_validator("password")
    @classmethod
    def _password_ok(cls, value: str) -> str:
        return _validate_new_password(value)

    @field_validator("role")
    @classmethod
    def _role_ok(cls, value: str) -> str:
        role = (value or "").strip().lower()
        if role not in USER_ROLES:
            raise ValueError(f"Role harus salah satu dari: {', '.join(USER_ROLES)}")
        return role


class UserOut(BaseModel):
    id: str
    nama: str
    email: str
    role: str
    must_change_password: bool = False
    model_config = ConfigDict(from_attributes=True)


class AkunKasIn(BaseModel):
    kode: str = Field(min_length=1, max_length=64)
    nama: str = Field(min_length=1, max_length=255)
    jenis: str = "kas"
    saldo_awal: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    plafon: Optional[Decimal] = Field(default=None, gt=0, max_digits=14, decimal_places=2)


class AkunKasOut(BaseModel):
    id: str
    kode: str
    nama: str
    jenis: str
    saldo_awal: Decimal
    plafon: Optional[Decimal] = None
    aktif: bool
    saldo: Decimal
    model_config = ConfigDict(from_attributes=True)


class KategoriIn(BaseModel):
    nama: str = Field(min_length=1, max_length=128)
    jenis: str


class KategoriOut(BaseModel):
    id: str
    nama: str
    jenis: str
    aktif: bool
    model_config = ConfigDict(from_attributes=True)


class TransaksiIn(BaseModel):
    tanggal: Optional[date] = None
    akun_id: str
    kategori_id: str
    jenis: str
    jumlah: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    keterangan: str = Field(default="", max_length=1000)


class TransaksiOut(BaseModel):
    id: str
    tanggal: date
    akun_id: str
    kategori_id: str
    jenis: str
    jumlah: Decimal
    keterangan: str
    dibuat_oleh: str
    dibatalkan: bool
    alasan_batal: Optional[str] = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class TransferIn(BaseModel):
    tanggal: Optional[date] = None
    dari_akun_id: str
    ke_akun_id: str
    jumlah: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    keterangan: str = Field(default="", max_length=1000)


class TransferOut(BaseModel):
    id: str
    tanggal: date
    dari_akun_id: str
    ke_akun_id: str
    jumlah: Decimal
    jenis: str
    keterangan: str
    dibuat_oleh: str
    dibatalkan: bool
    alasan_batal: Optional[str] = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class BatalIn(BaseModel):
    alasan: str = Field(min_length=3, max_length=1000)


class PengisianKasKecilOut(BaseModel):
    akun_id: str
    plafon: Decimal
    saldo: Decimal
    perlu_diisi: Decimal
    saldo_kas_utama: Decimal
    cukup: bool


class ProfilIn(BaseModel):
    nama_usaha: str = Field(min_length=1, max_length=255)
    alamat: str = Field(default="", max_length=1000)
    telepon: str = Field(default="", max_length=64)
    email: str = Field(default="", max_length=255)
    catatan: str = Field(default="", max_length=2000)
    biaya_proses_order: Decimal = Field(default=Decimal("10000"), ge=0, max_digits=14, decimal_places=2)


class ProporsiItemOut(BaseModel):
    id: str
    penerima: str
    persen: Decimal
    model_config = ConfigDict(from_attributes=True)


class ProporsiIn(BaseModel):
    """Bagi hasil hanya untuk dua orang: admin dan owner. Total harus 100."""

    persen_admin: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)
    persen_owner: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)


class ProfilOut(BaseModel):
    nama_usaha: str
    alamat: str
    telepon: str
    email: str
    catatan: str
    biaya_proses_order: Decimal
    proporsi_bagi_hasil: list[ProporsiItemOut]
