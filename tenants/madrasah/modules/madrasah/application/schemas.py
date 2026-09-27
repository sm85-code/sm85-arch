"""Pydantic payloads for the multi-role madrasah HTTP API."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    # Nama field tetap "no_hp" (kolom UserMadrasah.no_hp yang sudah ada --
    # unique string identifier tanpa validasi format telepon di mana pun),
    # tapi min_length direlaksasi supaya username biasa (mis. "admin", 5
    # karakter) bisa dipakai, bukan cuma nomor HP.
    no_hp: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=4, max_length=72)


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
    # Kosong -> jatuh ke unit default (satu-satunya unit untuk pelanggan
    # yang belum multi-madrasah). Lihat services._default_unit_id().
    madrasah_unit_id: Optional[str] = None


class RombelIn(BaseModel):
    nama: str
    tingkat_id: Optional[str] = None
    wali_kelas_id: Optional[str] = None
    madrasah_unit_id: Optional[str] = None


class GuruIn(BaseModel):
    nama: str
    no_hp: str
    # Kosongkan untuk minta password acak dibuatkan otomatis (lihat
    # services.create_guru/create_wali_santri) -- JANGAN diberi default
    # tetap di sini, supaya tidak ada akun (termasuk akun admin lewat
    # Kelola Akun) yang lahir dengan password seragam yang mudah ditebak.
    password: Optional[str] = Field(default=None, max_length=72)
    role: str = "wali_kelas"
    madrasah_unit_id: Optional[str] = None


class SantriIn(BaseModel):
    nama: str
    rombel_id: Optional[str] = None
    kelas_id: Optional[str] = None
    orang_tua_id: Optional[str] = None
    no_hp_wali: Optional[str] = None
    madrasah_unit_id: Optional[str] = None


class PlacementIn(BaseModel):
    santri_id: str
    rombel_id: str


class MapelIn(BaseModel):
    kode: str
    nama: str
    madrasah_unit_id: Optional[str] = None


class MateriIn(BaseModel):
    mapel_id: str
    judul: str
    urutan: int = 1
    aktif: bool = True


class MateriPatch(BaseModel):
    judul: Optional[str] = None
    urutan: Optional[int] = None
    aktif: Optional[bool] = None


class SantriPatch(BaseModel):
    nama: Optional[str] = None
    rombel_id: Optional[str] = None
    orang_tua_id: Optional[str] = None


class RombelPatch(BaseModel):
    nama: Optional[str] = None
    tingkat_id: Optional[str] = None
    wali_kelas_id: Optional[str] = None


class MapelPatch(BaseModel):
    kode: Optional[str] = None
    nama: Optional[str] = None


class UserPatch(BaseModel):
    """Edit akun guru/wali_santri oleh admin. Semua field opsional (PATCH
    parsial); password kalau diisi akan di-hash ulang."""

    nama: Optional[str] = None
    no_hp: Optional[str] = None
    role: Optional[str] = None
    password: Optional[str] = Field(default=None, max_length=72)
    madrasah_unit_id: Optional[str] = None


class TingkatPatch(BaseModel):
    nama: Optional[str] = None
    urutan: Optional[int] = None


class PesanIn(BaseModel):
    santri_id: str
    isi: str = Field(..., min_length=1)


class PengumumanIn(BaseModel):
    judul: str
    isi: str = ""


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
    # Kosong -> generate_honor_massal() jatuh ke tarif default (env
    # HONOR_PER_SESI) untuk penugasan ini.
    tarif_per_sesi: Optional[Decimal] = Field(default=None, gt=0)


class ProgresPatch(BaseModel):
    capaian: str | None = None
    catatan_guru: str | None = None


class AbsenMapelItem(BaseModel):
    santri_id: str
    status: Literal["hadir", "sakit", "izin", "alpa"]


class BukuKasIn(BaseModel):
    tanggal: date
    tipe: Literal["masuk", "keluar"]
    kategori: str = Field(..., min_length=1, max_length=64)
    jumlah: Decimal = Field(..., gt=0)
    keterangan: str = ""


class PengaturanPatch(BaseModel):
    nama_sekolah: Optional[str] = Field(None, min_length=1, max_length=255)
    tagline: Optional[str] = Field(None, max_length=255)
    logo_url: Optional[str] = Field(None, max_length=500)
    alamat: Optional[str] = Field(None, max_length=500)
    # Ditampilkan di section "Penerimaan Santri Baru" landing page publik.
    info_psb: Optional[str] = Field(None, max_length=5000)
    kontak_psb: Optional[str] = Field(None, max_length=64)


class KegiatanIn(BaseModel):
    judul: str = Field(..., min_length=1, max_length=255)
    deskripsi: str = ""
    urutan: int = 1


class PendaftaranIn(BaseModel):
    """Form PSB publik -- tanpa auth, diisi calon wali santri."""

    nama_calon: str = Field(..., min_length=1, max_length=255)
    tempat_lahir: Optional[str] = Field(None, max_length=255)
    tanggal_lahir: Optional[date] = None
    nama_orang_tua: str = Field(..., min_length=1, max_length=255)
    no_hp: str = Field(..., min_length=5, max_length=32)
    alamat: str = Field("", max_length=2000)
    asal_sekolah: Optional[str] = Field(None, max_length=255)
    catatan: str = Field("", max_length=2000)


class ProfilPatch(BaseModel):
    """Self-service: pengguna mengubah profilnya sendiri lewat halaman
    "Profil Saya" -- beda dari UserPatch (admin mengedit akun ORANG LAIN
    tanpa perlu tahu password lama mereka). Ganti password di sini wajib
    menyertakan current_password yang benar."""

    nama: Optional[str] = Field(None, min_length=1, max_length=255)
    current_password: Optional[str] = Field(None, max_length=72)
    new_password: Optional[str] = Field(None, min_length=4, max_length=72)


class PendaftaranPatch(BaseModel):
    status: Optional[Literal["baru", "dihubungi", "diterima", "ditolak"]] = None
    catatan: Optional[str] = Field(None, max_length=2000)


class YayasanPatch(BaseModel):
    nama: Optional[str] = Field(None, min_length=1, max_length=255)
    alamat: Optional[str] = Field(None, max_length=500)


class MadrasahUnitIn(BaseModel):
    nama: str = Field(..., min_length=1, max_length=255)
    alamat: str = ""
    kepala_unit: str = ""
    # Kalau diisi, akun kepala_sekolah itu diikat ke unit ini.
    kepala_user_id: Optional[str] = None


class MadrasahUnitPatch(BaseModel):
    nama: Optional[str] = Field(None, min_length=1, max_length=255)
    alamat: Optional[str] = None
    kepala_unit: Optional[str] = None
    kepala_user_id: Optional[str] = None
    aktif: Optional[bool] = None


class SantriStatusIn(BaseModel):
    status: Literal["aktif", "lulus", "keluar", "pindah"]
    tanggal: Optional[date] = None


class KenaikanKelasItem(BaseModel):
    santri_id: str
    # rombel_tujuan_id kosong/None berarti santri ini LULUS di kenaikan ini
    # (tingkat tertinggi), bukan dipindah ke rombel lain.
    rombel_tujuan_id: Optional[str] = None


class KenaikanKelasRequest(BaseModel):
    items: list[KenaikanKelasItem] = Field(..., min_length=1)


class TahunAjaranIn(BaseModel):
    kode: str = Field(..., min_length=4, max_length=16, description='mis. "2025/2026"')
    tanggal_mulai: date
    tanggal_selesai: date


class SemesterIn(BaseModel):
    tahun_ajaran_id: str
    nama: Literal["Ganjil", "Genap"]
    tanggal_mulai: date
    tanggal_selesai: date


class AbsenMapelBulkRequest(BaseModel):
    """Sama seperti AbsenBulkRequest, tapi wajib menyertakan mapel_id +
    rombel_id karena ini absensi per sesi mapel (guru mapel), bukan absensi
    harian per rombel (wali kelas)."""

    tanggal: date
    rombel_id: str
    mapel_id: str
    items: list[AbsenMapelItem] = Field(..., min_length=1)
