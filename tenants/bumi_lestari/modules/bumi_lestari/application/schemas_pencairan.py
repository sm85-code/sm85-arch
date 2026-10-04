"""Skema format file penghasilan & pencairan (spesifikasi 8.3, 10.3, 10.6)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

KolomTujuan = Literal["kode_pesanan", "tanggal_cair", "harga_jual", "potongan_biaya", "jumlah_cair"]
Kelompok = Literal["cocok", "selisih", "tidak_cocok", "duplikat", "penyesuaian"]


class KolomPetaIn(BaseModel):
    kolom_tujuan: KolomTujuan
    kolom_sumber: str = Field(min_length=1, max_length=255)
    operasi: Literal["ambil", "jumlahkan", "mutlak", "balik_tanda"] = "ambil"
    nama_rincian: Optional[str] = Field(default=None, max_length=128)


class KolomPetaOut(KolomPetaIn):
    model_config = ConfigDict(from_attributes=True)


class FormatPenghasilanIn(BaseModel):
    saluran_id: str
    nama: str = Field(min_length=2, max_length=128)
    jenis_file: Literal["xlsx", "csv"] = "xlsx"
    nama_sheet: Optional[str] = Field(default=None, max_length=128)
    baris_header: int = Field(default=1, ge=1, le=200)
    baris_data_mulai: Optional[int] = Field(default=None, ge=1)
    format_tanggal: str = Field(default="dd/mm/yyyy", max_length=32)
    pemisah_desimal: Literal[",", "."] = ","
    pemisah_ribuan: Literal[".", ",", " ", ""] = "."
    aturan_tanda: Literal["mutlak", "positif", "kurung"] = "mutlak"
    aturan_jenis_baris: dict = {}
    satuan_baris: Literal["per_pesanan", "per_produk"] = "per_pesanan"
    aturan_abaikan: dict = {}
    catatan: str = Field(default="", max_length=2000)
    kolom: list[KolomPetaIn] = Field(min_length=2)


class FormatPenghasilanOut(BaseModel):
    id: str
    saluran_id: str
    nama: str
    versi: int
    status: str
    jenis_file: str
    nama_sheet: Optional[str]
    baris_header: int
    baris_data_mulai: Optional[int]
    format_tanggal: str
    pemisah_desimal: str
    pemisah_ribuan: str
    aturan_tanda: str
    aturan_jenis_baris: dict
    satuan_baris: str
    aturan_abaikan: dict
    catatan: str
    contoh_nama: Optional[str]
    hasil_uji: Optional[dict]
    lulus_uji: bool
    diaktifkan_pada: Optional[datetime]
    created_at: datetime
    kolom: list[KolomPetaOut] = []
    model_config = ConfigDict(from_attributes=True)


class BacaHeaderOut(BaseModel):
    sheets: list[str]
    nama_sheet: Optional[str]
    baris_header: int
    kolom: list[str]
    contoh: list[list[str]]
    saran: dict[str, Optional[str]]


class MasalahOut(BaseModel):
    baris: int
    kolom: str
    nilai: str
    alasan: str


class BarisStandarOut(BaseModel):
    baris_file: Optional[int]
    kode_pesanan: str
    tanggal_cair: date
    harga_jual: Optional[Decimal]
    potongan_biaya: Optional[Decimal]
    rincian_biaya: dict[str, Decimal]
    jumlah_cair: Decimal
    jenis_baris: str
    mode_catat: str
    catatan: list[str] = []
    kelompok: Kelompok
    order_id: Optional[str] = None
    perkiraan_cair: Optional[Decimal] = None  # harapan aplikasi (penjualan − potongan tercatat)
    selisih: Decimal = Decimal("0")
    # Kode pesanan ini sudah dicatat lewat Catat manual: dilewati, kecuali dipilih "ganti entri manual".
    dicatat_manual: bool = False
    manual_unggahan_id: Optional[str] = None
    manual_bisa_diganti: bool = False  # entri manual masih draf
    alasan: str = ""  # untuk tidak cocok / duplikat


class KelompokOut(BaseModel):
    jumlah: int = 0
    total_cair: Decimal = Decimal("0")


class UjiFormatOut(BaseModel):
    lulus: bool
    jumlah_sah: int
    jumlah_masalah: int
    total_cair: Decimal
    neto: bool
    baris: list[BarisStandarOut]
    masalah: list[MasalahOut]


class PratinjauOut(BaseModel):
    saluran_id: str
    format_id: Optional[str]
    format_versi: Optional[int]
    nama_file: str
    kelompok: dict[str, KelompokOut]
    bermasalah: int
    baris: list[BarisStandarOut]
    masalah: list[MasalahOut]
    jumlah_disimpan: int  # baris baru (cocok, selisih, penyesuaian, tidak cocok) yang akan tersimpan
    total_dibukukan: Decimal  # total jumlah cair yang akan dibukukan (tanpa baris tidak cocok)
    neto: bool  # ada baris tanpa rincian biaya (dicatat neto)
    sudah_manual: int = 0  # baris yang sudah dicatat manual (dilewati)
    manual_bisa_diganti: int = 0  # dari jumlah itu, yang entri manualnya masih draf (bisa diganti isi file)


class PencairanBarisOut(BaseModel):
    id: str
    kode_pesanan: str
    tanggal_cair: date
    harga_jual: Optional[Decimal]
    potongan_biaya: Optional[Decimal]
    rincian_biaya: dict
    jumlah_cair: Decimal
    jenis_baris: str
    mode_catat: str
    order_id: Optional[str]
    status_cocok: str
    selisih: Decimal
    baris_file: Optional[int]
    masalah: Optional[list]
    dibatalkan: bool
    model_config = ConfigDict(from_attributes=True)


class PencairanUnggahanOut(BaseModel):
    id: str
    saluran_id: str
    format_id: Optional[str]
    format_versi: Optional[int]
    nama_file: str
    tanggal: date
    periode_dari: Optional[date]
    periode_sampai: Optional[date]
    jumlah_baris: int
    total: Decimal
    total_harga_jual: Decimal
    total_potongan: Decimal
    status_kirim: str
    kiriman_id: Optional[str]
    diunggah_oleh: str
    sumber_sistem: Optional[str] = None  # "manual" = Catat manual; kosong = file
    dibatalkan: bool
    alasan_batal: Optional[str]
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class PencairanDetailOut(PencairanUnggahanOut):
    baris: list[PencairanBarisOut] = []


class PencairanManualIn(BaseModel):
    """Catat manual per order untuk semua saluran marketplace & Toko web (masa transisi / file belum ada).
    Dicatat bruto (harga jual + potongan biaya), masuk tabel pencairan sebagai draf, lalu Kirim ke laporan keuangan."""
    saluran_id: str
    kode_pesanan: str = Field(min_length=1, max_length=128)
    tanggal_cair: date
    harga_jual: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    potongan: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    jumlah_cair: Optional[Decimal] = Field(default=None, max_digits=14, decimal_places=2)


class HubungkanIn(BaseModel):
    order_id: str
