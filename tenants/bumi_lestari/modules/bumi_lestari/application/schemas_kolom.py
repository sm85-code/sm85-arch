"""Skema definisi kolom (spesifikasi 10.4-10.6)."""
from __future__ import annotations

from typing import Annotated, Any, Optional

from pydantic import BaseModel, BeforeValidator, Field, PlainSerializer

from tenants.bumi_lestari.modules.bumi_lestari.infrastructure.kolom_konteks import saring

# Dipakai di skema keluaran entitas: kosong untuk owner, hanya kolom staf untuk staf (AB-DM-9).
KolomTambahanOut = Annotated[
    dict[str, Any], BeforeValidator(lambda v: v or {}), PlainSerializer(saring, return_type=dict[str, Any]),
]
KolomTambahanIn = Optional[dict[str, Any]]

Entitas = Field(pattern=r"^(order|produk|pemasok|pelanggan|transaksi|karyawan)$")
Tipe = Field(pattern=r"^(teks|angka|mata_uang|tanggal|pilihan|ya_tidak)$")


class PilihanOut(BaseModel):
    nilai: str
    arsip: bool = False


class DefinisiKolomOut(BaseModel):
    id: Optional[str]  # None = kolom inti yang labelnya belum pernah diubah
    entitas: str
    kunci: str
    lapisan: str
    label: str
    label_bawaan: Optional[str]
    tipe: Optional[str]
    wajib: bool
    pilihan: list[PilihanOut] = []
    nilai_bawaan: Optional[str] = None
    min: Optional[str] = None
    maks: Optional[str] = None
    tampil_form: bool = True
    tampil_tabel: bool = True
    bisa_filter: bool = False
    ikut_ekspor: bool = True
    untuk_laporan: bool = False
    tampil_staf: bool = False
    urutan: int = 0
    aktif: bool = True
    terisi: Optional[int] = None  # jumlah data yang mengisi kolom ini (admin, kolom tambahan)


class DefinisiKolomIn(BaseModel):
    entitas: str = Entitas
    label: str = Field(min_length=1, max_length=128)
    tipe: str = Tipe
    wajib: bool = False
    pilihan: list[str] = []
    nilai_bawaan: Optional[str] = None
    min: Optional[str] = Field(default=None, max_length=32)
    maks: Optional[str] = Field(default=None, max_length=32)
    tampil_form: bool = True
    tampil_tabel: bool = False
    bisa_filter: bool = False
    ikut_ekspor: bool = True
    untuk_laporan: bool = False
    tampil_staf: bool = False
    urutan: Optional[int] = None


class DefinisiKolomPatch(BaseModel):
    label: Optional[str] = Field(default=None, min_length=1, max_length=128)
    tipe: Optional[str] = Field(default=None, pattern=r"^(teks|angka|mata_uang|tanggal|pilihan|ya_tidak)$")
    wajib: Optional[bool] = None
    pilihan: Optional[list[str]] = None
    nilai_bawaan: Optional[str] = None
    min: Optional[str] = Field(default=None, max_length=32)
    maks: Optional[str] = Field(default=None, max_length=32)
    tampil_form: Optional[bool] = None
    tampil_tabel: Optional[bool] = None
    bisa_filter: Optional[bool] = None
    ikut_ekspor: Optional[bool] = None
    untuk_laporan: Optional[bool] = None
    tampil_staf: Optional[bool] = None
    urutan: Optional[int] = None
    aktif: Optional[bool] = None


class LabelIntiIn(BaseModel):
    label: Optional[str] = Field(default=None, min_length=1, max_length=128)
    urutan: Optional[int] = None
    tampil_tabel: Optional[bool] = None
