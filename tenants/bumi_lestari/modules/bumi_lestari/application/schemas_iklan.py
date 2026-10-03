"""Skema kas iklan: platform, budget 25/75, top up, pengembalian, plafon & lognya (spesifikasi 8.7)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Grup = Literal["internal", "eksternal"]


class PlatformIklanIn(BaseModel):
    nama: str = Field(min_length=2, max_length=128)
    grup: Grup
    saluran_id: Optional[str] = None


class PlatformIklanPatch(BaseModel):
    nama: Optional[str] = Field(default=None, min_length=2, max_length=128)
    grup: Optional[Grup] = None
    saluran_id: Optional[str] = None
    aktif: Optional[bool] = None


class PlatformIklanOut(BaseModel):
    id: str
    nama: str
    grup: str
    saluran_id: Optional[str]
    aktif: bool
    model_config = ConfigDict(from_attributes=True)


class BudgetGrupOut(BaseModel):
    grup: str
    porsi: Decimal
    budget: Decimal
    terpakai: Decimal
    sisa: Decimal


class BudgetIklanOut(BaseModel):
    periode: str
    budget_total: Decimal
    dasar: str  # pengaturan | plafon (plafon kas iklan × jumlah Selasa)
    grup: list[BudgetGrupOut]


class PengaturanIklanIn(BaseModel):
    porsi_internal: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)
    porsi_eksternal: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)
    budget_bulanan: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)


class PengaturanIklanOut(PengaturanIklanIn):
    pass


class TopupIklanIn(BaseModel):
    tanggal: Optional[date] = None
    platform_iklan_id: str
    jumlah: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    keterangan: str = Field(default="", max_length=1000)
    talangan_oleh: Optional[str] = Field(default=None, max_length=128)


class PengembalianIklanIn(BaseModel):
    jumlah: Optional[Decimal] = Field(default=None, gt=0, max_digits=14, decimal_places=2)  # kosong = kelebihan di atas plafon
    tanggal: Optional[date] = None
    keterangan: str = Field(default="", max_length=1000)


class PlafonIn(BaseModel):
    plafon: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    alasan: str = Field(default="", max_length=1000)


class PlafonLogOut(BaseModel):
    id: str
    akun_id: str
    tanggal: date
    dari: Decimal
    ke: Decimal
    oleh: str
    alasan: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
