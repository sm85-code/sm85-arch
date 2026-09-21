"""Pydantic payloads for the multi-role madrasah HTTP API."""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    no_hp: str = Field(..., min_length=8, max_length=32)
    password: str = Field(..., min_length=4)


class AbsenItem(BaseModel):
    santri_id: str
    status: Literal["hadir", "sakit", "izin", "alpa"]


class AbsenBulkRequest(BaseModel):
    tanggal: date
    guru_id: Optional[str] = None
    items: list[AbsenItem] = Field(..., min_length=1)


class ProgresCreateRequest(BaseModel):
    tanggal: date
    santri_id: str
    tipe: str = Field(..., min_length=1, max_length=64)
    capaian: str = Field(..., min_length=1, max_length=255)
    catatan_guru: str = ""
    mapel_id: Optional[str] = None
    materi_id: Optional[str] = None


class TingkatIn(BaseModel):
    nama: str
    urutan: int = 1


class RombelIn(BaseModel):
    nama: str
    tingkat_id: Optional[str] = None
    wali_kelas_id: Optional[str] = None


class GuruIn(BaseModel):
    nama: str
    no_hp: str
    password: str = "password123"
    role: str = "wali_kelas"


class SantriIn(BaseModel):
    nama: str
    rombel_id: Optional[str] = None
    kelas_id: Optional[str] = None
    orang_tua_id: Optional[str] = None
    no_hp_wali: Optional[str] = None


class PlacementIn(BaseModel):
    santri_id: str
    rombel_id: str


class MapelIn(BaseModel):
    kode: str
    nama: str


class MateriIn(BaseModel):
    mapel_id: str
    judul: str
    urutan: int = 1
    aktif: bool = True


class MateriPatch(BaseModel):
    judul: Optional[str] = None
    urutan: Optional[int] = None
    aktif: Optional[bool] = None


class JadwalIn(BaseModel):
    rombel_id: str
    mapel_id: str
    hari: str
    jam_mulai: str = "07:00"
    jam_selesai: str = "08:00"


class PenugasanIn(BaseModel):
    """Menugaskan seorang guru mengajar satu mapel di satu rombel."""

    guru_id: str
    mapel_id: str
    rombel_id: str


class ProgresPatch(BaseModel):
    capaian: str | None = None
    catatan_guru: str | None = None


class AbsenMapelItem(BaseModel):
    santri_id: str
    status: Literal["hadir", "sakit", "izin", "alpa"]


class AbsenMapelBulkRequest(BaseModel):
    """Sama seperti AbsenBulkRequest, tapi wajib menyertakan mapel_id +
    rombel_id karena ini absensi per sesi mapel (guru mapel), bukan absensi
    harian per rombel (wali kelas)."""

    tanggal: date
    rombel_id: str
    mapel_id: str
    items: list[AbsenMapelItem] = Field(..., min_length=1)
